from __future__ import annotations

import os
from pathlib import Path

import pytest

from songeval.analyzer import ProjectAnalyzer
from songeval.enums import (
    Axis,
    ComparisonOutcome,
    EvaluationStatus,
    OperationType,
    RecommendationStatus,
)
from songeval.importers import build_suno_project, load_suno_snapshots
from songeval.listening import (
    build_blind_session,
    build_listening_review,
    merge_project_reviews,
)
from songeval.models import (
    ArtifactReview,
    AxisThreshold,
    ComplianceFloor,
    ListeningResponse,
    OrdinalObservation,
    ProjectDecisionPolicy,
    ProjectReviewPacket,
)

SEVENTEEN_SNAPSHOT = (
    Path(value).expanduser()
    if (value := os.environ.get("SEVENTEEN_SNAPSHOT"))
    else None
)
SEVENTEEN_AUDIO_DIR = (
    Path(value).expanduser()
    if (value := os.environ.get("SEVENTEEN_AUDIO_DIR"))
    else None
)
RANKING = (
    "f926221b-e926-43aa-9559-87df32c9567f",
    "2be28cb5-96c9-4ebf-bb91-04f9f08a3a11",
    "8fb083b0-223d-45c0-9d0a-37b8c5e54588",
    "0e3cee7e-8252-44c9-9cce-9c6969834b63",
    "f97e84f0-5c52-47f1-9aa1-9d63a9bb5697",
)


def seventeen_manifest():
    if (
        SEVENTEEN_SNAPSHOT is None
        or SEVENTEEN_AUDIO_DIR is None
        or not SEVENTEEN_SNAPSHOT.exists()
        or not all(
            (SEVENTEEN_AUDIO_DIR / f"{clip_id}.mp3").exists() for clip_id in RANKING
        )
    ):
        pytest.skip("local Seventeen acceptance data is unavailable")
    result = build_suno_project(
        load_suno_snapshots(SEVENTEEN_SNAPSHOT),
        project_id="seventeen-acceptance",
        title="《十七》候选评估",
        media_dir=SEVENTEEN_AUDIO_DIR,
    )
    return result.manifest


def complete_policy() -> ProjectDecisionPolicy:
    return ProjectDecisionPolicy(
        id="policy_seventeen_acceptance",
        project_id="seventeen-acceptance",
        version="v1",
        declared_by_user=True,
        priority_declared_by_user=True,
        axis_priority=(
            Axis.COMPLIANCE,
            Axis.CRAFT,
            Axis.RELEASE_READINESS,
            Axis.DISTINCTIVENESS,
        ),
        compliance_floor=ComplianceFloor(
            reject_confirmed_t1=True,
            burden_lyrics_must_pass=True,
            hard_requirements_must_pass=True,
            abstain_on_critical_unknown=True,
        ),
        axis_thresholds=(
            AxisThreshold(axis=Axis.COMPLIANCE, source="acceptance"),
            AxisThreshold(axis=Axis.CRAFT, source="acceptance"),
            AxisThreshold(axis=Axis.RELEASE_READINESS, source="acceptance"),
        ),
        max_na_ratio=0.0,
        abstention_strategy="abstain on critical unknown or unresolved tie",
    )


