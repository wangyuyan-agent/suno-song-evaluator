from __future__ import annotations

import base64
import binascii
import html
import json
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .analyzer import ProjectAnalyzer
from .db import Database
from .enums import Axis, PreservationIntent
from .importers import SunoImportError, SunoPublicClient, hydrate_local_artifacts
from .listening import (
    BlindBundle,
    build_blind_session,
    build_listening_review,
    materialize_blind_media,
    merge_project_reviews,
)
from .llm import DeterministicNarrator, OpenAICompatibleNarrator
from .lyrics import confirm_burden_lyric_defect
from .migration import recommend_suno_workflow
from .models import (
    AxisThreshold,
    ComplianceFloor,
    ListeningResponse,
    ListeningReviewRecord,
    LyricAnalysis,
    PreservationDirective,
    ProjectDecisionPolicy,
    ProjectManifest,
    ProjectRecord,
    ProjectReviewPacket,
    ReleaseArtifact,
    StoredAnalysisReport,
    StoredListeningBundle,
    StoredProjectReview,
    StoredReleaseDecision,
)
from .recommendation import record_user_override
from .reference_workflow import (
    register_local_reference,
    registered_directive_target_id,
)
from .reporting import render_markdown
from .util import expand_path, slugify

WEB_ROOT = Path(__file__).with_name("web")
WEB_ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}


def _basic_credentials(header: str | None) -> tuple[bytes, bytes] | None:
    if header is None:
        return None
    scheme, separator, encoded = header.partition(" ")
    if not separator or scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    username, separator, password = decoded.partition(b":")
    if not separator:
        return None
    return username, password


def _configured_secret(
    *,
    value_env: str,
    file_env: str,
) -> str | None:
    direct = os.environ.get(value_env)
    secret_path = os.environ.get(file_env)
    if direct and secret_path:
        raise ValueError(f"configure only one of {value_env} or {file_env}")
    if direct:
        return direct
    if not secret_path:
        return None
    try:
        raw = Path(secret_path).expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"unable to read the configured {file_env}") from error
    secret = raw.rstrip("\r\n")
    if not secret or "\n" in secret or "\r" in secret:
        raise ValueError(f"{file_env} must contain exactly one non-empty line")
    return secret


def _render_ui_shell(
    *,
    page: Literal["projects", "workspace", "blind"],
    page_title: str,
    project_id: str | None = None,
    session_id: str | None = None,
    initial_view: Literal["overview", "named", "plan"] | None = None,
) -> str:
    bootstrap = {
        "page": page,
        "project_id": project_id,
        "session_id": session_id,
        "initial_view": initial_view,
    }
    serialized = json.dumps(
        bootstrap,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    template = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    return template.replace("__PAGE_TITLE__", html.escape(page_title)).replace(
        "__BOOTSTRAP__",
        serialized,
    )


class SunoFetchRequest(BaseModel):
    url: str


class AnalyzeRequest(BaseModel):
    review: ProjectReviewPacket | None = None


class BlindSessionRequest(BaseModel):
    run_id: str
    include_probes: bool = True
    max_hotspots_per_pair: int = Field(default=2, ge=1, le=8)


class ListeningSubmissionRequest(BaseModel):
    responses: tuple[ListeningResponse, ...]


class PolicyDeclarationRequest(BaseModel):
    confirm: bool
    max_na_ratio: float = 0.25
    abstention_strategy: str = (
        "abstain on critical unknown, invalid blind evidence, or unresolved tie"
    )
    require_preservation: bool = True


class ReferenceRegistrationRequest(BaseModel):
    target_artifact_id: str
    reference_path: str
    intent: PreservationIntent = PreservationIntent.STRUCTURAL_GESTURE
    start_s: float = 0.0
    end_s: float | None = None


class SunoPlanRequest(BaseModel):
    directive_id: str
    target_artifact_id: str
    prompt: str
    lyrics_excerpt: str | None = None
    subscription_tier: Literal["pro", "premier", "unknown"] = "pro"
    studio_available: bool = False


class NarrativeRequest(BaseModel):
    run_id: str
    provider: Literal["deterministic", "openai-compatible"] = "deterministic"
    language: str = "zh-CN"
    base_url: str | None = None
    model: str | None = None


class LyricDefectConfirmationRequest(BaseModel):
    artifact_id: str
    line_index: int = Field(ge=0)
    description: str = Field(min_length=3, max_length=500)
    confirm: bool
    analysis_id: str | None = None

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value):
        if isinstance(value, str):
            return " ".join(value.split())
        return value


class ReleaseDecisionRequest(BaseModel):
    artifact_id: str
    confirm: bool
    reason: str | None = Field(default=None, max_length=1000)
    run_id: str | None = None


