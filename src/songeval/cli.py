from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError

from .analyzer import ProjectAnalyzer
from .api import create_app
from .audio import verify_deterministic_crop
from .db import Database
from .enums import Axis, PreservationIntent
from .importers import (
    SunoImportError,
    SunoPublicClient,
    load_manifest,
    load_suno_snapshots,
)
from .intake import IntakeParent, IntakePaths, IntakeRequest, IntakeService
from .listening import (
    BlindBundle,
    build_blind_session,
    build_listening_review,
    materialize_blind_media,
    merge_project_reviews,
)
from .lyrics import (
    JsonTranscriptProvider,
    MlxWhisperTranscriptProvider,
    analyze_lyrics,
    confirm_burden_lyric_defect,
)
from .migration import (
    plan_structural_gesture_replace,
    recommend_suno_workflow,
)
from .models import (
    AxisThreshold,
    ComplianceFloor,
    CreativeBriefVersion,
    GenerationEvent,
    ListeningResponse,
    ListeningReviewRecord,
    LyricAnalysis,
    PreservationDirective,
    ProjectDecisionPolicy,
    ProjectRecord,
    ProjectReviewPacket,
    ReleaseArtifact,
    StoredAnalysisReport,
    StoredListeningBundle,
    StoredProjectReview,
    StoredReleaseDecision,
    Take,
)
from .recommendation import record_user_override
from .reference_workflow import (
    register_local_reference,
    registered_directive_target_id,
)
from .reporting import render_markdown
from .util import project_key

app = typer.Typer(
    name="song-eval",
    help="Evidence-first Suno candidate comparison and release decision support.",
    no_args_is_help=True,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_secret_file(path: Path, *, label: str) -> str:
    try:
        raw = path.expanduser().read_text(encoding="utf-8")
    except OSError as error:
        raise typer.BadParameter(f"unable to read {label}") from error
    secret = raw.rstrip("\r\n")
    if not secret or "\n" in secret or "\r" in secret:
        raise typer.BadParameter(f"{label} must contain exactly one non-empty line")
    return secret


def _validate_allowed_hosts(values: list[str] | None) -> tuple[str, ...]:
    hosts = tuple(values or ())
    for host in hosts:
        if (
            not host
            or "*" in host
            or "://" in host
            or "/" in host
            or any(character.isspace() for character in host)
        ):
            raise typer.BadParameter(
                "allowed hosts must be exact hostnames or IP addresses"
            )
    return hosts


@app.command("init")
def initialize(
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
) -> None:
    with Database(db):
        pass
    typer.echo(f"initialized {db.resolve()}")


@app.command("configure-policy")
def configure_policy_command(
    project_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Explicitly accept the displayed lexical decision policy",
        ),
    ] = False,
    max_na_ratio: Annotated[
        float,
        typer.Option("--max-na-ratio", min=0.0, max=1.0),
    ] = 0.25,
    abstention_strategy: Annotated[
        str,
        typer.Option("--abstention-strategy"),
    ] = "abstain on critical unknown, invalid blind evidence, or unresolved tie",
    require_preservation: Annotated[
        bool,
        typer.Option(
            "--require-preservation/--allow-unverified-preservation",
        ),
    ] = True,
) -> None:
    """Declare the project-level release policy; never inferred automatically."""
    if not confirm:
        raise typer.BadParameter(
            "--confirm is required because decision policy must be user-declared"
        )
    with Database(db) as database:
        database.require(ProjectRecord, project_id)
        with database.transaction():
            existing = database.list(ProjectDecisionPolicy, project_id)
            version_number = len(existing) + 1
            policy = ProjectDecisionPolicy(
                id=f"policy_{project_key(project_id)}_v{version_number}",
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
                    must_preserve_directives_must_pass=require_preservation,
                ),
                axis_thresholds=tuple(
                    AxisThreshold(
                        axis=axis,
                        ordinal_delta=1,
                        source="user-confirmed v0.2 policy",
                    )
                    for axis in (
                        Axis.COMPLIANCE,
                        Axis.CRAFT,
                        Axis.RELEASE_READINESS,
                    )
                ),
                max_na_ratio=max_na_ratio,
                abstention_strategy=abstention_strategy,
            )
            database.save(policy)
    typer.echo(policy.model_dump_json())


