from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from .enums import (
    Axis,
    DefectTier,
    EvaluationStatus,
    ReadinessStatus,
)
from .models import (
    AxisEvaluation,
    CandidateAssessment,
    CreativeBriefVersion,
    Defect,
    OrdinalObservation,
    PairwiseComparison,
    PreservationDirective,
    PreservationEvaluation,
    ProjectDecisionPolicy,
    ReleaseAction,
)
from .reference import preservation_enters_compliance


def promote_common_mode_defects(
    defects: Iterable[Defect],
    artifacts_by_brief: Mapping[str, set[str]],
) -> tuple[Defect, ...]:
    defects = tuple(defects)
    grouped: dict[tuple[str, str, DefectTier], set[str]] = defaultdict(set)
    for defect in defects:
        if defect.confirmed:
            grouped[(defect.brief_id, defect.code, defect.tier)].add(defect.artifact_id)
    common_keys = {
        key
        for key, artifact_ids in grouped.items()
        if artifact_ids and artifact_ids == artifacts_by_brief.get(key[0], set())
    }
    return tuple(
        defect.model_copy(
            update={
                "common_mode": (
                    defect.brief_id,
                    defect.code,
                    defect.tier,
                )
                in common_keys
            }
        )
        for defect in defects
    )


def evaluate_compliance(
    *,
    artifact_id: str,
    brief: CreativeBriefVersion,
    requirement_observations: Mapping[str, OrdinalObservation],
    policy: ProjectDecisionPolicy | None,
    directives: Iterable[PreservationDirective] = (),
    preservation_results: Iterable[PreservationEvaluation] = (),
    comparison_mode: str = "as_generated",
) -> AxisEvaluation:
    observations: list[OrdinalObservation] = []
    evidence_gaps: list[str] = []
    hard_failure = False
    critical_unknown = False
    for requirement in brief.requirements:
        observation = requirement_observations.get(requirement.id)
        if observation is None:
            observation = OrdinalObservation(
                criterion=requirement.id,
                value=None,
                evidence="not evaluated",
            )
        observations.append(observation)
        if observation.value is None:
            evidence_gaps.append(f"{requirement.id}: unknown")
            if requirement.hard or requirement.burden_bearing:
                critical_unknown = True
        elif observation.value == 0 and requirement.hard:
            hard_failure = True

    result_by_directive = {
        result.directive_id: result for result in preservation_results
    }
    preservation_gate_enabled = not (
        policy is not None
        and policy.compliance_floor is not None
        and not policy.compliance_floor.must_preserve_directives_must_pass
    )
    for directive in directives:
        result = result_by_directive.get(directive.id)
        if not directive.must_preserve or not preservation_gate_enabled:
            continue
        if result is None:
            observations.append(
                OrdinalObservation(
                    criterion=f"preservation:{directive.id}",
                    value=None,
                    evidence="must-preserve directive not evaluated",
                )
            )
            critical_unknown = True
            evidence_gaps.append(f"must-preserve {directive.id}: not evaluated")
            continue
        if preservation_enters_compliance(directive, result):
            value = {
                EvaluationStatus.PASS: 3,
                EvaluationStatus.FAIL: 0,
                EvaluationStatus.INDETERMINATE: None,
            }.get(result.status)
            observations.append(
                OrdinalObservation(
                    criterion=f"preservation:{directive.id}",
                    value=value,
                    evidence=f"retention status={result.status.value}",
                )
            )
            hard_failure = hard_failure or result.status == EvaluationStatus.FAIL
            critical_unknown = critical_unknown or result.status in {
                EvaluationStatus.INDETERMINATE,
                EvaluationStatus.NOT_EVALUATED,
            }

    if comparison_mode not in {"as_generated", "vs_target"}:
        raise ValueError("comparison_mode must be as_generated or vs_target")
    if hard_failure:
        status = EvaluationStatus.FAIL
    elif critical_unknown:
        status = EvaluationStatus.INDETERMINATE
    elif not observations:
        status = EvaluationStatus.INDETERMINATE
        evidence_gaps.append("brief has no evaluable requirements")
    else:
        status = EvaluationStatus.PASS

    if policy and policy.max_na_ratio is not None and observations:
        na_ratio = sum(item.value is None for item in observations) / len(observations)
        if na_ratio > policy.max_na_ratio:
            status = EvaluationStatus.INDETERMINATE
            evidence_gaps.append(
                f"N/A ratio {na_ratio:.3f} exceeds policy {policy.max_na_ratio:.3f}"
            )
    return AxisEvaluation(
        artifact_id=artifact_id,
        axis=Axis.COMPLIANCE,
        status=status,
        observations=tuple(observations),
        evidence_gaps=tuple(evidence_gaps),
    )


