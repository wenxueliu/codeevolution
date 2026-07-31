"""Evolution Engine — orchestrates the full analysis pipeline.

Ties together Walker → CodeGraph (via SQLite) → Matcher → Analyzer → Store.

Replaces the old tree-sitter-based SnapshotParser + CrossFileIndex with
CodeGraph's knowledge graph.  CodeGraph handles ALL parsing, cross-file
call resolution, import resolution, and type inference — the engine now
only reads results from CodeGraph's SQLite.
"""

import logging
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .analyzer import EvolutionAnalyzer, EvolutionEvent, SnapshotData
from .codegraph_reader import CodeGraphReader
from .config import Config
from .matcher import FeatureMatcher
from .store import EvolutionStore
from .walker import CommitInfo, HistoryWalker

logger = logging.getLogger(__name__)


class EvolutionEngine:
    """Main engine that drives the evolution analysis.

    Usage:
        # 1. First, index the repo with CodeGraph:
        #    $ cd /path/to/repo && codegraph init

        # 2. Then run backfill:
        config = Config(repo_path="/path/to/repo")
        engine = EvolutionEngine(config)
        engine.backfill()

        # 3. Incremental updates:
        engine.update()
    """

    def __init__(self, config: Config):
        self.config = config
        self.store = EvolutionStore(config.db_path)
        self.walker = HistoryWalker(config.repo_path, first_parent=config.first_parent)
        self.matcher = FeatureMatcher(threshold=config.l1_match_threshold)
        self.analyzer = EvolutionAnalyzer(
            growth_threshold=config.growth_threshold_ratio,
            shrink_threshold=config.shrink_threshold_ratio,
        )

        self._reader: CodeGraphReader | None = None
        self._commit_count: int = 0
        self._analysis_repo_path = config.repo_path

        # Verify prerequisites
        self._check_codegraph()
        self._hydrate_matcher()

    def _check_codegraph(self):
        """Verify CodeGraph is initialized on the target repo."""
        cg_dir = Path(self.config.repo_path) / ".codegraph"
        cg_db = cg_dir / "codegraph.db"
        if not cg_db.exists():
            raise RuntimeError(
                f"CodeGraph not initialized in {self.config.repo_path}. "
                f"Run: cd {self.config.repo_path} && codegraph init"
            )
        if not self._which("codegraph"):
            raise RuntimeError(
                "codegraph CLI not found in PATH. Install: npm i -g @colbymchenry/codegraph"
            )

    @property
    def reader(self) -> CodeGraphReader:
        if self._reader is None:
            cg_db = str(Path(self._analysis_repo_path) / ".codegraph" / "codegraph.db")
            self._reader = CodeGraphReader(cg_db)
        return self._reader

    def _hydrate_matcher(self):
        """Restore active feature identities when update runs in a new process."""
        for feature in self.store.get_active_features():
            snapshot = self.store.get_latest_snapshot(feature["id"])
            chain = snapshot.get("call_chain", []) if snapshot else []
            nodes = [edge.get("to", "") for edge in chain]
            self.matcher.register_feature(
                feature["stable_id"],
                feature["entry_type"],
                feature["entry_signature"],
                call_tree=nodes,
                content=" ".join(nodes),
            )

    def _source_status(self) -> str:
        result = subprocess.run(
            ["git", "-C", self.config.repo_path, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    @contextmanager
    def _isolated_worktree(self):
        """Analyze history in a disposable worktree, never in the user's checkout."""
        if self._source_status().strip():
            logger.info("Source repository has local changes; isolated worktree will preserve them")

        worktree_path = tempfile.mkdtemp(prefix="codehistory-worktree-")
        added = False
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    self.config.repo_path,
                    "worktree",
                    "add",
                    "--detach",
                    worktree_path,
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            added = True
            self._analysis_repo_path = worktree_path

            result = subprocess.run(
                ["codegraph", "init"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"codegraph init failed: {result.stderr[:300]}")
            yield
        finally:
            if self._reader:
                self._reader.close()
                self._reader = None
            self._analysis_repo_path = self.config.repo_path
            if added:
                cleanup = subprocess.run(
                    [
                        "git",
                        "-C",
                        self.config.repo_path,
                        "worktree",
                        "remove",
                        "--force",
                        worktree_path,
                    ],
                    capture_output=True,
                    text=True,
                )
                if cleanup.returncode != 0:
                    logger.warning(
                        "Failed to unregister temporary worktree: %s",
                        cleanup.stderr[:300],
                    )
            shutil.rmtree(worktree_path, ignore_errors=True)

    def _sync_codegraph(self) -> bool:
        """Run `codegraph sync` to update the index. Returns True on success."""
        try:
            result = subprocess.run(
                ["codegraph", "sync"],
                cwd=self._analysis_repo_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning(f"codegraph sync failed: {result.stderr[:200]}")
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("codegraph sync timed out")
            return False
        except FileNotFoundError:
            logger.error("codegraph not found in PATH")
            return False

    def _checkout(self, commit_hash: str):
        """Checkout a git commit inside the disposable analysis worktree."""
        subprocess.run(
            ["git", "-C", self._analysis_repo_path, "checkout", "--force", commit_hash],
            capture_output=True,
            check=True,
        )

    def backfill(
        self,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ):
        """Walk all git history and build the evolution database from scratch.

        For each commit: git checkout → codegraph sync → query → analyze.

        Args:
            progress_callback: Optional callback(current, total, message).
        """
        if self.store.get_latest_commit_id() is not None:
            raise RuntimeError(
                "Evolution database is not empty; use 'codehistory update' for "
                "incremental analysis or choose a new --db path for backfill"
            )

        total = self.walker.count_commits()
        logger.info(f"Starting backfill: {total} commits")

        with self._isolated_worktree():
            for commit in self.walker.iter_commits():
                self._process_commit(commit)
                self._commit_count += 1
                if progress_callback and self._commit_count % 5 == 0:
                    progress_callback(
                        self._commit_count,
                        total,
                        f"[{self._commit_count}/{total}] {commit.hash[:8]}",
                    )

        if progress_callback:
            progress_callback(total, total, "Backfill complete")

        stats = self.store.get_stats()
        logger.info(f"Backfill complete: {stats}")

    def update(
        self,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ):
        """Process new commits since the last analyzed commit.

        Uses a disposable worktree and leaves the user's working tree untouched.
        """
        last_commit = self.store.get_latest_commit_id()
        start_from = None
        if last_commit:
            last_hash_row = self.store.conn.execute(
                "SELECT hash FROM commits WHERE id = ?", (last_commit,)
            ).fetchone()
            if last_hash_row:
                start_from = last_hash_row[0]

        new_count = 0
        with self._isolated_worktree():
            for commit in self.walker.iter_commits(start_from=start_from):
                self._process_commit(commit)
                new_count += 1
                self._commit_count += 1

        logger.info(f"Update complete: {new_count} new commits processed")
        if progress_callback:
            progress_callback(new_count, new_count, "Update complete")

    def _process_commit(self, commit: CommitInfo):
        """Process a single commit."""
        # Checkout this commit and sync CodeGraph
        self._checkout(commit.hash)

        if not self._sync_codegraph():
            raise RuntimeError(
                f"CodeGraph sync failed at {commit.hash[:8]}; commit was not recorded"
            )

        # Reopen reader (SQLite connection may have been replaced by sync)
        if self._reader:
            self._reader.close()
            self._reader = None

        matcher_snapshot = self.matcher.snapshot()
        try:
            with self.store.transaction():
                commit_id = self.store.insert_commit(
                    hash_=commit.hash,
                    parent_hash=commit.parent_hash,
                    timestamp=commit.timestamp,
                    author=commit.author,
                    message=commit.message,
                    semantic_type=commit.semantic_type,
                    tags=commit.tags if commit.tags else None,
                )
                self._process_entry_points(commit_id)
        except Exception:
            self.matcher.restore(matcher_snapshot)
            raise

    def _process_entry_points(self, commit_id: int):
        """Extract entry points from CodeGraph's graph and process features."""
        active_before = {
            feature["stable_id"]: feature for feature in self.store.get_active_features()
        }
        seen: set[str] = set()
        entry_points = self.reader.get_entry_points()

        for ep in entry_points:
            stable_id = self._process_one_entry_point(ep, commit_id)
            if stable_id in seen:
                feature = self.store.get_feature(stable_id)
                if feature:
                    self.store.insert_event(
                        feature["id"],
                        commit_id,
                        "SPLIT",
                        {"reason": "multiple entry points matched one prior identity"},
                    )
            seen.add(stable_id)

        for stable_id, feature in active_before.items():
            if stable_id in seen:
                continue
            previous = self.store.get_latest_snapshot(feature["id"])
            if previous:
                snapshot = SnapshotData(**previous)
                event = self.analyzer.died_event(snapshot)
                self.store.insert_event(feature["id"], commit_id, event.event_type, event.detail)
            self.store.mark_feature_removed(feature["id"])
            self.matcher.unregister_feature(feature["entry_type"], feature["entry_signature"])

    def _process_one_entry_point(self, ep, commit_id: int) -> str:
        """Process a single entry point — match to feature, compute snapshot."""
        # Classify entry type (uses matcher's heuristics as fallback)
        entry_type = self.matcher.classify_entry_type(ep.name, ep.file_path, ep.params)
        if ep.entry_type != "other":
            entry_type = ep.entry_type  # Decorator-based detection is more accurate

        entry_signature = self.matcher.build_signature(
            entry_type, ep.name, ep.file_path, ep.start_line
        )

        # Build call tree from CodeGraph edges (cross-file resolved)
        call_tree_node_ids = self.reader.get_call_tree(ep.node_id)
        call_tree_depth = self.reader.get_call_tree_depth(ep.node_id)
        call_chain = self.reader.get_call_chain(ep.node_id)

        # Count outgoing edges from entry point (direct deps)
        callees = self.reader.get_callees(ep.node_id)
        call_tree_edges = len(callees)

        snapshot = SnapshotData(
            call_tree_nodes=len(call_tree_node_ids),
            call_tree_edges=call_tree_edges,
            call_tree_depth=call_tree_depth,
            file_path=ep.file_path,
            line_start=ep.start_line,
            line_end=getattr(ep, "end_line", ep.start_line),
        )

        # Match to existing feature
        content_fingerprint = " ".join(
            f"{edge.get('from', '')} {edge.get('to', '')}" for edge in call_chain
        )
        match = self.matcher.match(
            entry_type, entry_signature, call_tree=call_tree_node_ids, content=content_fingerprint
        )
        descriptions = self._generate_descriptions(ep.name, ep.file_path)

        if match.matched_feature_id:
            # Existing feature — compare with previous snapshot
            feature = self.store.get_feature(match.matched_feature_id)
            if feature:
                feature_id = feature["id"]
                prev = self.store.get_latest_snapshot(feature_id)
                prev_data = SnapshotData(**prev) if prev else None
                events = self.analyzer.analyze(prev_data, snapshot)
                candidates = (match.evidence or {}).get("candidates", [])
                if len(candidates) > 1:
                    events.append(
                        EvolutionEvent(
                            "MERGED",
                            {"source_features": candidates, "match_level": match.match_level},
                        )
                    )
                if feature["entry_signature"] != entry_signature:
                    events.append(
                        EvolutionEvent(
                            "RENAMED",
                            {
                                "from_signature": feature["entry_signature"],
                                "to_signature": entry_signature,
                                "match_level": match.match_level,
                                "confidence": round(match.confidence, 4),
                            },
                        )
                    )
                    self.matcher.unregister_feature(entry_type, feature["entry_signature"])
                    self.store.update_feature_signature(feature_id, entry_signature)
                self.store.update_feature_last_seen(feature_id, commit_id)
            else:
                logger.warning(f"Feature {match.matched_feature_id} in matcher but not in DB")
                feature_id = self.store.insert_feature(
                    stable_id=match.matched_feature_id,
                    canonical_name=ep.name,
                    entry_type=entry_type,
                    entry_signature=entry_signature,
                    first_seen_at=commit_id,
                    **descriptions,
                )
                events = self.analyzer.analyze(None, snapshot)
        else:
            # New feature
            stable_id = ep.qualified_name
            existing = self.store.get_feature(stable_id)
            feature_id = self.store.insert_feature(
                stable_id=stable_id,
                canonical_name=ep.name,
                entry_type=entry_type,
                entry_signature=entry_signature,
                first_seen_at=commit_id,
                **descriptions,
            )
            if existing and existing["status"] == "removed":
                self.store.mark_feature_active(feature_id, commit_id)
            self.matcher.register_feature(
                stable_id,
                entry_type,
                entry_signature,
                call_tree=call_tree_node_ids,
                content=content_fingerprint,
            )
            events = self.analyzer.analyze(None, snapshot)

        # Write snapshot
        self.store.insert_snapshot(
            feature_id,
            commit_id,
            {
                "call_tree_nodes": snapshot.call_tree_nodes,
                "call_tree_edges": snapshot.call_tree_edges,
                "call_tree_depth": snapshot.call_tree_depth,
                "file_path": snapshot.file_path,
                "line_start": snapshot.line_start,
                "line_end": snapshot.line_end,
                "test_nodes": snapshot.test_nodes,
                "call_chain": call_chain,
            },
        )

        for ev in events:
            if ev.event_type != "UNCHANGED":
                self.store.insert_event(feature_id, commit_id, ev.event_type, ev.detail)
        if match.matched_feature_id:
            self.matcher.register_feature(
                match.matched_feature_id,
                entry_type,
                entry_signature,
                call_tree=call_tree_node_ids,
                content=content_fingerprint,
            )
        return match.matched_feature_id or stable_id

    @staticmethod
    def _generate_descriptions(func_name: str, file_path: str) -> dict:
        """Auto-generate English and Chinese descriptions from function name."""
        patterns = {
            "get_metadata": ("Retrieve metadata for the graph", "获取图谱元数据"),
            "get_node": ("Retrieve a specific node from the graph", "获取图谱中的指定节点"),
            "get_nodes_by_file": ("Retrieve nodes filtered by file path", "按文件路径获取节点列表"),
            "get_db_path": ("Get the database file path", "获取数据库文件路径"),
            "get_changed_files": ("Detect changed files from git", "通过 git 检测变更文件"),
            "get_impact_radius": (
                "Calculate the impact radius of a change",
                "计算代码变更的影响范围",
            ),
        }

        if func_name in patterns:
            en, zh = patterns[func_name]
            return {"description": en, "description_zh": zh}

        # Heuristic fallback
        words = func_name.replace("_", " ")
        en = f"{' '.join(w.capitalize() for w in words.split())}"
        zh = f"功能：{words}"
        return {"description": en, "description_zh": zh}
