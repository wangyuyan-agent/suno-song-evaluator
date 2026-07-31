from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    AcquisitionPath,
    Axis,
    BoundaryQuality,
    ComparisonOutcome,
    Confidence,
    CraftAttribute,
    DefectTier,
    EvaluationStatus,
    EvidenceInheritance,
    OperationType,
    PreservationIntent,
    ProtectedDimension,
    Provenance,
    ReadinessStatus,
    RecommendationStatus,
    SourceKind,
    TargetPlacement,
    TaskType,
)
from .util import content_hash, utc_now


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class FrozenModel(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class ProvenanceRecord(FrozenModel):
    provenance: Provenance
    confidence: Confidence = Confidence.HIGH
    source: str | None = None
    note: str | None = None


class BriefRequirement(FrozenModel):
    id: str
    label: str
    value: Any
    hard: bool = False
    burden_bearing: bool = False
    provenance: ProvenanceRecord


class CreativeBriefVersion(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("brief"))
    project_id: str
    version: str
    lyrics: str
    style: str | None = None
    exclude: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    raw_sliders: dict[str, Any] = Field(default_factory=dict)
    duration_intent: str | None = None
    requirements: tuple[BriefRequirement, ...] = ()
    field_provenance: dict[str, ProvenanceRecord] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    content_sha256: str = ""

    @model_validator(mode="after")
    def compute_content_hash(self) -> CreativeBriefVersion:
        payload = {
            "project_id": self.project_id,
            "version": self.version,
            "lyrics": self.lyrics,
            "style": self.style,
            "exclude": self.exclude,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "raw_sliders": self.raw_sliders,
            "duration_intent": self.duration_intent,
            "requirements": [
                item.model_dump(mode="json") for item in self.requirements
            ],
            "field_provenance": {
                key: value.model_dump(mode="json")
                for key, value in self.field_provenance.items()
            },
        }
        expected = content_hash(payload)
        if self.content_sha256 and self.content_sha256 != expected:
            raise ValueError(
                "CreativeBriefVersion content hash does not match its content"
            )
        object.__setattr__(self, "content_sha256", expected)
        return self


class SourceMaterial(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("source"))
    project_id: str
    kind: SourceKind
    title: str | None = None
    platform_id: str | None = None
    url: str | None = None
    local_path: str | None = None
    file_sha256: str | None = None
    acquisition_path: AcquisitionPath = AcquisitionPath.UNKNOWN
    provenance: ProvenanceRecord
    raw_payload: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SourceStateAssessment(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("source_assessment"))
    project_id: str
    original_source_material_id: str | None = None
    added_source_material_id: str | None = None
    generation_event_id: str | None = None
    relationship: Literal["replaced", "coexists", "unknown"]
    provenance: ProvenanceRecord
    evidence: tuple[str, ...] = ()
    limitation: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class AcquisitionSnapshot(FrozenModel):
    id: str
    project_id: str
    url: str
    platform_id: str | None = None
    file_sha256: str | None = None
    payload_sha256: str
    raw_payload: dict[str, Any]
    revision_of: str | None = None
    content_changed_from_previous: bool = False
    warning: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ParameterValue(FrozenModel):
    value: Any = None
    provenance: ProvenanceRecord


class GenerationEvent(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("event"))
    project_id: str
    brief_id: str
    source_material_ids: tuple[str, ...] = ()
    task: TaskType = TaskType.UNKNOWN
    batch_id: str | None = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, ParameterValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Take(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("take"))
    project_id: str
    generation_event_id: str
    batch_index: int | None = None
    platform_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class EncodingFingerprint(FrozenModel):
    container: str | None = None
    codec: str | None = None
    sample_rate_hz: int | None = None
    bit_depth: int | None = None
    channels: int | None = None
    channel_layout: str | None = None


class ReleaseArtifact(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("artifact"))
    project_id: str
    take_id: str
    title: str | None = None
    platform_id: str | None = None
    url: str | None = None
    local_path: str | None = None
    file_sha256: str | None = None
    operation: OperationType = OperationType.RAW
    acquisition_path: AcquisitionPath = AcquisitionPath.UNKNOWN
    encoding: EncodingFingerprint = Field(default_factory=EncodingFingerprint)
    platform_reported_duration_s: float | None = None
    measured_file_duration_s: float | None = None
    duration_mismatch_s: float | None = None
    duration_mismatch_tolerance_s: float = 0.05
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def derive_duration_mismatch(self) -> ReleaseArtifact:
        if (
            self.platform_reported_duration_s is None
            or self.measured_file_duration_s is None
        ):
            expected = None
        else:
            expected = self.measured_file_duration_s - self.platform_reported_duration_s
        if (
            self.duration_mismatch_s is not None
            and expected is not None
            and abs(self.duration_mismatch_s - expected) > 1e-6
        ):
            raise ValueError("duration_mismatch_s conflicts with the two durations")
        if expected is not None:
            object.__setattr__(self, "duration_mismatch_s", expected)
        return self

    @property
    def has_duration_mismatch(self) -> bool:
        return (
            self.duration_mismatch_s is not None
            and abs(self.duration_mismatch_s) > self.duration_mismatch_tolerance_s
        )

    @property
    def format_sensitive_comparison_allowed(self) -> bool:
        return self.acquisition_path.format_sensitive_allowed


class ArtifactEdge(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("edge"))
    project_id: str
    parent_artifact_id: str
    child_artifact_id: str
    operation: OperationType
    generation_event_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    source_interval_s: tuple[float, float] | None = None
    deterministic: bool = False
    evidence_inheritance: dict[str, EvidenceInheritance] = Field(default_factory=dict)
    provenance: ProvenanceRecord
    verified_run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def no_self_edge(self) -> ArtifactEdge:
        if self.parent_artifact_id == self.child_artifact_id:
            raise ValueError("Artifact DAG cannot contain a self-edge")
        if self.operation in {
            OperationType.REPLACE_SECTION,
            OperationType.EXTEND,
            OperationType.REMASTER,
        } and any(
            value == EvidenceInheritance.INHERIT_PRESERVED_REGION
            for value in self.evidence_inheritance.values()
        ):
            raise ValueError("generated regions cannot inherit evidence directly")
        return self


class ReferenceSegment(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("reference"))
    project_id: str
    source_artifact_id: str
    title: str | None = None
    start_s: float
    end_s: float
    start_boundary: BoundaryQuality = BoundaryQuality.UNKNOWN
    end_boundary: BoundaryQuality = BoundaryQuality.UNKNOWN
    onset_proximity_start_s: float | None = None
    onset_proximity_end_s: float | None = None
    vocal_activity_ratio: float | None = None
    local_energy_percentile: float | None = None
    clean_superset_start_s: float | None = None
    clean_superset_end_s: float | None = None
    semantic_roles: tuple[str, ...] = ()
    internal_homogeneity: float | None = None
    silence_ratio: float | None = None
    structure_section_count: int | None = None
    analysis_run_id: str | None = None
    evidence: tuple[ProvenanceRecord, ...] = ()

    @model_validator(mode="after")
    def valid_interval(self) -> ReferenceSegment:
        if self.start_s < 0 or self.end_s <= self.start_s:
            raise ValueError("ReferenceSegment requires 0 <= start_s < end_s")
        return self

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


class PreservationThresholds(FrozenModel):
    min_feature_families: int = 2
    min_coverage_ratio: float | None = None
    min_best_to_second_margin: float | None = None
    min_null_margin: float | None = None
    min_batch_variance_margin: float | None = None
    gesture_valley_duration_s: float | None = None
    gesture_min_step_db: float | None = None
    gesture_max_delay_s: float | None = None
    noise_band: float | None = None


class PreservationDirective(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("directive"))
    project_id: str
    brief_id: str
    reference_segment_id: str
    target_artifact_id: str | None = None
    preservation_intent: PreservationIntent
    protected_dimensions: tuple[ProtectedDimension, ...]
    target_placement: TargetPlacement
    placement_enforceable: bool
    must_preserve: bool
    thresholds: PreservationThresholds
    status: EvaluationStatus = EvaluationStatus.NOT_EVALUATED
    abstention_strategy: str

    @model_validator(mode="after")
    def audio_upload_is_not_position_enforceable(self) -> PreservationDirective:
        if (
            self.target_placement == TargetPlacement.GLOBAL_CONDITIONER
            and self.placement_enforceable
        ):
            raise ValueError("global audio conditioning cannot enforce placement")
        return self


class ComplianceFloor(FrozenModel):
    reject_confirmed_t1: bool
    burden_lyrics_must_pass: bool
    hard_requirements_must_pass: bool
    abstain_on_critical_unknown: bool
    must_preserve_directives_must_pass: bool = True


class AxisThreshold(FrozenModel):
    axis: Axis
    ordinal_delta: int = 1
    source: str


class ProjectDecisionPolicy(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("policy"))
    project_id: str
    version: str
    declared_by_user: bool
    priority_declared_by_user: bool = False
    axis_priority: tuple[Axis, ...]
    compliance_floor: ComplianceFloor | None = None
    axis_thresholds: tuple[AxisThreshold, ...] = ()
    max_na_ratio: float | None = None
    abstention_strategy: str | None = None
    gesture_thresholds: PreservationThresholds | None = None
    created_at: datetime = Field(default_factory=utc_now)
    content_sha256: str = ""

    @model_validator(mode="after")
    def validate_policy(self) -> ProjectDecisionPolicy:
        if self.declared_by_user and not self.priority_declared_by_user:
            object.__setattr__(self, "priority_declared_by_user", True)
        if len(set(self.axis_priority)) != len(self.axis_priority):
            raise ValueError("axis_priority cannot contain duplicates")
        if self.max_na_ratio is not None and not 0 <= self.max_na_ratio <= 1:
            raise ValueError("max_na_ratio must be between 0 and 1")
        if self.declared_by_user and (
            len(self.axis_priority) != len(Axis)
            or self.compliance_floor is None
            or self.max_na_ratio is None
            or not self.abstention_strategy
        ):
            raise ValueError("a declared policy must be complete")
        payload = self.model_dump(
            mode="json",
            exclude={"content_sha256", "id", "created_at"},
        )
        expected = content_hash(payload)
        if self.content_sha256 and self.content_sha256 != expected:
            raise ValueError("ProjectDecisionPolicy content hash mismatch")
        object.__setattr__(self, "content_sha256", expected)
        return self


class EndingDiagnostics(FrozenModel):
    classification: Literal[
        "silence_tail",
        "natural_decay",
        "active_audio_at_boundary",
        "likely_abrupt_boundary",
        "indeterminate",
    ]
    status: EvaluationStatus
    trailing_silence_s: float
    final_100ms_rms_dbfs: float
    preceding_1s_rms_dbfs: float
    final_to_preceding_db: float
    boundary_sample_peak_dbfs: float
    evidence: tuple[str, ...]


class AudioMetrics(FrozenModel):
    artifact_id: str
    analysis_run_id: str
    measured_file_duration_s: float
    sample_rate_hz: int
    channels: int
    peak_dbfs: float
    true_peak_dbfs: float
    integrated_lufs: float | None = None
    approximate_lra_lu: float | None = None
    clipped_sample_ratio: float
    dc_offset: float
    initial_silence_s: float
    trailing_silence_s: float
    macro_click_count: int
    stereo_correlation: float | None = None
    side_to_mid_db: float | None = None
    acquisition_degraded: bool
    warnings: tuple[str, ...] = ()
    technical_checks: dict[str, EvaluationStatus] = Field(default_factory=dict)
    evidence_gaps: tuple[str, ...] = ()
    ending: EndingDiagnostics


class StructureBoundary(FrozenModel):
    time_s: float
    confidence: float
    label: str | None = None
    label_provenance: Provenance = Provenance.UNKNOWN


class StructureSegment(FrozenModel):
    start_s: float
    end_s: float
    repeat_group: str
    similarity_to_group: float
    label: str | None = None
    label_provenance: Provenance = Provenance.UNKNOWN


class StructureAnalysis(FrozenModel):
    artifact_id: str
    analysis_run_id: str
    boundaries: tuple[StructureBoundary, ...]
    segments: tuple[StructureSegment, ...] = ()
    feature_hop_s: float
    warnings: tuple[str, ...] = ()


class DifferenceHotspot(FrozenModel):
    a_start_s: float
    a_end_s: float
    b_start_s: float
    b_end_s: float
    feature_family: Literal["pitch_harmony", "rhythm_onset", "energy_structure"]
    magnitude: float
    evidence: str


class PairwiseComparison(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("comparison"))
    project_id: str
    artifact_a_id: str
    artifact_b_id: str
    analysis_run_id: str
    comparable_region_a_s: tuple[float, float]
    comparable_region_b_s: tuple[float, float]
    pitch_harmony_distance: float
    rhythm_onset_distance: float
    energy_structure_distance: float
    hotspots: tuple[DifferenceHotspot, ...]
    same_generation_event: bool
    parameter_attribution_allowed: bool
    acquisition_warning: str | None = None
    deterministic_relation: bool = False
    excluded_difference_regions: tuple[tuple[float, float], ...] = ()


class CropVerification(FrozenModel):
    parent_artifact_id: str
    child_artifact_id: str
    analysis_run_id: str
    lag_s: float
    retained_region_correlation: float
    correlation_threshold: float
    verified: bool
    used_measured_audio: bool = True


class Defect(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("defect"))
    project_id: str
    artifact_id: str
    brief_id: str
    code: str
    tier: DefectTier
    description: str
    start_s: float | None = None
    end_s: float | None = None
    confirmed: bool
    hard_requirement: bool = False
    evidence_source: str
    common_mode: bool = False


class OrdinalObservation(FrozenModel):
    criterion: str
    value: int | None
    evidence: str
    start_s: float | None = None
    end_s: float | None = None

    @model_validator(mode="after")
    def ordinal_range(self) -> OrdinalObservation:
        if self.value is not None and self.value not in {0, 1, 2, 3}:
            raise ValueError("ordinal values must be 0..3 or N/A")
        return self


class AxisEvaluation(FrozenModel):
    artifact_id: str
    axis: Axis
    status: EvaluationStatus
    observations: tuple[OrdinalObservation, ...] = ()
    readiness: ReadinessStatus | None = None
    evidence_gaps: tuple[str, ...] = ()
    ignored_for_ordering: bool = False

    @property
    def na_ratio(self) -> float:
        if not self.observations:
            return 1.0
        return sum(item.value is None for item in self.observations) / len(
            self.observations
        )


class ListeningStimulus(FrozenModel):
    sample_id: str
    media_path: str
    start_s: float
    end_s: float


class ListeningTrial(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("trial"))
    left: ListeningStimulus
    right: ListeningStimulus
    pair_key: str
    order: Literal["ab", "ba"]
    probe_type: Literal["real", "a_vs_a", "loudness_variant"]


class ListeningSession(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("listen"))
    project_id: str
    blinded: bool = True
    trials: tuple[ListeningTrial, ...]
    created_at: datetime = Field(default_factory=utc_now)


class ListeningResponse(FrozenModel):
    trial_id: str
    outcome: ComparisonOutcome
    observations: tuple[OrdinalObservation, ...] = ()
    reason_tags: tuple[CraftAttribute, ...] = ()
    comment: str | None = Field(default=None, max_length=2000)


class ListeningValidation(FrozenModel):
    valid: bool
    failures: tuple[str, ...]
    pair_outcomes: dict[str, ComparisonOutcome]


class ListeningStimulusSecret(FrozenModel):
    sample_id: str
    artifact_id: str
    source_path: str
    start_s: float
    end_s: float
    gain_variant_db: float = 0.0


class ListeningTrialSecret(FrozenModel):
    trial_id: str
    canonical_a_artifact_id: str
    canonical_b_artifact_id: str
    left_artifact_id: str
    right_artifact_id: str


class StoredListeningBundle(FrozenModel):
    id: str
    project_id: str
    run_id: str
    session: ListeningSession
    stimuli: tuple[ListeningStimulusSecret, ...]
    trial_secrets: tuple[ListeningTrialSecret, ...]
    media_files: dict[str, str]
    created_at: datetime = Field(default_factory=utc_now)


class PreservationFamilyResult(FrozenModel):
    family: Literal["pitch_harmony", "rhythm_onset", "vocal_f0", "embedding"]
    status: EvaluationStatus
    best_window_s: tuple[float, float] | None = None
    score: float | None = None
    coverage_ratio: float | None = None
    second_best_score: float | None = None
    shuffled_score: float | None = None
    null_baseline: float | None = None
    batch_variance_floor: float | None = None
    evidence: str


class PreservationEvaluation(FrozenModel):
    directive_id: str
    artifact_id: str
    status: EvaluationStatus
    family_results: tuple[PreservationFamilyResult, ...] = ()
    target_window_s: tuple[float, float] | None = None
    evidence_gaps: tuple[str, ...] = ()
    objective_checks: dict[str, EvaluationStatus] = Field(default_factory=dict)
    review_questions: tuple[str, ...] = ()


class PreflightFinding(FrozenModel):
    code: str
    severity: Literal["info", "warning", "blocking"]
    message: str
    evidence: str


class ReferencePreflight(FrozenModel):
    reference_segment_id: str
    findings: tuple[PreflightFinding, ...]
    effective_non_silent_duration_s: float | None = None
    multi_state: bool
    crosses_sections: bool
    dirty_boundaries: bool


class ReleaseAction(FrozenModel):
    operation: OperationType
    target_interval_s: tuple[float, float]
    feasibility_verified: bool
    protected_island_status: Literal["true_island", "soft_island", "none", "unknown"]
    note: str


class CandidateAssessment(FrozenModel):
    artifact_id: str
    take_id: str
    brief_id: str
    evaluations: tuple[AxisEvaluation, ...]
    compliance_as_generated: AxisEvaluation | None = None
    compliance_vs_target: AxisEvaluation | None = None
    defects: tuple[Defect, ...] = ()
    release_actions: tuple[ReleaseAction, ...] = ()
    preservation: tuple[PreservationEvaluation, ...] = ()

    def evaluation_for(self, axis: Axis) -> AxisEvaluation | None:
        if axis == Axis.COMPLIANCE:
            if self.compliance_vs_target is not None:
                return self.compliance_vs_target
            if self.compliance_as_generated is not None:
                return self.compliance_as_generated
        return next((item for item in self.evaluations if item.axis == axis), None)


class RecommendationCard(FrozenModel):
    status: RecommendationStatus
    recommended_artifact_id: str | None
    alternate_artifact_id: str | None
    policy_id: str | None
    priority: tuple[Axis, ...]
    user_declared_priority: bool
    rationale: tuple[str, ...]
    alternate_costs: tuple[str, ...]
    confidence: Confidence
    evidence_gaps: tuple[str, ...]
    ignored_axes: tuple[Axis, ...]
    common_mode_issue: bool = False
    zero_survivor_cause: Literal[
        "brief_or_model_common_mode",
        "candidate_independent",
        "none",
    ] = "none"
    user_final_choice: str | None = None
    user_override_reason: str | None = None


class AnalysisRun(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    project_id: str
    tool_version: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ProjectManifest(Model):
    schema_version: str = "1"
    project_id: str
    title: str
    briefs: list[CreativeBriefVersion]
    acquisition_snapshots: list[AcquisitionSnapshot] = Field(default_factory=list)
    sources: list[SourceMaterial]
    source_assessments: list[SourceStateAssessment] = Field(default_factory=list)
    generation_events: list[GenerationEvent]
    takes: list[Take]
    artifacts: list[ReleaseArtifact]
    edges: list[ArtifactEdge] = Field(default_factory=list)
    references: list[ReferenceSegment] = Field(default_factory=list)
    directives: list[PreservationDirective] = Field(default_factory=list)
    policies: list[ProjectDecisionPolicy] = Field(default_factory=list)
    defects: list[Defect] = Field(default_factory=list)


class ProjectRecord(FrozenModel):
    id: str
    title: str
    created_at: datetime = Field(default_factory=utc_now)


class LLMNarrativeRequest(FrozenModel):
    project_id: str
    recommendation: RecommendationCard
    assessments: tuple[CandidateAssessment, ...]
    comparisons: tuple[PairwiseComparison, ...]
    source_notes: tuple[str, ...] = ()


class LLMNarrative(FrozenModel):
    markdown: str
    provider: str
    model: str
    evidence_hash: str


class SunoOperationPlan(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("plan"))
    project_id: str
    directive_id: str
    target_artifact_id: str
    operation: OperationType
    objective: str
    source_handling: Literal[
        "do_not_attach_reference_as_sample",
        "use_target_as_edit_parent",
    ]
    selection_start_rule: str
    selection_end_rule: str
    frozen_lyrics_excerpt: str | None = None
    prompt: str
    takes_per_batch: int = 2
    max_batches: int = 2
    rejection_conditions: tuple[str, ...]
    fallback_artifact_id: str
    exact_retention_claimed: bool = False
    external_postproduction_required: bool = False
    subscription_tier: Literal["pro", "premier", "unknown"] = "pro"
    studio_available: bool = False
    workflow_surface: Literal["song_editor", "studio", "create"] = "song_editor"
    source_rules: tuple[
        Literal[
            "do_not_attach_reference_as_sample",
            "use_target_as_edit_parent",
        ],
        ...,
    ] = ()
    steps: tuple[str, ...] = ()
    capability_verified_on: str = "2026-07-31"
    official_sources: tuple[str, ...] = ()
    credit_guardrail: str = (
        "User must explicitly initiate every generation; the tool never spends credits."
    )


class SunoWorkflowRecommendation(FrozenModel):
    status: Literal["actionable", "abstain"]
    project_id: str
    directive_id: str
    target_artifact_id: str
    requested_intent: PreservationIntent
    plan: SunoOperationPlan | None = None
    rationale: tuple[str, ...]
    suggested_fallback: str
    capability_verified_on: str = "2026-07-31"
    official_sources: tuple[str, ...] = ()


class TranscriptSegment(FrozenModel):
    start_s: float
    end_s: float
    text: str
    confidence: float | None = None


class LyricLineLocation(FrozenModel):
    line_index: int
    expected_text: str
    status: Literal[
        "located",
        "possible_missing",
        "possible_changed",
        "low_confidence",
        "unlocatable",
    ]
    start_s: float | None = None
    end_s: float | None = None
    transcript_text: str | None = None
    similarity: float | None = None
    requires_human_confirmation: bool = True


class LyricAnalysis(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("lyrics"))
    project_id: str
    artifact_id: str
    brief_id: str
    provider: str
    transcript: tuple[TranscriptSegment, ...]
    locations: tuple[LyricLineLocation, ...]
    transcript_sha256: str
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactReview(Model):
    artifact_id: str
    requirement_observations: dict[str, OrdinalObservation] = Field(
        default_factory=dict
    )
    craft_observations: tuple[OrdinalObservation, ...] = ()
    release_actions: tuple[ReleaseAction, ...] = ()
    target_windows: dict[str, tuple[float, float]] = Field(default_factory=dict)
    technical_confirmations: dict[str, EvaluationStatus] = Field(default_factory=dict)


class ProjectReviewPacket(Model):
    project_id: str
    artifact_reviews: list[ArtifactReview] = Field(default_factory=list)
    target_brief_id: str | None = None
    target_requirement_observations: dict[str, dict[str, OrdinalObservation]] = Field(
        default_factory=dict
    )
    listening_round_valid: bool = False
    cross_brief_target_compliance_complete: bool = False
    null_baselines: dict[str, dict[str, float]] = Field(default_factory=dict)
    batch_variance_floors: dict[str, dict[str, float]] = Field(default_factory=dict)
    lyric_analyses: list[LyricAnalysis] = Field(default_factory=list)


class ListeningReviewRecord(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("listening_review"))
    project_id: str
    session_id: str
    responses: tuple[ListeningResponse, ...]
    validation: ListeningValidation
    review_packet: ProjectReviewPacket
    created_at: datetime = Field(default_factory=utc_now)


class StoredProjectReview(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("project_review"))
    project_id: str
    source: Literal["cli", "api", "web"]
    review_packet: ProjectReviewPacket
    created_at: datetime = Field(default_factory=utc_now)


class ProjectAnalysisReport(FrozenModel):
    schema_version: str = "1"
    project_id: str
    run: AnalysisRun
    source_assessments: tuple[SourceStateAssessment, ...]
    audio_metrics: tuple[AudioMetrics, ...]
    structures: tuple[StructureAnalysis, ...]
    comparisons: tuple[PairwiseComparison, ...]
    reference_segments: tuple[ReferenceSegment, ...]
    reference_preflights: tuple[ReferencePreflight, ...]
    lyric_analyses: tuple[LyricAnalysis, ...] = ()
    assessments: tuple[CandidateAssessment, ...]
    recommendation: RecommendationCard
    warnings: tuple[str, ...] = ()


class StoredAnalysisReport(FrozenModel):
    id: str
    project_id: str
    report: ProjectAnalysisReport
    created_at: datetime = Field(default_factory=utc_now)


class StoredReleaseDecision(FrozenModel):
    id: str = Field(default_factory=lambda: new_id("release_decision"))
    project_id: str
    analysis_run_id: str
    recommendation: RecommendationCard
    created_at: datetime = Field(default_factory=utc_now)
