from __future__ import annotations

import pytest

from songeval.enums import AcquisitionPath, OperationType, Provenance
from songeval.lineage import (
    ArtifactGraph,
    common_craft_regions,
    crop_edge_from_verification,
    filename_relationship_is_evidence,
)
from songeval.models import (
    ArtifactEdge,
    CropVerification,
    ProvenanceRecord,
    ReleaseArtifact,
)


def artifact(artifact_id: str, duration: float) -> ReleaseArtifact:
    return ReleaseArtifact(
        id=artifact_id,
        project_id="p",
        take_id=f"t_{artifact_id}",
        measured_file_duration_s=duration,
        acquisition_path=AcquisitionPath.UNKNOWN,
    )


def test_graph_handles_dag_and_ancestors():
    items = [artifact("a", 10), artifact("b", 8), artifact("c", 7)]
    provenance = ProvenanceRecord(provenance=Provenance.DECLARED)
    edges = [
        ArtifactEdge(
            project_id="p",
            parent_artifact_id="a",
            child_artifact_id="b",
            operation=OperationType.CROP,
            provenance=provenance,
        ),
        ArtifactEdge(
            project_id="p",
            parent_artifact_id="a",
            child_artifact_id="c",
            operation=OperationType.COVER,
            provenance=provenance,
        ),
    ]
    graph = ArtifactGraph(items, edges)
    assert graph.descendants("a") == {"b", "c"}
    assert graph.ancestors("b") == {"a"}


def test_graph_rejects_unknown_traversal_roots():
    graph = ArtifactGraph([artifact("a", 10)], [])
    with pytest.raises(ValueError, match="unknown artifact missing"):
        graph.ancestors("missing")
    with pytest.raises(ValueError, match="unknown artifact missing"):
        graph.descendants("missing")


def test_graph_rejects_cycle():
    items = [artifact("a", 10), artifact("b", 8)]
    provenance = ProvenanceRecord(provenance=Provenance.DECLARED)
    with pytest.raises(ValueError, match="cycle"):
        ArtifactGraph(
            items,
            [
                ArtifactEdge(
                    project_id="p",
                    parent_artifact_id="a",
                    child_artifact_id="b",
                    operation=OperationType.CROP,
                    provenance=provenance,
                ),
                ArtifactEdge(
                    project_id="p",
                    parent_artifact_id="b",
                    child_artifact_id="a",
                    operation=OperationType.CROP,
                    provenance=provenance,
                ),
            ],
        )


def test_verified_crop_edge_has_region_scoped_inheritance():
    verification = CropVerification(
        parent_artifact_id="parent",
        child_artifact_id="child",
        analysis_run_id="r",
        lag_s=0.005,
        retained_region_correlation=0.998,
        correlation_threshold=0.995,
        verified=True,
    )
    edge = crop_edge_from_verification(
        verification,
        project_id="p",
        source_interval_s=(0.005, 8.005),
    )
    assert edge.deterministic
    assert edge.evidence_inheritance["structure"].value == "recompute"
    assert (
        edge.evidence_inheritance["preserved_audio_content"].value
        == "inherit_preserved_region"
    )


def test_parent_crop_craft_scope_uses_only_common_region():
    parent = artifact("parent", 10)
    child = artifact("child", 8)
    edge = ArtifactEdge(
        project_id="p",
        parent_artifact_id="parent",
        child_artifact_id="child",
        operation=OperationType.CROP,
        source_interval_s=(1.0, 9.0),
        deterministic=True,
        provenance=ProvenanceRecord(provenance=Provenance.INFERRED),
    )
    parent_region, child_region, excluded = common_craft_regions(
        parent,
        child,
        edge,
    )
    assert parent_region == (1.0, 9.0)
    assert child_region == (0.0, 8.0)
    assert excluded == ((0.0, 1.0), (9.0, 10.0))


def test_filename_is_never_lineage_evidence():
    assert not filename_relationship_is_evidence(
        "song-v1.6-cut.wav",
        "song-v2-cover1.6.wav",
    )
