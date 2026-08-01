from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .analyzer import ProjectAnalyzer
from .db import Database
from .importers import (
    ParentDeclaration,
    SunoClipSnapshot,
    SunoImportError,
    SunoPublicClient,
    build_local_project,
    build_suno_project,
)
from .models import ProjectRecord, StoredAnalysisReport
from .reporting import render_markdown
from .util import project_key, utc_now

IntakeKind = Literal["suno", "upload"]
IntakeStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "canceled",
]

PROJECT_ID_PATTERN = r"^[\w\u4e00-\u9fff-]{1,80}$"

logger = logging.getLogger(__name__)


class IntakeCanceled(RuntimeError):
    pass


class IntakeConflict(ValueError):
    pass


class IntakeParent(BaseModel):
    model_config = ConfigDict(frozen=True)

    child_clip_id: str = Field(min_length=1, max_length=160)
    parent: str = Field(min_length=1, max_length=2048)


class IntakeRequest(BaseModel):
    """Validated internal request shared by the CLI and Web worker.

    ``upload_paths`` are server-owned staging paths. The API never accepts them
    from JSON clients and never includes them in a public response.
    """

    model_config = ConfigDict(frozen=True)

    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    kind: IntakeKind
    suno_url: str | None = Field(default=None, max_length=2048)
    snapshots: tuple[SunoClipSnapshot, ...] = ()
    selected_clip_ids: tuple[str, ...] = ()
    upload_paths: tuple[str, ...] = ()
    original_filenames: tuple[str, ...] = ()
    lyrics: str | None = Field(default=None, max_length=100_000)
    style: str | None = Field(default=None, max_length=10_000)
    exclude: str | None = Field(default=None, max_length=10_000)
    parents: tuple[IntakeParent, ...] = ()
    download_audio: bool = True
    analyze_now: bool = True
    allow_local_parent_paths: bool = False
    allow_external_upload_paths: bool = False
    media_dir_override: str | None = None
    report_dir_override: str | None = None

    @field_validator("project_id", "title", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("lyrics", "style", "exclude", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def validate_source(self) -> IntakeRequest:
        if self.kind == "suno":
            if (self.suno_url is None) == (not self.snapshots):
                raise ValueError("Suno intake requires exactly one URL or snapshot set")
            if self.upload_paths:
                raise ValueError("Suno intake cannot contain upload paths")
            if len(self.snapshots) > 50:
                raise ValueError("Suno snapshot intake is limited to 50 clips")
        else:
            if not self.upload_paths:
                raise ValueError("upload intake requires at least one staged file")
            if self.suno_url is not None or self.snapshots or self.parents:
                raise ValueError("upload intake cannot contain Suno fields")
            if len(self.upload_paths) != len(self.original_filenames):
                raise ValueError("every staged upload requires an original filename")
        return self


class IntakeJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    project_id: str
    kind: IntakeKind
    status: IntakeStatus
    step: str
    progress: int = Field(ge=0, le=100)
    request: IntakeRequest
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    attempt: int = 0
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def public_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude={"request"})
        payload["source"] = {
            "kind": self.kind,
            "url": self.request.suno_url if self.kind == "suno" else None,
            "filenames": (
                list(self.request.original_filenames) if self.kind == "upload" else []
            ),
            "selected_clip_ids": list(self.request.selected_clip_ids),
        }
        payload["title"] = self.request.title
        return payload


class IntakeJobStore:
    """Mutable operational state kept beside immutable domain records."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    step TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intake_jobs_status_created
                ON intake_jobs(status, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_intake_jobs_project
                ON intake_jobs(project_id, created_at)
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IntakeJob:
        return IntakeJob(
            id=row["id"],
            project_id=row["project_id"],
            kind=row["kind"],
            status=row["status"],
            step=row["step"],
            progress=row["progress"],
            request=IntakeRequest.model_validate_json(row["request_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            cancel_requested=bool(row["cancel_requested"]),
            attempt=row["attempt"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            started_at=(
                datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            ),
            completed_at=(
                datetime.fromisoformat(row["completed_at"])
                if row["completed_at"]
                else None
            ),
        )

    def create(self, request: IntakeRequest, *, job_id: str | None = None) -> IntakeJob:
        now = utc_now().isoformat()
        identifier = job_id or f"intake_{uuid.uuid4().hex}"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A project owns one durable intake lifecycle. Terminal failures are
            # retried or explicitly deleted so staging and provenance cannot fork.
            existing = connection.execute(
                """
                SELECT id, status FROM intake_jobs
                WHERE project_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (request.project_id,),
            ).fetchone()
            if existing is not None:
                raise IntakeConflict(
                    f"project already has intake job {existing['id']} "
                    f"with status {existing['status']}"
                )
            connection.execute(
                """
                INSERT INTO intake_jobs(
                    id, project_id, kind, status, step, progress, request_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', 'queued', 0, ?, ?, ?)
                """,
                (
                    identifier,
                    request.project_id,
                    request.kind,
                    request.model_dump_json(),
                    now,
                    now,
                ),
            )
        return self.require(identifier)

    def get(self, job_id: str) -> IntakeJob | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def require(self, job_id: str) -> IntakeJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list(self, *, limit: int = 50) -> list[IntakeJob]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM intake_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def job_ids(self) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT id FROM intake_jobs").fetchall()
        return {row["id"] for row in rows}

    def recover_interrupted(self) -> int:
        now = utc_now().isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE intake_jobs
                SET status = 'queued', step = 'recovered_after_restart',
                    progress = CASE WHEN progress > 90 THEN 90 ELSE progress END,
                    cancel_requested = 0, updated_at = ?, completed_at = NULL,
                    error = NULL
                WHERE status = 'running'
                """,
                (now,),
            )
        return cursor.rowcount

    def claim_next(self) -> IntakeJob | None:
        now = utc_now().isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM intake_jobs
                WHERE status = 'queued'
                ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE intake_jobs
                SET status = 'running', step = 'starting', progress = 1,
                    attempt = attempt + 1, started_at = ?, completed_at = NULL,
                    updated_at = ?, error = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, row["id"]),
            )
        return self.require(row["id"])

    def update_progress(self, job_id: str, *, step: str, progress: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE intake_jobs SET step = ?, progress = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (step, progress, utc_now().isoformat(), job_id),
            )

    def finish(
        self,
        job_id: str,
        *,
        status: Literal["succeeded", "failed", "canceled"],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        step = {
            "succeeded": "complete",
            "failed": "failed",
            "canceled": "canceled",
        }[status]
        progress = 100 if status == "succeeded" else self.require(job_id).progress
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE intake_jobs
                SET status = ?, step = ?, progress = ?, result_json = ?, error = ?,
                    updated_at = ?, completed_at = ?, cancel_requested = 0
                WHERE id = ?
                """,
                (
                    status,
                    step,
                    progress,
                    json.dumps(result, ensure_ascii=False) if result else None,
                    error,
                    now,
                    now,
                    job_id,
                ),
            )

    def request_cancel(self, job_id: str) -> IntakeJob:
        now = utc_now().isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = self._from_row(row)
            if job.status == "queued":
                connection.execute(
                    """
                    UPDATE intake_jobs
                    SET status = 'canceled', step = 'canceled',
                        cancel_requested = 0, updated_at = ?, completed_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (now, now, job_id),
                )
            elif job.status == "running":
                connection.execute(
                    """
                    UPDATE intake_jobs
                    SET cancel_requested = 1, step = 'cancel_requested', updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now, job_id),
                )
            updated = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - guarded by the write transaction
            raise KeyError(job_id)
        return self._from_row(updated)

    def retry(self, job_id: str) -> IntakeJob:
        now = utc_now().isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = self._from_row(row)
            if job.status not in {"failed", "canceled"}:
                raise IntakeConflict(
                    "only failed or canceled intake jobs can be retried"
                )
            connection.execute(
                """
                UPDATE intake_jobs
                SET status = 'queued', step = 'queued', progress = 0,
                    result_json = NULL, error = NULL, cancel_requested = 0,
                    updated_at = ?, started_at = NULL, completed_at = NULL
                WHERE id = ?
                """,
                (now, job_id),
            )
            updated = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - guarded by the write transaction
            raise KeyError(job_id)
        return self._from_row(updated)

    def delete_terminal(
        self,
        job_id: str,
        *,
        before_delete: Callable[[IntakeJob], None] | None = None,
        discard_project: bool = False,
    ) -> IntakeJob:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = self._from_row(row)
            if job.status not in {"failed", "canceled"}:
                raise IntakeConflict("only failed or canceled jobs can be deleted")
            if before_delete is not None:
                before_delete(job)
            if discard_project:
                connection.execute(
                    "DELETE FROM artifact_links WHERE project_id = ?",
                    (job.project_id,),
                )
                connection.execute(
                    "DELETE FROM entities WHERE project_id = ?",
                    (job.project_id,),
                )
            connection.execute("DELETE FROM intake_jobs WHERE id = ?", (job_id,))
        return job

    def cancel_requested(self, job_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM intake_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])


