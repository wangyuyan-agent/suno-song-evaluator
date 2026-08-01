from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from songeval.analyzer import ProjectAnalyzer
from songeval.api import create_app
from songeval.db import Database
from songeval.importers import SunoClipSnapshot, SunoImportError, SunoPublicClient
from songeval.intake import (
    IntakeCanceled,
    IntakeConflict,
    IntakeJobStore,
    IntakePaths,
    IntakeRequest,
    IntakeService,
    IntakeWorker,
    remove_orphan_upload_staging,
    remove_upload_staging,
)
from songeval.models import ProjectRecord, ReleaseArtifact, StoredAnalysisReport
from songeval.util import project_key


def wait_for_job(
    client: TestClient,
    job_id: str,
    *statuses: str,
    timeout_s: float = 10.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/intake-jobs/{job_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in statuses:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"intake job {job_id} did not reach {statuses}")


def test_upload_intake_is_byte_exact_analyzed_and_cleans_staging(
    tmp_path,
    tone_a,
    tone_b,
):
    db_path = tmp_path / "upload.sqlite"
    upload_root = tmp_path / "uploads"
    media_root = tmp_path / "project-media"
    app = create_app(
        db_path,
        upload_dir=upload_root,
        intake_media_dir=media_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        response = client.post(
            "/intakes/upload",
            data={
                "project_id": "web-upload",
                "title": "Web Upload",
                "style": "warm and full",
            },
            files=[
                ("files", ("春-a.wav", tone_a.read_bytes(), "audio/wav")),
                ("files", ("春-b.wav", tone_b.read_bytes(), "audio/wav")),
            ],
        )
        assert response.status_code == 202, response.text
        created = response.json()
        assert "upload_paths" not in response.text
        assert str(tmp_path) not in response.text

        completed = wait_for_job(client, created["id"], "succeeded")
        assert completed["result"]["project_url"] == "/projects/web-upload"
        assert completed["result"]["candidates"] == 2
        assert not (upload_root / created["id"]).exists()

        workspace = client.get("/projects/web-upload/workspace-context")
        assert workspace.status_code == 200

    with Database(db_path) as database:
        artifacts = database.list(ReleaseArtifact, "web-upload")
        reports = database.list(StoredAnalysisReport, "web-upload")
    assert [item.title for item in artifacts] == ["春-a", "春-b"]
    assert Path(artifacts[0].local_path).read_bytes() == tone_a.read_bytes()
    assert Path(artifacts[1].local_path).read_bytes() == tone_b.read_bytes()
    assert len(reports) == 1


def test_upload_intake_rejects_duplicate_limits_and_cleans_partial_files(
    tmp_path,
    tone_a,
):
    upload_root = tmp_path / "uploads"
    app = create_app(
        tmp_path / "limits.sqlite",
        upload_dir=upload_root,
        max_upload_file_bytes=len(tone_a.read_bytes()) - 1,
        max_upload_total_bytes=len(tone_a.read_bytes()) * 3,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        oversized = client.post(
            "/intakes/upload",
            data={"project_id": "too-large", "title": "Too large"},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert oversized.status_code == 413
        assert list(upload_root.iterdir()) == []

    duplicate_app = create_app(
        tmp_path / "duplicate.sqlite",
        upload_dir=upload_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(duplicate_app) as client:
        duplicate = client.post(
            "/intakes/upload",
            data={"project_id": "duplicate", "title": "Duplicate"},
            files=[
                ("files", ("one.wav", tone_a.read_bytes(), "audio/wav")),
                ("files", ("two.wav", tone_a.read_bytes(), "audio/wav")),
            ],
        )
        assert duplicate.status_code == 422
        assert "duplicate audio content" in duplicate.text
        assert list(upload_root.iterdir()) == []

    invalid_audio = create_app(
        tmp_path / "invalid-audio.sqlite",
        upload_dir=upload_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(invalid_audio) as client:
        undecodable = client.post(
            "/intakes/upload",
            data={"project_id": "undecodable", "title": "Undecodable"},
            files=[("files", ("fake.wav", b"not audio", "audio/wav"))],
        )
        assert undecodable.status_code == 422
        assert "fake.wav is not decodable audio" in undecodable.text
        assert str(tmp_path) not in undecodable.text
        assert list(upload_root.iterdir()) == []

        invalid_title = client.post(
            "/intakes/upload",
            data={"project_id": "invalid-title", "title": "   "},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert invalid_title.status_code == 422
        assert list(upload_root.iterdir()) == []


def test_upload_intake_conflict_returns_409_and_cleans_new_staging(tmp_path, tone_a):
    upload_root = tmp_path / "uploads"
    app = create_app(
        tmp_path / "conflict.sqlite",
        upload_dir=upload_root,
        extra_allowed_hosts=("testserver",),
    )
    request = IntakeRequest(
        project_id="conflicting-project",
        title="Existing intake",
        kind="upload",
        upload_paths=(str(upload_root / "missing.wav"),),
        original_filenames=("missing.wav",),
    )
    existing = app.state.intake_store.create(request, job_id="intake_existing")
    app.state.intake_store.request_cancel(existing.id)

    with TestClient(app) as client:
        response = client.post(
            "/intakes/upload",
            data={"project_id": "conflicting-project", "title": "Conflict"},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )

    assert response.status_code == 409
    assert "already has intake job" in response.text
    assert not [
        path for path in upload_root.iterdir() if path.name != "intake_existing"
    ]


def test_active_job_delete_checks_status_before_partial_project(tmp_path):
    app = create_app(
        tmp_path / "active-delete.sqlite",
        extra_allowed_hosts=("testserver",),
    )
    app.state.database.save(ProjectRecord(id="active-project", title="Active"))
    request = IntakeRequest(
        project_id="active-project",
        title="Active",
        kind="upload",
        upload_paths=(str(tmp_path / "missing.wav"),),
        original_filenames=("missing.wav",),
    )
    job = app.state.intake_store.create(request, job_id="intake_active")
    client = TestClient(app)
    response = client.delete(f"/intake-jobs/{job.id}")
    client.close()

    assert response.status_code == 409
    assert response.json()["detail"] == "only failed or canceled jobs can be deleted"


def test_upload_pending_limit_cannot_be_below_request_limit(tmp_path):
    with pytest.raises(
        ValueError,
        match="total upload limit cannot exceed the pending upload storage limit",
    ):
        create_app(
            tmp_path / "invalid-limits.sqlite",
            max_upload_file_bytes=4,
            max_upload_total_bytes=8,
            max_upload_pending_bytes=7,
        )


def test_cross_site_state_changes_are_rejected_before_intake(tmp_path):
    app = create_app(
        tmp_path / "csrf.sqlite",
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        origin_rejected = client.post(
            "/intakes/suno/preview",
            headers={"Origin": "https://evil.example"},
            json={"url": "https://suno.com/s/example"},
        )
        fetch_site_rejected = client.post(
            "/intakes/suno/preview",
            headers={"Sec-Fetch-Site": "cross-site"},
            json={"url": "https://suno.com/s/example"},
        )
    assert origin_rejected.status_code == 403
    assert fetch_site_rejected.status_code == 403


def test_request_stream_limit_and_snapshot_host_validation(tmp_path):
    app = create_app(
        tmp_path / "body-limit.sqlite",
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        oversized = client.post(
            "/manifests/import",
            content=b"x" * (8 * 1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        chunked = client.post(
            "/manifests/import",
            content=iter((b"x" * (4 * 1024 * 1024), b"y" * (5 * 1024 * 1024))),
            headers={"Content-Type": "application/json"},
        )
        invalid_snapshot = client.post(
            "/intakes/suno/snapshot",
            json={
                "project_id": "unsafe-snapshot",
                "title": "Unsafe snapshot",
                "snapshots": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "audio_url": "https://cdn1.suno.ai/song.mp3",
                        "metadata": {},
                        "raw_payload": {"id": "11111111"},
                        "source_url": "https://evil.example/forged",
                    }
                ],
            },
        )
    assert oversized.status_code == 413
    assert chunked.status_code == 413
    assert invalid_snapshot.status_code == 422
    assert "not on suno.com" in invalid_snapshot.text


def test_app_applies_security_headers_without_caddy(tmp_path):
    app = create_app(
        tmp_path / "headers.sqlite",
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        response = client.get("/")
        static = client.get("/ui/app.css")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert static.headers["cache-control"] == "no-cache"

    protected = create_app(
        tmp_path / "protected-headers.sqlite",
        extra_allowed_hosts=("testserver",),
        auth_username="admin",
        auth_password="a sufficiently long password",
    )
    with TestClient(protected) as client:
        anonymous = client.get("/")
    assert anonymous.status_code == 401
    assert anonymous.headers["cache-control"] == "no-store"
    assert anonymous.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in anonymous.headers["content-security-policy"]


def test_suno_preview_selection_and_intake_never_generate_or_publish(
    tmp_path,
    tone_a,
    monkeypatch,
):
    clips = [
        SunoClipSnapshot(
            id="11111111-1111-1111-1111-111111111111",
            title="First",
            audio_url="https://cdn1.suno.ai/first.wav",
            duration=8.0,
            metadata={"prompt": "lyrics", "tags": "warm"},
            raw_payload={"id": "11111111-1111-1111-1111-111111111111"},
            source_url="https://suno.com/playlist/test",
        ),
        SunoClipSnapshot(
            id="22222222-2222-2222-2222-222222222222",
            title="Second",
            audio_url="https://cdn1.suno.ai/second.wav",
            duration=8.0,
            metadata={"prompt": "lyrics", "tags": "warm"},
            raw_payload={"id": "22222222-2222-2222-2222-222222222222"},
            source_url="https://suno.com/playlist/test",
        ),
    ]

    def fake_fetch(self, url):
        assert url == "https://suno.com/playlist/test"
        return clips

    def fake_download(self, clip, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tone_a, destination)
        return destination

    monkeypatch.setattr(SunoPublicClient, "fetch", fake_fetch)
    monkeypatch.setattr(SunoPublicClient, "download_audio", fake_download)
    app = create_app(
        tmp_path / "suno.sqlite",
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        preview = client.post(
            "/intakes/suno/preview",
            json={"url": "https://suno.com/playlist/test"},
        )
        assert preview.status_code == 200
        assert preview.json()["count"] == 2
        assert preview.json()["generation_or_publication_performed"] is False

        created = client.post(
            "/intakes/suno",
            json={
                "project_id": "suno-selected",
                "title": "Selected Suno clip",
                "url": "https://suno.com/playlist/test",
                "selected_clip_ids": [clips[1].id],
            },
        )
        assert created.status_code == 202, created.text
        completed = wait_for_job(client, created.json()["id"], "succeeded")
        assert completed["result"]["candidates"] == 1

    with Database(tmp_path / "suno.sqlite") as database:
        artifacts = database.list(ReleaseArtifact, "suno-selected")
    assert [item.platform_id for item in artifacts] == [clips[1].id]
    assert Path(artifacts[0].local_path).read_bytes() == tone_a.read_bytes()


def test_snapshot_intake_enforces_configured_clip_limit(tmp_path):
    snapshots = [
        {
            "id": f"{index}" * 36,
            "audio_url": f"https://cdn1.suno.ai/{index}.mp3",
            "metadata": {},
            "raw_payload": {"id": f"{index}" * 36},
            "source_url": "https://suno.com/playlist/test",
        }
        for index in (1, 2)
    ]
    app = create_app(
        tmp_path / "clip-limit.sqlite",
        max_upload_files=1,
        max_upload_file_bytes=1024,
        max_upload_total_bytes=2048,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        response = client.post(
            "/intakes/suno/snapshot",
            json={
                "project_id": "too-many-clips",
                "title": "Too many clips",
                "snapshots": snapshots,
            },
        )
    assert response.status_code == 422
    assert "select at most 1 clips" in response.text


def test_failed_analysis_can_retry_without_reupload(tmp_path, tone_a, monkeypatch):
    db_path = tmp_path / "retry.sqlite"
    upload_root = tmp_path / "uploads"
    original_analyze = ProjectAnalyzer.analyze
    attempts = 0

    def fail_once(self, *args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("intentional first analysis failure")
        return original_analyze(self, *args, **kwargs)

    monkeypatch.setattr(ProjectAnalyzer, "analyze", fail_once)
    app = create_app(
        db_path,
        upload_dir=upload_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        created = client.post(
            "/intakes/upload",
            data={"project_id": "retry-me", "title": "Retry me"},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        failed = wait_for_job(client, job_id, "failed")
        assert "intentional first analysis failure" in failed["error"]
        assert str(tmp_path) not in failed["error"]
        assert (upload_root / job_id).is_dir()
        refused_cleanup = client.delete(f"/intake-jobs/{job_id}")
        assert refused_cleanup.status_code == 409
        assert "retry" in refused_cleanup.text

        retried = client.post(f"/intake-jobs/{job_id}/retry")
        assert retried.status_code == 202
        completed = wait_for_job(client, job_id, "succeeded")
        assert completed["attempt"] == 2
        assert not (upload_root / job_id).exists()

    with Database(db_path) as database:
        assert database.get(ProjectRecord, "retry-me") is not None
        assert len(database.list(StoredAnalysisReport, "retry-me")) == 1


def test_partial_project_can_be_explicitly_discarded_and_recreated(
    tmp_path,
    tone_a,
    monkeypatch,
):
    db_path = tmp_path / "discard.sqlite"
    upload_root = tmp_path / "uploads"
    media_root = tmp_path / "media"
    original_analyze = ProjectAnalyzer.analyze

    def always_fail(self, *args, **kwargs):
        raise ValueError("deterministic analysis failure")

    monkeypatch.setattr(ProjectAnalyzer, "analyze", always_fail)
    app = create_app(
        db_path,
        upload_dir=upload_root,
        intake_media_dir=media_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        created = client.post(
            "/intakes/upload",
            data={"project_id": "discard-me", "title": "Discard me"},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        wait_for_job(client, job_id, "failed")

        refused = client.delete(f"/intake-jobs/{job_id}")
        assert refused.status_code == 409
        assert "discard_partial_project=true" in refused.text

        discarded = client.delete(f"/intake-jobs/{job_id}?discard_partial_project=true")
        assert discarded.status_code == 200, discarded.text
        assert not (media_root / project_key("discard-me")).exists()
        with Database(db_path) as database:
            assert database.get(ProjectRecord, "discard-me") is None

        monkeypatch.setattr(ProjectAnalyzer, "analyze", original_analyze)
        recreated = client.post(
            "/intakes/upload",
            data={"project_id": "discard-me", "title": "Discard me"},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert recreated.status_code == 202, recreated.text
        wait_for_job(client, recreated.json()["id"], "succeeded")


def test_discard_does_not_delete_case_colliding_project_media(
    tmp_path,
    tone_a,
    monkeypatch,
):
    db_path = tmp_path / "case-collision.sqlite"
    media_root = tmp_path / "media"
    original_analyze = ProjectAnalyzer.analyze

    def fail_uppercase_project(self, *args, **kwargs):
        if self.manifest.project_id == "Spring":
            raise ValueError("intentional uppercase project failure")
        return original_analyze(self, *args, **kwargs)

    monkeypatch.setattr(ProjectAnalyzer, "analyze", fail_uppercase_project)
    app = create_app(
        db_path,
        intake_media_dir=media_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        lower = client.post(
            "/intakes/upload",
            data={"project_id": "spring", "title": "Lowercase spring"},
            files=[("files", ("lower.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert lower.status_code == 202, lower.text
        wait_for_job(client, lower.json()["id"], "succeeded")
        lower_artifact = app.state.database.list(ReleaseArtifact, "spring")[0]
        lower_media = Path(lower_artifact.local_path)
        assert lower_media.is_file()

        upper = client.post(
            "/intakes/upload",
            data={"project_id": "Spring", "title": "Uppercase spring"},
            files=[("files", ("upper.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert upper.status_code == 202, upper.text
        upper_job_id = upper.json()["id"]
        wait_for_job(client, upper_job_id, "failed")
        discarded = client.delete(
            f"/intake-jobs/{upper_job_id}?discard_partial_project=true"
        )
        assert discarded.status_code == 200, discarded.text

        assert lower_media.is_file()
        assert app.state.database.get(ProjectRecord, "spring") is not None
        assert app.state.database.list(StoredAnalysisReport, "spring")
        assert app.state.database.get(ProjectRecord, "Spring") is None


def test_report_materialization_failure_is_repaired_on_retry(
    tmp_path,
    tone_a,
    monkeypatch,
):
    db_path = tmp_path / "report-retry.sqlite"
    report_root = tmp_path / "reports"
    original_materialize = IntakeService._materialize_report
    attempts = 0

    def fail_once(self, report, report_dir):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(f"cannot write {report_dir}")
        return original_materialize(self, report, report_dir)

    monkeypatch.setattr(IntakeService, "_materialize_report", fail_once)
    app = create_app(
        db_path,
        report_dir=report_root,
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        created = client.post(
            "/intakes/upload",
            data={"project_id": "report-retry", "title": "Report retry"},
            files=[("files", ("song.wav", tone_a.read_bytes(), "audio/wav"))],
        )
        assert created.status_code == 202
        job_id = created.json()["id"]
        failed = wait_for_job(client, job_id, "failed")
        assert "cannot write" in failed["error"]
        assert str(tmp_path) not in failed["error"]

        with Database(db_path) as database:
            stored = database.list(StoredAnalysisReport, "report-retry")
        assert len(stored) == 1
        assert not list(report_root.rglob("*.json"))

        retried = client.post(f"/intake-jobs/{job_id}/retry")
        assert retried.status_code == 202
        completed = wait_for_job(client, job_id, "succeeded")
        assert completed["attempt"] == 2

    run_id = stored[0].id
    project_reports = report_root / project_key("report-retry")
    assert (project_reports / f"{run_id}.json").is_file()
    assert (project_reports / f"{run_id}.md").is_file()


def test_job_store_recovers_running_jobs_and_supports_cancel_retry_delete(tmp_path):
    staged = tmp_path / "staged.wav"
    staged.write_bytes(b"placeholder")
    store = IntakeJobStore(tmp_path / "jobs.sqlite")
    request = IntakeRequest(
        project_id="recover-me",
        title="Recover me",
        kind="upload",
        upload_paths=(str(staged),),
        original_filenames=("staged.wav",),
    )
    created = store.create(request)
    claimed = store.claim_next()
    assert claimed is not None and claimed.status == "running"
    with pytest.raises(IntakeConflict):
        store.delete_terminal(created.id)
    assert store.require(created.id).status == "running"

    restarted = IntakeJobStore(tmp_path / "jobs.sqlite")
    assert restarted.recover_interrupted() == 1
    assert restarted.require(created.id).status == "queued"
    canceled = restarted.request_cancel(created.id)
    assert canceled.status == "canceled"
    with pytest.raises(IntakeConflict, match="already has intake job"):
        restarted.create(request)
    assert restarted.retry(created.id).status == "queued"
    restarted.request_cancel(created.id)
    with pytest.raises(OSError):
        restarted.delete_terminal(
            created.id,
            before_delete=lambda _job: (_ for _ in ()).throw(
                OSError("intentional cleanup failure")
            ),
        )
    assert restarted.require(created.id).status == "canceled"
    deleted = restarted.delete_terminal(created.id)
    assert deleted.status == "canceled"
    assert restarted.get(created.id) is None


def test_missing_and_orphan_upload_staging_can_be_cleaned(tmp_path):
    upload_root = tmp_path / "uploads"
    known_stage = upload_root / "intake_known"
    orphan_stage = upload_root / "intake_orphan"
    known_stage.mkdir(parents=True)
    orphan_stage.mkdir()
    known_file = known_stage / "song.wav"
    known_file.write_bytes(b"placeholder")
    (orphan_stage / "leftover.wav").write_bytes(b"orphan")

    store = IntakeJobStore(tmp_path / "jobs.sqlite")
    request = IntakeRequest(
        project_id="known-stage",
        title="Known stage",
        kind="upload",
        upload_paths=(str(known_file),),
        original_filenames=("song.wav",),
    )
    created = store.create(request, job_id="intake_known")
    store.request_cancel(created.id)

    assert remove_orphan_upload_staging(store, upload_root) == 1
    assert known_stage.is_dir()
    assert not orphan_stage.exists()

    shutil.rmtree(known_stage)
    deleted = store.delete_terminal(
        created.id,
        before_delete=lambda job: remove_upload_staging(job, upload_root),
    )
    assert deleted.status == "canceled"


def test_worker_reports_expected_suno_validation_failure(tmp_path):
    app = create_app(
        tmp_path / "suno-error.sqlite",
        extra_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        created = client.post(
            "/intakes/suno",
            json={
                "project_id": "bad-suno-host",
                "title": "Bad Suno host",
                "url": "https://example.com/not-suno",
            },
        )
        assert created.status_code == 202
        failed = wait_for_job(client, created.json()["id"], "failed")

    assert "not on suno.com" in failed["error"]
    assert "unexpected intake failure" not in failed["error"]


def test_intake_worker_survives_transient_store_failure(tmp_path):
    class FlakyStore:
        calls = 0

        @staticmethod
        def recover_interrupted():
            return 0

        def claim_next(self):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("temporary database failure")
            return None

    store = FlakyStore()
    service = IntakeService(
        tmp_path / "domain.sqlite",
        IntakePaths(
            media_root=tmp_path / "media",
            report_root=tmp_path / "reports",
            upload_root=tmp_path / "uploads",
        ),
    )
    worker = IntakeWorker(store, service, cleanup_upload=lambda _job: None)
    worker.start()
    deadline = time.monotonic() + 2
    while store.calls < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()

    assert store.calls >= 2


def test_cancel_during_analysis_stops_before_report_commit(tmp_path, tone_a):
    db_path = tmp_path / "cancel-before-report.sqlite"
    upload_root = tmp_path / "uploads"
    staged = upload_root / "intake_cancel" / "song.wav"
    staged.parent.mkdir(parents=True)
    shutil.copyfile(tone_a, staged)
    service = IntakeService(
        db_path,
        IntakePaths(
            media_root=tmp_path / "media",
            report_root=tmp_path / "reports",
            upload_root=upload_root,
        ),
    )
    request = IntakeRequest(
        project_id="cancel-before-report",
        title="Cancel before report",
        kind="upload",
        upload_paths=(str(staged),),
        original_filenames=("song.wav",),
    )
    cancel_requested = False

    def progress(step, _value):
        nonlocal cancel_requested
        if step == "analyzing_audio":
            cancel_requested = True

    with pytest.raises(IntakeCanceled):
        service.run(
            request,
            progress=progress,
            canceled=lambda: cancel_requested,
        )

    with Database(db_path) as database:
        assert database.get(ProjectRecord, request.project_id) is not None
        assert database.list(StoredAnalysisReport, request.project_id) == []


def test_worker_cleanup_callback_failure_does_not_stop_queue(tmp_path, monkeypatch):
    store = IntakeJobStore(tmp_path / "cleanup-callback.sqlite")
    request = IntakeRequest(
        project_id="cleanup-callback",
        title="Cleanup callback",
        kind="upload",
        upload_paths=(str(tmp_path / "staged.wav"),),
        original_filenames=("staged.wav",),
    )
    created = store.create(request)
    service = IntakeService(
        tmp_path / "domain.sqlite",
        IntakePaths(
            media_root=tmp_path / "media",
            report_root=tmp_path / "reports",
            upload_root=tmp_path / "uploads",
        ),
    )
    monkeypatch.setattr(
        service,
        "run",
        lambda *_args, **_kwargs: {"project_id": request.project_id},
    )

    def fail_cleanup(_job):
        raise RuntimeError("unexpected cleanup callback failure")

    worker = IntakeWorker(store, service, cleanup_upload=fail_cleanup)
    worker.start()
    deadline = time.monotonic() + 2
    while (
        store.require(created.id).status != "succeeded" and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    worker.stop()

    assert store.require(created.id).status == "succeeded"


def test_intake_paths_are_normalized_when_configured_relatively(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    paths = IntakePaths(
        media_root=Path("media"),
        report_root=Path("reports"),
        upload_root=Path("uploads"),
    )

    assert paths.media_root == (tmp_path / "media").resolve()
    assert paths.report_root == (tmp_path / "reports").resolve()
    assert paths.upload_root == (tmp_path / "uploads").resolve()


def test_local_parent_paths_still_require_a_known_child_clip():
    request = IntakeRequest(
        project_id="local-parent",
        title="Local parent",
        kind="suno",
        snapshots=(
            SunoClipSnapshot(
                id="known-child",
                audio_url="https://cdn1.suno.ai/known-child.mp3",
                metadata={},
                raw_payload={"id": "known-child"},
                source_url="https://suno.com/song/known-child",
            ),
        ),
        parents=(
            {
                "child_clip_id": "unknown-child",
                "parent": "/tmp/local-parent.wav",
            },
        ),
        allow_local_parent_paths=True,
    )

    with pytest.raises(SunoImportError, match="unknown child clip ID"):
        IntakeService._validate_web_parents(request, request.snapshots)
