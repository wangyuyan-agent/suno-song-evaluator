from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .enums import (
    Axis,
    Confidence,
    DefectTier,
    EvaluationStatus,
    ReadinessStatus,
    RecommendationStatus,
)
from .models import (
    AxisEvaluation,
    CandidateAssessment,
    ProjectDecisionPolicy,
    RecommendationCard,
)


def record_user_override(
    card: RecommendationCard,
    *,
    artifact_id: str,
    reason: str | None = None,
) -> RecommendationCard:
    """Record the human decision without changing the policy or prior result."""
    return card.model_copy(
        update={
            "user_final_choice": artifact_id,
            "user_override_reason": reason,
        }
    )


def _axis_threshold(policy: ProjectDecisionPolicy, axis: Axis) -> int:
    match = next(
        (item for item in policy.axis_thresholds if item.axis == axis),
        None,
    )
    return match.ordinal_delta if match else 1


def _compare_observations(
    a: AxisEvaluation,
    b: AxisEvaluation,
    threshold: int,
) -> tuple[int, tuple[str, ...]]:
    by_criterion_a = {item.criterion: item for item in a.observations}
    by_criterion_b = {item.criterion: item for item in b.observations}
    common = sorted(set(by_criterion_a) & set(by_criterion_b))
    if not common:
        return 0, ()
    signs: set[int] = set()
    deciding_criteria: list[str] = []
    for criterion in common:
        value_a = by_criterion_a[criterion].value
        value_b = by_criterion_b[criterion].value
        if value_a is None or value_b is None:
            continue
        difference = value_a - value_b
        if abs(difference) >= threshold:
            signs.add(1 if difference > 0 else -1)
            deciding_criteria.append(criterion)
    # Within-axis conflicts are not summed into a fabricated winner.
    if len(signs) == 1:
        return next(iter(signs)), tuple(deciding_criteria)
    return 0, ()


def _compare_axis(
    a: AxisEvaluation,
    b: AxisEvaluation,
    policy: ProjectDecisionPolicy,
) -> tuple[int, tuple[str, ...]]:
    if a.ignored_for_ordering or b.ignored_for_ordering:
        return 0, ()
    if a.axis == Axis.RELEASE_READINESS:
        rank = {
            ReadinessStatus.BLOCKED: 0,
            ReadinessStatus.INDETERMINATE: 1,
            ReadinessStatus.NEEDS_SUNO_EDIT: 2,
            ReadinessStatus.READY: 3,
            ReadinessStatus.NOT_ELIGIBLE: -1,
            None: 1,
        }
        difference = rank[a.readiness] - rank[b.readiness]
        threshold = _axis_threshold(policy, a.axis)
        outcome = (
            1 if difference >= threshold else -1 if difference <= -threshold else 0
        )
        return outcome, ("readiness",) if outcome else ()
    return _compare_observations(a, b, _axis_threshold(policy, a.axis))


def _compare_candidates(
    a: CandidateAssessment,
    b: CandidateAssessment,
    policy: ProjectDecisionPolicy,
) -> tuple[int, str | None]:
    for axis in policy.axis_priority:
        evaluation_a = a.evaluation_for(axis)
        evaluation_b = b.evaluation_for(axis)
        if evaluation_a is None or evaluation_b is None:
            return 0, f"missing {axis.value} evaluation"
        if axis == Axis.COMPLIANCE and (
            evaluation_a.status == EvaluationStatus.INDETERMINATE
            or evaluation_b.status == EvaluationStatus.INDETERMINATE
        ):
            return 0, "critical Compliance evidence is indeterminate"
        comparison, _ = _compare_axis(evaluation_a, evaluation_b, policy)
        if comparison:
            return comparison, f"{axis.value} exceeds the declared noise threshold"
    return 0, None


def _t1_failures(candidate: CandidateAssessment) -> list:
    return [
        defect
        for defect in candidate.defects
        if defect.confirmed and defect.tier == DefectTier.T1
    ]


def _policy_preflight(
    policy: ProjectDecisionPolicy | None,
    candidates: tuple[CandidateAssessment, ...],
) -> tuple[bool, tuple[str, ...]]:
    gaps: list[str] = []
    if policy is None:
        gaps.append("ProjectDecisionPolicy is missing")
        return False, tuple(gaps)
    if not policy.declared_by_user:
        gaps.append("ProjectDecisionPolicy has not been fully declared by the user")
    if policy.compliance_floor is None:
        gaps.append("Compliance floor is missing")
    if policy.max_na_ratio is None:
        gaps.append("N/A ceiling is missing")
    for candidate in candidates:
        compliance = candidate.evaluation_for(Axis.COMPLIANCE)
        if compliance is None:
            gaps.append(f"{candidate.artifact_id}: Compliance is missing")
        elif (
            policy.max_na_ratio is not None
            and compliance.na_ratio > policy.max_na_ratio
        ):
            gaps.append(
                f"{candidate.artifact_id}: Compliance N/A exceeds policy ceiling"
            )
    return not gaps, tuple(gaps)


