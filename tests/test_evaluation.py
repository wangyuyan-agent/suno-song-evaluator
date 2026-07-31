from __future__ import annotations

from songeval.enums import (
    DefectTier,
    EvaluationStatus,
    OperationType,
    PreservationIntent,
    ProtectedDimension,
    ReadinessStatus,
    TargetPlacement,
)
from songeval.evaluation import (
    evaluate_compliance,
    evaluate_distinctiveness,
    evaluate_release_readiness,
    promote_common_mode_defects,
)
from songeval.models import (
    Defect,
    DifferenceHotspot,
    OrdinalObservation,
    PairwiseComparison,
    PreservationDirective,
    PreservationEvaluation,
    PreservationThresholds,
    ReleaseAction,
)


def defect(
    artifact_id: str,
    *,
    code: str = "same",
    tier: DefectTier = DefectTier.T1,
    hard: bool = True,
) -> Defect:
    return Defect(
        project_id="project_test",
        artifact_id=artifact_id,
        brief_id="brief_test",
        code=code,
        tier=tier,
        description=code,
        confirmed=True,
        hard_requirement=hard,
        evidence_source="human",
    )


def test_common_mode_is_promoted_only_when_all_brief_candidates_share_it():
    defects = [defect("a"), defect("b"), defect("b", code="unique")]
    promoted = promote_common_mode_defects(
        defects,
        {"brief_test": {"a", "b"}},
    )
    assert [item.common_mode for item in promoted if item.code == "same"] == [
        True,
        True,
    ]
    assert not next(item for item in promoted if item.code == "unique").common_mode


def test_compliance_hard_failure_and_unknown_are_not_reweighted(
    brief,
    complete_policy,
):
    failed = evaluate_compliance(
        artifact_id="a",
        brief=brief,
        requirement_observations={
            "lyrics": OrdinalObservation(
                criterion="lyrics",
                value=0,
                evidence="human confirmed",
            ),
            "style": OrdinalObservation(
                criterion="style",
                value=3,
                evidence="human",
            ),
        },
        policy=complete_policy,
    )
    assert failed.status == EvaluationStatus.FAIL
    unknown = evaluate_compliance(
        artifact_id="a",
        brief=brief,
        requirement_observations={
            "style": OrdinalObservation(
                criterion="style",
                value=3,
                evidence="human",
            )
        },
        policy=complete_policy,
    )
    assert unknown.status == EvaluationStatus.INDETERMINATE
    assert any("lyrics" in gap for gap in unknown.evidence_gaps)


def test_policy_can_make_unverified_preservation_descriptive(
    brief,
    complete_policy,
):
    directive = PreservationDirective(
        id="directive_exact",
        project_id="project_test",
        brief_id=brief.id,
        reference_segment_id="reference",
        preservation_intent=PreservationIntent.EXACT_AUDIO,
        protected_dimensions=(ProtectedDimension.MELODY,),
        target_placement=TargetPlacement.GLOBAL_CONDITIONER,
        placement_enforceable=False,
        must_preserve=True,
        thresholds=PreservationThresholds(),
        abstention_strategy="abstain",
    )
    result = PreservationEvaluation(
        directive_id=directive.id,
        artifact_id="a",
        status=EvaluationStatus.INDETERMINATE,
        evidence_gaps=("retention evidence unavailable",),
    )
    observations = {
        requirement.id: OrdinalObservation(
            criterion=requirement.id,
            value=3,
            evidence="human",
        )
        for requirement in brief.requirements
    }
    optional_policy = complete_policy.model_copy(
        update={
            "compliance_floor": complete_policy.compliance_floor.model_copy(
                update={"must_preserve_directives_must_pass": False}
            )
        }
    )

    optional = evaluate_compliance(
        artifact_id="a",
        brief=brief,
        requirement_observations=observations,
        policy=optional_policy,
        directives=(directive,),
        preservation_results=(result,),
    )
    required = evaluate_compliance(
        artifact_id="a",
        brief=brief,
        requirement_observations=observations,
        policy=complete_policy,
        directives=(directive,),
        preservation_results=(result,),
    )

    assert optional.status == EvaluationStatus.PASS
    assert not any(
        item.criterion.startswith("preservation:") for item in optional.observations
    )
    assert required.status == EvaluationStatus.INDETERMINATE


