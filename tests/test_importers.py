from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import soundfile as sf

from songeval.enums import AcquisitionPath, OperationType, TaskType
from songeval.importers import (
    ParentDeclaration,
    SunoClipSnapshot,
    SunoImportError,
    SunoPublicClient,
    build_local_project,
    build_suno_project,
    clip_to_generation_records,
    hydrate_local_artifacts,
    make_acquisition_snapshot,
)


def test_hydration_keeps_platform_and_measured_durations_separate(minimal_manifest):
    hydrated = hydrate_local_artifacts(minimal_manifest)
    artifact = hydrated.artifacts[0]
    assert artifact.platform_reported_duration_s == 8.2
    assert artifact.measured_file_duration_s == pytest.approx(8.0)
    assert artifact.duration_mismatch_s == pytest.approx(-0.2)
    assert artifact.encoding.container == "WAV"
    assert artifact.file_sha256
    assert artifact.acquisition_path == AcquisitionPath.UNKNOWN
    assert not artifact.format_sensitive_comparison_allowed


def test_missing_audio_can_remain_unresolved(minimal_manifest):
    artifact = minimal_manifest.artifacts[0].model_copy(
        update={"local_path": "/does/not/exist.wav"}
    )
    hydrated = hydrate_local_artifacts(
        minimal_manifest.model_copy(update={"artifacts": [artifact]}),
        require_files=False,
    )
    assert hydrated.artifacts[0].measured_file_duration_s is None


def test_suno_next_flight_payload_is_parsed_without_field_rewrite():
    clip = {
        "id": "8747c11b-80e2-4974-b677-ee4bff42a01e",
        "title": "title",
        "audio_url": "https://cdn1.suno.ai/a.mp3",
        "batch_index": 1,
        "metadata": {
            "task": "cover",
            "duration": 12.34,
            "control_sliders": {
                "style_weight": 0.85,
                "weirdness_constraint": 0.25,
            },
            "future_field": {"preserve": True},
        },
    }
    flight = json.dumps([1, json.dumps({"playlist_clips": [{"clip": clip}]})])
    html = f"<html><script>self.__next_f.push({flight})</script></html>"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    with SunoPublicClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        snapshots = client.fetch("https://suno.com/playlist/example")
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.metadata["control_sliders"]["style_weight"] == 0.85
    assert snapshot.metadata["future_field"] == {"preserve": True}
    event, take, artifact = clip_to_generation_records(
        snapshot,
        project_id="p",
        brief_id="b",
    )
    assert event.parameters["style_weight"].value == 0.85
    assert event.raw_metadata["future_field"] == {"preserve": True}
    assert artifact.platform_reported_duration_s == 12.34
    assert take.batch_index == 1


def test_suno_metadata_type_maps_gen_and_crop_without_guessing():
    def snapshot(clip_id: str, type_value: str) -> SunoClipSnapshot:
        return SunoClipSnapshot(
            id=clip_id,
            title=type_value,
            duration=1.0,
            metadata={"type": type_value},
            raw_payload={"id": clip_id, "metadata": {"type": type_value}},
            source_url="https://suno.com/playlist/example",
        )

    generated = clip_to_generation_records(
        snapshot("generated-clip-id", "gen"),
        project_id="p",
        brief_id="b",
    )
    cropped = clip_to_generation_records(
        snapshot("cropped-clip-id", "edit_crop"),
        project_id="p",
        brief_id="b",
    )
    assert generated[0].task == TaskType.CREATE
    assert generated[2].operation == OperationType.RAW
    assert cropped[0].task == TaskType.EDIT_CROP
    assert cropped[2].operation == OperationType.CROP


def test_suno_url_validation_rejects_other_hosts():
    with pytest.raises(SunoImportError, match="not on suno.com"):
        SunoPublicClient.validate_url("https://example.com/song/1")


def test_suno_page_fetch_rejects_redirect_to_non_suno_host():
    requested_hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "suno.com":
            return httpx.Response(
                302,
                headers={"location": "https://example.com/private"},
            )
        return httpx.Response(200, text="<html></html>")

    with (
        SunoPublicClient(
            client=httpx.Client(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
            )
        ) as client,
        pytest.raises(SunoImportError, match="not on suno.com"),
    ):
        client.fetch("https://suno.com/playlist/example")
    assert requested_hosts == ["suno.com"]


def test_suno_audio_url_validation_blocks_snapshot_ssrf():
    SunoPublicClient.validate_audio_url("https://cdn1.suno.ai/clip.mp3")
    with pytest.raises(SunoImportError, match="Suno-controlled"):
        SunoPublicClient.validate_audio_url("http://127.0.0.1/private.mp3")


def test_suno_audio_download_ignores_malformed_content_length(tone_a, tmp_path):
    clip = SunoClipSnapshot(
        id="download-clip",
        audio_url="https://cdn1.suno.ai/download-clip.wav",
        raw_payload={"id": "download-clip"},
        source_url="https://suno.com/s/download-clip",
    )
    payload = tone_a.read_bytes()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={
                "content-type": "audio/wav",
                "content-length": "invalid, 123",
            },
        )

    with SunoPublicClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        downloaded = client.download_audio(clip, tmp_path / "downloaded.wav")
    assert downloaded.read_bytes() == payload


def test_same_url_changed_content_creates_new_immutable_revision():
    first = make_acquisition_snapshot(
        project_id="p",
        url="https://suno.com/s/example",
        platform_id="clip",
        raw_payload={"value": 1},
    )
    same = make_acquisition_snapshot(
        project_id="p",
        url="https://suno.com/s/example",
        platform_id="clip",
        raw_payload={"value": 1},
        existing=[first],
    )
    assert same.id == first.id
    changed = make_acquisition_snapshot(
        project_id="p",
        url="https://suno.com/s/example",
        platform_id="clip",
        raw_payload={"value": 2},
        existing=[first],
    )
    assert changed.id != first.id
    assert changed.revision_of == first.id
    assert changed.content_changed_from_previous
    assert "different content" in changed.warning