def create_app(
    db_path: str | Path,
    *,
    media_dir: str | Path | None = None,
    library_roots: tuple[str | Path, ...] = (),
    extra_allowed_hosts: tuple[str, ...] = (),
    auth_username: str | None = None,
    auth_password: str | None = None,
) -> FastAPI:
    if (auth_username is None) != (auth_password is None):
        raise ValueError("auth_username and auth_password must be configured together")
    if auth_username is not None:
        if (
            not auth_username
            or ":" in auth_username
            or "\n" in auth_username
            or "\r" in auth_username
        ):
            raise ValueError("auth_username must be non-empty and cannot contain ':'")
        if not auth_password or "\n" in auth_password or "\r" in auth_password:
            raise ValueError("auth_password must be a non-empty single line")
    for allowed_host in extra_allowed_hosts:
        if (
            not allowed_host
            or "*" in allowed_host
            or "://" in allowed_host
            or "/" in allowed_host
            or any(character.isspace() for character in allowed_host)
        ):
            raise ValueError(
                "extra_allowed_hosts must contain exact hostnames or IP addresses"
            )
    database = Database(db_path)
    media_root = (
        Path(media_dir or tempfile.mkdtemp(prefix="song-eval-media-"))
        .expanduser()
        .resolve()
    )
    media_root.mkdir(parents=True, exist_ok=True)
    trusted_media_roots = (
        media_root,
        *(Path(root).expanduser().resolve() for root in library_roots),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        database.close()

    app = FastAPI(
        title="Suno Song Evaluator",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            "127.0.0.1",
            "localhost",
            "[::1]",
            *extra_allowed_hosts,
        ],
    )

    if auth_username is not None and auth_password is not None:
        expected_username = auth_username.encode("utf-8")
        expected_password = auth_password.encode("utf-8")

        @app.middleware("http")
        async def require_basic_auth(request: Request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            credentials = _basic_credentials(request.headers.get("Authorization"))
            supplied_username, supplied_password = credentials or (b"", b"")
            authenticated = secrets.compare_digest(
                supplied_username,
                expected_username,
            ) and secrets.compare_digest(
                supplied_password,
                expected_password,
            )
            if not authenticated:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "authentication required"},
                    headers={
                        "WWW-Authenticate": (
                            'Basic realm="song-eval", charset="UTF-8"'
                        ),
                        "Cache-Control": "no-store",
                    },
                )
            return await call_next(request)

    app.state.database = database
    app.state.trusted_media_roots = trusted_media_roots
    app.state.auth_enabled = auth_username is not None

    def trusted_local_path(value: str, *, label: str) -> Path:
        path = expand_path(value)
        if not any(path.is_relative_to(root) for root in trusted_media_roots):
            raise ValueError(
                f"{label} must resolve inside media_dir or a configured library root"
            )
        return path

    def validate_manifest_local_paths(manifest: ProjectManifest) -> None:
        for artifact in manifest.artifacts:
            if artifact.local_path:
                trusted_local_path(
                    artifact.local_path,
                    label=f"artifact {artifact.id} local_path",
                )
        for source in manifest.sources:
            if source.local_path:
                trusted_local_path(
                    source.local_path,
                    label=f"source {source.id} local_path",
                )

    def listening_payload(record: StoredListeningBundle) -> dict:
        payload = BlindBundle.from_record(record).public_payload()
        for trial in payload["trials"]:
            for side in ("left", "right"):
                sample_id = trial[side]["sample_id"]
                trial[side]["media_url"] = f"/listening-media/{record.id}/{sample_id}"
        payload["review_url"] = f"/listening/{record.id}"
        payload["response_url"] = f"/listening/{record.id}/responses"
        payload["project_id"] = record.project_id
        payload["project_url"] = f"/projects/{record.project_id}?view=named"
        payload["reason_tags"] = [
            "warmth_fullness",
            "hook_catchiness",
            "vocal_timbre_identity",
            "arrangement_harmony_development",
            "lyric_delivery",
            "ending_completeness",
            "overall_preference",
        ]
        matching_reviews = [
            item
            for item in database.list(ListeningReviewRecord, record.project_id)
            if item.session_id == record.id
        ]
        latest_review = (
            max(matching_reviews, key=lambda item: item.created_at)
            if matching_reviews
            else None
        )
        has_real_trials = any(
            trial.probe_type == "real" for trial in record.session.trials
        )
        review_failures = (
            list(latest_review.validation.failures) if latest_review is not None else []
        )
        empty_trial_failure = (
            "blind-listening session contains no real comparison trials"
        )
        if (
            latest_review is not None
            and not has_real_trials
            and empty_trial_failure not in review_failures
        ):
            review_failures.append(empty_trial_failure)
        payload["review_status"] = (
            {
                "submitted": True,
                "valid": latest_review.validation.valid and has_real_trials,
                "failures": review_failures,
            }
            if latest_review is not None
            else None
        )
        return payload

    def resolve_artifact_brief(
        artifact: ReleaseArtifact,
        *,
        take_by_id: dict,
        event_by_id: dict,
        brief_by_id: dict,
    ):
        take = take_by_id.get(artifact.take_id)
        if take is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"artifact {artifact.id} references missing take {artifact.take_id}"
                ),
            )
        event = event_by_id.get(take.generation_event_id)
        if event is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"take {take.id} references missing generation event "
                    f"{take.generation_event_id}"
                ),
            )
        brief = brief_by_id.get(event.brief_id)
        if brief is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"generation event {event.id} references missing brief "
                    f"{event.brief_id}"
                ),
            )
        return brief

    def workspace_payload(project_id: str) -> dict:
        try:
            manifest = database.export_manifest(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error

        take_by_id = {item.id: item for item in manifest.takes}
        event_by_id = {item.id: item for item in manifest.generation_events}
        brief_by_id = {item.id: item for item in manifest.briefs}
        artifact_by_id = {item.id: item for item in manifest.artifacts}
        report_records = database.list(StoredAnalysisReport, project_id)
        latest_report_record = (
            max(report_records, key=lambda item: item.created_at)
            if report_records
            else None
        )
        latest_report = (
            latest_report_record.report if latest_report_record is not None else None
        )
        metrics = (
            {item.artifact_id: item for item in latest_report.audio_metrics}
            if latest_report is not None
            else {}
        )
        assessments = (
            {item.artifact_id: item for item in latest_report.assessments}
            if latest_report is not None
            else {}
        )
        review_records = database.list(StoredProjectReview, project_id)
        latest_review = (
            max(review_records, key=lambda item: item.created_at)
            if review_records
            else None
        )
        policy_records = database.list(ProjectDecisionPolicy, project_id)
        latest_policy = (
            max(policy_records, key=lambda item: item.created_at)
            if policy_records
            else None
        )
        release_decisions = database.list(StoredReleaseDecision, project_id)
        latest_release_decision = (
            max(release_decisions, key=lambda item: item.created_at)
            if release_decisions
            else None
        )
        listening_records = database.list(StoredListeningBundle, project_id)
        latest_listening = (
            max(listening_records, key=lambda item: item.created_at)
            if listening_records
            else None
        )
        listening_reviews = database.list(ListeningReviewRecord, project_id)
        latest_listening_review = None
        if latest_listening is not None:
            matching_reviews = [
                item
                for item in listening_reviews
                if item.session_id == latest_listening.id
            ]
            latest_listening_review = (
                max(matching_reviews, key=lambda item: item.created_at)
                if matching_reviews
                else None
            )
        latest_listening_has_real_trials = bool(
            latest_listening
            and any(
                trial.probe_type == "real" for trial in latest_listening.session.trials
            )
        )
        latest_lyrics_by_artifact: dict[str, LyricAnalysis] = {}
        for item in sorted(
            database.list(LyricAnalysis, project_id),
            key=lambda value: value.created_at,
        ):
            latest_lyrics_by_artifact[item.artifact_id] = item

        parents_by_child: dict[str, list[str]] = {}
        for edge in manifest.edges:
            parents_by_child.setdefault(edge.child_artifact_id, []).append(
                edge.parent_artifact_id
            )

        candidates = []
        for artifact in manifest.artifacts:
            if artifact.raw_payload.get("analysis_role") in {
                "reference_only",
                "lineage_only",
            }:
                continue
            brief = resolve_artifact_brief(
                artifact,
                take_by_id=take_by_id,
                event_by_id=event_by_id,
                brief_by_id=brief_by_id,
            )
            metric = metrics.get(artifact.id)
            parent_ids = parents_by_child.get(artifact.id, [])
            parent_state = (
                "已记录"
                if parent_ids
                else ("unknown" if artifact.operation.value != "raw" else None)
            )
            candidates.append(
                {
                    "artifact_id": artifact.id,
                    "title": artifact.title or artifact.platform_id or artifact.id,
                    "platform_id": artifact.platform_id,
                    "operation": artifact.operation.value,
                    "audio_url": (
                        f"/projects/{project_id}/artifacts/{artifact.id}/audio"
                    ),
                    "audio_available": bool(
                        artifact.local_path and Path(artifact.local_path).exists()
                    ),
                    "platform_duration_s": artifact.platform_reported_duration_s,
                    "measured_duration_s": (
                        metric.measured_file_duration_s
                        if metric is not None
                        else artifact.measured_file_duration_s
                    ),
                    "acquisition_path": artifact.acquisition_path.value,
                    "brief_id": brief.id,
                    "requirements": [
                        requirement.model_dump(mode="json")
                        for requirement in brief.requirements
                    ],
                    "ending": (
                        metric.ending.model_dump(mode="json")
                        if metric is not None
                        else None
                    ),
                    "assessment": (
                        assessments[artifact.id].model_dump(mode="json")
                        if artifact.id in assessments
                        else None
                    ),
                    "parent_state": parent_state,
                    "parent_artifact_ids": parent_ids,
                }
            )

        reference_payloads = []
        for reference in manifest.references:
            source_artifact = artifact_by_id.get(reference.source_artifact_id)
            reference_payloads.append(
                {
                    **reference.model_dump(mode="json"),
                    "duration_s": reference.duration_s,
                    "source_artifact_title": (
                        source_artifact.title if source_artifact is not None else None
                    ),
                }
            )

        unknown_parent_count = sum(
            item["parent_state"] == "unknown" for item in candidates
        )
        return {
            "project": {
                "id": manifest.project_id,
                "title": manifest.title,
            },
            "briefs": [item.model_dump(mode="json") for item in manifest.briefs],
            "candidates": candidates,
            "latest_report": (
                latest_report.model_dump(mode="json")
                if latest_report is not None
                else None
            ),
            "latest_review": (
                latest_review.model_dump(mode="json")
                if latest_review is not None
                else None
            ),
            "policy": (
                latest_policy.model_dump(mode="json")
                if latest_policy is not None
                else None
            ),
            "latest_release_decision": (
                latest_release_decision.model_dump(mode="json")
                if latest_release_decision is not None
                else None
            ),
            "latest_listening": (
                {
                    "session_id": latest_listening.id,
                    "run_id": latest_listening.run_id,
                    "review_url": f"/listening/{latest_listening.id}",
                    "valid": bool(
                        latest_listening_review
                        and latest_listening_review.validation.valid
                        and latest_listening_has_real_trials
                    ),
                    "failures": (
                        [
                            *latest_listening_review.validation.failures,
                            *(
                                ()
                                if latest_listening_has_real_trials
                                else (
                                    "blind-listening session contains no real "
                                    "comparison trials",
                                )
                            ),
                        ]
                        if latest_listening_review is not None
                        else []
                    ),
                    "created_at": latest_listening.created_at.isoformat(),
                }
                if latest_listening is not None
                else None
            ),
            "lyric_analyses": [
                item.model_dump(mode="json")
                for item in latest_lyrics_by_artifact.values()
            ],
            "references": reference_payloads,
            "directives": [
                {
                    **item.model_dump(mode="json"),
                    "resolved_target_id": registered_directive_target_id(
                        item,
                        manifest.artifacts,
                    ),
                }
                for item in manifest.directives
            ],
            "source_assessments": [
                item.model_dump(mode="json") for item in manifest.source_assessments
            ],
            "lineage": {
                "known_edges": len(manifest.edges),
                "unknown_parent_count": unknown_parent_count,
            },
        }

    def redact_local_paths(value):
        explicit_path_fields = {
            "local_path",
            "media_path",
            "source_path",
            "reference_path",
            "media_files",
            "output_path",
        }
        semantic_path_fields = {"acquisition_path"}

        def is_path_field(key: str) -> bool:
            normalized = key.lower()
            if normalized in semantic_path_fields:
                return False
            return (
                normalized in explicit_path_fields
                or normalized in {"path", "paths"}
                or normalized.endswith(("_path", "_paths", "_path_map"))
            )

        if isinstance(value, dict):
            return {
                key: (
                    "<local-path-redacted>"
                    if is_path_field(key) and item is not None
                    else redact_local_paths(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [redact_local_paths(item) for item in value]
        return value

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def project_index_page() -> str:
        return _render_ui_shell(
            page="projects",
            page_title="歌曲评估 · 本地项目",
        )

    @app.get("/ui/{asset_name}")
    def web_asset(asset_name: str) -> FileResponse:
        media_type = WEB_ASSETS.get(asset_name)
        asset_path = WEB_ROOT / asset_name
        if media_type is None or not asset_path.is_file():
            raise HTTPException(status_code=404, detail="UI asset not found")
        return FileResponse(
            asset_path,
            media_type=media_type,
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/projects")
    def projects() -> list[dict]:
        return [item.model_dump(mode="json") for item in database.list_projects()]

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_workspace_page(project_id: str) -> str:
        project = database.get(ProjectRecord, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render_ui_shell(
            page="workspace",
            page_title=f"{project.title} · 项目总览",
            project_id=project_id,
            initial_view="overview",
        )

    @app.get("/projects/{project_id}/plan", response_class=HTMLResponse)
    def project_plan_page(project_id: str) -> str:
        project = database.get(ProjectRecord, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render_ui_shell(
            page="workspace",
            page_title=f"{project.title} · 参考段计划",
            project_id=project_id,
            initial_view="plan",
        )

    @app.post("/manifests/import")
    def import_manifest(manifest: ProjectManifest) -> dict:
        try:
            validate_manifest_local_paths(manifest)
            hydrated = hydrate_local_artifacts(
                manifest,
                require_files=True,
            )
            database.import_manifest(hydrated)
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "project_id": hydrated.project_id,
            "artifacts": len(hydrated.artifacts),
        }

    @app.get("/projects/{project_id}/manifest")
    def export_manifest(project_id: str) -> dict:
        try:
            manifest = database.export_manifest(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        return manifest.model_dump(mode="json")

    @app.post("/projects/{project_id}/policy")
    def declare_policy(
        project_id: str,
        request: PolicyDeclarationRequest,
    ) -> dict:
        if database.get(ProjectRecord, project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not request.confirm:
            raise HTTPException(
                status_code=422,
                detail="decision policy requires explicit user confirmation",
            )
        if not 0 <= request.max_na_ratio <= 1:
            raise HTTPException(
                status_code=422,
                detail="max_na_ratio must be between 0 and 1",
            )
        with database.transaction():
            existing = database.list(ProjectDecisionPolicy, project_id)
            version_number = len(existing) + 1
            policy = ProjectDecisionPolicy(
                id=f"policy_{slugify(project_id)}_v{version_number}",
                project_id=project_id,
                version=f"v{version_number}",
                declared_by_user=True,
                priority_declared_by_user=True,
                axis_priority=(
                    Axis.COMPLIANCE,
                    Axis.CRAFT,
                    Axis.RELEASE_READINESS,
                    Axis.DISTINCTIVENESS,
                ),
                compliance_floor=ComplianceFloor(
                    reject_confirmed_t1=True,
                    burden_lyrics_must_pass=True,
                    hard_requirements_must_pass=True,
                    abstain_on_critical_unknown=True,
                    must_preserve_directives_must_pass=request.require_preservation,
                ),
                axis_thresholds=tuple(
                    AxisThreshold(
                        axis=axis,
                        ordinal_delta=1,
                        source="user-confirmed local web policy",
                    )
                    for axis in (
                        Axis.COMPLIANCE,
                        Axis.CRAFT,
                        Axis.RELEASE_READINESS,
                    )
                ),
                max_na_ratio=request.max_na_ratio,
                abstention_strategy=request.abstention_strategy,
            )
            database.save(policy)
        return policy.model_dump(mode="json")

    @app.post("/projects/{project_id}/reviews")
    def save_project_review(
        project_id: str,
        review: ProjectReviewPacket,
    ) -> dict:
        if database.get(ProjectRecord, project_id) is None:
            raise HTTPException(status_code=404, detail="project not found")
        if review.project_id != project_id:
            raise HTTPException(status_code=422, detail="review project_id mismatch")
        record = StoredProjectReview(
            project_id=project_id,
            source="web",
            review_packet=review,
        )
        with database.transaction():
            database.save(record)
        return {
            "review_id": record.id,
            "artifact_reviews": len(review.artifact_reviews),
        }

    @app.post("/projects/{project_id}/lyric-defects")
    def confirm_lyric_defect(
        project_id: str,
        request: LyricDefectConfirmationRequest,
    ) -> dict:
        if not request.confirm:
            raise HTTPException(
                status_code=422,
                detail="human confirmation is required for a burden-bearing lyric T1",
            )
        artifact = database.get(ReleaseArtifact, request.artifact_id)
        if artifact is None or artifact.project_id != project_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        analyses = [
            item
            for item in database.list(LyricAnalysis, project_id)
            if item.artifact_id == artifact.id
            and (request.analysis_id is None or item.id == request.analysis_id)
        ]
        if not analyses:
            raise HTTPException(
                status_code=404,
                detail="lyric analysis not found for artifact",
            )
        analysis = max(analyses, key=lambda item: item.created_at)
        location = next(
            (
                item
                for item in analysis.locations
                if item.line_index == request.line_index
            ),
            None,
        )
        if location is None:
            raise HTTPException(status_code=422, detail="lyric line index not found")
        defect = confirm_burden_lyric_defect(
            location,
            project_id=project_id,
            artifact_id=artifact.id,
            brief_id=analysis.brief_id,
            human_confirmation=True,
            description=request.description,
        )
        with database.transaction():
            database.save(defect)
        return defect.model_dump(mode="json")

    @app.post("/projects/{project_id}/release-decisions")
    def record_release_decision(
        project_id: str,
        request: ReleaseDecisionRequest,
    ) -> dict:
        if not request.confirm:
            raise HTTPException(
                status_code=422,
                detail="final human choice requires explicit confirmation",
            )
        reports = database.list(StoredAnalysisReport, project_id)
        if request.run_id is not None:
            reports = [item for item in reports if item.id == request.run_id]
        if not reports:
            raise HTTPException(status_code=404, detail="analysis report not found")
        stored = max(reports, key=lambda item: item.created_at)
        candidate_ids = {item.artifact_id for item in stored.report.assessments}
        if request.artifact_id not in candidate_ids:
            raise HTTPException(
                status_code=422,
                detail="final choice must be a candidate from the selected report",
            )
        recommendation = record_user_override(
            stored.report.recommendation,
            artifact_id=request.artifact_id,
            reason=request.reason,
        )
        record = StoredReleaseDecision(
            project_id=project_id,
            analysis_run_id=stored.id,
            recommendation=recommendation,
        )
        with database.transaction():
            database.save(record)
        return record.model_dump(mode="json")

    @app.post("/projects/{project_id}/references/register")
    def register_reference(
        project_id: str,
        request: ReferenceRegistrationRequest,
    ) -> dict:
        try:
            manifest = database.export_manifest(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        try:
            reference_path = trusted_local_path(
                request.reference_path,
                label="reference audio path",
            )
            registration = register_local_reference(
                manifest,
                target_artifact_id=request.target_artifact_id,
                reference_path=reference_path,
                media_dir=media_root / "references" / project_id,
                intent=request.intent,
                start_s=request.start_s,
                end_s=request.end_s,
            )
        except (KeyError, ValueError, FileNotFoundError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        with database.transaction():
            for record in (registration.event, registration.take):
                if record is not None and database.get(type(record), record.id) is None:
                    database.save(record)
            for record in (
                registration.artifact,
                registration.reference,
                registration.directive,
            ):
                if database.get(type(record), record.id) is None:
                    database.save(record)
        return {
            "reference_artifact_id": registration.artifact.id,
            "reference_segment_id": registration.reference.id,
            "directive_id": registration.directive.id,
            "reference_attached_to_generation": False,
        }

    @app.post("/projects/{project_id}/suno-plan")
    def suno_plan(project_id: str, request: SunoPlanRequest) -> dict:
        directive = database.get(PreservationDirective, request.directive_id)
        artifact = database.get(ReleaseArtifact, request.target_artifact_id)
        if (
            directive is None
            or artifact is None
            or directive.project_id != project_id
            or artifact.project_id != project_id
        ):
            raise HTTPException(
                status_code=404,
                detail="directive or target artifact not found",
            )
        registered_target_id = registered_directive_target_id(
            directive,
            database.list(ReleaseArtifact, project_id),
        )
        if registered_target_id is not None and registered_target_id != artifact.id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "directive was registered for a different target artifact; "
                    "register a new directive for this target"
                ),
            )
        recommendation = recommend_suno_workflow(
            directive=directive,
            target_artifact=artifact,
            prompt=request.prompt,
            frozen_lyrics_excerpt=request.lyrics_excerpt,
            subscription_tier=request.subscription_tier,
            studio_available=request.studio_available,
        )
        return recommendation.model_dump(mode="json")

    @app.get("/projects/{project_id}/review-context")
    def review_context(project_id: str) -> dict:
        try:
            manifest = database.export_manifest(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        take_by_id = {item.id: item for item in manifest.takes}
        event_by_id = {item.id: item for item in manifest.generation_events}
        brief_by_id = {item.id: item for item in manifest.briefs}
        reports = database.list(StoredAnalysisReport, project_id)
        latest_report = (
            max(reports, key=lambda item: item.created_at).report if reports else None
        )
        metrics = (
            {item.artifact_id: item for item in latest_report.audio_metrics}
            if latest_report is not None
            else {}
        )
        policies = database.list(ProjectDecisionPolicy, project_id)
        latest_policy = (
            max(policies, key=lambda item: item.created_at) if policies else None
        )
        candidates = []
        for artifact in manifest.artifacts:
            if artifact.raw_payload.get("analysis_role") in {
                "reference_only",
                "lineage_only",
            }:
                continue
            brief = resolve_artifact_brief(
                artifact,
                take_by_id=take_by_id,
                event_by_id=event_by_id,
                brief_by_id=brief_by_id,
            )
            candidates.append(
                {
                    "artifact_id": artifact.id,
                    "title": artifact.title or artifact.id,
                    "audio_url": (
                        f"/projects/{project_id}/artifacts/{artifact.id}/audio"
                    ),
                    "brief_id": brief.id,
                    "requirements": [
                        requirement.model_dump(mode="json")
                        for requirement in brief.requirements
                    ],
                    "ending": (
                        metrics[artifact.id].ending.model_dump(mode="json")
                        if artifact.id in metrics
                        else None
                    ),
                }
            )
        return {
            "project_id": project_id,
            "title": manifest.title,
            "policy_declared": bool(
                latest_policy is not None and latest_policy.declared_by_user
            ),
            "candidates": candidates,
        }

    @app.get("/projects/{project_id}/workspace-context")
    def project_workspace_context(project_id: str) -> dict:
        return workspace_payload(project_id)

    @app.get("/projects/{project_id}/evidence.json")
    def project_evidence_package(project_id: str) -> JSONResponse:
        context = workspace_payload(project_id)
        manifest = database.export_manifest(project_id)
        safe_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in project_id
        )
        payload = redact_local_paths(
            {
                "schema_version": "1",
                "project": context["project"],
                "manifest": manifest.model_dump(mode="json"),
                "latest_report": context["latest_report"],
                "latest_review": context["latest_review"],
                "policy": context["policy"],
                "latest_release_decision": context["latest_release_decision"],
                "latest_listening": context["latest_listening"],
                "lyric_analyses": context["lyric_analyses"],
                "references": context["references"],
                "directives": context["directives"],
            }
        )
        return JSONResponse(
            content=payload,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="song-eval-{safe_id}-evidence.json"'
                ),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/projects/{project_id}/artifacts/{artifact_id}/audio")
    def artifact_audio(project_id: str, artifact_id: str) -> FileResponse:
        artifact = database.get(ReleaseArtifact, artifact_id)
        if artifact is None or artifact.project_id != project_id:
            raise HTTPException(status_code=404, detail="artifact not found")
        if not artifact.local_path:
            raise HTTPException(status_code=404, detail="audio not found")
        try:
            path = trusted_local_path(
                artifact.local_path,
                label=f"artifact {artifact.id} local_path",
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail="audio not found") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="audio not found")
        return FileResponse(path)

    @app.get("/projects/{project_id}/review", response_class=HTMLResponse)
    def project_review_page(project_id: str) -> str:
        project = database.get(ProjectRecord, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _render_ui_shell(
            page="workspace",
            page_title=f"{project.title} · 候选需求与技术复核",
            project_id=project_id,
            initial_view="named",
        )

    @app.post("/suno/fetch")
    def suno_fetch(request: SunoFetchRequest) -> list[dict]:
        try:
            with SunoPublicClient() as client:
                clips = client.fetch(request.url)
        except (SunoImportError, httpx.HTTPError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return [clip.model_dump(mode="json") for clip in clips]

    @app.post("/projects/{project_id}/analysis")
    def analyze(project_id: str, request: AnalyzeRequest) -> dict:
        try:
            manifest = database.export_manifest(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        review = request.review or ProjectReviewPacket(project_id=project_id)
        if review.project_id != project_id:
            raise HTTPException(status_code=422, detail="review project_id mismatch")
        project_reviews = database.list(StoredProjectReview, project_id)
        if project_reviews:
            latest_project_review = max(
                project_reviews,
                key=lambda item: item.created_at,
            )
            review = merge_project_reviews(
                review,
                latest_project_review.review_packet,
            )
        stored_lyrics = database.list(LyricAnalysis, project_id)
        listening_sessions = database.list(StoredListeningBundle, project_id)
        listening_reviews = database.list(ListeningReviewRecord, project_id)
        if listening_sessions:
            latest_session = max(
                listening_sessions,
                key=lambda item: item.created_at,
            )
            matching_reviews = [
                item
                for item in listening_reviews
                if item.session_id == latest_session.id
            ]
            if matching_reviews:
                latest_listening_review = max(
                    matching_reviews,
                    key=lambda item: item.created_at,
                )
                review = merge_project_reviews(
                    review,
                    latest_listening_review.review_packet,
                )
        if stored_lyrics:
            latest_by_artifact = {
                item.artifact_id: item
                for item in sorted(stored_lyrics, key=lambda item: item.created_at)
            }
            review = merge_project_reviews(
                review,
                ProjectReviewPacket(
                    project_id=project_id,
                    lyric_analyses=list(latest_by_artifact.values()),
                ),
            )
        try:
            report = ProjectAnalyzer(manifest).analyze(review)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        stored = StoredAnalysisReport(
            id=report.run.id,
            project_id=project_id,
            report=report,
        )
        with database.transaction():
            database.save(stored)
        return report.model_dump(mode="json")

    @app.get(
        "/projects/{project_id}/reports/{run_id}.md",
        response_class=PlainTextResponse,
    )
    def get_markdown_report(project_id: str, run_id: str) -> str:
        stored = database.get(StoredAnalysisReport, run_id)
        if not stored or stored.project_id != project_id:
            raise HTTPException(status_code=404, detail="report not found")
        return render_markdown(stored.report)

    @app.get("/projects/{project_id}/reports/{run_id}")
    def get_report(project_id: str, run_id: str) -> dict:
        stored = database.get(StoredAnalysisReport, run_id)
        if not stored or stored.project_id != project_id:
            raise HTTPException(status_code=404, detail="report not found")
        return stored.report.model_dump(mode="json")

    @app.post("/projects/{project_id}/blind-sessions")
    def blind_session(project_id: str, request: BlindSessionRequest) -> dict:
        stored = database.get(StoredAnalysisReport, request.run_id)
        if not stored or stored.project_id != project_id:
            raise HTTPException(status_code=404, detail="report not found")
        manifest = database.export_manifest(project_id)
        artifact_paths = {
            artifact.id: artifact.local_path
            for artifact in manifest.artifacts
            if artifact.local_path
        }
        try:
            bundle = build_blind_session(
                project_id=project_id,
                comparisons=stored.report.comparisons,
                artifact_paths=artifact_paths,
                include_probes=request.include_probes,
                max_hotspots_per_pair=request.max_hotspots_per_pair,
            )
            generated = materialize_blind_media(
                bundle,
                media_root / bundle.session.id,
            )
        except (KeyError, ValueError, OSError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        record = bundle.to_record(run_id=request.run_id, media_files=generated)
        with database.transaction():
            database.save(record)
        return listening_payload(record)

    @app.get("/listening-sessions/{session_id}")
    def listening_session(session_id: str) -> dict:
        record = database.get(StoredListeningBundle, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="listening session not found")
        return listening_payload(record)

    @app.get("/listening-media/{session_id}/{sample_id}")
    def listening_media(session_id: str, sample_id: str) -> FileResponse:
        record = database.get(StoredListeningBundle, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="listening session not found")
        value = record.media_files.get(sample_id)
        if not value:
            raise HTTPException(status_code=404, detail="sample not found")
        try:
            path = trusted_local_path(
                value,
                label=f"listening sample {sample_id} media_path",
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail="sample not found") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="sample not found")
        return FileResponse(path, media_type="audio/wav", filename="sample.wav")

    @app.post("/listening/{session_id}/responses")
    def listening_responses(
        session_id: str,
        request: ListeningSubmissionRequest,
    ) -> dict:
        stored_bundle = database.get(StoredListeningBundle, session_id)
        if stored_bundle is None:
            raise HTTPException(status_code=404, detail="listening session not found")
        bundle = BlindBundle.from_record(stored_bundle)
        validation, review_packet = build_listening_review(
            bundle,
            request.responses,
        )
        record = ListeningReviewRecord(
            project_id=stored_bundle.project_id,
            session_id=session_id,
            responses=request.responses,
            validation=validation,
            review_packet=review_packet,
        )
        with database.transaction():
            existing_reviews = [
                item
                for item in database.list(
                    ListeningReviewRecord,
                    stored_bundle.project_id,
                )
                if item.session_id == session_id and item.validation.valid
            ]
            if existing_reviews:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "listening session already has a valid review; "
                        "create a new session to review again"
                    ),
                )
            database.save(record)
        return {
            "review_id": record.id,
            "valid": validation.valid,
            "failures": list(validation.failures),
            "review": review_packet.model_dump(mode="json"),
        }

    @app.get("/listening/{session_id}", response_class=HTMLResponse)
    def listening_page(session_id: str) -> str:
        record = database.get(StoredListeningBundle, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="listening session not found")
        return _render_ui_shell(
            page="blind",
            page_title="匿名盲听复核",
            project_id=record.project_id,
            session_id=session_id,
        )

    @app.post("/projects/{project_id}/narratives")
    def narrative(project_id: str, request: NarrativeRequest) -> dict:
        stored = database.get(StoredAnalysisReport, request.run_id)
        if not stored or stored.project_id != project_id:
            raise HTTPException(status_code=404, detail="report not found")
        if request.provider == "deterministic":
            result = DeterministicNarrator().narrate(
                stored.report,
                language=request.language,
            )
        else:
            try:
                api_key = _configured_secret(
                    value_env="SONG_EVAL_LLM_API_KEY",
                    file_env="SONG_EVAL_LLM_API_KEY_FILE",
                )
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            base_url = os.environ.get("SONG_EVAL_LLM_BASE_URL")
            model = request.model or os.environ.get("SONG_EVAL_LLM_MODEL")
            if not api_key or not base_url or not model:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "openai-compatible narration requires base_url/model and "
                        "SONG_EVAL_LLM_API_KEY"
                    ),
                )
            if request.base_url is not None and request.base_url != base_url:
                raise HTTPException(
                    status_code=422,
                    detail="base_url must match SONG_EVAL_LLM_BASE_URL",
                )
            try:
                with OpenAICompatibleNarrator(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                ) as narrator:
                    result = narrator.narrate(
                        stored.report,
                        language=request.language,
                    )
            except (
                httpx.HTTPError,
                IndexError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        return result.model_dump(mode="json")

    return app
