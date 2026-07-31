from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from songeval.analyzer import ProjectAnalyzer
from songeval.audio import FeatureSeries
from songeval.enums import (
    BoundaryQuality,
    EvaluationStatus,
    PreservationIntent,
    ProtectedDimension,
    TargetPlacement,
)
from songeval.models import (
    PreservationDirective,
    PreservationThresholds,
    ReferenceSegment,
    StructureAnalysis,
    StructureBoundary,
)
from songeval.reference import (
    ProtectedIslandTracker,
    _sequence_similarity,
    _sliding_matches,
    analyze_reference_facts,
    evaluate_exact_or_melody_retention,
    evaluate_structural_gesture,
    preflight_reference,
    preservation_enters_compliance,
)


def directive(
    intent: PreservationIntent,
    *,
    must_preserve: bool = False,
    thresholds: PreservationThresholds | None = None,
) -> PreservationDirective:
    return PreservationDirective(
        id=f"directive_{intent.value}",
        project_id="p",
        brief_id="b",
        reference_segment_id="r",
        preservation_intent=intent,
        protected_dimensions=(
            ProtectedDimension.MELODY,
            ProtectedDimension.RHYTHM,
        ),
        target_placement=TargetPlacement.SECTION_RELATIVE,
        placement_enforceable=True,
        must_preserve=must_preserve,
        thresholds=thresholds or PreservationThresholds(),
        abstention_strategy="abstain",
    )


def test_analyzer_reports_unknown_reference_segment(minimal_manifest):
    missing = PreservationDirective(
        id="directive_missing_reference",
        project_id=minimal_manifest.project_id,
        brief_id=minimal_manifest.briefs[0].id,
        reference_segment_id="reference_missing",
        target_artifact_id=minimal_manifest.artifacts[0].id,
        preservation_intent=PreservationIntent.STRUCTURAL_GESTURE,
        protected_dimensions=(ProtectedDimension.STRUCTURE_SHAPE,),
        target_placement=TargetPlacement.SECTION_RELATIVE,
        placement_enforceable=True,
        must_preserve=False,
        thresholds=PreservationThresholds(),
        abstention_strategy="abstain",
    )
    manifest = minimal_manifest.model_copy(update={"directives": [missing]})
    with pytest.raises(
        ValueError,
        match="directive directive_missing_reference references unknown reference",
    ):
        ProjectAnalyzer(manifest).analyze()


def test_current_like_reference_hits_all_three_risk_classes():
    segment = ReferenceSegment(
        id="r",
        project_id="p",
        source_artifact_id="a",
        start_s=0,
        end_s=16.36,
        start_boundary=BoundaryQuality.MID_PHRASE,
        end_boundary=BoundaryQuality.MID_WORD,
        semantic_roles=("bridge_close", "rest", "lift", "chorus_entry"),
        internal_homogeneity=0.42,
        structure_section_count=2,
    )
    result = preflight_reference(segment, target_song_duration_s=248.48)
    codes = {item.code for item in result.findings}
    assert result.dirty_boundaries
    assert result.multi_state
    assert result.crosses_sections
    assert "dirty_boundaries" in codes
    assert "multiple_statistical_states" in codes
    assert "crosses_structure_sections" in codes


def test_preflight_ignores_unknown_or_zero_target_duration():
    segment = ReferenceSegment(
        id="r",
        project_id="p",
        source_artifact_id="a",
        start_s=0,
        end_s=10,
    )

    result = preflight_reference(segment, target_song_duration_s=0)

    assert "short_relative_to_target" not in {
        finding.code for finding in result.findings
    }


def test_exact_retention_without_null_control_is_indeterminate(tone_a):
    result = evaluate_exact_or_melody_retention(
        directive(PreservationIntent.MELODY_RHYTHM),
        artifact_id="a",
        reference_path=str(tone_a),
        target_path=str(tone_a),
        null_baselines=None,
        batch_variance_floors=None,
    )
    assert result.status == EvaluationStatus.INDETERMINATE
    assert {item.family for item in result.family_results} == {
        "pitch_harmony",
        "rhythm_onset",
    }
    assert any("missing null control" in gap for gap in result.evidence_gaps)