def test_playlist_clips_are_not_misclassified_as_url_revisions():
    first = make_acquisition_snapshot(
        project_id="p",
        url="https://suno.com/playlist/example",
        platform_id="clip-a",
        raw_payload={"id": "clip-a"},
    )
    second = make_acquisition_snapshot(
        project_id="p",
        url="https://suno.com/playlist/example",
        platform_id="clip-b",
        raw_payload={"id": "clip-b"},
        existing=[first],
    )
    assert second.revision_of is None
    assert not second.content_changed_from_previous


def test_suno_intake_downloads_without_transcoding_and_builds_verified_crop(
    tone_a,
    tmp_path,
):
    samples, sample_rate = sf.read(tone_a)
    child_path = tmp_path / "child-source.wav"
    sf.write(child_path, samples[: sample_rate * 4], sample_rate, subtype="PCM_16")
    child_bytes = child_path.read_bytes()
    clip = SunoClipSnapshot(
        id="child-crop-clip",
        title="crop",
        audio_url="https://cdn1.suno.ai/child-crop-clip.wav",
        duration=4.0,
        metadata={
            "type": "edit_crop",
            "prompt": "same lyrics",
            "tags": "same style",
        },
        raw_payload={
            "id": "child-crop-clip",
            "batch_index": 1,
            "metadata": {"type": "edit_crop"},
        },
        source_url="https://suno.com/playlist/example",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == clip.audio_url
        return httpx.Response(
            200,
            content=child_bytes,
            headers={"content-type": "audio/wav"},
        )

    with SunoPublicClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        result = build_suno_project(
            [clip],
            project_id="intake",
            title="Intake",
            media_dir=tmp_path / "media",
            client=client,
            parent_declarations=[
                ParentDeclaration(
                    child_clip_id=clip.id,
                    parent=str(tone_a),
                )
            ],
        )
    assert len(result.manifest.artifacts) == 2
    child = next(
        item
        for item in result.manifest.artifacts
        if item.platform_id == "child-crop-clip"
    )
    assert child.operation == OperationType.CROP
    assert child.file_sha256
    edge = result.manifest.edges[0]
    assert edge.child_artifact_id == child.id
    assert edge.deterministic
    assert edge.source_interval_s == pytest.approx((0.0, 4.0), abs=0.01)
    assert edge.verified_run_id == "intake_crop_verification"


def test_suno_intake_reuses_declared_parent_already_in_candidate_set(tmp_path):
    parent = SunoClipSnapshot(
        id="parent-generation-clip",
        title="parent",
        duration=8.0,
        metadata={"type": "gen"},
        raw_payload={"id": "parent-generation-clip", "metadata": {"type": "gen"}},
        source_url="https://suno.com/playlist/example",
    )
    child = SunoClipSnapshot(
        id="child-crop-clip",
        title="crop",
        duration=4.0,
        metadata={"type": "edit_crop"},
        raw_payload={"id": "child-crop-clip", "metadata": {"type": "edit_crop"}},
        source_url="https://suno.com/playlist/example",
    )

    result = build_suno_project(
        [parent, child],
        project_id="existing-parent",
        title="Existing parent",
        media_dir=tmp_path / "media",
        download_audio=False,
        parent_declarations=[
            ParentDeclaration(child_clip_id=child.id, parent=parent.id)
        ],
    )

    assert len(result.manifest.generation_events) == 2
    assert len(result.manifest.takes) == 2
    assert len(result.manifest.artifacts) == 2
    edge = result.manifest.edges[0]
    assert edge.parent_artifact_id == "artifact_parent-generation-clip"
    assert edge.child_artifact_id == "artifact_child-crop-clip"


def test_suno_intake_rejects_duplicate_parent_declarations(tmp_path):
    parent = SunoClipSnapshot(
        id="parent-generation-clip",
        duration=8.0,
        metadata={"type": "gen"},
        raw_payload={"id": "parent-generation-clip", "metadata": {"type": "gen"}},
        source_url="https://suno.com/playlist/example",
    )
    child = SunoClipSnapshot(
        id="child-crop-clip",
        duration=4.0,
        metadata={"type": "edit_crop"},
        raw_payload={"id": "child-crop-clip", "metadata": {"type": "edit_crop"}},
        source_url="https://suno.com/playlist/example",
    )
    declaration = ParentDeclaration(child_clip_id=child.id, parent=parent.id)

    with pytest.raises(SunoImportError, match="duplicate parent declaration"):
        build_suno_project(
            [parent, child],
            project_id="duplicate-parent",
            title="Duplicate parent",
            media_dir=tmp_path / "media",
            download_audio=False,
            parent_declarations=[declaration, declaration],
        )


def test_local_intake_copies_bytes_into_stable_cache(tone_a, tone_b, tmp_path):
    result = build_local_project(
        [tone_a, tone_b],
        project_id="local-project",
        title="Local",
        media_dir=tmp_path / "cache",
        lyrics="frozen",
    )
    assert len(result.manifest.artifacts) == 2
    assert result.manifest.briefs[0].requirements[0].id == "frozen_lyrics"
    for artifact, original in zip(
        result.manifest.artifacts,
        (tone_a, tone_b),
        strict=True,
    ):
        assert artifact.local_path
        assert artifact.file_sha256
        cached = Path(artifact.local_path)
        assert (tmp_path / "cache") in cached.parents
        assert cached.read_bytes() == original.read_bytes()