def evaluate_craft(
    artifact_id: str,
    observations: Iterable[OrdinalObservation],
    *,
    listening_round_valid: bool,
) -> AxisEvaluation:
    observations = tuple(observations)
    if not listening_round_valid:
        return AxisEvaluation(
            artifact_id=artifact_id,
            axis=Axis.CRAFT,
            status=EvaluationStatus.INDETERMINATE,
            observations=(),
            evidence_gaps=("blind-listening probes failed",),
        )
    if not observations or all(item.value is None for item in observations):
        status = EvaluationStatus.INDETERMINATE
        gaps = ("no valid blind-listening Craft observations",)
    else:
        status = EvaluationStatus.PASS
        gaps = ()
    return AxisEvaluation(
        artifact_id=artifact_id,
        axis=Axis.CRAFT,
        status=status,
        observations=observations,
        evidence_gaps=gaps,
    )


def evaluate_distinctiveness(
    artifact_id: str,
    comparisons: Iterable[PairwiseComparison],
    *,
    has_confirmed_defect_outlier: bool = False,
    sibling_noise_floor: Mapping[str, float] | None = None,
) -> AxisEvaluation:
    comparisons = tuple(comparisons)
    if has_confirmed_defect_outlier:
        return AxisEvaluation(
            artifact_id=artifact_id,
            axis=Axis.DISTINCTIVENESS,
            status=EvaluationStatus.INDETERMINATE,
            evidence_gaps=("outlier is explained by a confirmed defect",),
            ignored_for_ordering=True,
        )
    relevant = [
        item
        for item in comparisons
        if artifact_id in {item.artifact_a_id, item.artifact_b_id}
    ]
    if not relevant:
        return AxisEvaluation(
            artifact_id=artifact_id,
            axis=Axis.DISTINCTIVENESS,
            status=EvaluationStatus.INDETERMINATE,
            evidence_gaps=("no pairwise comparison evidence",),
            ignored_for_ordering=True,
        )
    floors = sibling_noise_floor or {}
    artifact_ids = {
        artifact_id
        for item in comparisons
        for artifact_id in (item.artifact_a_id, item.artifact_b_id)
    }
    observations: list[OrdinalObservation] = []
    for family, attribute in (
        ("pitch_harmony", "pitch_harmony_distance"),
        ("rhythm_onset", "rhythm_onset_distance"),
        ("energy_structure", "energy_structure_distance"),
    ):
        values = [getattr(item, attribute) for item in relevant]
        mean_distance = sum(values) / len(values)
        floor = floors.get(family, 0.0)
        distances_by_artifact: dict[str, float] = {}
        for candidate_id in artifact_ids:
            candidate_values = [
                getattr(item, attribute)
                for item in comparisons
                if candidate_id in {item.artifact_a_id, item.artifact_b_id}
            ]
            if candidate_values:
                distances_by_artifact[candidate_id] = sum(candidate_values) / len(
                    candidate_values
                )
        other_distances = [
            value
            for candidate_id, value in distances_by_artifact.items()
            if candidate_id != artifact_id
        ]
        unique_outlier = bool(
            other_distances and mean_distance - max(other_distances) > max(floor, 0.05)
        )
        if mean_distance <= floor:
            ordinal = 0
        elif mean_distance <= floor * 1.5 + 0.05:
            ordinal = 1
        elif mean_distance <= floor * 2.0 + 0.10:
            ordinal = 2
        else:
            ordinal = 3
        observations.append(
            OrdinalObservation(
                criterion=family,
                value=ordinal,
                evidence=(
                    f"computed batch-centroid proxy distance={mean_distance:.4f}, "
                    f"sibling floor={floor:.4f}, "
                    f"unique_outlier={str(unique_outlier).lower()}"
                ),
            )
        )
    return AxisEvaluation(
        artifact_id=artifact_id,
        axis=Axis.DISTINCTIVENESS,
        status=EvaluationStatus.PASS,
        observations=tuple(observations),
        # Distinctiveness describes identity; more different is not inherently better.
        ignored_for_ordering=True,
    )