def test_not_evaluated_required_preservation_is_a_critical_unknown(
    brief,
    complete_policy,
):
    directive = PreservationDirective(
        id="directive_exact",
        project_id="project_test",
        brief_id=brief.id,
        reference_segment_id="reference",
        preservation_intent=PreservationIntent.EXACT_AUDIO,
        protected_dimensions=(ProtectedDimension.MELODY,),
        target_placement=TargetPlacement.GLOBAL_CONDITIONER,
        placement_enforceable=False,
        must_preserve=True,
        thresholds=PreservationThresholds(),
        abstention_strategy="abstain",
    )
    observations = {
        requirement.id: OrdinalObservation(
            criterion=requirement.id,
            value=3,
            evidence="human",
        )
        for requirement in brief.requirements
    }
    result = evaluate_compliance(
        artifact_id="a",
        brief=brief,
        requirement_observations=observations,
        policy=complete_policy,
        directives=(directive,),
        preservation_results=(
            PreservationEvaluation(
                directive_id=directive.id,
                artifact_id="a",
                status=EvaluationStatus.NOT_EVALUATED,
                evidence_gaps=("not run",),
            ),
        ),
    )
    assert result.status == EvaluationStatus.INDETERMINATE


def test_release_readiness_is_ready_without_t2():
    result = evaluate_release_readiness(
        artifact_id="a",
        defects=[],
        release_actions=[],
    )
    assert result.readiness == ReadinessStatus.READY
    assert result.status == EvaluationStatus.PASS


def test_release_readiness_waits_for_technical_boundary_confirmation():
    result = evaluate_release_readiness(
        artifact_id="a",
        defects=[],
        release_actions=[],
        technical_evidence_gaps=[
            "ending boundary has active audio; human confirmation is required"
        ],
    )
    assert result.readiness == ReadinessStatus.INDETERMINATE
    assert result.status == EvaluationStatus.INDETERMINATE
    assert "ending boundary" in result.evidence_gaps[0]


def test_release_readiness_does_not_duplicate_t1():
    result = evaluate_release_readiness(
        artifact_id="a",
        defects=[defect("a")],
        release_actions=[],
    )
    assert result.readiness == ReadinessStatus.NOT_ELIGIBLE
    assert result.status == EvaluationStatus.NOT_EVALUATED


def test_t2_without_verified_edit_is_indeterminate():
    result = evaluate_release_readiness(
        artifact_id="a",
        defects=[defect("a", tier=DefectTier.T2, hard=False)],
        release_actions=[],
    )
    assert result.readiness == ReadinessStatus.INDETERMINATE


def test_t2_with_verified_soft_island_needs_suno_edit():
    result = evaluate_release_readiness(
        artifact_id="a",
        defects=[defect("a", tier=DefectTier.T2, hard=False)],
        release_actions=[
            ReleaseAction(
                operation=OperationType.REPLACE_SECTION,
                target_interval_s=(10, 20),
                feasibility_verified=True,
                protected_island_status="soft_island",
                note="tested",
            )
        ],
    )
    assert result.readiness == ReadinessStatus.NEEDS_SUNO_EDIT


def test_t2_with_no_island_is_blocked():
    result = evaluate_release_readiness(
        artifact_id="a",
        defects=[defect("a", tier=DefectTier.T2, hard=False)],
        release_actions=[
            ReleaseAction(
                operation=OperationType.REPLACE_SECTION,
                target_interval_s=(10, 20),
                feasibility_verified=True,
                protected_island_status="none",
                note="overlaps protected region",
            )
        ],
    )
    assert result.readiness == ReadinessStatus.BLOCKED


def test_distinctiveness_is_computed_but_non_directional():
    comparison = PairwiseComparison(
        project_id="project_test",
        artifact_a_id="a",
        artifact_b_id="b",
        analysis_run_id="r",
        comparable_region_a_s=(0, 10),
        comparable_region_b_s=(0, 10),
        pitch_harmony_distance=0.3,
        rhythm_onset_distance=0.2,
        energy_structure_distance=0.1,
        hotspots=(
            DifferenceHotspot(
                a_start_s=1,
                a_end_s=2,
                b_start_s=1,
                b_end_s=2,
                feature_family="pitch_harmony",
                magnitude=0.3,
                evidence="computed",
            ),
        ),
        same_generation_event=False,
        parameter_attribution_allowed=True,
    )
    result = evaluate_distinctiveness("a", [comparison])
    assert result.status == EvaluationStatus.PASS
    assert result.ignored_for_ordering
    assert all(item.evidence.startswith("computed") for item in result.observations)
