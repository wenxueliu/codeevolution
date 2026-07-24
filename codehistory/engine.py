"""Evolution Engine — orchestrates the full analysis pipeline.

Ties together Walker → Parser → EntryPointDetector → Matcher → Analyzer → Store.
"""

import logging
from typing import Callable

from .analyzer import EvolutionAnalyzer, SnapshotData
from .config import Config
from .matcher import FeatureMatcher
from .parser import SnapshotParser
from .store import EvolutionStore
from .walker import CommitInfo, HistoryWalker

logger = logging.getLogger(__name__)


class EvolutionEngine:
    """Main engine that drives the evolution analysis.

    Usage:
        config = Config(repo_path="/path/to/repo")
        engine = EvolutionEngine(config)
        engine.backfill()       # First run: walk all history
        # Or:
        engine.update()         # Subsequent: process new commits only
    """

    def __init__(self, config: Config):
        self.config = config
        self.store = EvolutionStore(config.db_path)
        self.walker = HistoryWalker(config.repo_path, first_parent=config.first_parent)
        self.parser = SnapshotParser()
        self.matcher = FeatureMatcher(threshold=config.l1_match_threshold)
        self.analyzer = EvolutionAnalyzer(
            growth_threshold=config.growth_threshold_ratio,
            shrink_threshold=config.shrink_threshold_ratio,
        )
        # Track known files at the current HEAD of analysis
        self._known_files: set[str] = set()
        self._commit_count: int = 0

    def backfill(self, progress_callback: Callable[[int, int, str], None] | None = None):
        """Walk all git history and build the evolution database from scratch.

        Args:
            progress_callback: Optional callback(current, total, message) for progress.
        """
        total = self.walker.count_commits()
        logger.info(f"Starting backfill: {total} commits")

        for commit in self.walker.iter_commits():
            self._process_commit(commit)
            self._commit_count += 1
            if progress_callback and self._commit_count % 10 == 0:
                progress_callback(
                    self._commit_count, total,
                    f"Processed {self._commit_count}/{total} commits"
                )

        if progress_callback:
            progress_callback(total, total, "Backfill complete")

        stats = self.store.get_stats()
        logger.info(f"Backfill complete: {stats}")

    def update(self, progress_callback: Callable[[int, int, str], None] | None = None):
        """Process new commits since the last analyzed commit."""
        last_commit = self.store.get_latest_commit_id()
        start_from = None
        if last_commit:
            last = self.store.get_commit_by_hash(
                self.store.conn.execute(
                    "SELECT hash FROM commits WHERE id = ?", (last_commit,)
                ).fetchone()[0]
            )
            if last:
                start_from = last["hash"]

        total = self.walker.count_commits()
        new_count = 0
        for commit in self.walker.iter_commits(start_from=start_from):
            self._process_commit(commit)
            new_count += 1
            self._commit_count += 1

        logger.info(f"Update complete: {new_count} new commits processed")
        if progress_callback:
            progress_callback(new_count, new_count, "Update complete")

    def _process_commit(self, commit: CommitInfo):
        """Process a single commit: record it, then analyze changes."""

        # Insert commit record
        commit_id = self.store.insert_commit(
            hash_=commit.hash,
            parent_hash=commit.parent_hash,
            timestamp=commit.timestamp,
            author=commit.author,
            message=commit.message,
            semantic_type=commit.semantic_type,
            tags=commit.tags if commit.tags else None,
        )

        if commit.parent_hash is None:
            # First commit: full parse
            self._process_initial_commit(commit, commit_id)
        else:
            # Subsequent commit: delta parse
            self._process_delta_commit(commit, commit_id)

    def _process_initial_commit(self, commit: CommitInfo, commit_id: int):
        """Process the first commit: parse all files."""
        files = self.walker.get_files_at(commit.hash)
        # Filter for supported languages
        supported_files = [f for f in files if self._is_supported(f)]

        for fpath in supported_files:
            content = self.walker.read_file(commit.hash, fpath)
            if content is None:
                continue
            self._known_files.add(fpath)

            parsed = self.parser.parse_file(fpath, content)
            self._process_entry_points(parsed, commit_id)

    def _process_delta_commit(self, commit: CommitInfo, commit_id: int):
        """Process a delta commit: only parse changed files."""
        if commit.parent_hash is None:
            return

        changed_files = self.walker.get_changed_files(commit.parent_hash, commit.hash)

        for fpath in changed_files:
            if not self._is_supported(fpath):
                continue

            content = self.walker.read_file(commit.hash, fpath)
            if content is None:
                # File was deleted
                self._known_files.discard(fpath)
                self._handle_file_deletion(fpath, commit_id)
                continue

            self._known_files.add(fpath)
            parsed = self.parser.parse_file(fpath, content)
            self._process_entry_points(parsed, commit_id)

    def _process_entry_points(self, parsed, commit_id: int):
        """Process entry points from a parsed file."""
        for ep in parsed.entry_points:
            entry_type = self.matcher.classify_entry_type(ep.name, ep.file_path, ep.params)
            entry_signature = self.matcher.build_signature(
                entry_type, ep.name, ep.file_path, ep.line_start
            )

            # Calculate call tree stats
            call_tree_nodes = len(self._get_call_tree(ep, parsed))
            call_tree_depth = self._max_call_depth(ep, parsed)
            call_tree_edges = len([c for c in parsed.calls if c.caller == ep.qualified_name])

            snapshot = SnapshotData(
                call_tree_nodes=call_tree_nodes,
                call_tree_edges=call_tree_edges,
                call_tree_depth=call_tree_depth,
                file_path=ep.file_path,
                line_start=ep.line_start,
                line_end=ep.line_end,
                test_nodes=sum(1 for f in parsed.functions if f.is_test),
            )

            # Match to existing feature
            match = self.matcher.match(entry_type, entry_signature)

            if match.matched_feature_id:
                # Existing feature: get last snapshot and analyze
                feature = self.store.get_feature(match.matched_feature_id)
                if feature:
                    feature_id = feature["id"]
                    prev_snapshot = self.store.get_latest_snapshot(feature_id)
                    prev = None
                    if prev_snapshot:
                        prev = SnapshotData(**prev_snapshot)

                    events = self.analyzer.analyze(prev, snapshot)
                    self.store.update_feature_last_seen(feature_id, commit_id)
                else:
                    # Feature registered in matcher but not in DB (shouldn't happen)
                    logger.warning(f"Feature {match.matched_feature_id} in matcher but not in DB, treating as new")
                    feature_id = self.store.insert_feature(
                        stable_id=match.matched_feature_id,
                        canonical_name=ep.name,
                        entry_type=entry_type,
                        entry_signature=entry_signature,
                        first_seen_at=commit_id,
                    )
                    events = self.analyzer.analyze(None, snapshot)
            else:
                # New feature
                stable_id = ep.qualified_name
                feature_id = self.store.insert_feature(
                    stable_id=stable_id,
                    canonical_name=ep.name,
                    entry_type=entry_type,
                    entry_signature=entry_signature,
                    first_seen_at=commit_id,
                )
                self.matcher.register_feature(stable_id, entry_type, entry_signature)
                events = self.analyzer.analyze(None, snapshot)

            # Write snapshot and events (skip UNCHANGED)
            try:
                self.store.insert_snapshot(feature_id, commit_id, {
                    "call_tree_nodes": snapshot.call_tree_nodes,
                    "call_tree_edges": snapshot.call_tree_edges,
                    "call_tree_depth": snapshot.call_tree_depth,
                    "file_path": snapshot.file_path,
                    "line_start": snapshot.line_start,
                    "line_end": snapshot.line_end,
                    "test_nodes": snapshot.test_nodes,
                })
            except Exception as e:
                logger.error(
                    f"insert_snapshot failed: feature_id={feature_id} commit_id={commit_id} "
                    f"ep={ep.qualified_name} error={e}"
                )
                raise
            for ev in events:
                if ev.event_type != "UNCHANGED":
                    self.store.insert_event(feature_id, commit_id, ev.event_type, ev.detail)

    def _handle_file_deletion(self, filepath: str, commit_id: int):
        """Mark features whose entry points were in the deleted file."""
        # Check all known features for this file
        for feature in self.store.get_all_features():
            if feature["entry_signature"].startswith(filepath):
                self.store.mark_feature_removed(feature["id"])
                # Add DIED event
                prev_snapshot = self.store.get_latest_snapshot(feature["id"])
                if prev_snapshot:
                    ev = self.analyzer.died_event(SnapshotData(**prev_snapshot))
                    self.store.insert_event(feature["id"], commit_id, ev.event_type, ev.detail)

    def _is_supported(self, filepath: str) -> bool:
        from .parser import detect_language
        lang = detect_language(filepath)
        return lang in self.config.languages

    def _get_call_tree(self, entry_point, parsed) -> set:
        """Get the set of functions reachable from entry_point via calls.

        Simple BFS within a single file. Cross-file calls are unresolved in Phase 1.
        """
        visited = {entry_point.qualified_name}
        queue = [entry_point.qualified_name]
        while queue:
            caller = queue.pop(0)
            for call in parsed.calls:
                if call.caller == caller and call.is_resolved:
                    if call.resolved_to not in visited:
                        visited.add(call.resolved_to)
                        queue.append(call.resolved_to)
        return visited

    def _max_call_depth(self, entry_point, parsed) -> int:
        """Calculate maximum call depth from entry point."""
        tree = self._get_call_tree(entry_point, parsed)
        if len(tree) <= 1:
            return 1

        # BFS-based depth calculation
        depth = {entry_point.qualified_name: 1}
        queue = [entry_point.qualified_name]
        max_depth = 1
        while queue:
            caller = queue.pop(0)
            for call in parsed.calls:
                if call.caller == caller and call.is_resolved and call.resolved_to in tree:
                    if call.resolved_to not in depth:
                        depth[call.resolved_to] = depth[caller] + 1
                        max_depth = max(max_depth, depth[call.resolved_to])
                        queue.append(call.resolved_to)
        return max_depth
