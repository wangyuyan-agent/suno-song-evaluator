from __future__ import annotations

import json
from pathlib import Path

from click import unstyle
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from songeval.api import create_app
from songeval.cli import app
from songeval.db import Database
from songeval.models import (
    Defect,
    ProjectRecord,
    ReleaseArtifact,
    StoredAnalysisReport,
    StoredListeningBundle,
    StoredReleaseDecision,
)

runner = CliRunner()


def test_cli_manifest_to_analysis(tmp_path, minimal_manifest):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        minimal_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    db_path = tmp_path / "cli.sqlite"
    imported = runner.invoke(
        app,
        ["import-manifest", str(manifest_path), "--db", str(db_path)],
    )
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.output)["artifacts"] == 1

    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"
    analyzed = runner.invoke(
        app,
        [
            "analyze",
            "project_test",
            "--db",
            str(db_path),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ],
    )
    assert analyzed.exit_code == 0, analyzed.output
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["recommendation"]["status"] == "abstain"
    assert markdown_out.exists()


def test_cli_help_exposes_required_workflows():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "intake",
        "configure-policy",
        "import-manifest",
        "fetch-suno",
        "analyze",
        "blind-session",
        "review-listening",
        "locate-lyrics",
        "confirm-lyric-defect",
        "record-review",
        "record-final-choice",
        "register-reference",
        "plan-suno",
        "verify-crop",
        "serve",
    ):
        assert command in result.output


def test_cli_local_intake_creates_project_without_manifest(
    tmp_path,
    tone_a,
    tone_b,
):
    db_path = tmp_path / "intake.sqlite"
    media_dir = tmp_path / "stable-media"
    result = runner.invoke(
        app,
        [
            "intake",
            "local-intake",
            "--title",
            "Local intake",
            "--db",
            str(db_path),
            "--media-dir",
            str(media_dir),
            "--audio",
            str(tone_a),
            "--audio",
            str(tone_b),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["project_id"] == "local-intake"
    assert payload["candidates"] == 2
    assert len(list(media_dir.glob("*.wav"))) == 2
    initial = payload["initial_analysis"]
    assert initial["run_id"]
    assert initial["recommendation_status"] == "abstain"
    assert Path(initial["json_report"]).exists()
    assert Path(initial["markdown_report"]).exists()

    analyzed = runner.invoke(
        app,
        ["analyze", "local-intake", "--db", str(db_path)],
    )
    assert analyzed.exit_code == 0, analyzed.output
    assert "Pairwise difference hotspots" in analyzed.output
    with Database(db_path) as database:
        run_id = database.list(StoredAnalysisReport, "local-intake")[-1].id
    blind = runner.invoke(
        app,
        [
            "blind-session",
            run_id,
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "blind-session"),
        ],
    )
    assert blind.exit_code == 0, blind.output
    blind_payload = json.loads(blind.output)
    assert blind_payload["review_url"].startswith("/listening/")
    exported_media = list((tmp_path / "blind-session" / "media").glob("*.wav"))
    assert exported_media
    with Database(db_path) as database:
        stored_bundle = database.require(
            StoredListeningBundle,
            blind_payload["session_id"],
        )
    trusted_root = (tmp_path / "song-eval-review-media").resolve()
    assert all(
        Path(path).resolve().is_relative_to(trusted_root)
        for path in stored_bundle.media_files.values()
    )
    service = create_app(
        db_path,
        media_dir=tmp_path / "song-eval-review-media",
        extra_allowed_hosts=("testserver",),
    )
    sample_id = next(iter(stored_bundle.media_files))
    with TestClient(service) as client:
        response = client.get(f"/listening-media/{stored_bundle.id}/{sample_id}")
        assert response.status_code == 200

    custom_media_root = tmp_path / "custom-review-media"
    custom_blind = runner.invoke(
        app,
        [
            "blind-session",
            run_id,
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "custom-blind-export"),
            "--media-dir",
            str(custom_media_root),
        ],
    )
    assert custom_blind.exit_code == 0, custom_blind.output
    custom_payload = json.loads(custom_blind.output)
    assert list((tmp_path / "custom-blind-export" / "media").glob("*.wav"))
    with Database(db_path) as database:
        custom_bundle = database.require(
            StoredListeningBundle,
            custom_payload["session_id"],
        )
    assert all(
        Path(path).resolve().is_relative_to(custom_media_root.resolve())
        for path in custom_bundle.media_files.values()
    )
    custom_sample_id = next(iter(custom_bundle.media_files))
    custom_service = create_app(
        db_path,
        media_dir=custom_media_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(custom_service) as client:
        response = client.get(f"/listening-media/{custom_bundle.id}/{custom_sample_id}")
        assert response.status_code == 200

    repeated = runner.invoke(
        app,
        [
            "intake",
            "local-intake",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
        ],
    )
    assert repeated.exit_code != 0
    assert "already exists" in repeated.output
    assert "analyze" in repeated.output
    assert "resume analysis" in repeated.output


def test_cli_intake_reports_initial_analysis_failure_without_traceback(
    tmp_path,
    tone_a,
):
    import numpy as np
    import soundfile as sf

    empty = tmp_path / "empty.wav"
    sf.write(empty, np.zeros((0, 2), dtype=np.float32), 16_000)
    db_path = tmp_path / "intake-error.sqlite"
    result = runner.invoke(
        app,
        [
            "intake",
            "intake-error",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
            "--audio",
            str(empty),
        ],
    )

    assert result.exit_code != 0
    assert "project imported, but initial analysis failed" in result.output
    assert "Traceback" not in result.output
    with Database(db_path) as database:
        assert database.get(ProjectRecord, "intake-error") is not None


def test_cli_analyze_unknown_project_and_invalid_review_are_readable(tmp_path):
    db_path = tmp_path / "analysis-errors.sqlite"
    missing = runner.invoke(
        app,
        ["analyze", "missing", "--db", str(db_path)],
    )
    assert missing.exit_code != 0
    assert "project 'missing' not found" in missing.output
    assert "Traceback" not in missing.output

    invalid_review = tmp_path / "review.json"
    invalid_review.write_text("{not-json", encoding="utf-8")
    invalid = runner.invoke(
        app,
        [
            "analyze",
            "missing",
            "--db",
            str(db_path),
            "--review",
            str(invalid_review),
        ],
    )
    assert invalid.exit_code != 0
    assert "review JSON is not valid" in invalid.output
    assert "Traceback" not in invalid.output


def test_cli_review_listening_rejects_malformed_response_json(tmp_path):
    responses = tmp_path / "responses.json"
    responses.write_text("{not-json", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "review-listening",
            "missing-session",
            "--responses",
            str(responses),
            "--db",
            str(tmp_path / "responses.sqlite"),
        ],
    )
    assert result.exit_code != 0
    assert "responses JSON is not valid" in result.output
    assert "Traceback" not in result.output