def test_exact_retention_pass_needs_two_families_and_all_controls(
    tone_a,
    tone_b,
    tmp_path,
):
    reference, sample_rate = sf.read(tone_a)
    other, other_rate = sf.read(tone_b)
    assert sample_rate == other_rate
    target = tmp_path / "target-with-one-reference.wav"
    sf.write(
        target,
        np.concatenate((other, reference, other)),
        sample_rate,
    )
    result = evaluate_exact_or_melody_retention(
        directive(
            PreservationIntent.MELODY_RHYTHM,
            thresholds=PreservationThresholds(
                min_feature_families=2,
                min_coverage_ratio=0.95,
                min_best_to_second_margin=0.05,
                min_null_margin=0.05,
                min_batch_variance_margin=0.05,
            ),
        ),
        artifact_id="target",
        reference_path=str(tone_a),
        target_path=str(target),
        null_baselines={"pitch_harmony": 0.1, "rhythm_onset": 0.1},
        batch_variance_floors={"pitch_harmony": 0.1, "rhythm_onset": 0.1},
        acquisition_comparable=True,
    )
    assert result.status == EvaluationStatus.PASS
    assert len(result.family_results) == 2
    assert all(item.status == EvaluationStatus.PASS for item in result.family_results)


def test_exact_retention_with_controls_still_abstains_for_uncomparable_paths(
    tone_a,
):
    result = evaluate_exact_or_melody_retention(
        directive(
            PreservationIntent.MELODY_RHYTHM,
            thresholds=PreservationThresholds(
                min_feature_families=2,
                min_coverage_ratio=0.9,
                min_best_to_second_margin=0.01,
                min_null_margin=0.01,
                min_batch_variance_margin=0.01,
            ),
        ),
        artifact_id="a",
        reference_path=str(tone_a),
        target_path=str(tone_a),
        null_baselines={"pitch_harmony": 0.0, "rhythm_onset": 0.0},
        batch_variance_floors={"pitch_harmony": 0.0, "rhythm_onset": 0.0},
        acquisition_comparable=False,
    )
    assert result.status == EvaluationStatus.INDETERMINATE
    assert any("acquisition paths" in gap for gap in result.evidence_gaps)


def test_sequence_similarity_discards_non_finite_constant_correlations():
    constant = np.ones(20)
    assert _sequence_similarity(constant, constant) == float("-inf")


def test_sliding_match_coverage_reflects_supported_frame_fraction():
    reference = np.sin(np.linspace(0, 4 * np.pi, 40))
    exact = _sliding_matches(reference, reference, 0.1, 0.1)
    partial_target = reference.copy()
    partial_target[::2] += 8
    partial = _sliding_matches(reference, partial_target, 0.1, 0.1)

    assert exact[0][3] == pytest.approx(1.0)
    assert 0.0 < partial[0][3] < exact[0][3]


def test_structural_gesture_uses_objective_valley_step_and_boundary(monkeypatch):
    monkeypatch.setattr(
        "songeval.reference.evaluate_exact_or_melody_retention",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("similarity evaluator must not be called")
        ),
    )
    times = np.arange(0, 10, 0.5)
    energy = np.zeros(len(times))
    energy[8:11] = -10
    energy[11:] = 5
    features = FeatureSeries(
        times_s=times,
        chroma=np.ones((len(times), 12)) / np.sqrt(12),
        onset=np.zeros(len(times)),
        energy_db=energy,
        centroid_hz=np.ones(len(times)) * 400,
        duration_s=10,
        hop_s=0.5,
    )
    structure = StructureAnalysis(
        artifact_id="a",
        analysis_run_id="run",
        boundaries=(
            StructureBoundary(time_s=0, confidence=1),
            StructureBoundary(time_s=5.5, confidence=0.9),
            StructureBoundary(time_s=10, confidence=1),
        ),
        feature_hop_s=0.5,
    )
    result = evaluate_structural_gesture(
        directive(
            PreservationIntent.STRUCTURAL_GESTURE,
            thresholds=PreservationThresholds(
                gesture_valley_duration_s=1.0,
                gesture_min_step_db=8.0,
                gesture_max_delay_s=1.0,
                noise_band=0.1,
            ),
        ),
        artifact_id="a",
        features=features,
        structure=structure,
        target_window_s=(3.0, 8.0),
    )
    assert result.status == EvaluationStatus.PASS
    assert all(
        value == EvaluationStatus.PASS for value in result.objective_checks.values()
    )
    assert result.family_results == ()


