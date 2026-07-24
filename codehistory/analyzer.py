"""Evolution Analyzer — generates evolution events by comparing snapshots."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SnapshotData:
    """A snapshot of a feature's call tree at a specific commit."""
    call_tree_nodes: int
    call_tree_edges: int
    call_tree_depth: int
    file_path: str
    line_start: int
    line_end: int
    cyclomatic_complexity: float | None = None
    test_nodes: int = 0
    call_chain: list = field(default_factory=list)


@dataclass
class EvolutionEvent:
    """An evolution event for a feature."""
    event_type: str
    detail: dict[str, Any] = field(default_factory=dict)


class EvolutionAnalyzer:
    """Compares feature snapshots across commits to generate evolution events."""

    def __init__(
        self,
        growth_threshold: float = 1.3,
        shrink_threshold: float = 0.7,
    ):
        self.growth_threshold = growth_threshold
        self.shrink_threshold = shrink_threshold

    def analyze(
        self,
        previous: SnapshotData | None,
        current: SnapshotData,
    ) -> list[EvolutionEvent]:
        """Compare two snapshots and generate evolution events.

        Args:
            previous: Previous snapshot (None if this is the first observation).
            current: Current snapshot.

        Returns:
            List of evolution events.
        """
        events: list[EvolutionEvent] = []

        if previous is None:
            # First time seeing this feature
            events.append(EvolutionEvent(
                event_type="BORN",
                detail={"call_tree_nodes": current.call_tree_nodes,
                        "call_tree_edges": current.call_tree_edges,
                        "file_path": current.file_path},
            ))
            return events

        # Check if the call tree structure changed
        changed = False

        # Size change
        node_ratio = current.call_tree_nodes / max(previous.call_tree_nodes, 1)
        if node_ratio >= self.growth_threshold:
            events.append(EvolutionEvent(
                event_type="GROWN",
                detail={"from_nodes": previous.call_tree_nodes,
                        "to_nodes": current.call_tree_nodes,
                        "ratio": round(node_ratio, 2)},
            ))
            changed = True
        elif node_ratio <= self.shrink_threshold:
            events.append(EvolutionEvent(
                event_type="SHRUNK",
                detail={"from_nodes": previous.call_tree_nodes,
                        "to_nodes": current.call_tree_nodes,
                        "ratio": round(node_ratio, 2)},
            ))
            changed = True

        # Depth change
        depth_ratio = current.call_tree_depth / max(previous.call_tree_depth, 1)
        if depth_ratio >= self.growth_threshold:
            events.append(EvolutionEvent(
                event_type="EXTENDED",
                detail={"from_depth": previous.call_tree_depth,
                        "to_depth": current.call_tree_depth},
            ))
            changed = True
        elif depth_ratio <= self.shrink_threshold:
            events.append(EvolutionEvent(
                event_type="CONTRACTED",
                detail={"from_depth": previous.call_tree_depth,
                        "to_depth": current.call_tree_depth},
            ))
            changed = True

        # Edge change
        if current.call_tree_edges > previous.call_tree_edges:
            events.append(EvolutionEvent(
                event_type="DEP_CREATED",
                detail={"from_edges": previous.call_tree_edges,
                        "to_edges": current.call_tree_edges,
                        "new_count": current.call_tree_edges - previous.call_tree_edges},
            ))
            changed = True
        elif current.call_tree_edges < previous.call_tree_edges:
            events.append(EvolutionEvent(
                event_type="DEP_REMOVED",
                detail={"from_edges": previous.call_tree_edges,
                        "to_edges": current.call_tree_edges,
                        "removed_count": previous.call_tree_edges - current.call_tree_edges},
            ))
            changed = True

        # File-level change (move)
        if previous.file_path != current.file_path:
            events.append(EvolutionEvent(
                event_type="MOVED",
                detail={"from_file": previous.file_path,
                        "to_file": current.file_path},
            ))
            changed = True

        # Test coverage change
        if current.test_nodes > previous.test_nodes:
            events.append(EvolutionEvent(
                event_type="TEST_ADDED",
                detail={"from_test_nodes": previous.test_nodes,
                        "to_test_nodes": current.test_nodes},
            ))
            changed = True

        # Generic MODIFIED if something changed but no specific event fired
        if changed and not events:
            events.append(EvolutionEvent(
                event_type="MODIFIED",
                detail={"field": "unknown"},
            ))

        # No change at all
        if not events:
            events.append(EvolutionEvent(event_type="UNCHANGED"))

        return events

    def died_event(self, last_snapshot: SnapshotData) -> EvolutionEvent:
        """Generate a DIED event when a feature is removed."""
        return EvolutionEvent(
            event_type="DIED",
            detail={"last_call_tree_nodes": last_snapshot.call_tree_nodes,
                    "last_file_path": last_snapshot.file_path},
        )
