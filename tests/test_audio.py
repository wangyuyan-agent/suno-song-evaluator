from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest
import soundfile as sf

from songeval.audio import (
    analyze_structure,
    compare_audio_files,
    compare_feature_series,
    diagnose_ending,
    extract_features,
    loudness_match_gain_db,
    measure_audio,
    probe_encoding,
    verify_deterministic_crop,
)
from songeval.models import EncodingFingerprint
from songeval.util import canonical_json


def comparison(path_a, path_b):
    return compare_audio_files(
        path_a,
        path_b,
        project_id="p",
        artifact_a_id="a",
        artifact_b_id="b",
        analysis_run_id="r",
        same_generation_event=False,
    )


def test_audio_measurements_are_diagnostics(tone_a):
    metrics = measure_audio(
        tone_a,
        artifact_id="a",
        analysis_run_id="r",
        acquisition_degraded=True,
    )
    assert metrics.measured_file_duration_s == pytest.approx(8.0)
    assert metrics.sample_rate_hz == 16_000
    assert metrics.channels == 2
    assert metrics.integrated_lufs is not None
    assert metrics.peak_dbfs < 0
    assert "format_sensitive_absolute_claims_disabled" in metrics.warnings
    assert metrics.technical_checks["decode"].value == "pass"
    assert metrics.technical_checks["vocal_timbre_change"].value == "not_evaluated"
    assert metrics.ending.classification == "natural_decay"
    assert metrics.evidence_gaps


def test_true_peak_uses_each_channel_before_any_mono_downmix(tmp_path):
    sample_rate = 48_000
    timeline = np.arange(sample_rate, dtype=np.float64) / sample_rate
    left = 0.5 * np.sin(2 * np.pi * 997 * timeline)
    anti_phase_stereo = np.column_stack((left, -left))
    path = tmp_path / "anti-phase.wav"
    sf.write(path, anti_phase_stereo, sample_rate, subtype="FLOAT")

    metrics = measure_audio(
        path,
        artifact_id="anti-phase",
        analysis_run_id="run",
        acquisition_degraded=False,
    )

    assert metrics.channels == 2
    assert metrics.stereo_correlation == pytest.approx(-1.0, abs=1e-6)
    assert metrics.true_peak_dbfs > -6.2


def test_a_vs_a_is_feature_tie(tone_a):
    result = comparison(tone_a, tone_a)
    assert result.pitch_harmony_distance == pytest.approx(0.0, abs=1e-10)
    assert result.rhythm_onset_distance == pytest.approx(0.0, abs=1e-10)
    assert result.energy_structure_distance == pytest.approx(0.0, abs=1e-10)


def test_gain_variant_ties_after_shape_normalization(tone_a, tmp_path):
    samples, sample_rate = sf.read(tone_a)
    variant = tmp_path / "gain.wav"
    sf.write(variant, samples * 0.5, sample_rate, subtype="PCM_16")
    result = comparison(tone_a, variant)
    assert result.pitch_harmony_distance < 1e-5
    assert result.rhythm_onset_distance < 1e-4
    assert result.energy_structure_distance < 1e-4


def test_different_song_emits_timestamped_hotspots(tone_a, tone_b):
    result = comparison(tone_a, tone_b)
    assert result.hotspots
    assert {item.feature_family for item in result.hotspots} <= {
        "pitch_harmony",
        "rhythm_onset",
        "energy_structure",
    }
    assert all(item.a_end_s > item.a_start_s for item in result.hotspots)
    assert result.pitch_harmony_distance > 0.01


def test_sliced_comparison_preserves_absolute_hotspot_coordinates(tone_a, tone_b):
    region_a = (3.0, 8.0)
    region_b = (1.0, 6.0)
    result = compare_feature_series(
        extract_features(tone_a),
        extract_features(tone_b),
        project_id="p",
        artifact_a_id="a",
        artifact_b_id="b",
        analysis_run_id="r",
        same_generation_event=False,
        comparable_region_a_s=region_a,
        comparable_region_b_s=region_b,
    )

    assert result.comparable_region_a_s == region_a
    assert result.comparable_region_b_s == region_b
    assert result.hotspots
    assert all(
        region_a[0] <= item.a_start_s < item.a_end_s <= region_a[1]
        for item in result.hotspots
    )
    assert all(
        region_b[0] <= item.b_start_s < item.b_end_s <= region_b[1]
        for item in result.hotspots
    )


def test_same_generation_event_disables_parameter_attribution(tone_a, tone_b):
    result = compare_audio_files(
        tone_a,
        tone_b,
        project_id="p",
        artifact_a_id="a",
        artifact_b_id="b",
        analysis_run_id="r",
        same_generation_event=True,
    )
    assert result.same_generation_event
    assert not result.parameter_attribution_allowed


def test_loudness_matching_only_attenuates_louder_item():
    gain_a, gain_b = loudness_match_gain_db(-12.0, -18.0)
    assert gain_a == pytest.approx(-6.0)
    assert gain_b == 0.0
    assert gain_a <= 0 and gain_b <= 0


