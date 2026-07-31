from __future__ import annotations

from conftest import candidate

from songeval.enums import (
    Axis,
    DefectTier,
    RecommendationStatus,
)
from songeval.models import Defect
from songeval.recommendation import recommend, record_user_override


def run(items, policy, **updates):
    values = {
        "listening_round_valid": True,
        "cross_brief_target_compliance_complete": True,
    }
    values.update(updates)
    return recommend(items, policy=policy, **values)


def test_missing_policy_abstains():
    result = run([candidate("a")], None)
    assert result.status == RecommendationStatus.ABSTAIN
    assert result.recommended_artifact_id is None
    assert any("Policy" in gap for gap in result.evidence_gaps)


def test_probe_failure_abstains_even_with_clear_candidate(complete_policy):
    result = run(
        [candidate("a"), candidate("b", craft=0)],
        complete_policy,
        listening_round_valid=False,
    )
    assert result.status == RecommendationStatus.ABSTAIN
    assert any("probes failed" in gap for gap in result.evidence_gaps)


def test_cross_brief_without_target_compliance_abstains(complete_policy):
    result = run(
        [candidate("a", brief_id="one"), candidate("b", brief_id="two")],
        complete_policy,
        cross_brief_target_compliance_complete=False,
    )
    assert result.status == RecommendationStatus.ABSTAIN
    assert any("cross-Brief" in gap for gap in result.evidence_gaps)


def test_compliance_na_ceiling_abstains(complete_policy):
    result = run(
        [candidate("a", compliance=None), candidate("b")],
        complete_policy,
    )
    assert result.status == RecommendationStatus.ABSTAIN
    assert any("N/A" in gap for gap in result.evidence_gaps)


def test_zero_survivors_common_mode_never_falls_back(complete_policy):
    shared = [
        Defect(
            project_id="project_test",
            artifact_id=artifact_id,
            brief_id="brief_test",
            code="wrong_hook",
            tier=DefectTier.T1,
            description="wrong burden lyric",
            confirmed=True,
            hard_requirement=True,
            evidence_source="human",
            common_mode=True,
        )
        for artifact_id in ("a", "b")
    ]
    result = run(
        [
            candidate("a", defects=(shared[0],)),
            candidate("b", craft=3, defects=(shared[1],)),
        ],
        complete_policy,
    )
    assert result.status == RecommendationStatus.NO_RELEASE_CANDIDATE
    assert result.recommended_artifact_id is None
    assert result.common_mode_issue
    assert result.zero_survivor_cause == "brief_or_model_common_mode"


def test_single_survivor_is_not_called_preference_recommendation(complete_policy):
    bad = Defect(
        project_id="project_test",
        artifact_id="b",
        brief_id="brief_test",
        code="glitch",
        tier=DefectTier.T1,
        description="glitch",
        confirmed=True,
        evidence_source="human",
    )
    result = run(
        [candidate("a"), candidate("b", defects=(bad,))],
        complete_policy,
    )
    assert result.status == RecommendationStatus.UNIQUE_SURVIVOR
    assert result.recommended_artifact_id == "a"


def test_single_take_representative_is_a_unique_survivor(complete_policy):
    result = run(
        [
            candidate("master", take_id="same-take", craft=3),
            candidate("alternate", take_id="same-take", craft=1),
        ],
        complete_policy,
    )
    assert result.status == RecommendationStatus.UNIQUE_SURVIVOR
    assert result.recommended_artifact_id == "master"


def test_all_axes_tie_causes_abstention(complete_policy):
    result = run([candidate("a"), candidate("b")], complete_policy)
    assert result.status == RecommendationStatus.ABSTAIN
    assert result.recommended_artifact_id is None


def test_lexical_priority_selects_craft_without_total_score(complete_policy):
    result = run(
        [candidate("a", craft=3), candidate("b", craft=1)],
        complete_policy,
    )
    assert result.status == RecommendationStatus.RECOMMENDED
    assert result.recommended_artifact_id == "a"
    assert result.alternate_artifact_id == "b"
    assert result.user_declared_priority
    assert any("craft" in reason for reason in result.rationale)
    assert any("12.00s" in cost for cost in result.alternate_costs)