@pytest.mark.real_audio
def test_seventeen_intake_diagnostics_and_preference_loop():
    manifest = seventeen_manifest()
    by_platform = {artifact.platform_id: artifact for artifact in manifest.artifacts}
    crop = by_platform[RANKING[0]]
    assert crop.operation == OperationType.CROP
    assert {item.id for item in manifest.briefs[0].requirements} == {
        "captured_lyrics",
        "captured_style",
    }

    initial_report = ProjectAnalyzer(manifest).analyze()
    assert len(initial_report.assessments) == 5
    assert len(initial_report.comparisons) == 10
    ending_by_platform = {
        next(
            artifact.platform_id
            for artifact in manifest.artifacts
            if artifact.id == metric.artifact_id
        ): metric.ending
        for metric in initial_report.audio_metrics
    }
    assert ending_by_platform[RANKING[-1]].classification in {
        "active_audio_at_boundary",
        "likely_abrupt_boundary",
    }

    artifact_paths = {
        artifact.id: artifact.local_path
        for artifact in manifest.artifacts
        if artifact.local_path
    }
    bundle = build_blind_session(
        project_id=manifest.project_id,
        comparisons=initial_report.comparisons,
        artifact_paths=artifact_paths,
        max_hotspots_per_pair=1,
    )
    rank_by_artifact = {
        by_platform[platform_id].id: index for index, platform_id in enumerate(RANKING)
    }
    secret_by_trial = {item.trial_id: item for item in bundle.trials}
    responses: list[ListeningResponse] = []
    for trial in bundle.session.trials:
        if trial.probe_type != "real":
            responses.append(
                ListeningResponse(
                    trial_id=trial.id,
                    outcome=ComparisonOutcome.TIE,
                )
            )
            continue
        secret = secret_by_trial[trial.id]
        left_rank = rank_by_artifact[secret.left_artifact_id]
        right_rank = rank_by_artifact[secret.right_artifact_id]
        winning_artifact = (
            secret.left_artifact_id
            if left_rank < right_rank
            else secret.right_artifact_id
        )
        winning_platform = next(
            platform_id
            for platform_id, artifact in by_platform.items()
            if artifact.id == winning_artifact
        )
        if winning_platform == RANKING[0]:
            tags = ("warmth_fullness", "overall_preference")
            note = "声音曲调饱满温暖"
        elif winning_platform == RANKING[1]:
            tags = (
                "hook_catchiness",
                "arrangement_harmony_development",
                "overall_preference",
            )
            note = "很抓人，末段有合唱和声"
        elif winning_platform == RANKING[2]:
            tags = ("hook_catchiness", "overall_preference")
            note = "抓耳但弱于前两名"
        else:
            tags = ("overall_preference",)
            note = "整体偏好"
        responses.append(
            ListeningResponse(
                trial_id=trial.id,
                outcome=(
                    ComparisonOutcome.A
                    if left_rank < right_rank
                    else ComparisonOutcome.B
                ),
                reason_tags=tags,
                comment=note,
            )
        )
    validation, craft_review = build_listening_review(bundle, responses)
    assert validation.valid

    requirements = manifest.briefs[0].requirements
    manual_review = ProjectReviewPacket(
        project_id=manifest.project_id,
        artifact_reviews=[
            ArtifactReview(
                artifact_id=artifact.id,
                requirement_observations={
                    requirement.id: OrdinalObservation(
                        criterion=requirement.id,
                        value=3,
                        evidence="real acceptance human confirmation",
                    )
                    for requirement in requirements
                },
                technical_confirmations={
                    "ending_boundary": (
                        EvaluationStatus.FAIL
                        if artifact.platform_id == RANKING[-1]
                        else EvaluationStatus.PASS
                    )
                },
            )
            for artifact in manifest.artifacts
        ],
    )
    review = merge_project_reviews(manual_review, craft_review)
    completed_manifest = manifest.model_copy(update={"policies": [complete_policy()]})
    final_report = ProjectAnalyzer(completed_manifest).analyze(review)
    assert final_report.recommendation.status == RecommendationStatus.RECOMMENDED
    assert final_report.recommendation.recommended_artifact_id == crop.id
    assert (
        final_report.recommendation.alternate_artifact_id == by_platform[RANKING[1]].id
    )
    runner_up = next(
        item
        for item in final_report.assessments
        if item.artifact_id == by_platform[RANKING[1]].id
    )
    craft = runner_up.evaluation_for(Axis.CRAFT)
    assert craft is not None
    assert any(
        item.criterion == "arrangement_harmony_development"
        and "合唱和声" in item.evidence
        for item in craft.observations
    )
