from __future__ import annotations

import os
from pathlib import Path

import pytest

from songeval.analyzer import ProjectAnalyzer
from songeval.audio import verify_deterministic_crop
from songeval.enums import AcquisitionPath, RecommendationStatus
from songeval.importers import hydrate_local_artifacts, load_manifest
from songeval.models import ProjectReviewPacket
from songeval.reporting import render_markdown

ROOT = Path(__file__).parents[1]
SPRING_MANIFEST = ROOT / "examples/spring/manifest.json"
SPRING_AUDIO_DIR = (
    Path(value).expanduser() if (value := os.environ.get("SPRING_AUDIO_DIR")) else None
)
SPRING_REFERENCE = (
    Path(value).expanduser()
    if (value := os.environ.get("SPRING_REFERENCE_AUDIO"))
    else None
)
SPRING_PUBLIC_AUDIO_DIR = Path(
    os.environ.get(
        "SPRING_PUBLIC_AUDIO_DIR",
        str(Path.home() / ".local/share/suno-song-evaluator/fixtures/spring"),
    )
).expanduser()
SPRING_PUBLIC_FILES = {
    "artifact_spring_v1_6_full": "14c15bf7-0ea1-444a-bca5-bf0bd0c1c301.mp3",
    "artifact_spring_v1_6_cut": "a6aaa6ea-3ea2-4f3a-8557-bdf991a1ede3.mp3",
    "artifact_spring_v2_0_1": "8747c11b-80e2-4974-b677-ee4bff42a01e.mp3",
    "artifact_spring_v2_0_2": "12d44545-825b-4f92-b297-bbf6c5a684c1.mp3",
    "artifact_spring_v1_4_reference": "b18598f1-88be-4503-bdd0-dd693a2c3ea0.mp3",
}


def spring_manifest(monkeypatch):
    if SPRING_AUDIO_DIR is None or SPRING_REFERENCE is None:
        pytest.skip("local Spring acceptance audio is unavailable")
    required = (
        SPRING_AUDIO_DIR / "春-正式候选-v1.6-cut.wav",
        SPRING_AUDIO_DIR / "春-正式候选-v2.0-cover1.6-1.wav",
        SPRING_AUDIO_DIR / "春-正式候选-v2.0-cover1.6-2.wav",
        SPRING_REFERENCE,
    )
    if not all(path.exists() for path in required):
        pytest.skip("local Spring acceptance audio is unavailable")
    monkeypatch.setenv("SPRING_AUDIO_DIR", str(SPRING_AUDIO_DIR))
    monkeypatch.setenv("SPRING_REFERENCE_AUDIO", str(SPRING_REFERENCE))
    return load_manifest(SPRING_MANIFEST)


def spring_public_manifest():
    paths = {
        artifact_id: SPRING_PUBLIC_AUDIO_DIR / filename
        for artifact_id, filename in SPRING_PUBLIC_FILES.items()
    }
    if not all(path.exists() for path in paths.values()):
        pytest.skip("cached public Spring audio is unavailable")
    manifest = load_manifest(SPRING_MANIFEST, hydrate_audio=False)
    artifacts = [
        artifact.model_copy(
            update={
                "local_path": str(paths[artifact.id]),
                "acquisition_path": AcquisitionPath.CDN_LOSSY,
            }
        )
        for artifact in manifest.artifacts
    ]
    sources = [
        source.model_copy(
            update={
                "local_path": (
                    str(paths["artifact_spring_v1_6_full"])
                    if source.id == "source_spring_v1_6_full"
                    else str(paths["artifact_spring_v1_4_reference"])
                ),
                "acquisition_path": AcquisitionPath.CDN_LOSSY,
            }
        )
        for source in manifest.sources
    ]
    return hydrate_local_artifacts(
        manifest.model_copy(update={"artifacts": artifacts, "sources": sources})
    )