def _select_within_take(
    candidates: list[CandidateAssessment],
    policy: ProjectDecisionPolicy,
) -> CandidateAssessment | None:
    if len(candidates) == 1:
        return candidates[0]
    winners: list[CandidateAssessment] = []
    for candidate in candidates:
        if all(
            candidate is other or _compare_candidates(candidate, other, policy)[0] >= 0
            for other in candidates
        ):
            winners.append(candidate)
    return winners[0] if len(winners) == 1 else None


def recommend(
    candidates: Iterable[CandidateAssessment],
    *,
    policy: ProjectDecisionPolicy | None,
    listening_round_valid: bool,
    cross_brief_target_compliance_complete: bool,
) -> RecommendationCard:
    candidates = tuple(candidates)
    priority = policy.axis_priority if policy else ()
    policy_ok, policy_gaps = _policy_preflight(policy, candidates)
    if not candidates:
        return RecommendationCard(
            status=RecommendationStatus.ABSTAIN,
            recommended_artifact_id=None,
            alternate_artifact_id=None,
            policy_id=policy.id if policy else None,
            priority=priority,
            user_declared_priority=bool(policy and policy.priority_declared_by_user),
            rationale=("No candidates were supplied.",),
            alternate_costs=(),
            confidence=Confidence.INDETERMINATE,
            evidence_gaps=("candidate set is empty",),
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )
    if not listening_round_valid:
        policy_gaps += ("blind-listening probes failed; subjective round invalid",)
        policy_ok = False
    if len({candidate.brief_id for candidate in candidates}) > 1 and (
        not cross_brief_target_compliance_complete
        or any(candidate.compliance_vs_target is None for candidate in candidates)
    ):
        policy_gaps += ("cross-Brief compliance_vs_target is missing",)
        policy_ok = False
    if not policy_ok or policy is None:
        return RecommendationCard(
            status=RecommendationStatus.ABSTAIN,
            recommended_artifact_id=None,
            alternate_artifact_id=None,
            policy_id=policy.id if policy else None,
            priority=priority,
            user_declared_priority=bool(policy and policy.priority_declared_by_user),
            rationale=(
                "Analysis is available, but formal recommendation is withheld.",
            ),
            alternate_costs=(),
            confidence=Confidence.INDETERMINATE,
            evidence_gaps=policy_gaps,
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )

    survivors = [candidate for candidate in candidates if not _t1_failures(candidate)]
    if not survivors:
        all_t1 = [
            defect for candidate in candidates for defect in _t1_failures(candidate)
        ]
        common_hard = any(
            defect.common_mode and defect.hard_requirement for defect in all_t1
        )
        return RecommendationCard(
            status=RecommendationStatus.NO_RELEASE_CANDIDATE,
            recommended_artifact_id=None,
            alternate_artifact_id=None,
            policy_id=policy.id,
            priority=priority,
            user_declared_priority=policy.priority_declared_by_user,
            rationale=(
                "All candidates failed the T1 gate.",
                (
                    "The hard failure is common-mode; selecting another take in this "
                    "batch cannot fix it."
                    if common_hard
                    else "The T1 failures are candidate-specific."
                ),
            ),
            alternate_costs=(),
            confidence=Confidence.HIGH,
            evidence_gaps=(),
            ignored_axes=(Axis.DISTINCTIVENESS,),
            common_mode_issue=common_hard,
            zero_survivor_cause=(
                "brief_or_model_common_mode" if common_hard else "candidate_independent"
            ),
        )

    eligible: list[CandidateAssessment] = []
    floor = policy.compliance_floor
    assert floor is not None
    for candidate in survivors:
        compliance = candidate.evaluation_for(Axis.COMPLIANCE)
        if compliance is None:
            continue
        if compliance.status == EvaluationStatus.FAIL:
            continue
        if (
            floor.abstain_on_critical_unknown
            and compliance.status == EvaluationStatus.INDETERMINATE
        ):
            continue
        eligible.append(candidate)
    if not eligible:
        return RecommendationCard(
            status=RecommendationStatus.ABSTAIN,
            recommended_artifact_id=None,
            alternate_artifact_id=None,
            policy_id=policy.id,
            priority=priority,
            user_declared_priority=policy.priority_declared_by_user,
            rationale=("No T1 survivor met the declared Compliance floor.",),
            alternate_costs=(),
            confidence=Confidence.INDETERMINATE,
            evidence_gaps=("Compliance floor eliminated every survivor",),
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )
    if len(eligible) == 1:
        only = eligible[0]
        return RecommendationCard(
            status=RecommendationStatus.UNIQUE_SURVIVOR,
            recommended_artifact_id=only.artifact_id,
            alternate_artifact_id=None,
            policy_id=policy.id,
            priority=priority,
            user_declared_priority=policy.priority_declared_by_user,
            rationale=("Only one candidate survived T1 and Compliance gating.",),
            alternate_costs=(),
            confidence=Confidence.HIGH,
            evidence_gaps=(),
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )

    grouped: dict[str, list[CandidateAssessment]] = defaultdict(list)
    for candidate in eligible:
        grouped[candidate.take_id].append(candidate)
    take_representatives: list[CandidateAssessment] = []
    unresolved_takes: list[str] = []
    for take_id, group in grouped.items():
        selected = _select_within_take(group, policy)
        if selected:
            take_representatives.append(selected)
        else:
            unresolved_takes.append(take_id)
    if unresolved_takes:
        return RecommendationCard(
            status=RecommendationStatus.ABSTAIN,
            recommended_artifact_id=None,
            alternate_artifact_id=None,
            policy_id=policy.id,
            priority=priority,
            user_declared_priority=policy.priority_declared_by_user,
            rationale=("Artifact-level selection within a Take is tied.",),
            alternate_costs=(),
            confidence=Confidence.INDETERMINATE,
            evidence_gaps=tuple(
                f"take {take_id}: no unique ReleaseArtifact"
                for take_id in unresolved_takes
            ),
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )

    if len(take_representatives) == 1:
        only = take_representatives[0]
        return RecommendationCard(
            status=RecommendationStatus.UNIQUE_SURVIVOR,
            recommended_artifact_id=only.artifact_id,
            alternate_artifact_id=None,
            policy_id=policy.id,
            priority=priority,
            user_declared_priority=policy.priority_declared_by_user,
            rationale=("Only one Take survived T1 and Compliance gating.",),
            alternate_costs=(),
            confidence=Confidence.HIGH,
            evidence_gaps=(),
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )

    winners: list[tuple[CandidateAssessment, list[str]]] = []
    for candidate in take_representatives:
        reasons: list[str] = []
        wins_all = True
        won_any = False
        for other in take_representatives:
            if candidate is other:
                continue
            outcome, reason = _compare_candidates(candidate, other, policy)
            if outcome < 0:
                wins_all = False
                break
            if outcome > 0:
                won_any = True
                if reason:
                    reasons.append(reason)
        if wins_all and won_any:
            winners.append((candidate, reasons))
    if len(winners) != 1:
        gaps = tuple(
            sorted(
                {
                    reason
                    for index, candidate in enumerate(take_representatives)
                    for other in take_representatives[index + 1 :]
                    for _, reason in [_compare_candidates(candidate, other, policy)]
                    if reason and "indeterminate" in reason
                }
            )
        )
        return RecommendationCard(
            status=RecommendationStatus.ABSTAIN,
            recommended_artifact_id=None,
            alternate_artifact_id=None,
            policy_id=policy.id,
            priority=priority,
            user_declared_priority=policy.priority_declared_by_user,
            rationale=("All declared directional axes tie or conflict.",),
            alternate_costs=(),
            confidence=Confidence.INDETERMINATE,
            evidence_gaps=gaps,
            ignored_axes=(Axis.DISTINCTIVENESS,),
        )

    winner, reasons = winners[0]
    alternatives = [
        candidate
        for candidate in take_representatives
        if candidate.artifact_id != winner.artifact_id
    ]
    if len(alternatives) == 1:
        alternate = alternatives[0]
    else:
        alternate_winners = [
            candidate
            for candidate in alternatives
            if all(
                candidate is other
                or _compare_candidates(candidate, other, policy)[0] >= 0
                for other in alternatives
            )
            and any(
                candidate is not other
                and _compare_candidates(candidate, other, policy)[0] > 0
                for other in alternatives
            )
        ]
        alternate = alternate_winners[0] if len(alternate_winners) == 1 else None
    costs: list[str] = []
    if alternate:
        for axis in policy.axis_priority:
            winner_axis = winner.evaluation_for(axis)
            alternate_axis = alternate.evaluation_for(axis)
            if not winner_axis or not alternate_axis:
                continue
            outcome, deciding_criteria = _compare_axis(
                winner_axis,
                alternate_axis,
                policy,
            )
            if outcome > 0:
                timestamped = [
                    observation
                    for observation in winner_axis.observations
                    if observation.criterion in deciding_criteria
                    and observation.start_s is not None
                ]
                deciding_start = min(
                    (observation.start_s for observation in timestamped),
                    default=None,
                )
                detail = (
                    f" at {deciding_start:.2f}s" if deciding_start is not None else ""
                )
                costs.append(f"alternate loses on {axis.value}{detail}")
                break
    return RecommendationCard(
        status=RecommendationStatus.RECOMMENDED,
        recommended_artifact_id=winner.artifact_id,
        alternate_artifact_id=alternate.artifact_id if alternate else None,
        policy_id=policy.id,
        priority=priority,
        user_declared_priority=policy.priority_declared_by_user,
        rationale=tuple(dict.fromkeys(reasons)),
        alternate_costs=tuple(costs),
        confidence=Confidence.MEDIUM,
        evidence_gaps=(),
        ignored_axes=(Axis.DISTINCTIVENESS,),
    )
