from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from songeval.enums import ComparisonOutcome
from songeval.listening import (
    build_blind_session,
    build_listening_review,
    materialize_blind_media,
    merge_project_reviews,
    validate_listening_session,
)
from songeval.models import (
    DifferenceHotspot,
    ListeningResponse,
    OrdinalObservation,
    PairwiseComparison,
    ProjectReviewPacket,
)
from songeval.util import file_sha256


def comparison() -> PairwiseComparison:
    return PairwiseComparison(
        id="comparison_secret_title_v2",
        project_id="p",
        artifact_a_id="artifact_a",
        artifact_b_id="artifact_b",
        analysis_run_id="r",
        comparable_region_a_s=(0, 8),
        comparable_region_b_s=(0, 8),
        pitch_harmony_distance=0.2,
        rhythm_onset_distance=0.1,
        energy_structure_distance=0.1,
        hotspots=(
            DifferenceHotspot(
                a_start_s=1,
                a_end_s=4,
                b_start_s=1.5,
                b_end_s=4.5,
                feature_family="pitch_harmony",
                magnitude=0.3,
                evidence="test",
            ),
        ),
        same_generation_event=True,
        parameter_attribution_allowed=False,
    )


def bundle(tone_a, tone_b):
    return build_blind_session(
        project_id="p",
        comparisons=[comparison()],
        artifact_paths={
            "artifact_a": str(tone_a),
            "artifact_b": str(tone_b),
        },
    )


def test_blind_session_requires_a_real_comparison(tone_a):
    with pytest.raises(ValueError, match="at least two candidates"):
        build_blind_session(
            project_id="p",
            comparisons=[],
            artifact_paths={"artifact_a": str(tone_a)},
        )


def test_public_blind_payload_contains_no_unblinding_metadata(tone_a, tone_b):
    payload = bundle(tone_a, tone_b).public_payload()
    serialized = json.dumps(payload).lower()
    for forbidden in (
        "artifact_a",
        "artifact_b",
        "secret_title",
        "batch_index",
        "lineage",
        "generation_event",
        "full_duration",
        "probe_type",
    ):
        assert forbidden not in serialized
    assert payload["blinded"] is True


def test_session_contains_order_swap_and_both_probes(tone_a, tone_b):
    result = bundle(tone_a, tone_b)
    real = [trial for trial in result.session.trials if trial.probe_type == "real"]
    assert {trial.order for trial in real} == {"ab", "ba"}
    assert {trial.probe_type for trial in result.session.trials} >= {
        "a_vs_a",
        "loudness_variant",
    }


def test_probe_failure_invalidates_subjective_round(tone_a, tone_b):
    result = bundle(tone_a, tone_b)
    responses = [
        ListeningResponse(
            trial_id=trial.id,
            outcome=ComparisonOutcome.A
            if trial.probe_type == "a_vs_a"
            else ComparisonOutcome.TIE,
        )
        for trial in result.session.trials
    ]
    validation = validate_listening_session(result, responses)
    assert not validation.valid
    assert "A-vs-A probe was not rated tie" in validation.failures


def test_position_reversal_downgrades_to_tie(tone_a, tone_b):
    result = bundle(tone_a, tone_b)
    responses = []
    for trial in result.session.trials:
        outcome = (
            ComparisonOutcome.A if trial.probe_type == "real" else ComparisonOutcome.TIE
        )
        responses.append(ListeningResponse(trial_id=trial.id, outcome=outcome))
    validation = validate_listening_session(result, responses)
    assert validation.valid
    assert set(validation.pair_outcomes.values()) == {ComparisonOutcome.TIE}


def test_consistent_preference_survives_order_swap(tone_a, tone_b):
    result = bundle(tone_a, tone_b)
    responses = []
    for trial in result.session.trials:
        if trial.probe_type != "real":
            outcome = ComparisonOutcome.TIE
        else:
            outcome = (
                ComparisonOutcome.A if trial.order == "ab" else ComparisonOutcome.B
            )
        responses.append(ListeningResponse(trial_id=trial.id, outcome=outcome))
    validation = validate_listening_session(result, responses)
    assert validation.valid
    assert set(validation.pair_outcomes.values()) == {ComparisonOutcome.A}


