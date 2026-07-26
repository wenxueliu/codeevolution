"""Evolution Engine — orchestrates the full analysis pipeline.

Ties together Walker → Parser → EntryPointDetector → Matcher → Analyzer → Store.
"""

import logging
from pathlib import Path
from typing import Callable

from .analyzer import EvolutionAnalyzer, SnapshotData
from .config import Config
from .crossfile import CrossFileIndex
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
        """Process the first commit: parse all files, build cross-file index."""
        files = self.walker.get_files_at(commit.hash)
        supported_files = [f for f in files if self._is_supported(f)]

        # Pass 1: parse all files, build cross-file index
        parsed_files = {}
        for fpath in supported_files:
            content = self.walker.read_file(commit.hash, fpath)
            if content is None:
                continue
            self._known_files.add(fpath)
            parsed_files[fpath] = self.parser.parse_file(fpath, content)

        # Build cross-file index
        cross_index = CrossFileIndex()
        for parsed in parsed_files.values():
            cross_index.add_file(parsed)
        # Register inheritance relationships
        for parsed in parsed_files.values():
            for inh in parsed.inheritance:
                cross_index.add_inheritance(inh["class"], inh["bases"])

        # Store for _get_call_tree to use
        self._cross_index = cross_index

        # Pass 2: process entry points
        for fpath, parsed in parsed_files.items():
            if parsed.language != "config":
                self._process_entry_points(parsed, commit_id)

        # Pass 3: process config changes
        config_files = {fp: p for fp, p in parsed_files.items() if p.language == "config"}
        if config_files:
            self._process_config_changes(config_files, parsed_files, commit_id)

    def _process_delta_commit(self, commit: CommitInfo, commit_id: int):
        """Process a delta commit: parse changed files, update cross-file index."""
        if commit.parent_hash is None:
            return

        changed_files = self.walker.get_changed_files(commit.parent_hash, commit.hash)
        if not changed_files:
            return

        # Get all current files (not just changed) for cross-file index
        all_files = self.walker.get_files_at(commit.hash)
        all_supported = [f for f in all_files if self._is_supported(f)]

        # Parse changed files; reuse cached results for unchanged files
        parsed_files = {}
        for fpath in all_supported:
            content = self.walker.read_file(commit.hash, fpath)
            if content is None:
                self._known_files.discard(fpath)
                continue
            self._known_files.add(fpath)
            parsed_files[fpath] = self.parser.parse_file(fpath, content)

        # Rebuild cross-file index
        cross_index = CrossFileIndex()
        for parsed in parsed_files.values():
            cross_index.add_file(parsed)
        for parsed in parsed_files.values():
            for inh in parsed.inheritance:
                cross_index.add_inheritance(inh["class"], inh["bases"])
        self._cross_index = cross_index

        # Process entry points from changed files only
        for fpath in changed_files:
            if fpath in parsed_files and parsed_files[fpath].language != "config":
                self._process_entry_points(parsed_files[fpath], commit_id)

        # Process config changes
        config_changed = {fp: parsed_files[fp] for fp in changed_files
                          if fp in parsed_files and parsed_files[fp].language == "config"}
        if config_changed:
            self._process_config_changes(config_changed, parsed_files, commit_id)

        # Handle deleted files
        for fpath in changed_files:
            if fpath not in parsed_files:
                self._handle_file_deletion(fpath, commit_id)

    def _process_entry_points(self, parsed, commit_id: int):
        """Process entry points from a parsed file."""
        for ep in parsed.entry_points:
            entry_type = self.matcher.classify_entry_type(ep.name, ep.file_path, ep.params)
            entry_signature = self.matcher.build_signature(
                entry_type, ep.name, ep.file_path, ep.line_start
            )

            # Calculate call tree stats + call chain (with cross-file resolution)
            cross_index = getattr(self, '_cross_index', None)
            call_tree = self._get_call_tree(ep, parsed, cross_index)
            call_tree_nodes = len(call_tree)
            call_tree_depth = self._max_call_depth(ep, parsed, cross_index)
            call_tree_edges = len([c for c in parsed.calls if c.caller == ep.qualified_name])
            call_chain = self._build_call_chain(ep, parsed, call_tree, cross_index)

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
            descriptions = self._generate_descriptions(ep.name, ep.file_path)

            if match.matched_feature_id:
                # Existing feature
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
                feature_id = self.store.insert_feature(
                    stable_id=stable_id,
                    canonical_name=ep.name,
                    entry_type=entry_type,
                    entry_signature=entry_signature,
                    first_seen_at=commit_id,
                    **descriptions,
                )
                self.matcher.register_feature(stable_id, entry_type, entry_signature)
                events = self.analyzer.analyze(None, snapshot)

            # Write snapshot with call_chain
            try:
                self.store.insert_snapshot(feature_id, commit_id, {
                    "call_tree_nodes": snapshot.call_tree_nodes,
                    "call_tree_edges": snapshot.call_tree_edges,
                    "call_tree_depth": snapshot.call_tree_depth,
                    "file_path": snapshot.file_path,
                    "line_start": snapshot.line_start,
                    "line_end": snapshot.line_end,
                    "test_nodes": snapshot.test_nodes,
                    "call_chain": call_chain,
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

    def _build_call_chain(self, entry_point, parsed, call_tree: set, cross_index=None) -> list[dict]:
        """Build call chain including cross-file resolved calls."""
        chain = []
        visited = set()

        def traverse(caller_qname: str, depth: int = 0):
            if depth > 10:
                return
            for call in parsed.calls:
                if call.caller == caller_qname:
                    target = self._resolve_call_target(call, parsed, cross_index)
                    if target and target in call_tree:
                        edge_key = (caller_qname, target)
                        if edge_key not in visited:
                            visited.add(edge_key)
                            # Display callee: use resolved name's short form
                            callee_display = call.callee_name.replace("self.", "")
                            if cross_index and not call.is_resolved:
                                parts = target.split("::")[-1].split(".")
                                callee_display = parts[-1] if parts else callee_display
                            chain.append({
                                "from": call.caller.split("::")[-1],
                                "to": callee_display,
                                "depth": depth,
                            })
                            traverse(target, depth + 1)

        traverse(entry_point.qualified_name)
        return chain

    @staticmethod
    def _generate_descriptions(func_name: str, file_path: str) -> dict:
        """Auto-generate English and Chinese descriptions from function name."""
        name = func_name.replace("_", " ").strip()

        # Common patterns → descriptions
        patterns = {
            "get_metadata": ("Retrieve metadata for the graph", "获取图谱元数据"),
            "get_node": ("Retrieve a specific node from the graph", "获取图谱中的指定节点"),
            "get_nodes_by_file": ("Retrieve nodes filtered by file path", "按文件路径获取节点列表"),
            "get_nodes_by_size": ("Retrieve nodes filtered by size", "按大小获取节点列表"),
            "get_edges_by_source": ("Retrieve edges originating from a node", "获取节点的出边列表"),
            "get_edges_by_target": ("Retrieve edges targeting a node", "获取节点的入边列表"),
            "get_all_files": ("Retrieve all tracked file paths", "获取所有已追踪的文件路径"),
            "get_all_nodes": ("Retrieve all nodes in the graph", "获取图谱中的所有节点"),
            "get_all_edges": ("Retrieve all edges in the graph", "获取图谱中的所有边"),
            "get_edges_among": ("Retrieve edges among specified nodes", "获取指定节点之间的边"),
            "get_impact_radius": ("Calculate the impact radius of a change", "计算代码变更的影响范围"),
            "get_subgraph": ("Retrieve a subgraph of the full graph", "获取图谱的子图"),
            "get_stats": ("Retrieve graph statistics", "获取图谱统计信息"),
            "get_session": ("Retrieve session data", "获取会话数据"),
            "get_communities": ("Retrieve community detection results", "获取社区检测结果"),
            "get_flows": ("Retrieve execution flows", "获取执行流程列表"),
            "get_flow": ("Retrieve a specific execution flow", "获取指定执行流程"),
            "get_flow_by_id": ("Retrieve an execution flow by its ID", "按 ID 获取执行流程"),
            "get_affected_flows": ("Retrieve flows affected by a change", "获取被变更影响的执行流程"),
            "get_community": ("Retrieve a community by ID", "按 ID 获取社区"),
            "get_community_ids_by_qualified_names": ("Map qualified names to community IDs", "将限定名映射到社区 ID"),
            "get_architecture_overview": ("Retrieve architecture overview", "获取架构概览"),
            "get_wiki_page": ("Generate a wiki page for a topic", "为指定主题生成 Wiki 页面"),
            "get_minimal_context": ("Retrieve minimal context for a task", "为任务获取最小上下文"),
            "get_docs_section": ("Retrieve a section of documentation", "获取文档章节"),
            "get_review_context": ("Retrieve review context for changes", "获取代码审查上下文"),
            "get_db_path": ("Get the database file path", "获取数据库文件路径"),
            "get_changed_files": ("Detect changed files from git", "通过 git 检测变更文件"),
            "get_staged_and_unstaged": ("Detect staged and unstaged changes", "检测暂存和未暂存的变更"),
            "get_all_tracked_files": ("List all tracked source files", "列出所有已追踪的源文件"),
            "get_parser": ("Get the configured parser instance", "获取已配置的解析器实例"),
            "get_config_consumers": ("Get configuration consumers", "获取配置消费者"),
            "get_data_dir": ("Get the data directory path", "获取数据目录路径"),
            "get_transitive_tests": ("Get transitively related tests", "获取传递相关的测试"),
            "get_hub_nodes": ("Get hub nodes in the graph", "获取图谱中的中心节点"),
            "get_bridge_nodes": ("Get bridge nodes between communities", "获取社区间的桥接节点"),
            "get_knowledge_gaps": ("Identify gaps in the knowledge graph", "识别知识图谱中的缺口"),
            "get_surprising_connections": ("Find surprising cross-community connections", "发现跨社区的意外连接"),
            "get_suggested_questions": ("Suggest relevant exploration questions", "推荐相关探索问题"),
            "get_flow_criticalities": ("Get execution flow criticality scores", "获取执行流程关键性评分"),
            "get_minimal_context_tool": ("MCP tool: retrieve minimal review context", "MCP 工具：获取最小审查上下文"),
            "get_impact_radius_tool": ("MCP tool: calculate impact radius", "MCP 工具：计算影响范围"),
            "get_review_context_tool": ("MCP tool: retrieve review context", "MCP 工具：获取审查上下文"),
            "get_docs_section_tool": ("MCP tool: retrieve documentation section", "MCP 工具：获取文档章节"),
            "get_flow_tool": ("MCP tool: retrieve execution flow", "MCP 工具：获取执行流程"),
            "get_affected_flows_tool": ("MCP tool: retrieve affected flows", "MCP 工具：获取受影响的流程"),
            "get_community_tool": ("MCP tool: retrieve community info", "MCP 工具：获取社区信息"),
            "get_architecture_overview_tool": ("MCP tool: retrieve architecture overview", "MCP 工具：获取架构概览"),
            "get_wiki_page_tool": ("MCP tool: generate wiki page", "MCP 工具：生成 Wiki 页面"),
            "get_hub_nodes_tool": ("MCP tool: find hub nodes", "MCP 工具：查找中心节点"),
            "get_bridge_nodes_tool": ("MCP tool: find bridge nodes", "MCP 工具：查找桥接节点"),
            "get_knowledge_gaps_tool": ("MCP tool: identify knowledge gaps", "MCP 工具：识别知识缺口"),
            "get_surprising_connections_tool": ("MCP tool: find surprising connections", "MCP 工具：发现意外连接"),
            "get_suggested_questions_tool": ("MCP tool: suggest exploration questions", "MCP 工具：建议探索问题"),
        }

        if func_name in patterns:
            en, zh = patterns[func_name]
            return {"description": en, "description_zh": zh}

        # Heuristic fallback
        words = name.replace("_", " ")
        en = f"{' '.join(w.capitalize() for w in words.split())}"
        zh = f"功能：{name}"
        return {"description": en, "description_zh": zh}

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

    def _process_config_changes(self, config_files: dict, all_parsed: dict, commit_id: int):
        """Generate INDIRECT_MODIFIED events for features affected by config changes.

        Strategy: find code files that import or reference the changed config file,
        then flag all features in those code files.
        """
        for config_path, config_parsed in config_files.items():
            config_basename = Path(config_path).name  # e.g., "settings.py" or "config.yml"
            config_stem = Path(config_path).stem      # e.g., "settings" or "config"
            changed_keys = set(config_parsed.config_keys)

            # Find code files that reference this config
            affected_features = set()
            for fpath, parsed in all_parsed.items():
                if parsed.language == "config":
                    continue
                # Check if this code file imports the config file
                imports_config = any(
                    config_stem in imp or config_basename in imp
                    for imp in parsed.imports
                )
                # Check if any function references changed config keys
                refs_config_key = False
                if changed_keys:
                    for func in parsed.functions:
                        for key in changed_keys:
                            if key in func.name:
                                refs_config_key = True
                                break

                if imports_config or refs_config_key:
                    # All features in this file are potentially affected
                    for ep in parsed.entry_points:
                        entry_type = self.matcher.classify_entry_type(
                            ep.name, ep.file_path, ep.params
                        )
                        entry_signature = self.matcher.build_signature(
                            entry_type, ep.name, ep.file_path, ep.line_start
                        )
                        match = self.matcher.match(entry_type, entry_signature)
                        if match.matched_feature_id:
                            feature = self.store.get_feature(match.matched_feature_id)
                            if feature:
                                affected_features.add(feature["id"])

            # Emit INDIRECT_MODIFIED events
            for feat_id in affected_features:
                self.store.insert_event(
                    feat_id, commit_id, "INDIRECT_MODIFIED",
                    {
                        "config_file": config_path,
                        "changed_keys": list(changed_keys)[:20],
                        "reason": "Config file modification may indirectly affect this feature",
                    },
                )
                logger.debug(
                    f"INDIRECT_MODIFIED: feature={feat_id} config={config_path}"
                )

    def _is_supported(self, filepath: str) -> bool:
        from .parser import detect_language
        if self._is_test_file(filepath):
            return False
        lang = detect_language(filepath)
        return lang in self.config.languages

    @staticmethod
    def _is_test_file(filepath: str) -> bool:
        """Check if a file is a test file and should be skipped.

        Patterns:
        - Directories: tests/, test/, __tests__/, spec/, specs/, fixtures/
        - Filenames: test_*, *_test, *.test.*, *.spec.*
        """
        path = Path(filepath)
        parts = path.parts
        name = path.name.lower()

        # Test directories anywhere in the path
        test_dirs = {"tests", "test", "__tests__", "spec", "specs", "fixtures",
                     "e2e", "integration", "__mocks__"}
        if any(p.lower() in test_dirs for p in parts[:-1]):  # exclude filename
            return True

        # Test filename patterns
        if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.java"):
            return True
        if ".test." in name or ".spec." in name:
            return True

        return False

    def _resolve_call_target(self, call, parsed, cross_index) -> str | None:
        """Resolve a call to its target qualified name, using 8-step emit order."""
        if call.is_resolved and call.resolved_to:
            return call.resolved_to
        if cross_index:
            # Extract caller's class from qualified name
            caller_class = None
            if "::" in call.caller:
                func_part = call.caller.split("::")[-1]
                if "." in func_part:
                    caller_class = func_part.rsplit(".", 1)[0]
            return cross_index.resolve_call(
                parsed.file_path, caller_class, call.callee_name
            )
        return None

    def _get_call_tree(self, entry_point, parsed, cross_index=None) -> set:
        """BFS from entry_point, traversing file-local and cross-file calls."""
        visited = {entry_point.qualified_name}
        queue = [entry_point.qualified_name]
        while queue:
            caller = queue.pop(0)
            for call in parsed.calls:
                if call.caller == caller:
                    target = self._resolve_call_target(call, parsed, cross_index)
                    if target and target not in visited:
                        visited.add(target)
                        queue.append(target)
        return visited

    def _max_call_depth(self, entry_point, parsed, cross_index=None) -> int:
        """Calculate maximum call depth via BFS."""
        tree = self._get_call_tree(entry_point, parsed, cross_index)
        if len(tree) <= 1:
            return 1
        depth = {entry_point.qualified_name: 1}
        queue = [entry_point.qualified_name]
        max_depth = 1
        while queue:
            caller = queue.pop(0)
            for call in parsed.calls:
                if call.caller == caller:
                    target = self._resolve_call_target(call, parsed, cross_index)
                    if target and target in tree and target not in depth:
                        depth[target] = depth.get(caller, 1) + 1
                        max_depth = max(max_depth, depth[target])
                        queue.append(target)
        return max_depth