def test_alternate_cost_uses_the_deciding_observation_timestamp(complete_policy):
    winner = candidate("winner", craft=3)
    alternate = candidate("alternate", craft=1)
    craft = winner.evaluation_for(Axis.CRAFT)
    assert craft is not None
    winner = winner.model_copy(
        update={
            "evaluations": (
                winner.evaluations[0],
                craft.model_copy(
                    update={
                        "observations": (
                            craft.observations[0].model_copy(
                                update={
                                    "criterion": "non_deciding",
                                    "value": 2,
                                    "start_s": 5.0,
                                }
                            ),
                            craft.observations[0].model_copy(
                                update={
                                    "criterion": "structure",
                                    "value": 3,
                                    "start_s": 12.0,
                                }
                            ),
                        )
                    }
                ),
                *winner.evaluations[2:],
            )
        }
    )
    alternate_craft = alternate.evaluation_for(Axis.CRAFT)
    assert alternate_craft is not None
    alternate = alternate.model_copy(
        update={
            "evaluations": (
                alternate.evaluations[0],
                alternate_craft.model_copy(
                    update={
                        "observations": (
                            alternate_craft.observations[0].model_copy(
                                update={
                                    "criterion": "non_deciding",
                                    "value": 2,
                                    "start_s": 5.0,
                                }
                            ),
                            alternate_craft.observations[0].model_copy(
                                update={
                                    "criterion": "structure",
                                    "value": 1,
                                    "start_s": 12.0,
                                }
                            ),
                        )
                    }
                ),
                *alternate.evaluations[2:],
            )
        }
    )
    result = run([winner, alternate], complete_policy)
    assert result.recommended_artifact_id == "winner"
    assert result.alternate_costs == ("alternate loses on craft at 12.00s",)


def test_alternate_is_ranked_instead_of_using_input_order(complete_policy):
    result = run(
        [
            candidate("winner", craft=3),
            candidate("third", craft=0),
            candidate("runner_up", craft=2),
        ],
        complete_policy,
    )
    assert result.recommended_artifact_id == "winner"
    assert result.alternate_artifact_id == "runner_up"


def test_distinctiveness_never_makes_different_equal_better(complete_policy):
    # Factory gives both the same quality views; the descriptive axis cannot
    # break the tie even if its observations were different.
    a = candidate("a")
    b = candidate("b")
    b_distinct = b.evaluations[-1].model_copy(
        update={
            "observations": (
                b.evaluations[-1].observations[0].model_copy(update={"value": 3}),
            )
        }
    )
    b = b.model_copy(update={"evaluations": (*b.evaluations[:-1], b_distinct)})
    result = run([a, b], complete_policy)
    assert result.status == RecommendationStatus.ABSTAIN
    assert result.recommended_artifact_id is None


def test_user_override_is_recorded_without_rewriting_policy_result(complete_policy):
    original = run(
        [candidate("a", craft=3), candidate("b", craft=1)],
        complete_policy,
    )
    recorded = record_user_override(
        original,
        artifact_id="b",
        reason="personal preference",
    )
    assert recorded.recommended_artifact_id == "a"
    assert recorded.policy_id == original.policy_id
    assert recorded.user_final_choice == "b"
    assert recorded.user_override_reason == "personal preference"


def test_cross_brief_flag_cannot_fake_missing_vs_target(complete_policy):
    result = run(
        [candidate("a", brief_id="one"), candidate("b", brief_id="two")],
        complete_policy,
        cross_brief_target_compliance_complete=True,
    )
    assert result.status == RecommendationStatus.ABSTAIN
    assert any("cross-Brief" in gap for gap in result.evidence_gaps)


def test_cross_brief_can_compare_only_with_explicit_vs_target(complete_policy):
    a = candidate("a", brief_id="one", craft=3)
    b = candidate("b", brief_id="two", craft=1)
    a = a.model_copy(
        update={
            "compliance_as_generated": a.evaluations[0],
            "compliance_vs_target": a.evaluations[0],
        }
    )
    b = b.model_copy(
        update={
            "compliance_as_generated": b.evaluations[0],
            "compliance_vs_target": b.evaluations[0],
        }
    )
    result = run(
        [a, b],
        complete_policy,
        cross_brief_target_compliance_complete=True,
    )
    assert result.status == RecommendationStatus.RECOMMENDED
    assert result.recommended_artifact_id == "a"
