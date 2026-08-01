from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest
from fastapi.testclient import TestClient

import songeval.api as api_module
from songeval.api import (
    LyricDefectConfirmationRequest,
    _basic_credentials,
    _configured_secret,
)
from songeval.api import (
    create_app as production_create_app,
)
from songeval.db import Database
from songeval.models import (
    ListeningReviewRecord,
    ListeningSession,
    ListeningValidation,
    LyricAnalysis,
    LyricLineLocation,
    ProjectRecord,
    ReleaseArtifact,
    StoredAnalysisReport,
    StoredListeningBundle,
    Take,
)
from songeval.util import project_key


def create_app(*args, **kwargs):
    if "library_roots" not in kwargs:
        db_path = Path(args[0] if args else kwargs["db_path"])
        kwargs["library_roots"] = (db_path.expanduser().resolve().parent,)
    return production_create_app(
        *args,
        extra_allowed_hosts=("testserver",),
        **kwargs,
    )


def test_body_limit_does_not_replace_an_already_started_response():
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"too large", "more_body": False}

    async def send(message):
        sent.append(message)

    async def downstream(_scope, limited_receive, tracked_send):
        await tracked_send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        try:
            await limited_receive()
        except api_module.RequestBodyTooLarge:
            await tracked_send({"type": "http.response.body", "body": b""})

    middleware = api_module.RequestBodyLimitMiddleware(
        downstream,
        default_limit_bytes=4,
        upload_limit_bytes=4,
    )
    asyncio.run(
        middleware(
            {"type": "http", "path": "/stream", "headers": []},
            receive,
            send,
        )
    )

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[0]["status"] == 200


