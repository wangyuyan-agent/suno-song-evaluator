from __future__ import annotations

import pytest

from songeval.enums import (
    PreservationIntent,
    ProtectedDimension,
    TargetPlacement,
)
from songeval.migration import (
    plan_structural_gesture_replace,
    recommend_suno_workflow,
)
from songeval.models import (
    PreservationDirective,
    PreservationThresholds,
    ReleaseArtifact,
)


def test_b_route_keeps_target_and_does_not_attach_sample():
    directive = PreservationDirective(
        id="directive",
        project_id="p",
        brief_id="b",
        reference_segment_id="r",
        preservation_intent=PreservationIntent.STRUCTURAL_GESTURE,
        protected_dimensions=(
            ProtectedDimension.STRUCTURE_SHAPE,
            ProtectedDimension.ENERGY_ENVELOPE,
        ),
        target_placement=TargetPlacement.SECTION_RELATIVE,
        placement_enforceable=True,
        must_preserve=False,
        thresholds=PreservationThresholds(),
        abstention_strategy="prefer, do not gate",
    )
    target = ReleaseArtifact(id="target", project_id="p", take_id="take")
    plan = plan_structural_gesture_replace(
        directive=directive,
        target_artifact=target,
        prompt="one-beat breath, immediate chorus",
        frozen_lyrics_excerpt="一枝也没折\n雨落得慢",
    )
    assert plan.operation.value == "replace_section"
    assert plan.source_handling == "do_not_attach_reference_as_sample"
    assert plan.fallback_artifact_id == "target"
    assert plan.max_batches == 2
    assert plan.takes_per_batch == 2
    assert not plan.exact_retention_claimed
    assert not plan.external_postproduction_required
    assert plan.workflow_surface == "song_editor"
    assert set(plan.source_rules) == {
        "use_target_as_edit_parent",
        "do_not_attach_reference_as_sample",
    }
    assert plan.steps

    recommendation = recommend_suno_workflow(
        directive=directive,
        target_artifact=target,
        prompt="one-beat breath, immediate chorus",
        subscription_tier="pro",
        studio_available=False,
    )
    assert recommendation.status == "actionable"
    assert recommendation.plan is not None
    assert recommendation.plan.workflow_surface == "song_editor"


def test_exact_reference_abstains_instead_of_promising_sample_placement():
    directive = PreservationDirective(
        id="exact",
        project_id="p",
        brief_id="b",
        reference_segment_id="r",
        preservation_intent=PreservationIntent.EXACT_AUDIO,
        protected_dimensions=(ProtectedDimension.MELODY,),
        target_placement=TargetPlacement.GLOBAL_CONDITIONER,
        placement_enforceable=False,
        must_preserve=True,
        thresholds=PreservationThresholds(),
        abstention_strategy="abstain",
    )
    target = ReleaseArtifact(id="target", project_id="p", take_id="take")
    recommendation = recommend_suno_workflow(
        directive=directive,
        target_artifact=target,
        prompt="keep this exact melody",
        subscription_tier="pro",
        studio_available=False,
    )
    assert recommendation.status == "abstain"
    assert recommendation.plan is None
    assert any(
        "cannot guarantee exact placement" in item for item in recommendation.rationale
    )


def test_exact_reference_uses_the_reported_subscription_tier_in_fallback():
    directive = PreservationDirective(
        id="exact-tier",
        project_id="p",
        brief_id="b",
        reference_segment_id="r",
        preservation_intent=PreservationIntent.EXACT_AUDIO,
        protected_dimensions=(ProtectedDimension.MELODY,),
        target_placement=TargetPlacement.GLOBAL_CONDITIONER,
        placement_enforceable=False,
        must_preserve=True,
        thresholds=PreservationThresholds(),
        abstention_strategy="abstain",
    )
    target = ReleaseArtifact(id="target", project_id="p", take_id="take")
    recommendation = recommend_suno_workflow(
        directive=directive,
        target_artifact=target,
        prompt="retain exact material",
        subscription_tier="unknown",
        studio_available=False,
    )
    assert any("'unknown'" in item for item in recommendation.rationale)
    assert not any("on Pro" in item for item in recommendation.rationale)


def test_registered_directive_cannot_be_applied_to_another_target():
    directive = PreservationDirective(
        id="fixed",
        project_id="p",
        brief_id="b",
        reference_segment_id="r",
        target_artifact_id="registered-target",
        preservation_intent=PreservationIntent.STRUCTURAL_GESTURE,
        protected_dimensions=(ProtectedDimension.STRUCTURE_SHAPE,),
        target_placement=TargetPlacement.SECTION_RELATIVE,
        placement_enforceable=True,
        must_preserve=False,
        thresholds=PreservationThresholds(),
        abstention_strategy="prefer",
    )
    other = ReleaseArtifact(id="other", project_id="p", take_id="take")

    with pytest.raises(ValueError, match="different target"):
        recommend_suno_workflow(
            directive=directive,
            target_artifact=other,
            prompt="one-beat breath",
        )