def evaluate_release_readiness(
    *,
    artifact_id: str,
    defects: Iterable[Defect],
    release_actions: Iterable[ReleaseAction],
    technical_evidence_gaps: Iterable[str] = (),
) -> AxisEvaluation:
    defects = tuple(defects)
    release_actions = tuple(release_actions)
    technical_evidence_gaps = tuple(technical_evidence_gaps)
    if any(defect.tier == DefectTier.T1 and defect.confirmed for defect in defects):
        return AxisEvaluation(
            artifact_id=artifact_id,
            axis=Axis.RELEASE_READINESS,
            status=EvaluationStatus.NOT_EVALUATED,
            readiness=ReadinessStatus.NOT_ELIGIBLE,
            evidence_gaps=("T1 gate failed; readiness is not evaluated",),
        )
    t2 = [
        defect
        for defect in defects
        if defect.tier == DefectTier.T2 and defect.confirmed
    ]
    if not t2 and technical_evidence_gaps:
        readiness = ReadinessStatus.INDETERMINATE
        status = EvaluationStatus.INDETERMINATE
        gaps = technical_evidence_gaps
    elif not t2:
        readiness = ReadinessStatus.READY
        status = EvaluationStatus.PASS
        gaps: tuple[str, ...] = ()
    elif not release_actions:
        readiness = ReadinessStatus.INDETERMINATE
        status = EvaluationStatus.INDETERMINATE
        gaps = ("T2 exists but Suno edit feasibility is unverified",)
    elif any(
        action.protected_island_status == "unknown" or not action.feasibility_verified
        for action in release_actions
    ):
        readiness = ReadinessStatus.INDETERMINATE
        status = EvaluationStatus.INDETERMINATE
        gaps = ("protected-island behavior or edit feasibility is unverified",)
    elif all(
        action.protected_island_status in {"true_island", "soft_island"}
        and action.feasibility_verified
        for action in release_actions
    ):
        readiness = ReadinessStatus.NEEDS_SUNO_EDIT
        status = EvaluationStatus.PASS
        gaps = ()
    else:
        readiness = ReadinessStatus.BLOCKED
        status = EvaluationStatus.FAIL
        gaps = ("no feasible Suno-only edit can repair the confirmed T2",)
    return AxisEvaluation(
        artifact_id=artifact_id,
        axis=Axis.RELEASE_READINESS,
        status=status,
        readiness=readiness,
        evidence_gaps=gaps,
        observations=(
            OrdinalObservation(
                criterion="suno_only_release_path",
                value={
                    ReadinessStatus.BLOCKED: 0,
                    ReadinessStatus.INDETERMINATE: None,
                    ReadinessStatus.NEEDS_SUNO_EDIT: 2,
                    ReadinessStatus.READY: 3,
                }.get(readiness),
                evidence=f"readiness={readiness.value}",
            ),
        ),
    )


def build_candidate_assessment(
    *,
    artifact_id: str,
    take_id: str,
    brief_id: str,
    evaluations: Iterable[AxisEvaluation],
    compliance_as_generated: AxisEvaluation | None = None,
    compliance_vs_target: AxisEvaluation | None = None,
    defects: Iterable[Defect] = (),
    release_actions: Iterable[ReleaseAction] = (),
    preservation: Iterable[PreservationEvaluation] = (),
) -> CandidateAssessment:
    evaluations = tuple(evaluations)
    axes = [item.axis for item in evaluations]
    if len(axes) != len(set(axes)):
        raise ValueError("CandidateAssessment cannot contain duplicate axes")
    return CandidateAssessment(
        artifact_id=artifact_id,
        take_id=take_id,
        brief_id=brief_id,
        evaluations=evaluations,
        compliance_as_generated=compliance_as_generated,
        compliance_vs_target=compliance_vs_target,
        defects=tuple(defects),
        release_actions=tuple(release_actions),
        preservation=tuple(preservation),
    )