def test_structural_gesture_without_interior_boundary_is_indeterminate():
    times = np.arange(0, 10, 0.5)
    energy = np.zeros(len(times))
    energy[8:11] = -10
    energy[11:] = 5
    features = FeatureSeries(
        times_s=times,
        chroma=np.ones((len(times), 12)) / np.sqrt(12),
        onset=np.zeros(len(times)),
        energy_db=energy,
        centroid_hz=np.ones(len(times)) * 400,
        duration_s=10,
        hop_s=0.5,
    )
    structure = StructureAnalysis(
        artifact_id="a",
        analysis_run_id="run",
        boundaries=(
            StructureBoundary(time_s=0.1, confidence=1),
            StructureBoundary(time_s=9.9, confidence=1),
        ),
        feature_hop_s=0.5,
    )
    result = evaluate_structural_gesture(
        directive(
            PreservationIntent.STRUCTURAL_GESTURE,
            thresholds=PreservationThresholds(
                gesture_valley_duration_s=1.0,
                gesture_min_step_db=8.0,
                gesture_max_delay_s=1.0,
                noise_band=0.1,
            ),
        ),
        artifact_id="a",
        features=features,
        structure=structure,
        target_window_s=(3.0, 8.0),
    )

    assert result.status == EvaluationStatus.INDETERMINATE
    assert (
        result.objective_checks["independent_structure_boundary"]
        == EvaluationStatus.INDETERMINATE
    )
    assert result.evidence_gaps == ("no interior structure boundary was detected",)


def test_structural_gesture_without_thresholds_abstains(tone_a):
    features = FeatureSeries(
        times_s=np.arange(4),
        chroma=np.ones((4, 12)),
        onset=np.zeros(4),
        energy_db=np.zeros(4),
        centroid_hz=np.zeros(4),
        duration_s=4,
        hop_s=1,
    )
    structure = StructureAnalysis(
        artifact_id="a",
        analysis_run_id="r",
        boundaries=(),
        feature_hop_s=1,
    )
    result = evaluate_structural_gesture(
        directive(PreservationIntent.STRUCTURAL_GESTURE),
        artifact_id="a",
        features=features,
        structure=structure,
        target_window_s=(0, 4),
    )
    assert result.status == EvaluationStatus.INDETERMINATE


def test_only_must_preserve_enters_compliance():
    optional = directive(PreservationIntent.MELODY_RHYTHM, must_preserve=False)
    required = directive(PreservationIntent.EXACT_AUDIO, must_preserve=True)
    from songeval.models import PreservationEvaluation

    evaluation = PreservationEvaluation(
        directive_id=required.id,
        artifact_id="a",
        status=EvaluationStatus.FAIL,
    )
    assert not preservation_enters_compliance(optional, evaluation)
    assert preservation_enters_compliance(required, evaluation)


def test_protected_island_never_uses_rejected_child_as_parent():
    tracker = ProtectedIslandTracker("original", max_accepted_edits=5)
    tracker.record("bad1", accepted=False, retention_vs_original=0.8)
    assert tracker.next_parent_id == "original"
    tracker.record("good1", accepted=True, retention_vs_original=0.9)
    assert tracker.next_parent_id == "good1"
    tracker.record("bad2", accepted=False, retention_vs_original=0.7)
    assert tracker.next_parent_id == "good1"


def test_protected_island_stops_after_two_rejections():
    tracker = ProtectedIslandTracker("original")
    tracker.record("bad1", accepted=False, retention_vs_original=0.9)
    tracker.record("bad2", accepted=False, retention_vs_original=0.8)
    assert tracker.should_stop


def test_protected_island_stops_at_credit_budget():
    tracker = ProtectedIslandTracker("original", credit_budget=20)
    tracker.record(
        "candidate",
        accepted=True,
        retention_vs_original=0.9,
        credit_cost=20,
    )
    assert tracker.should_stop


def test_reference_objective_facts_are_measured(tone_a):
    segment = ReferenceSegment(
        id="r",
        project_id="p",
        source_artifact_id="a",
        start_s=1,
        end_s=6,
        start_boundary=BoundaryQuality.UNKNOWN,
        end_boundary=BoundaryQuality.UNKNOWN,
    )
    from songeval.audio import analyze_structure, extract_features

    features = extract_features(tone_a)
    structure = analyze_structure(features, "a", "run", min_segment_s=1)
    analyzed = analyze_reference_facts(
        segment,
        source_path=str(tone_a),
        analysis_run_id="run",
        features=features,
        structure=structure,
    )
    assert analyzed.silence_ratio is not None
    assert analyzed.internal_homogeneity is not None
    assert analyzed.local_energy_percentile is not None
    assert analyzed.structure_section_count is not None
    assert analyzed.analysis_run_id == "run"
    assert analyzed.evidence[-1].provenance.value == "inferred"