@app.command("import-manifest")
def import_manifest_command(
    manifest_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    allow_missing_audio: Annotated[
        bool,
        typer.Option(
            "--allow-missing-audio",
            help="Keep unresolved paths instead of failing import.",
        ),
    ] = False,
) -> None:
    manifest = load_manifest(
        manifest_path,
        hydrate_audio=True,
        require_files=not allow_missing_audio,
    )
    with Database(db) as database:
        database.import_manifest(manifest)
    typer.echo(
        json.dumps(
            {
                "project_id": manifest.project_id,
                "briefs": len(manifest.briefs),
                "events": len(manifest.generation_events),
                "takes": len(manifest.takes),
                "artifacts": len(manifest.artifacts),
            },
            ensure_ascii=False,
        )
    )


@app.command("fetch-suno")
def fetch_suno(
    url: Annotated[str, typer.Argument(help="Suno share or playlist URL")],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Optional raw snapshot JSON output"),
    ] = None,
) -> None:
    with SunoPublicClient() as client:
        clips = client.fetch(url)
    payload = [clip.model_dump(mode="json") for clip in clips]
    if output:
        _write_json(output, payload)
        typer.echo(str(output.resolve()))
    else:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@app.command("intake")
def intake_command(
    project_id: Annotated[str, typer.Argument(help="Stable local project ID")],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    title: Annotated[str | None, typer.Option("--title")] = None,
    suno_url: Annotated[
        str | None,
        typer.Option("--suno-url", help="Suno share or playlist URL"),
    ] = None,
    snapshot: Annotated[
        Path | None,
        typer.Option(
            "--snapshot",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Previously captured fetch-suno JSON",
        ),
    ] = None,
    audio: Annotated[
        list[Path] | None,
        typer.Option(
            "--audio",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Local candidate audio; repeat for multiple files",
        ),
    ] = None,
    media_dir: Annotated[
        Path | None,
        typer.Option("--media-dir", help="Stable byte-for-byte audio cache"),
    ] = None,
    lyrics_file: Annotated[
        Path | None,
        typer.Option(
            "--lyrics-file",
            exists=True,
            dir_okay=False,
            readable=True,
            help="User-declared frozen lyrics",
        ),
    ] = None,
    style: Annotated[str | None, typer.Option("--style")] = None,
    exclude: Annotated[str | None, typer.Option("--exclude")] = None,
    parent: Annotated[
        list[str] | None,
        typer.Option(
            "--parent",
            help=(
                "Declared child=parent relation; parent may be a clip ID, "
                "local file, or Suno share URL"
            ),
        ),
    ] = None,
    download_audio: Annotated[
        bool,
        typer.Option(
            "--download-audio/--no-download-audio",
            help="Download captured Suno audio without transcoding",
        ),
    ] = True,
    manifest_out: Annotated[
        Path | None,
        typer.Option("--manifest-out", help="Optional generated manifest JSON"),
    ] = None,
    analyze_now: Annotated[
        bool,
        typer.Option(
            "--analyze/--no-analyze",
            help="Create the initial evidence report immediately",
        ),
    ] = True,
    report_dir: Annotated[
        Path | None,
        typer.Option("--report-dir", help="Initial JSON and Markdown report directory"),
    ] = None,
) -> None:
    """Create and import a comparison project without hand-writing a manifest."""
    local_audio = tuple(audio or ())
    source_modes = sum(
        (
            suno_url is not None,
            snapshot is not None,
            bool(local_audio),
        )
    )
    if source_modes != 1:
        raise typer.BadParameter(
            "choose exactly one source: --suno-url, --snapshot, or --audio"
        )
    if parent and local_audio:
        raise typer.BadParameter("--parent is only valid for Suno intake")
    lyrics = (
        lyrics_file.read_text(encoding="utf-8").strip()
        if lyrics_file is not None
        else None
    )
    destination = (
        media_dir
        if media_dir is not None
        else db.resolve().parent / "song-eval-media" / project_key(project_id)
    )
    declarations: list[IntakeParent] = []
    for value in parent or ():
        child, separator, declared_parent = value.partition("=")
        if not separator or not child.strip() or not declared_parent.strip():
            raise typer.BadParameter(
                "--parent must use CHILD_CLIP_ID=PARENT_CLIP_ID_OR_PATH_OR_URL"
            )
        try:
            declarations.append(
                IntakeParent(
                    child_clip_id=child.strip(),
                    parent=declared_parent.strip(),
                )
            )
        except ValidationError as error:
            raise typer.BadParameter(str(error)) from error
    destination_reports = (
        report_dir
        if report_dir is not None
        else db.resolve().parent / "song-eval-reports" / project_key(project_id)
    )
    with Database(db) as database:
        if database.get(ProjectRecord, project_id) is not None:
            raise typer.BadParameter(
                f"project {project_id!r} already exists; run "
                f"`song-eval analyze {project_id} --db {db}` to resume analysis, "
                "or choose a new project ID for a separate import"
            )
    try:
        intake_request = IntakeRequest(
            project_id=project_id,
            title=title or project_id,
            kind="upload" if local_audio else "suno",
            suno_url=suno_url,
            snapshots=(
                tuple(load_suno_snapshots(snapshot)) if snapshot is not None else ()
            ),
            upload_paths=tuple(str(path.resolve()) for path in local_audio),
            original_filenames=tuple(path.name for path in local_audio),
            lyrics=lyrics,
            style=style,
            exclude=exclude,
            parents=tuple(declarations),
            download_audio=download_audio,
            analyze_now=analyze_now,
            allow_local_parent_paths=True,
            allow_external_upload_paths=True,
            media_dir_override=str(destination.resolve()),
            report_dir_override=str(destination_reports.resolve()),
        )
    except (OSError, ValueError, SunoImportError) as error:
        raise typer.BadParameter(str(error)) from error
    service = IntakeService(
        db,
        IntakePaths(
            media_root=db.resolve().parent / "song-eval-media",
            report_root=db.resolve().parent / "song-eval-reports",
            upload_root=db.resolve().parent,
        ),
    )
    try:
        intake_result = service.run(intake_request)
    except (OSError, ValueError, SunoImportError) as error:
        with Database(db) as database:
            imported = database.get(ProjectRecord, project_id) is not None
        if imported and analyze_now:
            raise typer.BadParameter(
                f"project imported, but initial analysis failed: {error}"
            ) from error
        raise typer.BadParameter(str(error)) from error

    with Database(db) as database:
        manifest = database.export_manifest(project_id)
        reports = database.list(StoredAnalysisReport, project_id)
    initial_report = (
        max(reports, key=lambda item: item.created_at).report if reports else None
    )
    report_json = (
        destination_reports / f"{initial_report.run.id}.json"
        if initial_report is not None
        else None
    )
    report_markdown = (
        destination_reports / f"{initial_report.run.id}.md"
        if initial_report is not None
        else None
    )
    if manifest_out is not None:
        _write_json(manifest_out, manifest)
    typer.echo(
        json.dumps(
            {
                "project_id": project_id,
                "artifacts": len(manifest.artifacts),
                "candidates": intake_result["candidates"],
                "media_dir": str(destination.resolve()),
                "manifest_out": (
                    str(manifest_out.resolve()) if manifest_out is not None else None
                ),
                "warnings": intake_result["warnings"],
                "initial_analysis": (
                    {
                        "run_id": initial_report.run.id,
                        "recommendation_status": (
                            initial_report.recommendation.status.value
                        ),
                        "json_report": str(report_json.resolve()),
                        "markdown_report": str(report_markdown.resolve()),
                    }
                    if initial_report is not None
                    and report_json is not None
                    and report_markdown is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )
    )


@app.command("analyze")
def analyze_command(
    project_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    review_path: Annotated[
        Path | None,
        typer.Option(
            "--review",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Human review packet JSON",
        ),
    ] = None,
    json_out: Annotated[
        Path | None,
        typer.Option("--json-out", help="Structured analysis output"),
    ] = None,
    markdown_out: Annotated[
        Path | None,
        typer.Option("--markdown-out", help="Reader report output"),
    ] = None,
) -> None:
    review = None
    if review_path:
        try:
            review = ProjectReviewPacket.model_validate_json(
                review_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise typer.BadParameter(f"review JSON is not valid: {error}") from error
    with Database(db) as database:
        try:
            manifest = database.export_manifest(project_id)
        except KeyError as error:
            raise typer.BadParameter(f"project {project_id!r} not found") from error
        review = review or ProjectReviewPacket(project_id=project_id)
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
        listening_reviews = database.list(ListeningReviewRecord, project_id)
        listening_sessions = database.list(StoredListeningBundle, project_id)
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
        stored_lyrics = database.list(LyricAnalysis, project_id)
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
            raise typer.BadParameter(str(error)) from error
        with database.transaction():
            database.save(
                StoredAnalysisReport(
                    id=report.run.id,
                    project_id=project_id,
                    report=report,
                )
            )
    if json_out:
        _write_json(json_out, report)
    markdown = render_markdown(report)
    if markdown_out:
        markdown_out.parent.mkdir(parents=True, exist_ok=True)
        markdown_out.write_text(markdown, encoding="utf-8")
    if not json_out and not markdown_out:
        typer.echo(markdown)
    else:
        typer.echo(
            json.dumps(
                {
                    "run_id": report.run.id,
                    "recommendation_status": report.recommendation.status.value,
                    "json_out": str(json_out.resolve()) if json_out else None,
                    "markdown_out": str(markdown_out.resolve())
                    if markdown_out
                    else None,
                },
                ensure_ascii=False,
            )
        )


@app.command("locate-lyrics")
def locate_lyrics_command(
    project_id: Annotated[str, typer.Argument()],
    artifact_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    transcript: Annotated[
        Path | None,
        typer.Option(
            "--transcript",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Existing Whisper-compatible JSON",
        ),
    ] = None,
    provider: Annotated[
        str,
        typer.Option(
            "--provider",
            help="mlx-whisper, or use --transcript for JSON",
        ),
    ] = "mlx-whisper",
    model: Annotated[
        str,
        typer.Option("--model", help="Local path or Hugging Face MLX model"),
    ] = "mlx-community/whisper-small-mlx",
    language: Annotated[str | None, typer.Option("--language")] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Transcribe or import a transcript, then locate frozen lyric lines."""
    with Database(db) as database:
        artifact = database.require(ReleaseArtifact, artifact_id)
        if artifact.project_id != project_id:
            raise typer.BadParameter("artifact does not belong to project")
        if not artifact.local_path or not Path(artifact.local_path).exists():
            raise typer.BadParameter("artifact has no available local audio")
        take = database.require(Take, artifact.take_id)
        event = database.require(GenerationEvent, take.generation_event_id)
        brief = database.require(CreativeBriefVersion, event.brief_id)
        if not brief.lyrics.strip():
            raise typer.BadParameter("Brief has no lyrics to locate")
        if transcript is not None:
            active_provider = JsonTranscriptProvider(transcript)
            provider_name = f"json:{transcript.name}"
        elif provider == "mlx-whisper":
            active_provider = MlxWhisperTranscriptProvider(
                model=model,
                language=language,
            )
            provider_name = f"mlx-whisper:{model}"
        else:
            raise typer.BadParameter(
                "unsupported provider; use mlx-whisper or --transcript"
            )
        try:
            analysis = analyze_lyrics(
                project_id=project_id,
                artifact_id=artifact.id,
                brief_id=brief.id,
                lyrics=brief.lyrics,
                audio_path=artifact.local_path,
                provider=active_provider,
                provider_name=provider_name,
            )
        except (RuntimeError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
        with database.transaction():
            database.save(analysis)
    if output is not None:
        _write_json(output, analysis)
    counts: dict[str, int] = {}
    for location in analysis.locations:
        counts[location.status] = counts.get(location.status, 0) + 1
    typer.echo(
        json.dumps(
            {
                "analysis_id": analysis.id,
                "artifact_id": artifact_id,
                "provider": analysis.provider,
                "line_status_counts": counts,
                "human_confirmation_required": True,
                "output": str(output.resolve()) if output is not None else None,
            },
            ensure_ascii=False,
        )
    )


@app.command("record-review")
def record_review_command(
    project_id: Annotated[str, typer.Argument()],
    review_path: Annotated[
        Path,
        typer.Option(
            "--review",
            exists=True,
            dir_okay=False,
            readable=True,
            help="ProjectReviewPacket JSON; the local web form can create this",
        ),
    ],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
) -> None:
    review = ProjectReviewPacket.model_validate_json(
        review_path.read_text(encoding="utf-8")
    )
    if review.project_id != project_id:
        raise typer.BadParameter("review project_id does not match")
    record = StoredProjectReview(
        project_id=project_id,
        source="cli",
        review_packet=review,
    )
    with Database(db) as database:
        database.require(ProjectRecord, project_id)
        with database.transaction():
            database.save(record)
    typer.echo(
        json.dumps(
            {
                "review_id": record.id,
                "project_id": project_id,
                "artifact_reviews": len(review.artifact_reviews),
            },
            ensure_ascii=False,
        )
    )


@app.command("confirm-lyric-defect")
def confirm_lyric_defect_command(
    project_id: Annotated[str, typer.Argument()],
    artifact_id: Annotated[str, typer.Argument()],
    line_index: Annotated[int, typer.Option("--line-index", min=0)],
    description: Annotated[str, typer.Option("--description")],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Confirm that a human heard the burden-bearing lyric defect",
        ),
    ] = False,
    analysis_id: Annotated[
        str | None,
        typer.Option("--analysis-id"),
    ] = None,
) -> None:
    """Create a lyric T1 only after explicit human confirmation."""
    if not confirm:
        raise typer.BadParameter("--confirm is required for a lyric T1")
    if len(description.strip()) < 3:
        raise typer.BadParameter("--description must explain the heard defect")
    with Database(db) as database:
        artifact = database.require(ReleaseArtifact, artifact_id)
        if artifact.project_id != project_id:
            raise typer.BadParameter("artifact does not belong to project")
        analyses = [
            item
            for item in database.list(LyricAnalysis, project_id)
            if item.artifact_id == artifact_id
            and (analysis_id is None or item.id == analysis_id)
        ]
        if not analyses:
            raise typer.BadParameter("lyric analysis not found for artifact")
        analysis = max(analyses, key=lambda item: item.created_at)
        location = next(
            (item for item in analysis.locations if item.line_index == line_index),
            None,
        )
        if location is None:
            raise typer.BadParameter("lyric line index not found")
        defect = confirm_burden_lyric_defect(
            location,
            project_id=project_id,
            artifact_id=artifact_id,
            brief_id=analysis.brief_id,
            human_confirmation=True,
            description=description.strip(),
        )
        with database.transaction():
            database.save(defect)
    typer.echo(defect.model_dump_json(indent=2))


@app.command("record-final-choice")
def record_final_choice_command(
    project_id: Annotated[str, typer.Argument()],
    artifact_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Explicitly record the user's final release choice",
        ),
    ] = False,
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Record a final human choice without rewriting the policy result."""
    if not confirm:
        raise typer.BadParameter("--confirm is required for a final choice")
    with Database(db) as database:
        reports = database.list(StoredAnalysisReport, project_id)
        if run_id is not None:
            reports = [item for item in reports if item.id == run_id]
        if not reports:
            raise typer.BadParameter("analysis report not found")
        stored = max(reports, key=lambda item: item.created_at)
        if artifact_id not in {item.artifact_id for item in stored.report.assessments}:
            raise typer.BadParameter(
                "final choice must be a candidate from the selected report"
            )
        recommendation = record_user_override(
            stored.report.recommendation,
            artifact_id=artifact_id,
            reason=reason,
        )
        record = StoredReleaseDecision(
            project_id=project_id,
            analysis_run_id=stored.id,
            recommendation=recommendation,
        )
        with database.transaction():
            database.save(record)
    typer.echo(record.model_dump_json(indent=2))


@app.command("report")
def report_command(
    run_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    with Database(db) as database:
        stored = database.require(StoredAnalysisReport, run_id)
    markdown = render_markdown(stored.report)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
        typer.echo(str(output.resolve()))
    else:
        typer.echo(markdown)


@app.command("blind-session")
def blind_session_command(
    run_id: Annotated[str, typer.Argument()],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            help="Offline export directory for opaque listening clips",
        ),
    ],
    media_dir: Annotated[
        Path | None,
        typer.Option(
            "--media-dir",
            help=(
                "Persistent review-media root; use the same value passed to serve "
                "(default: DB sibling song-eval-review-media)"
            ),
        ),
    ] = None,
) -> None:
    with Database(db) as database:
        stored = database.require(StoredAnalysisReport, run_id)
        manifest = database.export_manifest(stored.project_id)
        paths = {
            artifact.id: artifact.local_path
            for artifact in manifest.artifacts
            if artifact.local_path
        }
        try:
            bundle = build_blind_session(
                project_id=stored.project_id,
                comparisons=stored.report.comparisons,
                artifact_paths=paths,
            )
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        review_media_root = (
            media_dir.resolve()
            if media_dir is not None
            else db.resolve().parent / "song-eval-review-media"
        )
        service_media_dir = review_media_root / "listening" / bundle.session.id
        generated = materialize_blind_media(bundle, service_media_dir)
        export_media_dir = (output_dir / "media").resolve()
        if export_media_dir != service_media_dir.resolve():
            export_media_dir.mkdir(parents=True, exist_ok=True)
            for sample_id, source in generated.items():
                shutil.copy2(source, export_media_dir / f"{sample_id}.wav")
        with database.transaction():
            database.save(bundle.to_record(run_id=run_id, media_files=generated))
        payload = bundle.public_payload()
        for trial in payload["trials"]:
            for side in ("left", "right"):
                sample_id = trial[side]["sample_id"]
                trial[side]["media_url"] = f"media/{sample_id}.wav"
        payload["response_schema"] = {
            "outcome": ["a", "b", "tie", "n/a"],
            "reason_tags": [
                "warmth_fullness",
                "hook_catchiness",
                "vocal_timbre_identity",
                "arrangement_harmony_development",
                "lyric_delivery",
                "ending_completeness",
                "overall_preference",
            ],
        }
        _write_json(output_dir / "session.json", payload)
    typer.echo(
        json.dumps(
            {
                "session_id": bundle.session.id,
                "session_json": str((output_dir / "session.json").resolve()),
                "review_url": f"/listening/{bundle.session.id}",
            },
            ensure_ascii=False,
        )
    )


@app.command("review-listening")
def review_listening_command(
    session_id: Annotated[str, typer.Argument()],
    responses_path: Annotated[
        Path,
        typer.Option(
            "--responses",
            exists=True,
            dir_okay=False,
            readable=True,
            help="JSON list, or object containing a responses list",
        ),
    ],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    try:
        raw = json.loads(responses_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise typer.BadParameter(f"responses JSON is not valid: {error}") from error
    values = raw.get("responses") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        raise typer.BadParameter("responses JSON must contain a list")
    try:
        responses = tuple(ListeningResponse.model_validate(item) for item in values)
    except ValidationError as error:
        raise typer.BadParameter(f"responses JSON is not valid: {error}") from error
    with Database(db) as database:
        stored_bundle = database.require(StoredListeningBundle, session_id)
        existing_reviews = [
            item
            for item in database.list(
                ListeningReviewRecord,
                stored_bundle.project_id,
            )
            if item.session_id == session_id and item.validation.valid
        ]
        if existing_reviews:
            raise typer.BadParameter(
                "listening session already has a valid review; "
                "create a new session to review again"
            )
        bundle = BlindBundle.from_record(stored_bundle)
        validation, review_packet = build_listening_review(bundle, responses)
        record = ListeningReviewRecord(
            project_id=stored_bundle.project_id,
            session_id=session_id,
            responses=responses,
            validation=validation,
            review_packet=review_packet,
        )
        with database.transaction():
            database.save(record)
    if output is not None:
        _write_json(output, record)
    typer.echo(
        json.dumps(
            {
                "review_id": record.id,
                "session_id": session_id,
                "valid": validation.valid,
                "failures": list(validation.failures),
                "artifact_reviews": len(review_packet.artifact_reviews),
                "output": str(output.resolve()) if output is not None else None,
            },
            ensure_ascii=False,
        )
    )
    if not validation.valid:
        raise typer.Exit(code=2)


@app.command("verify-crop")
def verify_crop_command(
    parent: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    child: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    parent_id: Annotated[str, typer.Option("--parent-id")] = "parent",
    child_id: Annotated[str, typer.Option("--child-id")] = "child",
) -> None:
    try:
        result = verify_deterministic_crop(
            parent,
            child,
            parent_artifact_id=parent_id,
            child_artifact_id=child_id,
            analysis_run_id="cli_verify_crop",
        )
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(result.model_dump_json(indent=2))
    if not result.verified:
        raise typer.Exit(code=2)


@app.command("plan-gesture")
def plan_gesture_command(
    project_id: Annotated[str, typer.Argument()],
    directive_id: Annotated[str, typer.Option("--directive-id")],
    target_artifact_id: Annotated[str, typer.Option("--target-artifact-id")],
    prompt: Annotated[str, typer.Option("--prompt")],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    lyrics_excerpt: Annotated[
        str | None,
        typer.Option("--lyrics-excerpt"),
    ] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    with Database(db) as database:
        directive = database.require(PreservationDirective, directive_id)
        artifact = database.require(ReleaseArtifact, target_artifact_id)
        project_artifacts = database.list(ReleaseArtifact, project_id)
    if directive.project_id != project_id or artifact.project_id != project_id:
        raise typer.BadParameter("directive/target does not belong to project")
    registered_target_id = registered_directive_target_id(
        directive,
        project_artifacts,
    )
    if registered_target_id is not None and registered_target_id != artifact.id:
        raise typer.BadParameter(
            "directive was registered for a different target artifact"
        )
    plan = plan_structural_gesture_replace(
        directive=directive,
        target_artifact=artifact,
        prompt=prompt,
        frozen_lyrics_excerpt=lyrics_excerpt,
    )
    if output:
        _write_json(output, plan)
        typer.echo(str(output.resolve()))
    else:
        typer.echo(plan.model_dump_json(indent=2))


@app.command("register-reference")
def register_reference_command(
    project_id: Annotated[str, typer.Argument()],
    target_artifact_id: Annotated[str, typer.Argument()],
    reference_audio: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    intent: Annotated[
        PreservationIntent,
        typer.Option("--intent"),
    ] = PreservationIntent.STRUCTURAL_GESTURE,
    start_s: Annotated[float, typer.Option("--start", min=0.0)] = 0.0,
    end_s: Annotated[float | None, typer.Option("--end", min=0.0)] = None,
    media_dir: Annotated[
        Path | None,
        typer.Option("--media-dir", help="Stable reference audio cache"),
    ] = None,
) -> None:
    """Register a reference as evidence; never attach it to a Suno generation."""
    with Database(db) as database:
        manifest = database.export_manifest(project_id)
        try:
            registration = register_local_reference(
                manifest,
                target_artifact_id=target_artifact_id,
                reference_path=reference_audio.expanduser().resolve(),
                media_dir=(
                    media_dir
                    if media_dir is not None
                    else db.resolve().parent
                    / "song-eval-media"
                    / project_key(project_id)
                ),
                intent=intent,
                start_s=start_s,
                end_s=end_s,
            )
        except (OSError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
        records = [
            registration.event,
            registration.take,
            registration.artifact,
            registration.reference,
            registration.directive,
        ]
        with database.transaction():
            for record in records:
                if record is not None and database.get(type(record), record.id) is None:
                    database.save(record)
    typer.echo(
        json.dumps(
            {
                "project_id": project_id,
                "reference_artifact_id": registration.artifact.id,
                "reference_segment_id": registration.reference.id,
                "directive_id": registration.directive.id,
                "intent": intent.value,
                "reference_attached_to_generation": False,
            },
            ensure_ascii=False,
        )
    )


@app.command("plan-suno")
def plan_suno_command(
    project_id: Annotated[str, typer.Argument()],
    directive_id: Annotated[str, typer.Option("--directive-id")],
    target_artifact_id: Annotated[str, typer.Option("--target-artifact-id")],
    prompt: Annotated[str, typer.Option("--prompt")],
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    lyrics_excerpt: Annotated[
        str | None,
        typer.Option("--lyrics-excerpt"),
    ] = None,
    subscription_tier: Annotated[
        str,
        typer.Option("--subscription-tier"),
    ] = "pro",
    studio_available: Annotated[
        bool,
        typer.Option("--studio/--no-studio"),
    ] = False,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Create a capability-aware plan without operating Suno or using credits."""
    with Database(db) as database:
        directive = database.require(PreservationDirective, directive_id)
        artifact = database.require(ReleaseArtifact, target_artifact_id)
        project_artifacts = database.list(ReleaseArtifact, project_id)
    if directive.project_id != project_id or artifact.project_id != project_id:
        raise typer.BadParameter("directive/target does not belong to project")
    registered_target_id = registered_directive_target_id(
        directive,
        project_artifacts,
    )
    if registered_target_id is not None and registered_target_id != artifact.id:
        raise typer.BadParameter(
            "directive was registered for a different target artifact"
        )
    recommendation = recommend_suno_workflow(
        directive=directive,
        target_artifact=artifact,
        prompt=prompt,
        frozen_lyrics_excerpt=lyrics_excerpt,
        subscription_tier=subscription_tier,
        studio_available=studio_available,
    )
    if output is not None:
        _write_json(output, recommendation)
    typer.echo(recommendation.model_dump_json(indent=2))


@app.command("serve")
def serve(
    db: Annotated[Path, typer.Option("--db", help="SQLite database path")],
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8765,
    media_dir: Annotated[
        Path | None,
        typer.Option("--media-dir", help="Persistent review media directory"),
    ] = None,
    library_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--library-root",
            help="Trusted local audio root; repeat for multiple libraries",
        ),
    ] = None,
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help="Permit a non-loopback bind; requires auth and an allowed host",
        ),
    ] = False,
    allowed_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-host",
            help="Exact trusted Host header; repeat for multiple hosts",
        ),
    ] = None,
    auth_username: Annotated[
        str | None,
        typer.Option(
            "--auth-username",
            help="Single administrator username (or SONG_EVAL_AUTH_USERNAME)",
        ),
    ] = None,
    auth_password_file: Annotated[
        Path | None,
        typer.Option(
            "--auth-password-file",
            help=(
                "Single-line password file "
                "(or SONG_EVAL_AUTH_PASSWORD_FILE; never pass the password directly)"
            ),
        ),
    ] = None,
) -> None:
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    remote_binding = host not in local_hosts
    trusted_hosts = _validate_allowed_hosts(allowed_host)
    resolved_username = auth_username or os.environ.get("SONG_EVAL_AUTH_USERNAME")
    password_file_value = auth_password_file
    if password_file_value is None:
        configured_password_file = os.environ.get("SONG_EVAL_AUTH_PASSWORD_FILE")
        if configured_password_file:
            password_file_value = Path(configured_password_file)

    if remote_binding and not allow_remote:
        raise typer.BadParameter(
            "serve is local-only by default; pass --allow-remote for an explicit "
            "authenticated deployment"
        )
    if remote_binding and not trusted_hosts:
        raise typer.BadParameter(
            "remote deployment requires at least one exact --allowed-host"
        )
    if remote_binding and (not resolved_username or password_file_value is None):
        raise typer.BadParameter(
            "remote deployment requires --auth-username and --auth-password-file"
        )
    if (resolved_username is None) != (password_file_value is None):
        raise typer.BadParameter(
            "auth username and password file must be configured together"
        )
    auth_password = (
        _read_secret_file(password_file_value, label="auth password file")
        if password_file_value is not None
        else None
    )
    if remote_binding and auth_password is not None and len(auth_password) < 16:
        raise typer.BadParameter(
            "remote deployment auth password must contain at least 16 characters"
        )
    uvicorn.run(
        create_app(
            db,
            media_dir=(
                media_dir
                if media_dir is not None
                else db.resolve().parent / "song-eval-review-media"
            ),
            library_roots=(
                db.resolve().parent / "song-eval-media",
                *(library_root or ()),
            ),
            extra_allowed_hosts=trusted_hosts,
            auth_username=resolved_username,
            auth_password=auth_password,
        ),
        host=host,
        port=port,
    )


if __name__ == "__main__":
    app()
