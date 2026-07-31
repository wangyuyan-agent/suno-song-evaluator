from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable

from .enums import EvidenceInheritance, OperationType, Provenance
from .models import ArtifactEdge, CropVerification, ProvenanceRecord, ReleaseArtifact

DEFAULT_CROP_INHERITANCE: dict[str, EvidenceInheritance] = {
    "preserved_audio_content": EvidenceInheritance.INHERIT_PRESERVED_REGION,
    "lyrics_in_preserved_region": EvidenceInheritance.INHERIT_PRESERVED_REGION,
    "measured_duration": EvidenceInheritance.RECOMPUTE,
    "structure": EvidenceInheritance.RECOMPUTE,
    "boundaries": EvidenceInheritance.RECOMPUTE,
    "ending": EvidenceInheritance.RECOMPUTE,
    "technical_integrity": EvidenceInheritance.RECOMPUTE,
}

GENERATIVE_INHERITANCE: dict[str, EvidenceInheritance] = {
    "generated_region": EvidenceInheritance.NEVER_INHERIT,
    "lyrics": EvidenceInheritance.RECOMPUTE,
    "structure": EvidenceInheritance.RECOMPUTE,
    "technical_integrity": EvidenceInheritance.RECOMPUTE,
}


class ArtifactGraph:
    def __init__(
        self, artifacts: Iterable[ReleaseArtifact], edges: Iterable[ArtifactEdge]
    ):
        self.artifacts = {artifact.id: artifact for artifact in artifacts}
        self.edges = list(edges)
        self.outgoing: dict[str, list[ArtifactEdge]] = defaultdict(list)
        self.incoming: dict[str, list[ArtifactEdge]] = defaultdict(list)
        for edge in self.edges:
            if edge.parent_artifact_id not in self.artifacts:
                raise ValueError(f"unknown parent artifact {edge.parent_artifact_id}")
            if edge.child_artifact_id not in self.artifacts:
                raise ValueError(f"unknown child artifact {edge.child_artifact_id}")
            self.outgoing[edge.parent_artifact_id].append(edge)
            self.incoming[edge.child_artifact_id].append(edge)
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        indegree = {artifact_id: 0 for artifact_id in self.artifacts}
        for edge in self.edges:
            indegree[edge.child_artifact_id] += 1
        queue = deque(
            artifact_id for artifact_id, degree in indegree.items() if degree == 0
        )
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for edge in self.outgoing[current]:
                indegree[edge.child_artifact_id] -= 1
                if indegree[edge.child_artifact_id] == 0:
                    queue.append(edge.child_artifact_id)
        if visited != len(self.artifacts):
            raise ValueError("Artifact graph contains a cycle")

    def ancestors(self, artifact_id: str) -> set[str]:
        if artifact_id not in self.artifacts:
            raise ValueError(f"unknown artifact {artifact_id}")
        result: set[str] = set()
        queue = deque([artifact_id])
        while queue:
            current = queue.popleft()
            for edge in self.incoming[current]:
                if edge.parent_artifact_id not in result:
                    result.add(edge.parent_artifact_id)
                    queue.append(edge.parent_artifact_id)
        return result

    def descendants(self, artifact_id: str) -> set[str]:
        if artifact_id not in self.artifacts:
            raise ValueError(f"unknown artifact {artifact_id}")
        result: set[str] = set()
        queue = deque([artifact_id])
        while queue:
            current = queue.popleft()
            for edge in self.outgoing[current]:
                if edge.child_artifact_id not in result:
                    result.add(edge.child_artifact_id)
                    queue.append(edge.child_artifact_id)
        return result

    def direct_relation(
        self, artifact_a_id: str, artifact_b_id: str
    ) -> ArtifactEdge | None:
        return next(
            (
                edge
                for edge in self.edges
                if {
                    edge.parent_artifact_id,
                    edge.child_artifact_id,
                }
                == {artifact_a_id, artifact_b_id}
            ),
            None,
        )


def crop_edge_from_verification(
    verification: CropVerification,
    *,
    project_id: str,
    source_interval_s: tuple[float, float],
) -> ArtifactEdge:
    if not verification.verified:
        raise ValueError("cannot create a deterministic crop edge from failed evidence")
    return ArtifactEdge(
        project_id=project_id,
        parent_artifact_id=verification.parent_artifact_id,
        child_artifact_id=verification.child_artifact_id,
        operation=OperationType.CROP,
        source_interval_s=source_interval_s,
        deterministic=True,
        evidence_inheritance=DEFAULT_CROP_INHERITANCE,
        provenance=ProvenanceRecord(
            provenance=Provenance.INFERRED,
            source="sample-aligned audio fingerprint",
            note=(
                f"lag={verification.lag_s:.6f}s, "
                f"r={verification.retained_region_correlation:.6f}"
            ),
        ),
        verified_run_id=verification.analysis_run_id,
    )


def common_craft_regions(
    artifact_a: ReleaseArtifact,
    artifact_b: ReleaseArtifact,
    edge: ArtifactEdge | None,
) -> tuple[tuple[float, float], tuple[float, float], tuple[tuple[float, float], ...]]:
    """Return A region, B region and longer-artifact regions excluded from Craft."""
    duration_a = artifact_a.measured_file_duration_s
    duration_b = artifact_b.measured_file_duration_s
    if duration_a is None or duration_b is None:
        raise ValueError("measured_file_duration_s is required for Craft scope")
    if not edge or not edge.deterministic or edge.operation != OperationType.CROP:
        return (
            (0.0, duration_a),
            (0.0, duration_b),
            (),
        )
    if edge.source_interval_s is None:
        raise ValueError("deterministic crop edge requires source_interval_s")
    start, end = edge.source_interval_s
    if edge.parent_artifact_id == artifact_a.id:
        excluded = tuple(
            interval
            for interval in ((0.0, start), (end, duration_a))
            if interval[1] - interval[0] > 1e-6
        )
        return (start, end), (0.0, duration_b), excluded
    excluded = tuple(
        interval
        for interval in ((0.0, start), (end, duration_b))
        if interval[1] - interval[0] > 1e-6
    )
    return (0.0, duration_a), (start, end), excluded


def filename_relationship_is_evidence(_: str, __: str) -> bool:
    """Deliberately false: names may only help a human locate records."""
    return False
