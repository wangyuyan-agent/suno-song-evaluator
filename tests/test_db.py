from __future__ import annotations

import sqlite3

import pytest

from songeval.db import Database, ImmutableRecordError, ReferentialIntegrityError
from songeval.enums import OperationType, Provenance
from songeval.models import ArtifactEdge, ProjectRecord, ProvenanceRecord


def test_manifest_round_trip_preserves_unknown_metadata(tmp_path, minimal_manifest):
    path = tmp_path / "data.sqlite"
    with Database(path) as database:
        database.import_manifest(minimal_manifest)
        exported = database.export_manifest("project_test")
        assert exported.generation_events[0].raw_metadata == {
            "unknown_platform_field": {"kept": True}
        }
        assert exported.artifacts[0].raw_payload == {"arbitrary": 7}
        assert exported.artifacts[0].measured_file_duration_s is None


def test_manifest_import_is_idempotent(tmp_path, minimal_manifest):
    with Database(tmp_path / "data.sqlite") as database:
        database.import_manifest(minimal_manifest)
        database.import_manifest(minimal_manifest)
        assert len(database.list_projects()) == 1


def test_immutable_record_rejects_same_id_different_content(
    tmp_path,
    minimal_manifest,
):
    with Database(tmp_path / "data.sqlite") as database:
        database.import_manifest(minimal_manifest)
        changed = minimal_manifest.briefs[0].model_copy(update={"style": "different"})
        with database.transaction(), pytest.raises(ImmutableRecordError):
            database.save(changed)


def test_manifest_rejects_missing_reference(minimal_manifest):
    broken = minimal_manifest.model_copy(
        update={
            "takes": [
                minimal_manifest.takes[0].model_copy(
                    update={"generation_event_id": "missing"}
                )
            ]
        }
    )
    with pytest.raises(ReferentialIntegrityError, match="missing IDs"):
        Database._validate_manifest_references(broken)


def test_db_rejects_dag_cycle(tmp_path, minimal_manifest):
    artifact_a = minimal_manifest.artifacts[0]
    artifact_b = artifact_a.model_copy(
        update={"id": "artifact_b", "take_id": artifact_a.take_id}
    )
    provenance = ProvenanceRecord(provenance=Provenance.DECLARED)
    edge_ab = ArtifactEdge(
        id="edge_ab",
        project_id="project_test",
        parent_artifact_id=artifact_a.id,
        child_artifact_id=artifact_b.id,
        operation=OperationType.CROP,
        provenance=provenance,
    )
    edge_ba = ArtifactEdge(
        id="edge_ba",
        project_id="project_test",
        parent_artifact_id=artifact_b.id,
        child_artifact_id=artifact_a.id,
        operation=OperationType.CROP,
        provenance=provenance,
    )
    manifest = minimal_manifest.model_copy(
        update={"artifacts": [artifact_a, artifact_b], "edges": [edge_ab, edge_ba]}
    )
    with pytest.raises(ReferentialIntegrityError, match="cycle"):
        Database._validate_manifest_references(manifest)


def test_sqlite_schema_version_is_present(tmp_path):
    path = tmp_path / "data.sqlite"
    with Database(path):
        pass
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "1"
        )


def test_direct_save_persists_and_transaction_rollback_remains_atomic(tmp_path):
    path = tmp_path / "data.sqlite"
    with Database(path) as database:
        database.save(ProjectRecord(id="direct", title="direct"))
        with pytest.raises(RuntimeError), database.transaction():
            database.save(ProjectRecord(id="rolled-back", title="rolled back"))
            raise RuntimeError("rollback")

    with Database(path) as database:
        assert database.get(ProjectRecord, "direct") is not None
        assert database.get(ProjectRecord, "rolled-back") is None


def test_failed_direct_save_rolls_back_and_next_direct_save_persists(
    tmp_path,
    minimal_manifest,
):
    path = tmp_path / "data.sqlite"
    artifact_a = minimal_manifest.artifacts[0]
    artifact_b = artifact_a.model_copy(
        update={"id": "artifact_b", "take_id": artifact_a.take_id}
    )
    provenance = ProvenanceRecord(provenance=Provenance.DECLARED)
    edge_ab = ArtifactEdge(
        id="edge_ab",
        project_id="project_test",
        parent_artifact_id=artifact_a.id,
        child_artifact_id=artifact_b.id,
        operation=OperationType.CROP,
        provenance=provenance,
    )
    edge_ba = ArtifactEdge(
        id="edge_ba",
        project_id="project_test",
        parent_artifact_id=artifact_b.id,
        child_artifact_id=artifact_a.id,
        operation=OperationType.CROP,
        provenance=provenance,
    )
    with Database(path) as database:
        database.import_manifest(
            minimal_manifest.model_copy(
                update={"artifacts": [artifact_a, artifact_b], "edges": []}
            )
        )
        database.save(edge_ab)
        with pytest.raises(ReferentialIntegrityError, match="cycle"):
            database.save(edge_ba)
        assert not database.connection.in_transaction
        database.save(ProjectRecord(id="after-error", title="after error"))

    with Database(path) as database:
        assert database.get(ArtifactEdge, "edge_ba") is None
        assert database.get(ProjectRecord, "after-error") is not None


def test_direct_edge_save_rejects_missing_or_cross_project_artifacts(
    tmp_path,
    minimal_manifest,
):
    artifact = minimal_manifest.artifacts[0]
    other_project_artifact = artifact.model_copy(
        update={
            "id": "artifact_other_project",
            "project_id": "other_project",
        }
    )
    provenance = ProvenanceRecord(provenance=Provenance.DECLARED)
    missing_edge = ArtifactEdge(
        id="edge_missing",
        project_id="project_test",
        parent_artifact_id=artifact.id,
        child_artifact_id="artifact_missing",
        operation=OperationType.CROP,
        provenance=provenance,
    )
    cross_project_edge = missing_edge.model_copy(
        update={
            "id": "edge_cross_project",
            "child_artifact_id": other_project_artifact.id,
        }
    )

    with Database(tmp_path / "edge-integrity.sqlite") as database:
        database.import_manifest(minimal_manifest)
        database.save(other_project_artifact)

        with pytest.raises(ReferentialIntegrityError, match="unknown artifact"):
            database.save(missing_edge)
        with pytest.raises(ReferentialIntegrityError, match="unknown artifact"):
            database.save(cross_project_edge)

        assert database.get(ArtifactEdge, missing_edge.id) is None
        assert database.get(ArtifactEdge, cross_project_edge.id) is None