@pytest.mark.real_audio
def test_spring_manifest_lineage_duration_and_provenance(monkeypatch):
    manifest = spring_manifest(monkeypatch)
    artifacts = {item.id: item for item in manifest.artifacts}
    events = {item.id: item for item in manifest.generation_events}
    takes = {item.id: item for item in manifest.takes}

    assert (
        takes["take_spring_v2_0_1"].generation_event_id
        == takes["take_spring_v2_0_2"].generation_event_id
        == "event_spring_v2_0_cover"
    )
    assert events["event_spring_v2_0_cover"].parameters["style_weight"].value == 0.85
    assert (
        events["event_spring_v2_0_cover"].parameters["weirdness_constraint"].value
        == 0.25
    )
    assert artifacts["artifact_spring_v2_0_1"].duration_mismatch_s == pytest.approx(0.2)
    assert artifacts["artifact_spring_v2_0_2"].duration_mismatch_s == pytest.approx(0.2)
    assert artifacts["artifact_spring_v2_0_1"].platform_reported_duration_s == 248.28
    assert artifacts["artifact_spring_v2_0_1"].measured_file_duration_s == 248.48
    assert not artifacts["artifact_spring_v2_0_1"].format_sensitive_comparison_allowed
    assert not any(
        {
            edge.parent_artifact_id,
            edge.child_artifact_id,
        }
        == {"artifact_spring_v1_6_cut", "artifact_spring_v2_0_1"}
        for edge in manifest.edges
    )
    source_state = manifest.source_assessments[0]
    assert source_state.relationship == "replaced"
    assert source_state.provenance.provenance.value == "inferred"
    assert source_state.provenance.confidence.value == "high"
    assert "failed generation IDs" in source_state.limitation


@pytest.mark.real_audio
def test_spring_real_analysis_meets_frozen_acceptance(monkeypatch):
    manifest = spring_manifest(monkeypatch)
    review = ProjectReviewPacket(
        project_id="spring-2026",
        listening_round_valid=False,
        cross_brief_target_compliance_complete=False,
    )
    report = ProjectAnalyzer(manifest).analyze(review)

    assert len(report.assessments) == 3
    assert len(report.comparisons) == 3
    assert all(comparison.hotspots for comparison in report.comparisons)
    sibling = next(
        item
        for item in report.comparisons
        if {
            item.artifact_a_id,
            item.artifact_b_id,
        }
        == {"artifact_spring_v2_0_1", "artifact_spring_v2_0_2"}
    )
    assert sibling.same_generation_event
    assert not sibling.parameter_attribution_allowed
    assert all(item.acquisition_warning for item in report.comparisons)

    preflight = report.reference_preflights[0]
    assert preflight.multi_state
    assert preflight.crosses_sections
    assert preflight.dirty_boundaries

    assert report.recommendation.status == RecommendationStatus.ABSTAIN
    assert report.recommendation.recommended_artifact_id is None
    assert any(
        "ProjectDecisionPolicy" in gap or "blind-listening" in gap
        for gap in report.recommendation.evidence_gaps
    )
    markdown = render_markdown(report)
    assert "Same GenerationEvent" in markdown
    assert "not used to rank candidates" in markdown
    assert "total score" not in markdown.lower()


@pytest.mark.real_audio
def test_spring_full_to_cut_real_fingerprint():
    full_audio_value = os.environ.get("SPRING_FULL_AUDIO_PATH")
    if SPRING_AUDIO_DIR is None or not full_audio_value:
        pytest.skip("full v1.6 acceptance audio is unavailable")
    parent = Path(full_audio_value)
    child = SPRING_AUDIO_DIR / "春-正式候选-v1.6-cut.wav"
    if not parent.exists() or not child.exists():
        pytest.skip("full v1.6 acceptance audio is unavailable")
    result = verify_deterministic_crop(
        parent,
        child,
        parent_artifact_id="artifact_spring_v1_6_full",
        child_artifact_id="artifact_spring_v1_6_cut",
        analysis_run_id="pytest_real_crop",
    )
    assert result.verified
    assert result.lag_s == pytest.approx(0.005, abs=0.001)
    assert result.retained_region_correlation >= 0.995


@pytest.mark.real_audio
def test_spring_public_cache_replays_analysis_and_crop_evidence():
    manifest = spring_public_manifest()
    report = ProjectAnalyzer(manifest).analyze(
        ProjectReviewPacket(project_id=manifest.project_id)
    )
    assert len(report.assessments) == 3
    assert len(report.comparisons) == 3
    assert len(report.audio_metrics) == 5
    assert all(metric.ending.evidence for metric in report.audio_metrics)
    assert report.recommendation.status == RecommendationStatus.ABSTAIN

    paths = {
        artifact_id: SPRING_PUBLIC_AUDIO_DIR / filename
        for artifact_id, filename in SPRING_PUBLIC_FILES.items()
    }
    crop = verify_deterministic_crop(
        paths["artifact_spring_v1_6_full"],
        paths["artifact_spring_v1_6_cut"],
        parent_artifact_id="artifact_spring_v1_6_full",
        child_artifact_id="artifact_spring_v1_6_cut",
        analysis_run_id="pytest_public_crop",
    )
    assert crop.verified
    assert crop.lag_s == pytest.approx(0.005, abs=0.001)
    assert crop.retained_region_correlation >= 0.995
