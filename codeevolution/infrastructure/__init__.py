"""Infrastructure adapters for CodeEvolution ports."""

from .explanation_snapshot_store import ExplanationSnapshotStore, SnapshotStateError

__all__ = ["ExplanationSnapshotStore", "SnapshotStateError"]