def test_production_app_does_not_trust_testserver_by_default(tmp_path):
    app = production_create_app(
        tmp_path / "production-host.sqlite",
        media_dir=tmp_path / "media",
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 400


def test_production_app_basic_auth_protects_everything_except_health(tmp_path):
    app = production_create_app(
        tmp_path / "authenticated.sqlite",
        media_dir=tmp_path / "media",
        extra_allowed_hosts=("testserver",),
        auth_username="reviewer",
        auth_password="correct horse battery staple",
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        anonymous = client.get("/projects")
        wrong = client.get("/projects", auth=("reviewer", "wrong"))
        authenticated = client.get(
            "/projects",
            auth=("reviewer", "correct horse battery staple"),
        )

        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"].startswith("Basic ")
        assert anonymous.headers["cache-control"] == "no-store"
        assert wrong.status_code == 401
        assert authenticated.status_code == 200


def test_basic_auth_parser_rejects_malformed_headers():
    assert _basic_credentials(None) is None
    assert _basic_credentials("Bearer token") is None
    assert _basic_credentials("Basic !!!") is None
    assert _basic_credentials("Basic dXNlcm5hbWU=") is None
    assert _basic_credentials("Basic dXNlcjpwYXNz") == (b"user", b"pass")


def test_configured_secret_rejects_ambiguous_or_invalid_files(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TEST_SECRET", "direct-secret")
    monkeypatch.delenv("TEST_SECRET_FILE", raising=False)
    assert (
        _configured_secret(
            value_env="TEST_SECRET",
            file_env="TEST_SECRET_FILE",
        )
        == "direct-secret"
    )

    monkeypatch.setenv("TEST_SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="configure only one"):
        _configured_secret(
            value_env="TEST_SECRET",
            file_env="TEST_SECRET_FILE",
        )

    monkeypatch.delenv("TEST_SECRET")
    with pytest.raises(ValueError, match="unable to read"):
        _configured_secret(
            value_env="TEST_SECRET",
            file_env="TEST_SECRET_FILE",
        )

    multiline = tmp_path / "multiline"
    multiline.write_text("first\nsecond\n", encoding="utf-8")
    monkeypatch.setenv("TEST_SECRET_FILE", str(multiline))
    with pytest.raises(ValueError, match="exactly one"):
        _configured_secret(
            value_env="TEST_SECRET",
            file_env="TEST_SECRET_FILE",
        )


def test_production_app_rejects_partial_auth_and_wildcard_hosts(tmp_path):
    with pytest.raises(ValueError, match="configured together"):
        production_create_app(
            tmp_path / "partial-auth.sqlite",
            auth_username="reviewer",
        )
    with pytest.raises(ValueError, match="exact hostnames"):
        production_create_app(
            tmp_path / "wildcard-host.sqlite",
            extra_allowed_hosts=("*",),
        )
    with pytest.raises(ValueError, match="cannot contain"):
        production_create_app(
            tmp_path / "invalid-user.sqlite",
            auth_username="invalid:user",
            auth_password="strong-enough-password",
        )
    with pytest.raises(ValueError, match="single line"):
        production_create_app(
            tmp_path / "invalid-password.sqlite",
            auth_username="reviewer",
            auth_password="first\nsecond",
        )


def test_api_rejects_manifest_paths_outside_trusted_roots_and_symlink_escapes(
    tmp_path,
    minimal_manifest,
):
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = Path(minimal_manifest.artifacts[0].local_path)
    app = production_create_app(
        tmp_path / "trusted-paths.sqlite",
        media_dir=tmp_path / "review-media",
        library_roots=(trusted,),
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        outside_result = client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        escape = trusted / "escape.wav"
        escape.symlink_to(outside)
        escaped_manifest = minimal_manifest.model_copy(
            update={
                "artifacts": [
                    minimal_manifest.artifacts[0].model_copy(
                        update={"local_path": str(escape)}
                    )
                ]
            }
        )
        escaped_result = client.post(
            "/manifests/import",
            json=escaped_manifest.model_dump(mode="json"),
        )

        assert outside_result.status_code == 422
        assert "configured library root" in outside_result.json()["detail"]
        assert escaped_result.status_code == 422
        assert "configured library root" in escaped_result.json()["detail"]
        assert app.state.database.list_projects() == []


def test_api_does_not_serve_untrusted_paths_from_an_existing_database(
    tmp_path,
    minimal_manifest,
):
    db_path = tmp_path / "legacy-untrusted.sqlite"
    with Database(db_path) as database:
        database.import_manifest(minimal_manifest)
    app = production_create_app(
        db_path,
        media_dir=tmp_path / "review-media",
        extra_allowed_hosts=("testserver",),
    )
    session = ListeningSession(
        id="listen_untrusted_media",
        project_id="project_test",
        trials=(),
    )
    app.state.database.save(
        StoredListeningBundle(
            id=session.id,
            project_id="project_test",
            run_id="legacy_run",
            session=session,
            stimuli=(),
            trial_secrets=(),
            media_files={
                "legacy_sample": str(minimal_manifest.artifacts[0].local_path)
            },
        )
    )
    with TestClient(app) as client:
        artifact_response = client.get(
            "/projects/project_test/artifacts/artifact_test/audio"
        )
        listening_response = client.get(
            "/listening-media/listen_untrusted_media/legacy_sample"
        )

        assert artifact_response.status_code == 404
        assert artifact_response.json()["detail"] == "audio not found"
        assert listening_response.status_code == 404
        assert listening_response.json()["detail"] == "sample not found"


def test_api_import_analyze_report_and_narrative(tmp_path, minimal_manifest):
    app = create_app(tmp_path / "api.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        imported = client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["project_id"] == "project_test"

        analyzed = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        )
        assert analyzed.status_code == 200, analyzed.text
        payload = analyzed.json()
        assert payload["recommendation"]["status"] == "abstain"
        run_id = payload["run"]["id"]

        saved = client.get(f"/projects/project_test/reports/{run_id}")
        assert saved.status_code == 200
        assert saved.json()["run"]["id"] == run_id

        markdown = client.get(f"/projects/project_test/reports/{run_id}.md")
        assert markdown.status_code == 200
        assert "Release decision" in markdown.text

        narrative = client.post(
            "/projects/project_test/narratives",
            json={"run_id": run_id, "provider": "deterministic"},
        )
        assert narrative.status_code == 200
        assert narrative.json()["provider"] == "deterministic"

        refused_choice = client.post(
            "/projects/project_test/release-decisions",
            json={
                "artifact_id": "artifact_test",
                "run_id": run_id,
                "confirm": False,
            },
        )
        assert refused_choice.status_code == 422
        final_choice = client.post(
            "/projects/project_test/release-decisions",
            json={
                "artifact_id": "artifact_test",
                "run_id": run_id,
                "confirm": True,
                "reason": "human final choice",
            },
        )
        assert final_choice.status_code == 200, final_choice.text
        assert (
            final_choice.json()["recommendation"]["user_final_choice"]
            == "artifact_test"
        )
        assert payload["recommendation"]["user_final_choice"] is None
        choice_context = client.get("/projects/project_test/workspace-context").json()
        assert (
            choice_context["latest_release_decision"]["recommendation"][
                "user_final_choice"
            ]
            == "artifact_test"
        )


def test_api_turns_narrator_failures_into_actionable_validation_errors(
    tmp_path,
    minimal_manifest,
    monkeypatch,
):
    app = create_app(tmp_path / "narrative.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post("/manifests/import", json=minimal_manifest.model_dump(mode="json"))
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        monkeypatch.setenv("SONG_EVAL_LLM_API_KEY", "test")
        monkeypatch.setenv("SONG_EVAL_LLM_BASE_URL", "https://example.invalid/v1")
        monkeypatch.setenv("SONG_EVAL_LLM_MODEL", "test-model")

        def fail_narration(*_args, **_kwargs):
            raise ValueError("provider returned malformed output")

        monkeypatch.setattr(
            "songeval.api.OpenAICompatibleNarrator.narrate",
            fail_narration,
        )
        response = client.post(
            "/projects/project_test/narratives",
            json={
                "run_id": report["run"]["id"],
                "provider": "openai-compatible",
            },
        )

        assert response.status_code == 422
        assert "malformed output" in response.json()["detail"]


def test_api_never_sends_configured_llm_key_to_request_supplied_base_url(
    tmp_path,
    minimal_manifest,
    monkeypatch,
):
    app = create_app(tmp_path / "narrative-url.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post("/manifests/import", json=minimal_manifest.model_dump(mode="json"))
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        monkeypatch.setenv("SONG_EVAL_LLM_API_KEY", "secret")
        monkeypatch.setenv("SONG_EVAL_LLM_BASE_URL", "https://configured.invalid/v1")
        monkeypatch.setenv("SONG_EVAL_LLM_MODEL", "test-model")
        response = client.post(
            "/projects/project_test/narratives",
            json={
                "run_id": report["run"]["id"],
                "provider": "openai-compatible",
                "base_url": "https://attacker.invalid/v1",
            },
        )
        assert response.status_code == 422
        assert "must match SONG_EVAL_LLM_BASE_URL" in response.json()["detail"]


def test_api_can_read_llm_key_from_a_secret_file(
    tmp_path,
    minimal_manifest,
    monkeypatch,
):
    app = create_app(
        tmp_path / "narrative-secret-file.sqlite",
        media_dir=tmp_path / "media",
    )
    secret_file = tmp_path / "llm-key"
    secret_file.write_text("file-backed-secret\n", encoding="utf-8")
    captured = {}

    class FailingNarrator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def narrate(self, *_args, **_kwargs):
            raise ValueError("stop after secret capture")

    monkeypatch.delenv("SONG_EVAL_LLM_API_KEY", raising=False)
    monkeypatch.setenv("SONG_EVAL_LLM_API_KEY_FILE", str(secret_file))
    monkeypatch.setenv("SONG_EVAL_LLM_BASE_URL", "https://configured.invalid/v1")
    monkeypatch.setenv("SONG_EVAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(api_module, "OpenAICompatibleNarrator", FailingNarrator)

    with TestClient(app) as client:
        client.post("/manifests/import", json=minimal_manifest.model_dump(mode="json"))
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        response = client.post(
            "/projects/project_test/narratives",
            json={
                "run_id": report["run"]["id"],
                "provider": "openai-compatible",
            },
        )

    assert response.status_code == 422
    assert captured["api_key"] == "file-backed-secret"


def test_api_reports_missing_llm_config_before_request_url_mismatch(
    tmp_path,
    minimal_manifest,
    monkeypatch,
):
    app = create_app(
        tmp_path / "missing-narrative.sqlite",
        media_dir=tmp_path / "media",
    )
    with TestClient(app) as client:
        client.post("/manifests/import", json=minimal_manifest.model_dump(mode="json"))
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        monkeypatch.delenv("SONG_EVAL_LLM_API_KEY", raising=False)
        monkeypatch.delenv("SONG_EVAL_LLM_API_KEY_FILE", raising=False)
        monkeypatch.delenv("SONG_EVAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("SONG_EVAL_LLM_MODEL", raising=False)
        response = client.post(
            "/projects/project_test/narratives",
            json={
                "run_id": report["run"]["id"],
                "provider": "openai-compatible",
                "base_url": "https://request.invalid/v1",
            },
        )
        assert response.status_code == 422
        assert "requires base_url/model" in response.json()["detail"]


def test_workspace_endpoints_reject_dangling_manifest_references(
    tmp_path,
    minimal_manifest,
):
    app = create_app(tmp_path / "dangling.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post("/manifests/import", json=minimal_manifest.model_dump(mode="json"))
        database = app.state.database
        database.connection.execute(
            "DELETE FROM entities WHERE kind = ? AND id = ?",
            ("Take", minimal_manifest.takes[0].id),
        )
        database.connection.commit()

        for path in (
            "/projects/project_test/workspace-context",
            "/projects/project_test/review-context",
        ):
            response = client.get(path)
            assert response.status_code == 422
            assert "references missing take" in response.json()["detail"]


def test_api_lyric_defect_description_normalizes_whitespace():
    request = LyricDefectConfirmationRequest(
        artifact_id="artifact",
        line_index=0,
        description="  heard \n a   changed line  ",
        confirm=True,
    )
    assert request.description == "heard a changed line"


def test_api_policy_ids_are_readable_and_collision_resistant(tmp_path):
    app = create_app(tmp_path / "policy-slug.sqlite", media_dir=tmp_path / "media")
    app.state.database.save(ProjectRecord(id="Project With Space", title="Policy"))
    with TestClient(app) as client:
        response = client.post(
            "/projects/Project%20With%20Space/policy",
            json={"confirm": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == f"policy_{project_key('Project With Space')}_v1"


def test_api_missing_allowlisted_ui_asset_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr("songeval.api.WEB_ROOT", tmp_path)
    app = create_app(tmp_path / "missing-ui.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        response = client.get("/ui/icon.svg")
    assert response.status_code == 404


def test_production_ui_assets_and_workspace_context_are_data_bound(
    tmp_path,
    minimal_manifest,
):
    app = create_app(tmp_path / "ui.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "歌曲评估 · 本地项目" in index.text
        assert 'src="/ui/app.js"' in index.text
        assert 'href="/ui/app.css"' in index.text
        assert 'href="/ui/icon.svg"' in index.text

        css = client.get("/ui/app.css")
        javascript = client.get("/ui/app.js")
        icon = client.get("/ui/icon.svg")
        assert css.status_code == 200
        assert css.headers["content-type"].startswith("text/css")
        assert "Premium Minimalism" not in css.text
        assert javascript.status_code == 200
        assert javascript.headers["content-type"].startswith("text/javascript")
        assert "17-v9" not in javascript.text
        assert "4:34" not in javascript.text
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg+xml")

        assert (
            client.post(
                "/manifests/import",
                json=minimal_manifest.model_dump(mode="json"),
            ).status_code
            == 200
        )
        analyzed = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        )
        assert analyzed.status_code == 200

        workspace = client.get("/projects/project_test")
        assert workspace.status_code == 200
        assert "《" not in workspace.text
        assert '"project_id":"project_test"' in workspace.text

        context = client.get("/projects/project_test/workspace-context")
        assert context.status_code == 200
        payload = context.json()
        assert payload["project"] == {
            "id": "project_test",
            "title": "test",
        }
        assert payload["latest_report"]["run"]["id"] == analyzed.json()["run"]["id"]
        assert payload["candidates"][0]["artifact_id"] == "artifact_test"
        assert payload["candidates"][0]["measured_duration_s"] == 8.0
        assert payload["candidates"][0]["audio_url"].endswith("/artifact_test/audio")
        assert payload["candidates"][0]["requirements"][0]["id"] == "lyrics"

        private_config_path = str(tmp_path / "private-analysis-source.wav")
        report_record = app.state.database.require(
            StoredAnalysisReport,
            analyzed.json()["run"]["id"],
        )
        private_run = report_record.report.run.model_copy(
            update={
                "id": "run_with_private_path",
                "configuration": {
                    "source_path": private_config_path,
                    "media_files": {"private": private_config_path},
                    "feature_path_map": {"private": private_config_path},
                    "acquisition_path": "user_provided_unknown",
                },
            }
        )
        app.state.database.save(
            StoredAnalysisReport(
                id=private_run.id,
                project_id="project_test",
                report=report_record.report.model_copy(update={"run": private_run}),
            )
        )
        evidence = client.get("/projects/project_test/evidence.json")
        assert evidence.status_code == 200
        assert evidence.headers["cache-control"] == "no-store"
        assert "attachment;" in evidence.headers["content-disposition"]
        assert evidence.json()["latest_report"]["run"]["id"] == private_run.id
        assert str(minimal_manifest.artifacts[0].local_path) not in evidence.text
        assert private_config_path not in evidence.text
        assert (
            evidence.json()["latest_report"]["run"]["configuration"]["source_path"]
            == "<local-path-redacted>"
        )
        assert (
            evidence.json()["latest_report"]["run"]["configuration"]["media_files"]
            == "<local-path-redacted>"
        )
        assert (
            evidence.json()["latest_report"]["run"]["configuration"]["feature_path_map"]
            == "<local-path-redacted>"
        )
        assert (
            evidence.json()["latest_report"]["run"]["configuration"]["acquisition_path"]
            == "user_provided_unknown"
        )
        assert (
            evidence.json()["manifest"]["artifacts"][0]["local_path"]
            == "<local-path-redacted>"
        )

        untrusted = client.get("/", headers={"host": "malicious.example"})
        assert untrusted.status_code == 400


def test_api_rejects_review_for_other_project(tmp_path, minimal_manifest):
    app = create_app(tmp_path / "api.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        response = client.post(
            "/projects/project_test/analysis",
            json={"review": {"project_id": "other"}},
        )
        assert response.status_code == 422


def test_api_policy_and_unblinded_review_page_require_explicit_confirmation(
    tmp_path,
    minimal_manifest,
):
    app = create_app(tmp_path / "review.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        assert (
            client.post(
                "/manifests/import",
                json=minimal_manifest.model_dump(mode="json"),
            ).status_code
            == 200
        )
        client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        )
        context = client.get("/projects/project_test/review-context")
        assert context.status_code == 200
        assert context.json()["candidates"][0]["ending"] is not None
        page = client.get("/projects/project_test/review")
        assert page.status_code == 200
        assert "候选需求与技术复核" in page.text
        refused = client.post(
            "/projects/project_test/policy",
            json={"confirm": False},
        )
        assert refused.status_code == 422
        confirmed = client.post(
            "/projects/project_test/policy",
            json={"confirm": True, "max_na_ratio": 0.25},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["axis_priority"] == [
            "compliance",
            "craft",
            "release_readiness",
            "distinctiveness",
        ]
        review = client.post(
            "/projects/project_test/reviews",
            json={
                "project_id": "project_test",
                "artifact_reviews": [
                    {
                        "artifact_id": "artifact_test",
                        "requirement_observations": {
                            "lyrics": {
                                "criterion": "lyrics",
                                "value": 3,
                                "evidence": "human confirmation",
                            },
                            "style": {
                                "criterion": "style",
                                "value": 3,
                                "evidence": "human confirmation",
                            },
                        },
                        "technical_confirmations": {"ending_boundary": "pass"},
                    }
                ],
            },
        )
        assert review.status_code == 200, review.text


def test_api_requires_human_confirmation_for_lyric_t1(
    tmp_path,
    minimal_manifest,
):
    app = create_app(tmp_path / "lyrics.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        analysis = LyricAnalysis(
            id="lyrics_test",
            project_id="project_test",
            artifact_id="artifact_test",
            brief_id="brief_test",
            provider="test",
            transcript=(),
            locations=(
                LyricLineLocation(
                    line_index=0,
                    expected_text="line one",
                    status="possible_changed",
                    start_s=1,
                    end_s=2,
                    transcript_text="wrong line",
                ),
            ),
            transcript_sha256="test",
        )
        with app.state.database.transaction():
            app.state.database.save(analysis)
        refused = client.post(
            "/projects/project_test/lyric-defects",
            json={
                "artifact_id": "artifact_test",
                "line_index": 0,
                "description": "heard wrong lyric",
                "confirm": False,
                "analysis_id": analysis.id,
            },
        )
        confirmed = client.post(
            "/projects/project_test/lyric-defects",
            json={
                "artifact_id": "artifact_test",
                "line_index": 0,
                "description": "heard wrong lyric",
                "confirm": True,
                "analysis_id": analysis.id,
            },
        )

        assert refused.status_code == 422
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["tier"] == "T1"
        assert confirmed.json()["confirmed"]


def test_api_registers_reference_and_returns_pro_non_studio_plan(
    tmp_path,
    minimal_manifest,
    tone_b,
):
    app = create_app(tmp_path / "plan.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        registered = client.post(
            "/projects/project_test/references/register",
            json={
                "target_artifact_id": "artifact_test",
                "reference_path": str(tone_b),
                "intent": "structural_gesture",
                "start_s": 1,
                "end_s": 5,
            },
        )
        assert registered.status_code == 200, registered.text
        context = client.get("/projects/project_test/workspace-context")
        assert context.status_code == 200
        assert context.json()["directives"][0]["target_artifact_id"] == "artifact_test"
        assert context.json()["directives"][0]["resolved_target_id"] == "artifact_test"
        planned = client.post(
            "/projects/project_test/suno-plan",
            json={
                "directive_id": registered.json()["directive_id"],
                "target_artifact_id": "artifact_test",
                "prompt": "one-beat breath, immediate chorus",
                "subscription_tier": "pro",
                "studio_available": False,
            },
        )
        assert planned.status_code == 200, planned.text
        payload = planned.json()
        assert payload["status"] == "actionable"
        assert payload["plan"]["workflow_surface"] == "song_editor"
        assert payload["plan"]["source_rules"] == [
            "use_target_as_edit_parent",
            "do_not_attach_reference_as_sample",
        ]


def test_api_reference_registration_errors_are_actionable(
    tmp_path,
    minimal_manifest,
):
    text_file = tmp_path / "not-audio.txt"
    text_file.write_text("not audio", encoding="utf-8")
    app = create_app(tmp_path / "plan-errors.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        missing = client.post(
            "/projects/project_test/references/register",
            json={
                "target_artifact_id": "artifact_test",
                "reference_path": str(tmp_path / "missing.wav"),
            },
        )
        invalid = client.post(
            "/projects/project_test/references/register",
            json={
                "target_artifact_id": "artifact_test",
                "reference_path": str(text_file),
            },
        )
        unknown_project = client.post(
            "/projects/project_missing/references/register",
            json={
                "target_artifact_id": "artifact_test",
                "reference_path": str(text_file),
            },
        )

        assert missing.status_code == 422
        assert "does not exist" in missing.json()["detail"]
        assert invalid.status_code == 422
        assert "decodable audio" in invalid.json()["detail"]
        assert unknown_project.status_code == 404
        assert unknown_project.json()["detail"] == "project not found"


def test_api_rejects_directive_target_mismatch(
    tmp_path,
    minimal_manifest,
    tone_b,
):
    second_take = Take(
        id="take_second",
        project_id="project_test",
        generation_event_id="event_test",
    )
    second_artifact = ReleaseArtifact(
        id="artifact_second",
        project_id="project_test",
        take_id=second_take.id,
        local_path=str(tone_b),
    )
    manifest = minimal_manifest.model_copy(
        update={
            "takes": [*minimal_manifest.takes, second_take],
            "artifacts": [*minimal_manifest.artifacts, second_artifact],
        }
    )
    app = create_app(tmp_path / "target.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post("/manifests/import", json=manifest.model_dump(mode="json"))
        registered = client.post(
            "/projects/project_test/references/register",
            json={
                "target_artifact_id": "artifact_test",
                "reference_path": str(tone_b),
                "intent": "structural_gesture",
            },
        )
        planned = client.post(
            "/projects/project_test/suno-plan",
            json={
                "directive_id": registered.json()["directive_id"],
                "target_artifact_id": "artifact_second",
                "prompt": "one-beat breath",
            },
        )

        assert planned.status_code == 422
        assert "different target" in planned.json()["detail"]


def test_api_builds_opaque_blind_media(tmp_path, minimal_manifest, tone_b):
    second_take = Take(
        id="take_second",
        project_id="project_test",
        generation_event_id="event_test",
        batch_index=1,
    )
    second_artifact = ReleaseArtifact(
        id="artifact_second",
        project_id="project_test",
        take_id=second_take.id,
        local_path=str(tone_b),
        platform_reported_duration_s=8.0,
    )
    manifest = minimal_manifest.model_copy(
        update={
            "takes": [*minimal_manifest.takes, second_take],
            "artifacts": [*minimal_manifest.artifacts, second_artifact],
        }
    )
    app = create_app(tmp_path / "api.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        assert (
            client.post(
                "/manifests/import",
                json=manifest.model_dump(mode="json"),
            ).status_code
            == 200
        )
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        session = client.post(
            "/projects/project_test/blind-sessions",
            json={"run_id": report["run"]["id"]},
        )
        assert session.status_code == 200, session.text
        payload = session.json()
        assert payload["blinded"]
        assert payload["review_url"].startswith("/listening/")
        media_url = payload["trials"][0]["left"]["media_url"]
        media = client.get(media_url)
        assert media.status_code == 200
        assert media.headers["content-type"].startswith("audio/wav")


def test_api_rejects_blind_session_without_comparable_candidates(
    tmp_path,
    minimal_manifest,
):
    app = create_app(tmp_path / "empty-blind.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        session = client.post(
            "/projects/project_test/blind-sessions",
            json={"run_id": report["run"]["id"]},
        )

        assert session.status_code == 422
        assert "at least two candidates" in session.json()["detail"]


def test_api_downgrades_legacy_zero_trial_review(
    tmp_path,
    minimal_manifest,
):
    app = create_app(tmp_path / "legacy-zero.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        client.post(
            "/manifests/import",
            json=minimal_manifest.model_dump(mode="json"),
        )
        session = ListeningSession(
            id="listen_legacy_empty",
            project_id="project_test",
            trials=(),
        )
        bundle = StoredListeningBundle(
            id=session.id,
            project_id="project_test",
            run_id="legacy_run",
            session=session,
            stimuli=(),
            trial_secrets=(),
            media_files={},
        )
        review = ListeningReviewRecord(
            project_id="project_test",
            session_id=session.id,
            responses=(),
            validation=ListeningValidation(
                valid=True,
                failures=(),
                pair_outcomes={},
            ),
            review_packet={
                "project_id": "project_test",
                "listening_round_valid": True,
            },
        )
        with app.state.database.transaction():
            app.state.database.save(bundle)
            app.state.database.save(review)

        restored = client.get(f"/listening-sessions/{session.id}")

        assert restored.status_code == 200
        assert not restored.json()["review_status"]["valid"]
        assert (
            "no real comparison trials"
            in restored.json()["review_status"]["failures"][0]
        )


def test_blind_session_survives_restart_and_accepts_review(
    tmp_path,
    minimal_manifest,
    tone_b,
    monkeypatch,
):
    second_take = Take(
        id="take_second",
        project_id="project_test",
        generation_event_id="event_test",
        batch_index=1,
    )
    second_artifact = ReleaseArtifact(
        id="artifact_second",
        project_id="project_test",
        take_id=second_take.id,
        local_path=str(tone_b),
        platform_reported_duration_s=8.0,
    )
    manifest = minimal_manifest.model_copy(
        update={
            "takes": [*minimal_manifest.takes, second_take],
            "artifacts": [*minimal_manifest.artifacts, second_artifact],
        }
    )
    db_path = tmp_path / "persistent.sqlite"
    media_dir = tmp_path / "persistent-media"
    first_app = create_app(db_path, media_dir=media_dir)
    with TestClient(first_app) as client:
        assert (
            client.post(
                "/manifests/import",
                json=manifest.model_dump(mode="json"),
            ).status_code
            == 200
        )
        report = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        ).json()
        session_payload = client.post(
            "/projects/project_test/blind-sessions",
            json={"run_id": report["run"]["id"]},
        ).json()
        session_id = session_payload["session_id"]

    second_app = create_app(db_path, media_dir=media_dir)
    with TestClient(second_app) as client:
        restored = client.get(f"/listening-sessions/{session_id}")
        assert restored.status_code == 200
        payload = restored.json()
        assert payload["project_id"] == "project_test"
        assert payload["project_url"] == "/projects/project_test?view=named"
        assert client.get(payload["trials"][0]["left"]["media_url"]).status_code == 200
        page = client.get(payload["review_url"])
        assert page.status_code == 200
        assert "匿名盲听复核" in page.text
        assert "artifact_test" not in page.text
        assert "artifact_second" not in page.text
        assert 'src="/ui/app.js"' in page.text
        stored = second_app.state.database.require(
            StoredListeningBundle,
            session_id,
        )
        responses = [
            {"trial_id": trial.id, "outcome": "tie"} for trial in stored.session.trials
        ]
        submitted = client.post(
            f"/listening/{session_id}/responses",
            json={"responses": responses},
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["valid"]
        assert submitted.json()["review"]["listening_round_valid"]
        duplicate = client.post(
            f"/listening/{session_id}/responses",
            json={"responses": responses},
        )
        assert duplicate.status_code == 409
        assert "already has a valid review" in duplicate.json()["detail"]

        concurrent_session = client.post(
            "/projects/project_test/blind-sessions",
            json={"run_id": report["run"]["id"]},
        ).json()
        concurrent_id = concurrent_session["session_id"]
        concurrent_record = second_app.state.database.require(
            StoredListeningBundle,
            concurrent_id,
        )
        concurrent_responses = [
            {"trial_id": trial.id, "outcome": "tie"}
            for trial in concurrent_record.session.trials
        ]
        original_builder = api_module.build_listening_review
        ready_to_save = Barrier(2)

        def synchronized_builder(*args, **kwargs):
            result = original_builder(*args, **kwargs)
            with suppress(BrokenBarrierError):
                ready_to_save.wait(timeout=10)
            return result

        monkeypatch.setattr(
            api_module,
            "build_listening_review",
            synchronized_builder,
        )

        def submit_concurrently():
            return client.post(
                f"/listening/{concurrent_id}/responses",
                json={"responses": concurrent_responses},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(
                executor.map(lambda _: submit_concurrently(), range(2))
            )
        monkeypatch.undo()
        assert sorted(item.status_code for item in concurrent_results) == [200, 409]
        concurrent_reviews = [
            item
            for item in second_app.state.database.list(
                ListeningReviewRecord,
                "project_test",
            )
            if item.session_id == concurrent_id
        ]
        assert len(concurrent_reviews) == 1

        completed_payload = client.get(f"/listening-sessions/{session_id}").json()
        assert completed_payload["review_status"] == {
            "submitted": True,
            "valid": True,
            "failures": [],
        }

        rerun = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        )
        assert rerun.status_code == 200, rerun.text
        craft = [
            evaluation
            for assessment in rerun.json()["assessments"]
            for evaluation in assessment["evaluations"]
            if evaluation["axis"] == "craft"
        ]
        assert len(craft) == len(rerun.json()["assessments"])
        assert all(item["status"] == "pass" for item in craft)

        newer_session = client.post(
            "/projects/project_test/blind-sessions",
            json={"run_id": report["run"]["id"]},
        )
        assert newer_session.status_code == 200, newer_session.text
        rerun_with_unfinished_latest = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        )
        assert rerun_with_unfinished_latest.status_code == 200
        assert (
            "blind-listening probes failed; subjective round invalid"
            in (rerun_with_unfinished_latest.json()["recommendation"]["evidence_gaps"])
        )


def test_api_rejects_empty_audio_without_persisting_nan(tmp_path, minimal_manifest):
    empty = tmp_path / "empty.wav"
    import numpy as np
    import soundfile as sf

    sf.write(empty, np.zeros((0, 2), dtype=np.float32), 16_000)
    artifact = minimal_manifest.artifacts[0].model_copy(
        update={"local_path": str(empty)}
    )
    manifest = minimal_manifest.model_copy(update={"artifacts": [artifact]})
    app = create_app(tmp_path / "empty.sqlite", media_dir=tmp_path / "media")
    with TestClient(app) as client:
        imported = client.post(
            "/manifests/import",
            json=manifest.model_dump(mode="json"),
        )
        assert imported.status_code == 200, imported.text
        analyzed = client.post(
            "/projects/project_test/analysis",
            json={"review": None},
        )

        assert analyzed.status_code == 422
        assert "no decodable frames" in analyzed.json()["detail"]
        assert not app.state.database.list(StoredAnalysisReport, "project_test")
