"""Feature Matcher — links entry points across commits to form feature identities."""

from dataclasses import dataclass


@dataclass
class EntryPointMatch:
    """Result of matching an entry point to an existing feature."""

    entry_signature: str
    entry_type: str
    matched_feature_id: str | None  # None = new feature
    confidence: float


class FeatureMatcher:
    """Matches entry points across commits to track feature identity.

    Phase 1: L1 exact match only (entry_type + entry_signature).
    """

    def __init__(self, threshold: float = 0.9):
        self.threshold = threshold
        # In-memory index: (entry_type, entry_signature) -> feature_stable_id
        self._feature_index: dict[tuple[str, str], str] = {}

    def register_feature(self, stable_id: str, entry_type: str, entry_signature: str):
        """Register a known feature for future matching."""
        key = (entry_type, entry_signature)
        self._feature_index[key] = stable_id

    def unregister_feature(self, entry_type: str, entry_signature: str):
        """Remove a feature from the index (e.g., when removed)."""
        key = (entry_type, entry_signature)
        self._feature_index.pop(key, None)

    def snapshot(self) -> dict[tuple[str, str], str]:
        """Copy the in-memory index so a failed commit can restore it."""
        return self._feature_index.copy()

    def restore(self, snapshot: dict[tuple[str, str], str]):
        """Restore a previously captured matcher index."""
        self._feature_index = snapshot.copy()

    def match(self, entry_type: str, entry_signature: str) -> EntryPointMatch:
        """Match an entry point to an existing feature.

        L1: Exact match on (entry_type, entry_signature). Confidence = 1.0.
        If no match found, returns confidence 0.0 and matched_feature_id=None.
        """
        key = (entry_type, entry_signature)
        if key in self._feature_index:
            return EntryPointMatch(
                entry_signature=entry_signature,
                entry_type=entry_type,
                matched_feature_id=self._feature_index[key],
                confidence=1.0,
            )

        return EntryPointMatch(
            entry_signature=entry_signature,
            entry_type=entry_type,
            matched_feature_id=None,
            confidence=0.0,
        )

    @staticmethod
    def build_signature(entry_type: str, func_name: str, file_path: str, line: int) -> str:
        """Build a stable entry point signature.

        For HTTP endpoints: "POST /api/login" style
        For CLI: "main"
        For others: "file_path:func_name"
        """
        if entry_type == "http":
            return func_name.lower()
        elif entry_type == "cli":
            return func_name
        else:
            return f"{file_path}:{func_name}"

    @staticmethod
    def classify_entry_type(func_name: str, file_path: str, params: list[str]) -> str:
        """Classify what type of entry point this is.

        Heuristic classification based on naming and location.
        """
        name_lower = func_name.lower()
        path_lower = file_path.lower()

        # HTTP/web patterns
        if any(
            p in path_lower for p in ("controller", "view", "handler", "route", "api", "endpoint")
        ):
            return "http"
        if any(name_lower.startswith(p) for p in ("get_", "post_", "put_", "delete_", "patch_")):
            return "http"

        # CLI patterns
        if func_name == "main":
            return "cli"
        if name_lower.startswith(("cmd_", "cli_", "run_")):
            return "cli"

        # event/cron patterns
        if any(p in name_lower for p in ("_handler", "_callback", "_listener", "_consumer")):
            return "event"
        if any(p in name_lower for p in ("_cron", "_scheduled", "_task", "_job")):
            return "cron"

        return "other"