@dataclass(frozen=True)
class IntakePaths:
    media_root: Path
    report_root: Path
    upload_root: Path

    def __post_init__(self) -> None:
        for field_name in ("media_root", "report_root", "upload_root"):
            configured = getattr(self, field_name)
            object.__setattr__(
                self,
                field_name,
                Path(configured).expanduser().resolve(),
            )


class IntakeService:
    def __init__(
        self,
        db_path: str | Path,
        paths: IntakePaths,
        *,
        max_suno_clips: int = 24,
        max_audio_file_bytes: int = 512 * 1024 * 1024,
        max_audio_duration_s: float = 1800.0,
    ):
        if min(max_suno_clips, max_audio_file_bytes) <= 0:
            raise ValueError("Suno intake limits must be positive")
        if max_audio_duration_s <= 0:
            raise ValueError("max_audio_duration_s must be positive")
        self.db_path = Path(db_path).expanduser().resolve()
        self.paths = paths
        self.max_suno_clips = max_suno_clips
        self.max_audio_file_bytes = max_audio_file_bytes
        self.max_audio_duration_s = max_audio_duration_s

    def run(
        self,
        request: IntakeRequest,
        *,
        progress: Callable[[str, int], None] | None = None,
        canceled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        notify = progress or (lambda _step, _value: None)
        is_canceled = canceled or (lambda: False)

        def checkpoint(step: str, value: int) -> None:
            if is_canceled():
                raise IntakeCanceled("intake canceled by user")
            notify(step, value)

        destination = (
            Path(request.media_dir_override).expanduser().resolve()
            if request.media_dir_override
            else self.paths.media_root / project_key(request.project_id)
        )
        checkpoint("validating_source", 5)
        with Database(self.db_path) as database:
            existing = database.get(ProjectRecord, request.project_id)
        if existing is None:
            if request.kind == "upload":
                staged = tuple(Path(value).resolve() for value in request.upload_paths)
                if not request.allow_external_upload_paths:
                    for path in staged:
                        if not path.is_relative_to(self.paths.upload_root):
                            raise ValueError("staged upload escaped the upload root")
                checkpoint("building_manifest", 15)
                result = build_local_project(
                    staged,
                    project_id=request.project_id,
                    title=request.title,
                    media_dir=destination,
                    lyrics=request.lyrics,
                    style=request.style,
                    exclude=request.exclude,
                )
                renamed_artifacts = [
                    artifact.model_copy(
                        update={
                            "title": Path(original).stem,
                            "raw_payload": {
                                **artifact.raw_payload,
                                "source_filename": original,
                            },
                        }
                    )
                    for artifact, original in zip(
                        result.manifest.artifacts,
                        request.original_filenames,
                        strict=True,
                    )
                ]
                result = type(result)(
                    manifest=result.manifest.model_copy(
                        update={"artifacts": renamed_artifacts}
                    ),
                    warnings=result.warnings,
                )
            else:
                checkpoint("fetching_suno", 10)
                with SunoPublicClient(
                    max_audio_bytes=self.max_audio_file_bytes
                ) as client:
                    clips = (
                        list(request.snapshots)
                        if request.snapshots
                        else client.fetch(request.suno_url or "")
                    )
                    if request.snapshots:
                        for clip in clips:
                            SunoPublicClient.validate_url(clip.source_url)
                            if clip.audio_url:
                                SunoPublicClient.validate_audio_url(clip.audio_url)
                    if request.selected_clip_ids:
                        selected = set(request.selected_clip_ids)
                        known = {clip.id for clip in clips}
                        missing = selected - known
                        if missing:
                            raise SunoImportError(
                                f"selected clips are not present: {sorted(missing)}"
                            )
                        clips = [clip for clip in clips if clip.id in selected]
                    if len(clips) > self.max_suno_clips:
                        raise SunoImportError(
                            "Suno intake is limited to "
                            f"{self.max_suno_clips} selected clips"
                        )
                    oversized_declared = [
                        clip.id
                        for clip in clips
                        if clip.duration is not None
                        and clip.duration > self.max_audio_duration_s
                    ]
                    if oversized_declared:
                        raise SunoImportError(
                            "selected clips exceed the "
                            f"{self.max_audio_duration_s:g}s duration limit: "
                            f"{oversized_declared}"
                        )
                    self._validate_web_parents(request, clips)
                    checkpoint("downloading_audio", 20)
                    result = build_suno_project(
                        clips,
                        project_id=request.project_id,
                        title=request.title,
                        media_dir=destination,
                        client=client,
                        download_audio=request.download_audio,
                        lyrics=request.lyrics,
                        style=request.style,
                        exclude=request.exclude,
                        parent_declarations=(
                            ParentDeclaration(
                                child_clip_id=item.child_clip_id,
                                parent=item.parent,
                            )
                            for item in request.parents
                        ),
                    )
                    oversized_measured = [
                        artifact.id
                        for artifact in result.manifest.artifacts
                        if artifact.measured_file_duration_s is not None
                        and artifact.measured_file_duration_s
                        > self.max_audio_duration_s
                    ]
                    if oversized_measured:
                        raise SunoImportError(
                            "downloaded clips exceed the "
                            f"{self.max_audio_duration_s:g}s duration limit: "
                            f"{oversized_measured}"
                        )
            checkpoint("importing_project", 60)
            with Database(self.db_path) as database:
                database.import_manifest(result.manifest)
            manifest = result.manifest
            warnings = list(result.warnings)
        else:
            checkpoint("resuming_project", 65)
            with Database(self.db_path) as database:
                manifest = database.export_manifest(request.project_id)
            warnings = ["resumed intake after project import"]

        report: StoredAnalysisReport | None = None
        if request.analyze_now:
            with Database(self.db_path) as database:
                existing_reports = database.list(
                    StoredAnalysisReport, request.project_id
                )
            if existing_reports:
                report = max(existing_reports, key=lambda item: item.created_at)
            else:
                checkpoint("analyzing_audio", 72)
                analyzed = ProjectAnalyzer(manifest).analyze()
                report = StoredAnalysisReport(
                    id=analyzed.run.id,
                    project_id=request.project_id,
                    report=analyzed,
                )
                checkpoint("saving_analysis", 90)
                with (
                    Database(self.db_path) as database,
                    database.transaction(),
                ):
                    database.save(report)
            report_dir = (
                Path(request.report_dir_override).expanduser().resolve()
                if request.report_dir_override
                else self.paths.report_root / project_key(request.project_id)
            )
            self._materialize_report(report, report_dir)

        notify("finalizing", 98)
        return {
            "project_id": request.project_id,
            "project_url": f"/projects/{request.project_id}",
            "artifacts": len(manifest.artifacts),
            "candidates": sum(
                item.raw_payload.get("analysis_role")
                not in {"reference_only", "lineage_only"}
                for item in manifest.artifacts
            ),
            "warnings": warnings,
            "analysis_run_id": report.id if report is not None else None,
        }

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _materialize_report(
        self,
        report: StoredAnalysisReport,
        report_dir: Path,
    ) -> None:
        self._atomic_write_text(
            report_dir / f"{report.id}.json",
            json.dumps(
                report.report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        self._atomic_write_text(
            report_dir / f"{report.id}.md",
            render_markdown(report.report),
        )

    @staticmethod
    def _validate_web_parents(
        request: IntakeRequest,
        clips: Iterable[SunoClipSnapshot],
    ) -> None:
        clip_ids = {clip.id for clip in clips}
        for declaration in request.parents:
            if declaration.child_clip_id not in clip_ids:
                raise SunoImportError(
                    f"unknown child clip ID: {declaration.child_clip_id}"
                )
            if request.allow_local_parent_paths:
                continue
            if declaration.parent in clip_ids:
                continue
            SunoPublicClient.validate_url(declaration.parent)


class IntakeWorker:
    def __init__(
        self,
        store: IntakeJobStore,
        service: IntakeService,
        *,
        cleanup_upload: Callable[[IntakeJob], None],
    ):
        self.store = store
        self.service = service
        self.cleanup_upload = cleanup_upload
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self.store.recover_interrupted()
        self._thread = threading.Thread(
            target=self._loop,
            name="song-eval-intake-worker",
            daemon=True,
        )
        self._thread.start()
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def notify(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.claim_next()
            except Exception:  # pragma: no cover - defensive worker boundary
                logger.exception("intake worker could not claim the next job")
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            if job is None:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            job_id = job.id

            def update_progress(
                step: str,
                value: int,
                current_job_id: str = job_id,
            ) -> None:
                self.store.update_progress(
                    current_job_id,
                    step=step,
                    progress=value,
                )

            def was_canceled(current_job_id: str = job_id) -> bool:
                return self.store.cancel_requested(current_job_id)

            try:
                result = self.service.run(
                    job.request,
                    progress=update_progress,
                    canceled=was_canceled,
                )
            except IntakeCanceled as error:
                self._finish_safely(
                    job_id,
                    status="canceled",
                    error=self._safe_error(error),
                )
            except (
                OSError,
                ValueError,
                SunoImportError,
                httpx.HTTPError,
                sqlite3.Error,
            ) as error:
                self._finish_safely(
                    job_id,
                    status="failed",
                    error=self._safe_error(error),
                )
            except Exception as error:  # pragma: no cover - worker safety boundary
                self._finish_safely(
                    job_id,
                    status="failed",
                    error=self._safe_error(
                        RuntimeError(
                            "unexpected intake failure: "
                            f"{type(error).__name__}: {error}"
                        )
                    ),
                )
            else:
                try:
                    self.cleanup_upload(job)
                except Exception:  # pragma: no cover - defensive callback boundary
                    logger.exception(
                        "intake job %s succeeded but staging cleanup failed",
                        job_id,
                    )
                self._finish_safely(job_id, status="succeeded", result=result)

    def _finish_safely(
        self,
        job_id: str,
        *,
        status: Literal["succeeded", "failed", "canceled"],
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        try:
            self.store.finish(job_id, status=status, result=result, error=error)
        except (sqlite3.Error, KeyError):
            logger.exception(
                "intake worker could not mark job %s as %s",
                job_id,
                status,
            )

    def _safe_error(self, error: Exception) -> str:
        message = str(error)
        for root in (
            self.service.paths.upload_root,
            self.service.paths.media_root,
            self.service.paths.report_root,
            self.service.db_path.parent,
        ):
            unresolved = str(root)
            resolved = str(root.resolve())
            for spelling in {unresolved, resolved}:
                message = message.replace(spelling, "<server-path>")
        return message[:2000]


def remove_upload_staging(job: IntakeJob, upload_root: Path) -> None:
    if job.kind != "upload" or not job.request.upload_paths:
        return
    first = Path(job.request.upload_paths[0]).resolve()
    stage_dir = first.parent
    root = upload_root.resolve()
    if stage_dir.parent == root and stage_dir.is_relative_to(root):
        try:
            shutil.rmtree(stage_dir)
        except FileNotFoundError:
            return


def remove_orphan_upload_staging(
    store: IntakeJobStore,
    upload_root: Path,
) -> int:
    """Remove crash leftovers that have no durable intake job."""

    root = upload_root.resolve()
    known_jobs = store.job_ids()
    removed = 0
    for stage_dir in root.iterdir():
        if (
            not stage_dir.name.startswith("intake_")
            or stage_dir.name in known_jobs
            or stage_dir.is_symlink()
            or not stage_dir.is_dir()
        ):
            continue
        try:
            shutil.rmtree(stage_dir)
        except FileNotFoundError:
            continue
        removed += 1
    return removed