def test_structure_has_start_end_and_internal_boundaries(tone_a):
    features = extract_features(tone_a)
    structure = analyze_structure(features, "a", "r", min_segment_s=1.0)
    assert structure.boundaries[0].time_s == 0
    assert structure.boundaries[-1].time_s == pytest.approx(8.0)
    assert len(structure.boundaries) >= 3
    assert structure.segments
    assert structure.segments[0].start_s == 0
    assert structure.segments[-1].end_s == pytest.approx(8.0)
    assert all(segment.repeat_group for segment in structure.segments)


def test_deterministic_crop_uses_measured_samples(tone_a, tmp_path):
    samples, sample_rate = sf.read(tone_a)
    child = tmp_path / "crop.wav"
    start = int(0.5 * sample_rate)
    sf.write(child, samples[start : start + int(4 * sample_rate)], sample_rate)
    result = verify_deterministic_crop(
        tone_a,
        child,
        parent_artifact_id="parent",
        child_artifact_id="child",
        analysis_run_id="r",
        correlation_threshold=0.995,
    )
    assert result.verified
    assert result.lag_s == pytest.approx(0.5, abs=0.005)
    assert result.retained_region_correlation > 0.995
    assert result.used_measured_audio


def test_deterministic_crop_accepts_a_sample_identical_middle_region(tmp_path):
    sample_rate = 16_000
    rng = np.random.default_rng(7)
    parent_samples = rng.normal(0, 0.08, sample_rate * 30)
    for second, gain in ((2, 0.4), (7, 0.9), (13, 0.5), (19, 0.8), (26, 0.3)):
        start = second * sample_rate
        frames = np.arange(sample_rate // 2)
        parent_samples[start : start + len(frames)] += gain * np.sin(
            2 * np.pi * 440 * frames / sample_rate
        )
    parent = tmp_path / "parent.wav"
    child = tmp_path / "middle.wav"
    sf.write(parent, parent_samples, sample_rate, subtype="PCM_16")
    sf.write(
        child,
        parent_samples[8 * sample_rate : 20 * sample_rate],
        sample_rate,
        subtype="PCM_16",
    )

    result = verify_deterministic_crop(
        parent,
        child,
        parent_artifact_id="parent",
        child_artifact_id="child",
        analysis_run_id="r",
    )

    assert result.verified
    assert result.lag_s == pytest.approx(8.0, abs=0.005)
    assert result.retained_region_correlation > 0.995


def test_empty_audio_is_rejected_before_metrics_can_contain_nan(tmp_path):
    empty = tmp_path / "empty.wav"
    sf.write(empty, np.zeros((0, 2), dtype=np.float32), 16_000)

    with pytest.raises(ValueError, match="no decodable frames"):
        measure_audio(
            empty,
            artifact_id="empty",
            analysis_run_id="run",
            acquisition_degraded=False,
        )
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"invalid": float("nan")})


def test_crop_rejects_longer_child(tone_a, tmp_path):
    samples, sample_rate = sf.read(tone_a)
    child = tmp_path / "longer.wav"
    sf.write(child, np.concatenate((samples, samples[:1000])), sample_rate)
    with pytest.raises(ValueError, match="longer"):
        verify_deterministic_crop(
            tone_a,
            child,
            parent_artifact_id="parent",
            child_artifact_id="child",
            analysis_run_id="r",
        )


def test_active_audio_at_file_boundary_is_flagged_without_declaring_a_defect():
    sample_rate = 16_000
    duration_s = 1.003
    time = np.arange(int(sample_rate * duration_s)) / sample_rate
    active = 0.4 * np.sin(2 * np.pi * 223.0 * time)
    result = diagnose_ending(
        active,
        sample_rate,
        trailing_silence_s=0.0,
    )
    assert result.classification in {
        "active_audio_at_boundary",
        "likely_abrupt_boundary",
    }
    assert result.status.value == "indeterminate"
    assert result.final_100ms_rms_dbfs > -35


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg is required to produce compressed audio fixtures",
)
@pytest.mark.parametrize(
    ("suffix", "codec_args"),
    [
        ("mp3", ["-codec:a", "libmp3lame"]),
        ("m4a", ["-codec:a", "aac"]),
        ("flac", ["-codec:a", "flac"]),
    ],
)
def test_supported_local_audio_formats_decode(tone_a, tmp_path, suffix, codec_args):
    converted = tmp_path / f"converted.{suffix}"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(tone_a),
            *codec_args,
            str(converted),
        ],
        check=True,
    )
    features = extract_features(converted)
    assert features.duration_s == pytest.approx(8.0, abs=0.1)


def test_external_audio_tool_timeouts_are_readable(tone_a, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("audio-tool", 1)

    def unsupported_by_soundfile(*_args, **_kwargs):
        raise RuntimeError("force external decoder")

    monkeypatch.setattr("songeval.audio.sf.info", unsupported_by_soundfile)
    monkeypatch.setattr("songeval.audio.subprocess.run", timeout)
    with pytest.raises(ValueError, match="audio inspection timed out"):
        probe_encoding(tone_a)

    monkeypatch.setattr("songeval.audio.sf.read", unsupported_by_soundfile)
    monkeypatch.setattr(
        "songeval.audio.probe_encoding",
        lambda _path: (
            EncodingFingerprint(sample_rate_hz=16_000, channels=2),
            1.0,
        ),
    )
    with pytest.raises(ValueError, match="audio decoding timed out"):
        extract_features(tone_a)