def test_cli_blind_session_reports_no_comparable_candidates_cleanly(
    tmp_path,
    tone_a,
):
    db_path = tmp_path / "single.sqlite"
    intake = runner.invoke(
        app,
        [
            "intake",
            "single-candidate",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
        ],
    )
    assert intake.exit_code == 0, intake.output
    analyzed = runner.invoke(
        app,
        [
            "analyze",
            "single-candidate",
            "--db",
            str(db_path),
            "--json-out",
            str(tmp_path / "single.json"),
            "--markdown-out",
            str(tmp_path / "single.md"),
        ],
    )
    assert analyzed.exit_code == 0, analyzed.output
    with Database(db_path) as database:
        run_id = database.list(StoredAnalysisReport, "single-candidate")[-1].id
    result = runner.invoke(
        app,
        [
            "blind-session",
            run_id,
            "--db",
            str(db_path),
            "--output-dir",
            str(tmp_path / "blind"),
        ],
    )
    assert result.exit_code != 0
    assert "requires at least two candidates" in result.output
    assert "Traceback" not in result.output


def test_cli_locate_lyrics_persists_json_locator_evidence(tmp_path, tone_a):
    db_path = tmp_path / "lyrics.sqlite"
    lyrics_path = tmp_path / "lyrics.txt"
    lyrics_path.write_text("line one\nline two", encoding="utf-8")
    intake = runner.invoke(
        app,
        [
            "intake",
            "lyrics-project",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
            "--lyrics-file",
            str(lyrics_path),
        ],
    )
    assert intake.exit_code == 0, intake.output
    with Database(db_path) as database:
        artifact_id = database.list(ReleaseArtifact, "lyrics-project")[0].id
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 1, "end": 2, "text": "line one", "confidence": 0.9},
                    {"start": 2, "end": 3, "text": "line two", "confidence": 0.9},
                ]
            }
        ),
        encoding="utf-8",
    )
    located = runner.invoke(
        app,
        [
            "locate-lyrics",
            "lyrics-project",
            artifact_id,
            "--db",
            str(db_path),
            "--transcript",
            str(transcript_path),
        ],
    )
    assert located.exit_code == 0, located.output
    assert json.loads(located.output)["line_status_counts"] == {"located": 2}

    refused_defect = runner.invoke(
        app,
        [
            "confirm-lyric-defect",
            "lyrics-project",
            artifact_id,
            "--db",
            str(db_path),
            "--line-index",
            "0",
            "--description",
            "human heard the lyric change",
        ],
    )
    assert refused_defect.exit_code != 0
    confirmed_defect = runner.invoke(
        app,
        [
            "confirm-lyric-defect",
            "lyrics-project",
            artifact_id,
            "--db",
            str(db_path),
            "--line-index",
            "0",
            "--description",
            "human heard the lyric change",
            "--confirm",
        ],
    )
    assert confirmed_defect.exit_code == 0, confirmed_defect.output
    with Database(db_path) as database:
        assert database.list(Defect, "lyrics-project")[0].tier.value == "T1"

    analyzed = runner.invoke(
        app,
        ["analyze", "lyrics-project", "--db", str(db_path)],
    )
    assert analyzed.exit_code == 0, analyzed.output
    assert "Lyric localization" in analyzed.output
    assert "human confirmation required" in analyzed.output


