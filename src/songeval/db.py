from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from .models import (
    AcquisitionSnapshot,
    AnalysisRun,
    ArtifactEdge,
    CandidateAssessment,
    CreativeBriefVersion,
    Defect,
    GenerationEvent,
    ListeningReviewRecord,
    LyricAnalysis,
    PairwiseComparison,
    PreservationDirective,
    ProjectDecisionPolicy,
    ProjectManifest,
    ProjectRecord,
    RecommendationCard,
    ReferenceSegment,
    ReleaseArtifact,
    SourceMaterial,
    SourceStateAssessment,
    StoredAnalysisReport,
    StoredListeningBundle,
    StoredProjectReview,
    StoredReleaseDecision,
    Take,
)
from .util import canonical_json, content_hash

T = TypeVar("T", bound=BaseModel)

MODEL_TYPES: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in (
        AnalysisRun,
        AcquisitionSnapshot,
        ArtifactEdge,
        CandidateAssessment,
        CreativeBriefVersion,
        Defect,
        GenerationEvent,
        ListeningReviewRecord,
        LyricAnalysis,
        PairwiseComparison,
        PreservationDirective,
        ProjectDecisionPolicy,
        ProjectRecord,
        RecommendationCard,
        ReferenceSegment,
        ReleaseArtifact,
        SourceMaterial,
        SourceStateAssessment,
        StoredListeningBundle,
        StoredProjectReview,
        StoredReleaseDecision,
        StoredAnalysisReport,
        Take,
    )
}


class ImmutableRecordError(ValueError):
    pass


class ReferentialIntegrityError(ValueError):
    pass