def test_valid_blind_reasons_become_artifact_specific_craft_evidence(
    tone_a,
    tone_b,
):
    result = bundle(tone_a, tone_b)
    responses = []
    for trial in result.session.trials:
        if trial.probe_type != "real":
            outcome = ComparisonOutcome.TIE
            reason_tags = ()
        else:
            outcome = (
                ComparisonOutcome.A if trial.order == "ab" else ComparisonOutcome.B
            )
            reason_tags = ("warmth_fullness", "arrangement_harmony_development")
        responses.append(
            ListeningResponse(
                trial_id=trial.id,
                outcome=outcome,
                reason_tags=reason_tags,
                comment="末段和声更饱满" if trial.probe_type == "real" else None,
            )
        )
    validation, review = build_listening_review(result, responses)
    assert validation.valid
    assert review.listening_round_valid
    by_artifact = {item.artifact_id: item for item in review.artifact_reviews}
    observations_a = {
        item.criterion: item for item in by_artifact["artifact_a"].craft_observations
    }
    observations_b = {
        item.criterion: item for item in by_artifact["artifact_b"].craft_observations
    }
    assert observations_a["warmth_fullness"].value == 3
    assert observations_b["warmth_fullness"].value == 1
    assert (
        "末段和声更饱满" in observations_a["arrangement_harmony_development"].evidence
    )


def test_materialization_preserves_loudness_probe_delta_without_touching_originals(
    tone_a,
    tone_b,
    tmp_path,
):
    original_hashes = (file_sha256(tone_a), file_sha256(tone_b))
    result = bundle(tone_a, tone_b)
    outputs = materialize_blind_media(result, tmp_path / "blind")
    assert outputs
    assert original_hashes == (file_sha256(tone_a), file_sha256(tone_b))
    loudness_probe = next(
        trial
        for trial in result.session.trials
        if trial.probe_type == "loudness_variant"
    )
    left, _ = sf.read(outputs[loudness_probe.left.sample_id])
    right, _ = sf.read(outputs[loudness_probe.right.sample_id])
    ratio = np.sqrt(np.mean(right**2)) / np.sqrt(np.mean(left**2))
    assert ratio == pytest.approx(10 ** (6 / 20), rel=0.02)


def test_materialization_rejects_empty_stimulus_window(tone_a, tone_b, tmp_path):
    result = bundle(tone_a, tone_b)
    first = result.stimuli[0]
    invalid = first.model_copy(update={"start_s": 99.0, "end_s": 100.0})
    broken = type(result)(
        session=result.session,
        stimuli=tuple(
            invalid if item.sample_id == first.sample_id else item
            for item in result.stimuli
        ),
        trials=result.trials,
    )
    with pytest.raises(ValueError, match="no decodable frames"):
        materialize_blind_media(broken, tmp_path / "broken")


def test_merge_project_reviews_preserves_cross_brief_and_control_evidence():
    base = ProjectReviewPacket(
        project_id="p",
        target_requirement_observations={
            "artifact": {
                "base": OrdinalObservation(
                    criterion="base",
                    value=3,
                    evidence="current",
                )
            }
        },
        null_baselines={"directive": {"pitch": 0.2}},
    )
    evidence = ProjectReviewPacket(
        project_id="p",
        target_brief_id="brief_target",
        target_requirement_observations={
            "artifact": {
                "stored": OrdinalObservation(
                    criterion="stored",
                    value=2,
                    evidence="stored",
                )
            }
        },
        cross_brief_target_compliance_complete=True,
        null_baselines={"directive": {"rhythm": 0.3}},
        batch_variance_floors={"directive": {"pitch": 0.1}},
    )

    merged = merge_project_reviews(base, evidence)

    assert merged.target_brief_id == "brief_target"
    assert merged.cross_brief_target_compliance_complete
    assert set(merged.target_requirement_observations["artifact"]) == {
        "base",
        "stored",
    }
    assert merged.null_baselines["directive"] == {
        "pitch": 0.2,
        "rhythm": 0.3,
    }
    assert merged.batch_variance_floors["directive"] == {"pitch": 0.1}
