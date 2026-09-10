"""Immutable, target-independent repository communication evidence.

The package deliberately contains no workspace-path based APIs.  Collectors
receive only the frozen graph and manifest-bounded sources made available by a
repository snapshot attempt.
"""

from .schema import (
    CallPathEvidence,
    CollectorCoverage,
    CollectorResult,
    CollectorRuleSet,
    CollectorStatus,
    CommunicationArtifactReference,
    CommunicationCollector,
    CommunicationObservation,
    CommunicationSchemaError,
    EntryFact,
    Location,
    NodeRef,
    RepositoryCommunicationArtifact,
    build_communication_artifact,
    deserialize_communication_artifact,
    serialize_communication_artifact,
)

__all__ = [
    "CommunicationArtifactReference",
    "CommunicationCollector",
    "CommunicationObservation",
    "CommunicationSchemaError",
    "CollectorCoverage",
    "CollectorResult",
    "CollectorRuleSet",
    "CollectorStatus",
    "CallPathEvidence",
    "EntryFact",
    "Location",
    "NodeRef",
    "RepositoryCommunicationArtifact",
    "build_communication_artifact",
    "deserialize_communication_artifact",
    "serialize_communication_artifact",
]