class Database:
    """Small immutable-record SQLite store.

    Domain records are JSON so unknown platform fields are retained verbatim. The
    lookup columns are intentionally small; the JSON payload is authoritative.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.initialize()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            owns_transaction = not self.connection.in_transaction
            if owns_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                if owns_transaction:
                    self.connection.commit()
            except Exception:
                if owns_transaction:
                    self.connection.rollback()
                raise

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta(key, value)
                VALUES ('schema_version', '1');

                CREATE TABLE IF NOT EXISTS entities (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT,
                    PRIMARY KEY(kind, id)
                );
                CREATE INDEX IF NOT EXISTS idx_entities_project_kind
                    ON entities(project_id, kind);

                CREATE TABLE IF NOT EXISTS artifact_links (
                    edge_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    parent_artifact_id TEXT NOT NULL,
                    child_artifact_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    deterministic INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_links_parent
                    ON artifact_links(project_id, parent_artifact_id);
                CREATE INDEX IF NOT EXISTS idx_links_child
                    ON artifact_links(project_id, child_artifact_id);
                """
            )

    @staticmethod
    def _payload(record: BaseModel) -> tuple[str, str]:
        payload = record.model_dump(mode="json")
        serialized = canonical_json(payload)
        return serialized, content_hash(payload)

    def save(self, record: BaseModel) -> None:
        with self._lock:
            already_in_transaction = self.connection.in_transaction
            try:
                self._save_locked(record)
                if not already_in_transaction:
                    self.connection.commit()
            except Exception:
                if not already_in_transaction:
                    self.connection.rollback()
                raise

    def _save_locked(self, record: BaseModel) -> None:
        kind = type(record).__name__
        if kind not in MODEL_TYPES:
            raise TypeError(f"unsupported record type: {kind}")
        record_id = getattr(record, "id", None)
        project_id = getattr(record, "project_id", None)
        if isinstance(record, ProjectRecord):
            project_id = record.id
        if not record_id or not project_id:
            raise ValueError(f"{kind} requires id and project_id")
        serialized, digest = self._payload(record)
        existing = self.connection.execute(
            "SELECT payload_sha256 FROM entities WHERE kind = ? AND id = ?",
            (kind, record_id),
        ).fetchone()
        if existing:
            if existing["payload_sha256"] != digest:
                raise ImmutableRecordError(
                    f"{kind} {record_id} already exists with different content"
                )
            return
        created_at = getattr(record, "created_at", None)
        self.connection.execute(
            """
            INSERT INTO entities(
                kind, id, project_id, payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                record_id,
                project_id,
                serialized,
                digest,
                created_at.isoformat() if created_at else None,
            ),
        )
        if isinstance(record, ArtifactEdge):
            self._save_edge_link(record)

    def _save_edge_link(self, edge: ArtifactEdge) -> None:
        for artifact_id in (edge.parent_artifact_id, edge.child_artifact_id):
            row = self.connection.execute(
                """
                SELECT 1
                FROM entities
                WHERE kind = 'ReleaseArtifact' AND id = ? AND project_id = ?
                """,
                (artifact_id, edge.project_id),
            ).fetchone()
            if row is None:
                raise ReferentialIntegrityError(
                    f"artifact edge {edge.id} references unknown artifact "
                    f"{artifact_id} in project {edge.project_id}"
                )
        if self._would_create_cycle(
            edge.project_id,
            edge.parent_artifact_id,
            edge.child_artifact_id,
        ):
            raise ReferentialIntegrityError("Artifact edge would create a cycle")
        self.connection.execute(
            """
            INSERT INTO artifact_links(
                edge_id, project_id, parent_artifact_id, child_artifact_id,
                operation, deterministic
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                edge.id,
                edge.project_id,
                edge.parent_artifact_id,
                edge.child_artifact_id,
                edge.operation.value,
                int(edge.deterministic),
            ),
        )

    def _would_create_cycle(self, project_id: str, parent: str, child: str) -> bool:
        if parent == child:
            return True
        row = self.connection.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_artifact_id
                FROM artifact_links
                WHERE project_id = ? AND parent_artifact_id = ?
                UNION
                SELECT links.child_artifact_id
                FROM artifact_links AS links
                JOIN descendants ON links.parent_artifact_id = descendants.id
                WHERE links.project_id = ?
            )
            SELECT 1 FROM descendants WHERE id = ? LIMIT 1
            """,
            (project_id, child, project_id, parent),
        ).fetchone()
        return row is not None

    def get(self, model_type: type[T], record_id: str) -> T | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT payload_json FROM entities WHERE kind = ? AND id = ?",
                (model_type.__name__, record_id),
            ).fetchone()
        if not row:
            return None
        return model_type.model_validate_json(row["payload_json"])

    def require(self, model_type: type[T], record_id: str) -> T:
        result = self.get(model_type, record_id)
        if result is None:
            raise KeyError(f"{model_type.__name__} {record_id} not found")
        return result

    def list(self, model_type: type[T], project_id: str) -> list[T]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT payload_json FROM entities
                WHERE kind = ? AND project_id = ?
                ORDER BY created_at, id
                """,
                (model_type.__name__, project_id),
            ).fetchall()
        return [model_type.model_validate_json(row["payload_json"]) for row in rows]

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT payload_json FROM entities
                WHERE kind = 'ProjectRecord'
                ORDER BY created_at, id
                """
            ).fetchall()
        return [ProjectRecord.model_validate_json(row["payload_json"]) for row in rows]

    def import_manifest(self, manifest: ProjectManifest) -> None:
        self._validate_manifest_references(manifest)
        with self.transaction():
            if self.get(ProjectRecord, manifest.project_id) is None:
                self.save(ProjectRecord(id=manifest.project_id, title=manifest.title))
            for collection in (
                manifest.briefs,
                manifest.acquisition_snapshots,
                manifest.sources,
                manifest.source_assessments,
                manifest.generation_events,
                manifest.takes,
                manifest.artifacts,
                manifest.edges,
                manifest.references,
                manifest.directives,
                manifest.policies,
                manifest.defects,
            ):
                for record in collection:
                    self.save(record)

    @staticmethod
    def _validate_manifest_references(manifest: ProjectManifest) -> None:
        brief_ids = {item.id for item in manifest.briefs}
        source_ids = {item.id for item in manifest.sources}
        event_ids = {item.id for item in manifest.generation_events}
        take_ids = {item.id for item in manifest.takes}
        artifact_ids = {item.id for item in manifest.artifacts}
        reference_ids = {item.id for item in manifest.references}

        def require_ids(
            actual: set[str] | tuple[str, ...], allowed: set[str], label: str
        ) -> None:
            missing = set(actual) - allowed
            if missing:
                raise ReferentialIntegrityError(
                    f"{label} references missing IDs: {sorted(missing)}"
                )

        for event in manifest.generation_events:
            require_ids({event.brief_id}, brief_ids, event.id)
            require_ids(event.source_material_ids, source_ids, event.id)
        for assessment in manifest.source_assessments:
            if assessment.original_source_material_id:
                require_ids(
                    {assessment.original_source_material_id},
                    source_ids,
                    assessment.id,
                )
            if assessment.added_source_material_id:
                require_ids(
                    {assessment.added_source_material_id},
                    source_ids,
                    assessment.id,
                )
            if assessment.generation_event_id:
                require_ids(
                    {assessment.generation_event_id},
                    event_ids,
                    assessment.id,
                )
        for take in manifest.takes:
            require_ids({take.generation_event_id}, event_ids, take.id)
        for artifact in manifest.artifacts:
            require_ids({artifact.take_id}, take_ids, artifact.id)
        for edge in manifest.edges:
            require_ids(
                {edge.parent_artifact_id, edge.child_artifact_id},
                artifact_ids,
                edge.id,
            )
            if edge.generation_event_id:
                require_ids({edge.generation_event_id}, event_ids, edge.id)
        for reference in manifest.references:
            require_ids({reference.source_artifact_id}, artifact_ids, reference.id)
        for directive in manifest.directives:
            require_ids({directive.brief_id}, brief_ids, directive.id)
            require_ids(
                {directive.reference_segment_id},
                reference_ids,
                directive.id,
            )
        for defect in manifest.defects:
            require_ids({defect.artifact_id}, artifact_ids, defect.id)
            require_ids({defect.brief_id}, brief_ids, defect.id)

        adjacency: dict[str, list[str]] = {}
        for edge in manifest.edges:
            adjacency.setdefault(edge.parent_artifact_id, []).append(
                edge.child_artifact_id
            )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ReferentialIntegrityError("Artifact DAG contains a cycle")
            if node in visited:
                return
            visiting.add(node)
            for child in adjacency.get(node, []):
                visit(child)
            visiting.remove(node)
            visited.add(node)

        for artifact_id in artifact_ids:
            visit(artifact_id)

    def export_manifest(
        self, project_id: str, title: str | None = None
    ) -> ProjectManifest:
        project = self.get(ProjectRecord, project_id)
        if not project:
            raise KeyError(project_id)
        return ProjectManifest(
            project_id=project_id,
            title=title or project.title,
            briefs=self.list(CreativeBriefVersion, project_id),
            acquisition_snapshots=self.list(AcquisitionSnapshot, project_id),
            sources=self.list(SourceMaterial, project_id),
            source_assessments=self.list(SourceStateAssessment, project_id),
            generation_events=self.list(GenerationEvent, project_id),
            takes=self.list(Take, project_id),
            artifacts=self.list(ReleaseArtifact, project_id),
            edges=self.list(ArtifactEdge, project_id),
            references=self.list(ReferenceSegment, project_id),
            directives=self.list(PreservationDirective, project_id),
            policies=self.list(ProjectDecisionPolicy, project_id),
            defects=self.list(Defect, project_id),
        )