def test_cli_policy_requires_explicit_confirmation(tmp_path, tone_a):
    db_path = tmp_path / "policy.sqlite"
    intake = runner.invoke(
        app,
        [
            "intake",
            "policy-project",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
        ],
    )
    assert intake.exit_code == 0, intake.output
    refused = runner.invoke(
        app,
        ["configure-policy", "policy-project", "--db", str(db_path)],
    )
    assert refused.exit_code != 0
    confirmed = runner.invoke(
        app,
        [
            "configure-policy",
            "policy-project",
            "--db",
            str(db_path),
            "--confirm",
        ],
    )
    assert confirmed.exit_code == 0, confirmed.output
    assert json.loads(confirmed.output)["declared_by_user"] is True


def test_cli_serve_rejects_remote_binding_without_explicit_security(tmp_path):
    result = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--host",
            "0.0.0.0",
        ],
    )
    assert result.exit_code != 0
    assert "local-only by default" in result.output


def test_cli_serve_remote_requires_exact_host_and_auth(tmp_path):
    missing_host = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--host",
            "0.0.0.0",
            "--allow-remote",
        ],
    )
    wildcard = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--host",
            "0.0.0.0",
            "--allow-remote",
            "--allowed-host",
            "*",
        ],
    )
    missing_auth = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--host",
            "0.0.0.0",
            "--allow-remote",
            "--allowed-host",
            "evaluator.example.com",
        ],
    )

    assert missing_host.exit_code != 0
    assert "exact --allowed-host" in unstyle(missing_host.output)
    assert wildcard.exit_code != 0
    assert "exact hostnames" in unstyle(wildcard.output)
    assert missing_auth.exit_code != 0
    assert "requires --auth-username" in unstyle(missing_auth.output)


def test_cli_serve_remote_uses_password_file_without_exposing_secret(
    tmp_path,
    monkeypatch,
):
    password_file = tmp_path / "admin-password"
    password_file.write_text("integration-server-secret\n", encoding="utf-8")
    captured = {}

    def capture_run(application, *, host, port):
        captured["application"] = application
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("songeval.cli.uvicorn.run", capture_run)
    result = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--host",
            "0.0.0.0",
            "--port",
            "9876",
            "--allow-remote",
            "--allowed-host",
            "evaluator.example.com",
            "--auth-username",
            "reviewer",
            "--auth-password-file",
            str(password_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "integration-server-secret" not in result.output
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9876
    assert captured["application"].state.auth_enabled is True


def test_cli_serve_remote_rejects_a_weak_password_file(tmp_path):
    password_file = tmp_path / "weak-password"
    password_file.write_text("too-short\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--host",
            "0.0.0.0",
            "--allow-remote",
            "--allowed-host",
            "evaluator.example.com",
            "--auth-username",
            "reviewer",
            "--auth-password-file",
            str(password_file),
        ],
    )

    assert result.exit_code != 0
    assert "at least 16" in result.output
    assert "characters" in result.output


