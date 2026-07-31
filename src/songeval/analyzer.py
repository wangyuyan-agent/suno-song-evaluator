from __future__ import annotations

from itertools import combinations
from pathlib import Path

from . import __version__
from .audio import (
    FeatureSeries,
    analyze_structure,
    compare_feature_series,
    extract_features,
    measure_audio,
)
from .enums import (
    AcquisitionPath,
    DefectTier,
    EvaluationStatus,
    PreservationIntent,
)
from .evaluation import (
    build_candidate_assessment,
    evaluate_compliance,
    evaluate_craft,
    evaluate_distinctiveness,
    evaluate_release_readiness,
    promote_common_mode_defects,
)
from .lineage import ArtifactGraph, common_craft_regions
from .models import (
    AnalysisRun,
    ArtifactReview,
    CandidateAssessment,
    Defect,
    ProjectAnalysisReport,
    ProjectManifest,
    ProjectReviewPacket,
    ReleaseArtifact,
)
from .recommendation import recommend
from .reference import (
    analyze_reference_facts,
    evaluate_exact_or_melody_retention,
    evaluate_structural_gesture,
    preflight_reference,
)
from .util import utc_now


class ProjectAnalyzer:
    def __init__(self, manifest: ProjectManifest):
        self.manifest = manifest
        self.artifacts = {item.id: item for item in manifest.artifacts}
        self.takes = {item.id: item for item in manifest.takes}
        self.events = {item.id: item for item in manifest.generation_events}
        self.briefs = {item.id: item for item in manifest.briefs}
        self.graph = ArtifactGraph(manifest.artifacts, manifest.edges)

    @property
    def candidates(self) -> list[ReleaseArtifact]:
        return [
            artifact
            for artifact in self.manifest.artifacts
            if artifact.raw_payload.get("analysis_role") != "reference_only"
            and artifact.raw_payload.get("analysis_role") != "lineage_only"
        ]

    def analyze(
        self,
        review: ProjectReviewPacket | None = None,
    ) -> ProjectAnalysisReport:
        review = review or ProjectReviewPacket(project_id=self.manifest.project_id)
        if review.project_id != self.manifest.project_id:
            raise ValueError("review packet belongs to a different project")
        for lyric_analysis in review.lyric_analyses:
            if lyric_analysis.project_id != self.manifest.project_id:
                raise ValueError("lyric analysis belongs to a different project")
            if lyric_analysis.artifact_id not in self.artifacts:
                raise ValueError(
                    f"lyric analysis references unknown artifact "
                    f"{lyric_analysis.artifact_id}"
                )
            if lyric_analysis.brief_id not in self.briefs:
                raise ValueError(
                    f"lyric analysis references unknown Brief {lyric_analysis.brief_id}"
                )
        run = AnalysisRun(
            project_id=self.manifest.project_id,
            tool_version=__version__,
            configuration={
                "comparison": "chroma+onset+energy, no aggregate score",
                "loudness_policy": "attenuate louder candidate; original unchanged",
            },
        )
        warnings: list[str] = []
        features: dict[str, FeatureSeries] = {}
        metrics = []
        structures = []
        for artifact in self.manifest.artifacts:
            if not artifact.local_path:
                warnings.append(f"{artifact.id}: local audio missing; analysis skipped")
                continue
            path = Path(artifact.local_path)
            if not path.exists():
                warnings.append(f"{artifact.id}: local path does not exist")
                continue
            metric = measure_audio(
                path,
                artifact.id,
                run.id,
                acquisition_degraded=not artifact.format_sensitive_comparison_allowed,
            )
            metrics.append(metric)
            if (
                artifact.measured_file_duration_s is not None
                and abs(
                    metric.measured_file_duration_s - artifact.measured_file_duration_s
                )
                > 0.002
            ):
                warnings.append(
                    f"{artifact.id}: hydrated duration differs from analysis duration"
                )
            if artifact.has_duration_mismatch:
                warnings.append(
                    f"{artifact.id}: platform/measured duration mismatch "
                    f"{artifact.duration_mismatch_s:+.6f}s"
                )
            series = extract_features(path)
            features[artifact.id] = series
            structures.append(analyze_structure(series, artifact.id, run.id))
        structure_by_artifact = {item.artifact_id: item for item in structures}
        metrics_by_artifact = {item.artifact_id: item for item in metrics}

        comparisons = []
        for artifact_a, artifact_b in combinations(self.candidates, 2):
            if artifact_a.id not in features or artifact_b.id not in features:
                continue
            take_a = self.takes[artifact_a.take_id]
            take_b = self.takes[artifact_b.take_id]
            same_event = take_a.generation_event_id == take_b.generation_event_id
            edge = self.graph.direct_relation(artifact_a.id, artifact_b.id)
            a_region, b_region, excluded = common_craft_regions(
                artifact_a,
                artifact_b,
                edge,
            )
            acquisition_warning = None
            if (
                not artifact_a.format_sensitive_comparison_allowed
                or not artifact_b.format_sensitive_comparison_allowed
                or artifact_a.acquisition_path != artifact_b.acquisition_path
            ):
                acquisition_warning = (
                    "format-sensitive absolute comparison disabled; "
                    "interpret only relative structural features"
                )
            comparisons.append(
                compare_feature_series(
                    features[artifact_a.id],
                    features[artifact_b.id],
                    project_id=self.manifest.project_id,
                    artifact_a_id=artifact_a.id,
                    artifact_b_id=artifact_b.id,
                    analysis_run_id=run.id,
                    same_generation_event=same_event,
                    acquisition_warning=acquisition_warning,
                    deterministic_relation=bool(edge and edge.deterministic),
                    excluded_difference_regions=excluded,
                    comparable_region_a_s=a_region,
                    comparable_region_b_s=b_region,
                )
            )

        analyzed_references = []
        preflights = []
        for reference in self.manifest.references:
            reference_artifact = self.artifacts[reference.source_artifact_id]
            analyzed_reference = reference
            if (
                reference_artifact.local_path
                and reference.source_artifact_id in features
                and reference.source_artifact_id in structure_by_artifact
            ):
                analyzed_reference = analyze_reference_facts(
                    reference,
                    source_path=reference_artifact.local_path,
                    analysis_run_id=run.id,
                    features=features[reference.source_artifact_id],
                    structure=structure_by_artifact[reference.source_artifact_id],
                )
            analyzed_references.append(analyzed_reference)
            measured_durations = [
                duration
                for item in self.candidates
                if (duration := item.measured_file_duration_s) is not None
                and duration > 0
            ]
            target_duration = max(measured_durations, default=None)
            preflights.append(
                preflight_reference(
                    analyzed_reference,
                    target_song_duration_s=target_duration,
                )
            )

        reviews = {item.artifact_id: item for item in review.artifact_reviews}
        policy = (
            sorted(self.manifest.policies, key=lambda item: item.created_at)[-1]
            if self.manifest.policies
            else None
        )
        defects_by_artifact: dict[str, list] = {
            artifact.id: [] for artifact in self.candidates
        }
        artifacts_by_brief: dict[str, set[str]] = {}
        for artifact in self.candidates:
            brief_id = self.events[
                self.takes[artifact.take_id].generation_event_id
            ].brief_id
            artifacts_by_brief.setdefault(brief_id, set()).add(artifact.id)
        promoted_defects = promote_common_mode_defects(
            self.manifest.defects,
            artifacts_by_brief,
        )
        for defect in promoted_defects:
            defects_by_artifact.setdefault(defect.artifact_id, []).append(defect)

        assessments: list[CandidateAssessment] = []
        for artifact in self.candidates:
            take = self.takes[artifact.take_id]
            event = self.events[take.generation_event_id]
            brief = self.briefs[event.brief_id]
            artifact_review = reviews.get(
                artifact.id,
                ArtifactReview(artifact_id=artifact.id),
            )
            applicable_directives = [
                item for item in self.manifest.directives if item.brief_id == brief.id
            ]
            preservation_results = []
            for directive in applicable_directives:
                reference = next(
                    (
                        item
                        for item in self.manifest.references
                        if item.id == directive.reference_segment_id
                    ),
                    None,
                )
                if reference is None:
                    raise ValueError(
                        f"directive {directive.id} references unknown reference "
                        f"segment {directive.reference_segment_id}"
                    )
                reference_artifact = self.artifacts[reference.source_artifact_id]
                if directive.preservation_intent in {
                    PreservationIntent.EXACT_AUDIO,
                    PreservationIntent.MELODY_RHYTHM,
                }:
                    if (
                        reference_artifact.local_path
                        and artifact.local_path
                        and Path(reference_artifact.local_path).exists()
                        and Path(artifact.local_path).exists()
                    ):
                        preservation_results.append(
                            evaluate_exact_or_melody_retention(
                                directive,
                                artifact_id=artifact.id,
                                reference_path=reference_artifact.local_path,
                                target_path=artifact.local_path,
                                null_baselines=review.null_baselines.get(directive.id),
                                batch_variance_floors=review.batch_variance_floors.get(
                                    directive.id
                                ),
                                acquisition_comparable=(
                                    reference_artifact.acquisition_path
                                    == artifact.acquisition_path
                                    and artifact.acquisition_path
                                    not in {
                                        AcquisitionPath.UNKNOWN,
                                        AcquisitionPath.USER_PROVIDED_UNKNOWN,
                                    }
                                ),
                            )
                        )
                elif artifact.id in features and artifact.id in structure_by_artifact:
                    preservation_results.append(
                        evaluate_structural_gesture(
                            directive,
                            artifact_id=artifact.id,
                            features=features[artifact.id],
                            structure=structure_by_artifact[artifact.id],
                            target_window_s=artifact_review.target_windows.get(
                                directive.id
                            ),
                        )
                    )
            defects = list(defects_by_artifact.get(artifact.id, []))
            technical_evidence_gaps: list[str] = []
            ending_confirmation = artifact_review.technical_confirmations.get(
                "ending_boundary"
            )
            metric = metrics_by_artifact.get(artifact.id)
            if ending_confirmation == EvaluationStatus.FAIL:
                defects.append(
                    Defect(
                        project_id=self.manifest.project_id,
                        artifact_id=artifact.id,
                        brief_id=brief.id,
                        code="ending_boundary_issue",
                        tier=DefectTier.T2,
                        description=(
                            "Listener confirmed an unacceptable ending boundary"
                        ),
                        start_s=(
                            max(0.0, metric.measured_file_duration_s - 1.0)
                            if metric is not None
                            else None
                        ),
                        end_s=(
                            metric.measured_file_duration_s
                            if metric is not None
                            else None
                        ),
                        confirmed=True,
                        evidence_source="human technical confirmation",
                    )
                )
            elif (
                metric is not None
                and metric.ending.status == EvaluationStatus.INDETERMINATE
                and ending_confirmation != EvaluationStatus.PASS
            ):
                technical_evidence_gaps.append(
                    "ending boundary has active audio or insufficient decay; "
                    "human confirmation is required"
                )
            compliance_as_generated = evaluate_compliance(
                artifact_id=artifact.id,
                brief=brief,
                requirement_observations=artifact_review.requirement_observations,
                policy=policy,
                directives=applicable_directives,
                preservation_results=preservation_results,
                comparison_mode="as_generated",
            )
            compliance_vs_target = None
            if review.target_brief_id is not None:
                target_brief = self.briefs.get(review.target_brief_id)
                if target_brief is None:
                    raise ValueError(f"unknown target Brief {review.target_brief_id}")
                if target_brief.id == brief.id:
                    compliance_vs_target = compliance_as_generated
                else:
                    compliance_vs_target = evaluate_compliance(
                        artifact_id=artifact.id,
                        brief=target_brief,
                        requirement_observations=(
                            review.target_requirement_observations.get(
                                artifact.id,
                                {},
                            )
                        ),
                        policy=policy,
                        directives=[
                            item
                            for item in self.manifest.directives
                            if item.brief_id == target_brief.id
                        ],
                        preservation_results=preservation_results,
                        comparison_mode="vs_target",
                    )
            craft = evaluate_craft(
                artifact.id,
                artifact_review.craft_observations,
                listening_round_valid=review.listening_round_valid,
            )
            distinctiveness = evaluate_distinctiveness(
                artifact.id,
                comparisons,
                has_confirmed_defect_outlier=any(
                    defect.confirmed for defect in defects
                ),
            )
            readiness = evaluate_release_readiness(
                artifact_id=artifact.id,
                defects=defects,
                release_actions=artifact_review.release_actions,
                technical_evidence_gaps=technical_evidence_gaps,
            )
            assessments.append(
                build_candidate_assessment(
                    artifact_id=artifact.id,
                    take_id=artifact.take_id,
                    brief_id=brief.id,
                    evaluations=(craft, readiness, distinctiveness),
                    compliance_as_generated=compliance_as_generated,
                    compliance_vs_target=compliance_vs_target,
                    defects=defects,
                    release_actions=artifact_review.release_actions,
                    preservation=preservation_results,
                )
            )
        cross_brief_complete = review.cross_brief_target_compliance_complete and all(
            assessment.compliance_vs_target is not None for assessment in assessments
        )
        recommendation = recommend(
            assessments,
            policy=policy,
            listening_round_valid=review.listening_round_valid,
            cross_brief_target_compliance_complete=cross_brief_complete,
        )
        completed_run = run.model_copy(update={"completed_at": utc_now()})
        return ProjectAnalysisReport(
            project_id=self.manifest.project_id,
            run=completed_run,
            source_assessments=tuple(self.manifest.source_assessments),
            audio_metrics=tuple(metrics),
            structures=tuple(structures),
            comparisons=tuple(comparisons),
            reference_segments=tuple(analyzed_references),
            reference_preflights=tuple(preflights),
            lyric_analyses=tuple(review.lyric_analyses),
            assessments=tuple(assessments),
            recommendation=recommendation,
            warnings=tuple(warnings),
        )