def test_cli_serve_rejects_partial_auth_and_invalid_secret_file(tmp_path):
    missing_file = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--auth-username",
            "reviewer",
        ],
    )
    multiline = tmp_path / "multiline-password"
    multiline.write_text("first\nsecond\n", encoding="utf-8")
    invalid_file = runner.invoke(
        app,
        [
            "serve",
            "--db",
            str(tmp_path / "serve.sqlite"),
            "--auth-username",
            "reviewer",
            "--auth-password-file",
            str(multiline),
        ],
    )

    assert missing_file.exit_code != 0
    assert "configured together" in missing_file.output
    assert invalid_file.exit_code != 0
    assert "exactly one" in invalid_file.output


def test_cli_records_final_choice_without_rewriting_analysis(tmp_path, tone_a):
    db_path = tmp_path / "choice.sqlite"
    intake = runner.invoke(
        app,
        [
            "intake",
            "choice-project",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
        ],
    )
    assert intake.exit_code == 0, intake.output
    with Database(db_path) as database:
        artifact_id = database.list(ReleaseArtifact, "choice-project")[0].id
    refused = runner.invoke(
        app,
        [
            "record-final-choice",
            "choice-project",
            artifact_id,
            "--db",
            str(db_path),
        ],
    )
    assert refused.exit_code != 0
    recorded = runner.invoke(
        app,
        [
            "record-final-choice",
            "choice-project",
            artifact_id,
            "--db",
            str(db_path),
            "--reason",
            "human preference",
            "--confirm",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    assert (
        json.loads(recorded.output)["recommendation"]["user_final_choice"]
        == artifact_id
    )
    with Database(db_path) as database:
        assert len(database.list(StoredReleaseDecision, "choice-project")) == 1


def test_cli_registers_reference_and_emits_non_studio_pro_plan(
    tmp_path,
    tone_a,
    tone_b,
    monkeypatch,
):
    db_path = tmp_path / "reference.sqlite"
    intake = runner.invoke(
        app,
        [
            "intake",
            "reference-project",
            "--db",
            str(db_path),
            "--audio",
            str(tone_a),
        ],
    )
    assert intake.exit_code == 0, intake.output
    with Database(db_path) as database:
        target_id = database.list(ReleaseArtifact, "reference-project")[0].id
    invalid_audio = tmp_path / "not-audio.txt"
    invalid_audio.write_text("not audio", encoding="utf-8")
    invalid = runner.invoke(
        app,
        [
            "register-reference",
            "reference-project",
            target_id,
            str(invalid_audio),
            "--db",
            str(db_path),
        ],
    )
    assert invalid.exit_code != 0
    assert "decodable audio" in invalid.output
    registered = runner.invoke(
        app,
        [
            "register-reference",
            "reference-project",
            target_id,
            str(tone_b),
            "--db",
            str(db_path),
            "--intent",
            "structural_gesture",
            "--start",
            "1",
            "--end",
            "5",
        ],
    )
    assert registered.exit_code == 0, registered.output
    monkeypatch.chdir(tone_b.parent)
    relative = runner.invoke(
        app,
        [
            "register-reference",
            "reference-project",
            target_id,
            tone_b.name,
            "--db",
            str(db_path),
            "--intent",
            "structural_gesture",
            "--start",
            "1",
            "--end",
            "5",
        ],
    )
    assert relative.exit_code == 0, relative.output
    directive_id = json.loads(registered.output)["directive_id"]
    planned = runner.invoke(
        app,
        [
            "plan-suno",
            "reference-project",
            "--directive-id",
            directive_id,
            "--target-artifact-id",
            target_id,
            "--prompt",
            "one-beat breath, immediate chorus",
            "--db",
            str(db_path),
            "--no-studio",
        ],
    )
    assert planned.exit_code == 0, planned.output
    recommendation = json.loads(planned.output)
    assert recommendation["status"] == "actionable"
    assert recommendation["plan"]["workflow_surface"] == "song_editor"
    assert recommendation["plan"]["studio_available"] is False
