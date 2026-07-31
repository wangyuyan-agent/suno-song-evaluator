from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .audio import FeatureSeries, extract_features, load_audio
from .enums import (
    BoundaryQuality,
    EvaluationStatus,
    PreservationIntent,
    Provenance,
)
from .models import (
    PreflightFinding,
    PreservationDirective,
    PreservationEvaluation,
    PreservationFamilyResult,
    ProvenanceRecord,
    ReferencePreflight,
    ReferenceSegment,
    StructureAnalysis,
)

DIRTY_BOUNDARIES = {
    BoundaryQuality.MID_PHRASE,
    BoundaryQuality.MID_WORD,
    BoundaryQuality.MID_TRANSIENT,
}


def analyze_reference_facts(
    segment: ReferenceSegment,
    *,
    source_path: str,
    analysis_run_id: str,
    features: FeatureSeries | None = None,
    structure: StructureAnalysis | None = None,
) -> ReferenceSegment:
    """Populate objective facts while retaining declared semantic annotations."""
    features = features or extract_features(source_path)
    audio = load_audio(source_path)
    start_s = min(segment.start_s, audio.duration_s)
    end_s = min(segment.end_s, audio.duration_s)
    start_sample = int(round(start_s * audio.sample_rate_hz))
    end_sample = int(round(end_s * audio.sample_rate_hz))
    mono = np.asarray(audio.mono[start_sample:end_sample], dtype=np.float64)
    silence_ratio = (
        float(np.mean(np.abs(mono) < 10 ** (-50 / 20))) if len(mono) else 1.0
    )
    frame_mask = (features.times_s >= start_s) & (features.times_s <= end_s)
    indices = np.flatnonzero(frame_mask)
    if len(indices):
        local_energy = features.energy_db[indices]
        energy_percentile = float(
            np.mean(
                np.searchsorted(
                    np.sort(features.energy_db),
                    local_energy,
                    side="right",
                )
                / max(1, len(features.energy_db))
            )
        )
        combined = np.column_stack(
            (
                features.chroma[indices],
                (local_energy - np.mean(local_energy))[:, None]
                / (np.std(local_energy) + 1e-9),
                (features.onset[indices] - np.mean(features.onset[indices]))[:, None]
                / (np.std(features.onset[indices]) + 1e-9),
            )
        )
        novelty = (
            np.linalg.norm(np.diff(combined, axis=0), axis=1)
            if len(combined) > 1
            else np.array([0.0])
        )
        homogeneity = float(
            np.clip(
                1.0 - np.percentile(novelty, 75) / (np.sqrt(14) + 1e-9),
                0.0,
                1.0,
            )
        )
    else:
        energy_percentile = None
        homogeneity = None
    high_onsets = features.times_s[features.onset >= np.percentile(features.onset, 80)]

    def nearest_onset(time_s: float) -> float | None:
        if not len(high_onsets):
            return None
        return float(np.min(np.abs(high_onsets - time_s)))

    low_energy = features.times_s[
        features.energy_db <= np.percentile(features.energy_db, 20)
    ]

    def clean_boundary(time_s: float, direction: int) -> float | None:
        if direction < 0:
            candidates = low_energy[
                (low_energy <= time_s) & (low_energy >= max(0.0, time_s - 3.0))
            ]
            return float(candidates[-1]) if len(candidates) else None
        candidates = low_energy[
            (low_energy >= time_s)
            & (low_energy <= min(features.duration_s, time_s + 3.0))
        ]
        return float(candidates[0]) if len(candidates) else None

    section_count = None
    if structure is not None:
        internal = [
            boundary
            for boundary in structure.boundaries
            if start_s < boundary.time_s < end_s
        ]
        section_count = len(internal) + 1
    evidence = segment.evidence + (
        ProvenanceRecord(
            provenance=Provenance.INFERRED,
            source=f"objective audio analysis run {analysis_run_id}",
            note="silence, onset, energy, homogeneity and structure facts",
        ),
    )
    payload = segment.model_dump(mode="python")
    payload.update(
        {
            "onset_proximity_start_s": nearest_onset(start_s),
            "onset_proximity_end_s": nearest_onset(end_s),
            "local_energy_percentile": energy_percentile,
            "clean_superset_start_s": clean_boundary(start_s, -1),
            "clean_superset_end_s": clean_boundary(end_s, 1),
            "internal_homogeneity": homogeneity,
            "silence_ratio": silence_ratio,
            "structure_section_count": section_count
            if section_count is not None
            else segment.structure_section_count,
            "analysis_run_id": analysis_run_id,
            "evidence": evidence,
        }
    )
    return ReferenceSegment.model_validate(payload)


def preflight_reference(
    segment: ReferenceSegment,
    *,
    accepted_min_duration_s: float = 6.0,
    accepted_max_duration_s: float = 8 * 60.0,
    target_song_duration_s: float | None = None,
) -> ReferencePreflight:
    findings: list[PreflightFinding] = []
    if not accepted_min_duration_s <= segment.duration_s <= accepted_max_duration_s:
        findings.append(
            PreflightFinding(
                code="platform_duration_risk",
                severity="blocking",
                message="Reference duration is outside the configured platform range.",
                evidence=f"duration={segment.duration_s:.3f}s",
            )
        )
    dirty = (
        segment.start_boundary in DIRTY_BOUNDARIES
        or segment.end_boundary in DIRTY_BOUNDARIES
    )
    if dirty:
        findings.append(
            PreflightFinding(
                code="dirty_boundaries",
                severity="warning",
                message=(
                    "The reference begins or ends inside a phrase, word, or transient."
                ),
                evidence=(
                    f"start={segment.start_boundary.value}, "
                    f"end={segment.end_boundary.value}"
                ),
            )
        )
    if (
        segment.onset_proximity_start_s is not None
        and segment.onset_proximity_start_s < 0.080
    ) or (
        segment.onset_proximity_end_s is not None
        and segment.onset_proximity_end_s < 0.080
    ):
        findings.append(
            PreflightFinding(
                code="boundary_near_transient",
                severity="warning",
                message="A boundary is very close to a detected onset.",
                evidence="onset proximity < 80 ms",
            )
        )
    crosses_sections = bool(
        segment.structure_section_count is not None
        and segment.structure_section_count > 1
    )
    if crosses_sections:
        findings.append(
            PreflightFinding(
                code="crosses_structure_sections",
                severity="warning",
                message="The reference crosses more than one structural section.",
                evidence=f"section_count={segment.structure_section_count}",
            )
        )
    multi_state = bool(
        (
            segment.internal_homogeneity is not None
            and segment.internal_homogeneity < 0.65
        )
        or len(segment.semantic_roles) >= 3
        or crosses_sections
    )
    if multi_state:
        findings.append(
            PreflightFinding(
                code="multiple_statistical_states",
                severity="warning",
                message="The reference contains multiple musical/statistical states.",
                evidence=(
                    f"homogeneity={segment.internal_homogeneity}, "
                    f"roles={list(segment.semantic_roles)}"
                ),
            )
        )
    if segment.vocal_activity_ratio is not None and segment.vocal_activity_ratio > 0.85:
        findings.append(
            PreflightFinding(
                code="vocal_active_boundaries",
                severity="info",
                message=(
                    "Most of the reference is vocally active; clean phrase cuts matter."
                ),
                evidence=f"vocal_activity_ratio={segment.vocal_activity_ratio:.3f}",
            )
        )
    effective_duration = (
        segment.duration_s * (1.0 - segment.silence_ratio)
        if segment.silence_ratio is not None
        else None
    )
    if segment.silence_ratio is not None and segment.silence_ratio > 0.35:
        findings.append(
            PreflightFinding(
                code="high_silence_ratio",
                severity="warning",
                message="A large part of the reference is silent or near-silent.",
                evidence=f"silence_ratio={segment.silence_ratio:.3f}",
            )
        )
    if (
        target_song_duration_s is not None
        and target_song_duration_s > 0
        and segment.duration_s / target_song_duration_s < 0.05
    ):
        findings.append(
            PreflightFinding(
                code="short_relative_to_target",
                severity="info",
                message="The reference is short relative to the target song.",
                evidence=(f"ratio={segment.duration_s / target_song_duration_s:.4f}"),
            )
        )
    if not findings:
        findings.append(
            PreflightFinding(
                code="no_automatic_risk_detected",
                severity="info",
                message="No automatic boundary or duration risk was detected.",
                evidence="automatic checks only; aesthetic value was not judged",
            )
        )
    return ReferencePreflight(
        reference_segment_id=segment.id,
        findings=tuple(findings),
        effective_non_silent_duration_s=effective_duration,
        multi_state=multi_state,
        crosses_sections=crosses_sections,
        dirty_boundaries=dirty,
    )


def _normalize_sequence(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    return (values - np.mean(values)) / (np.std(values) + 1e-9)


def _block_shuffle(values: np.ndarray, blocks: int = 4) -> np.ndarray:
    chunks = np.array_split(values, blocks)
    order = list(range(len(chunks)))
    if len(order) > 2:
        order = order[1::2] + order[::2]
    else:
        order.reverse()
    return np.concatenate([chunks[index] for index in order], axis=0)


def _sequence_similarity(reference: np.ndarray, candidate: np.ndarray) -> float:
    if reference.ndim == 2:
        ref_norm = reference / np.maximum(
            np.linalg.norm(reference, axis=1, keepdims=True), 1e-12
        )
        candidate_norm = candidate / np.maximum(
            np.linalg.norm(candidate, axis=1, keepdims=True), 1e-12
        )
        return float(np.mean(np.sum(ref_norm * candidate_norm, axis=1)))
    ref = _normalize_sequence(reference)
    other = _normalize_sequence(candidate)
    values: list[float] = []
    for lag in range(-3, 4):
        if lag < 0:
            ref_view = ref[-lag:]
            other_view = other[: len(other) + lag]
        elif lag > 0:
            ref_view = ref[: len(ref) - lag]
            other_view = other[lag:]
        else:
            ref_view = ref
            other_view = other
        if len(ref_view) >= max(4, int(0.9 * len(ref))):
            with np.errstate(invalid="ignore", divide="ignore"):
                score = float(np.corrcoef(ref_view, other_view)[0, 1])
            if np.isfinite(score):
                values.append(score)
    return max(values) if values else float("-inf")


def _sequence_coverage(reference: np.ndarray, candidate: np.ndarray) -> float:
    """Return the fraction of reference frames supported by a close target frame."""
    if len(reference) == 0 or len(reference) != len(candidate):
        return 0.0
    if reference.ndim == 2:
        ref_norms = np.linalg.norm(reference, axis=1)
        candidate_norms = np.linalg.norm(candidate, axis=1)
        both_silent = (ref_norms <= 1e-12) & (candidate_norms <= 1e-12)
        comparable = (ref_norms > 1e-12) & (candidate_norms > 1e-12)
        similarities = np.zeros(len(reference), dtype=np.float64)
        similarities[both_silent] = 1.0
        similarities[comparable] = np.sum(
            reference[comparable] * candidate[comparable],
            axis=1,
        ) / (ref_norms[comparable] * candidate_norms[comparable])
        return float(np.mean(similarities >= 0.8))
    ref = _normalize_sequence(reference)
    other = _normalize_sequence(candidate)
    coverages: list[float] = []
    total = len(ref)
    for lag in range(-3, 4):
        if lag < 0:
            ref_view = ref[-lag:]
            other_view = other[: len(other) + lag]
        elif lag > 0:
            ref_view = ref[: len(ref) - lag]
            other_view = other[lag:]
        else:
            ref_view = ref
            other_view = other
        if len(ref_view) >= max(4, int(0.9 * total)):
            finite = np.isfinite(ref_view) & np.isfinite(other_view)
            close = finite & (np.abs(ref_view - other_view) <= 0.75)
            coverages.append(float(np.count_nonzero(close) / total))
    return max(coverages, default=0.0)


def _sliding_matches(
    reference: np.ndarray,
    target: np.ndarray,
    reference_hop_s: float,
    target_hop_s: float,
    *,
    step_s: float = 0.1,
) -> list[tuple[float, float, float, float]]:
    target_length = max(
        2,
        int(round(len(reference) * reference_hop_s / target_hop_s)),
    )
    if len(target) < target_length:
        return []
    reference_positions = np.linspace(0, len(reference) - 1, target_length)
    if reference.ndim == 2:
        resampled = np.column_stack(
            [
                np.interp(
                    reference_positions, np.arange(len(reference)), reference[:, i]
                )
                for i in range(reference.shape[1])
            ]
        )
    else:
        resampled = np.interp(
            reference_positions,
            np.arange(len(reference)),
            reference,
        )
    step = max(1, int(round(step_s / target_hop_s)))
    result: list[tuple[float, float, float, float]] = []
    for start in range(0, len(target) - target_length + 1, step):
        end = start + target_length
        target_window = target[start:end]
        score = _sequence_similarity(resampled, target_window)
        coverage = _sequence_coverage(resampled, target_window)
        if np.isfinite(score) and np.isfinite(coverage):
            result.append(
                (
                    float(score),
                    start * target_hop_s,
                    end * target_hop_s,
                    coverage,
                )
            )
    return result


def _family_result(
    *,
    family: str,
    reference_values: np.ndarray,
    target_values: np.ndarray,
    reference_hop_s: float,
    target_hop_s: float,
    null_baseline: float | None,
    batch_variance_floor: float | None,
    minimum_coverage_ratio: float | None,
    best_to_second_margin: float | None,
    null_margin: float | None,
    batch_variance_margin: float | None,
) -> PreservationFamilyResult:
    matches = sorted(
        _sliding_matches(
            reference_values,
            target_values,
            reference_hop_s,
            target_hop_s,
        ),
        reverse=True,
    )
    if not matches:
        return PreservationFamilyResult(
            family=family,
            status=EvaluationStatus.INDETERMINATE,
            evidence="target is shorter than the reference or has no valid frames",
        )
    best_score, start, end, coverage_ratio = matches[0]
    second = next(
        (
            score
            for score, other_start, other_end, _coverage in matches[1:]
            if other_end <= start or other_start >= end
        ),
        None,
    )
    shuffled = _block_shuffle(reference_values)
    shuffled_matches = _sliding_matches(
        shuffled,
        target_values,
        reference_hop_s,
        target_hop_s,
    )
    shuffled_score = max((item[0] for item in shuffled_matches), default=None)
    gaps: list[str] = []
    if minimum_coverage_ratio is None:
        gaps.append("missing minimum coverage threshold")
    if best_to_second_margin is None:
        gaps.append("missing best-to-second threshold")
    if null_margin is None:
        gaps.append("missing null margin threshold")
    if batch_variance_margin is None:
        gaps.append("missing batch-variance margin threshold")
    if null_baseline is None:
        gaps.append("missing null control")
    if batch_variance_floor is None:
        gaps.append("missing batch variance floor")
    if second is None:
        gaps.append("missing non-overlapping second-best window")
    if shuffled_score is None:
        gaps.append("missing shuffled-reference control")
    if gaps:
        status = EvaluationStatus.INDETERMINATE
    else:
        passed = (
            coverage_ratio >= float(minimum_coverage_ratio)
            and best_score - float(second) >= float(best_to_second_margin)
            and best_score - float(shuffled_score) >= float(best_to_second_margin)
            and best_score - float(null_baseline) >= float(null_margin)
            and best_score - float(batch_variance_floor) >= float(batch_variance_margin)
        )
        status = EvaluationStatus.PASS if passed else EvaluationStatus.FAIL
    return PreservationFamilyResult(
        family=family,
        status=status,
        best_window_s=(start, end),
        score=best_score,
        coverage_ratio=coverage_ratio,
        second_best_score=second,
        shuffled_score=shuffled_score,
        null_baseline=null_baseline,
        batch_variance_floor=batch_variance_floor,
        evidence="; ".join(gaps)
        if gaps
        else (
            "coverage and independent control margins evaluated against "
            "declared thresholds"
        ),
    )


def evaluate_exact_or_melody_retention(
    directive: PreservationDirective,
    *,
    artifact_id: str,
    reference_path: str,
    target_path: str,
    null_baselines: dict[str, float] | None,
    batch_variance_floors: dict[str, float] | None,
    acquisition_comparable: bool = False,
) -> PreservationEvaluation:
    if directive.preservation_intent not in {
        PreservationIntent.EXACT_AUDIO,
        PreservationIntent.MELODY_RHYTHM,
    }:
        raise ValueError("directive is not exact_audio or melody_rhythm")
    reference = extract_features(reference_path)
    target = extract_features(target_path)
    null_baselines = null_baselines or {}
    batch_variance_floors = batch_variance_floors or {}
    pitch = _family_result(
        family="pitch_harmony",
        reference_values=reference.chroma,
        target_values=target.chroma,
        reference_hop_s=reference.hop_s,
        target_hop_s=target.hop_s,
        null_baseline=null_baselines.get("pitch_harmony"),
        batch_variance_floor=batch_variance_floors.get("pitch_harmony"),
        minimum_coverage_ratio=directive.thresholds.min_coverage_ratio,
        best_to_second_margin=directive.thresholds.min_best_to_second_margin,
        null_margin=directive.thresholds.min_null_margin,
        batch_variance_margin=(directive.thresholds.min_batch_variance_margin),
    )
    rhythm = _family_result(
        family="rhythm_onset",
        reference_values=reference.onset,
        target_values=target.onset,
        reference_hop_s=reference.hop_s,
        target_hop_s=target.hop_s,
        null_baseline=null_baselines.get("rhythm_onset"),
        batch_variance_floor=batch_variance_floors.get("rhythm_onset"),
        minimum_coverage_ratio=directive.thresholds.min_coverage_ratio,
        best_to_second_margin=directive.thresholds.min_best_to_second_margin,
        null_margin=directive.thresholds.min_null_margin,
        batch_variance_margin=(directive.thresholds.min_batch_variance_margin),
    )
    family_results = (pitch, rhythm)
    passing = sum(item.status == EvaluationStatus.PASS for item in family_results)
    required = directive.thresholds.min_feature_families
    if not acquisition_comparable or any(
        item.status == EvaluationStatus.INDETERMINATE for item in family_results
    ):
        status = EvaluationStatus.INDETERMINATE
    elif passing >= required:
        windows = [item.best_window_s for item in family_results if item.best_window_s]
        starts = [item[0] for item in windows]
        ends = [item[1] for item in windows]
        overlap = min(ends) - max(starts)
        status = (
            EvaluationStatus.PASS if overlap > 0 else EvaluationStatus.INDETERMINATE
        )
    else:
        status = EvaluationStatus.FAIL
    windows = [item.best_window_s for item in family_results if item.best_window_s]
    target_window = (
        (max(item[0] for item in windows), min(item[1] for item in windows))
        if windows
        and min(item[1] for item in windows) > max(item[0] for item in windows)
        else None
    )
    gaps = tuple(
        item.evidence
        for item in family_results
        if item.status == EvaluationStatus.INDETERMINATE
    )
    if not acquisition_comparable:
        gaps += ("reference and target acquisition paths are not comparable",)
    return PreservationEvaluation(
        directive_id=directive.id,
        artifact_id=artifact_id,
        status=status,
        family_results=family_results,
        target_window_s=target_window,
        evidence_gaps=gaps,
    )


def _longest_true_run(values: np.ndarray, hop_s: float) -> tuple[int, int, float]:
    best_start = best_end = current_start = 0
    in_run = False
    for index, value in enumerate(values):
        if value and not in_run:
            current_start = index
            in_run = True
        if in_run and (not value or index == len(values) - 1):
            end = index if not value else index + 1
            if end - current_start > best_end - best_start:
                best_start, best_end = current_start, end
            in_run = False
    return best_start, best_end, (best_end - best_start) * hop_s


def evaluate_structural_gesture(
    directive: PreservationDirective,
    *,
    artifact_id: str,
    features: FeatureSeries,
    structure: StructureAnalysis,
    target_window_s: tuple[float, float] | None,
) -> PreservationEvaluation:
    if directive.preservation_intent != PreservationIntent.STRUCTURAL_GESTURE:
        raise ValueError("directive is not structural_gesture")
    thresholds = directive.thresholds
    required = (
        thresholds.gesture_valley_duration_s,
        thresholds.gesture_min_step_db,
        thresholds.gesture_max_delay_s,
        thresholds.noise_band,
    )
    if target_window_s is None or any(value is None for value in required):
        return PreservationEvaluation(
            directive_id=directive.id,
            artifact_id=artifact_id,
            status=EvaluationStatus.INDETERMINATE,
            target_window_s=target_window_s,
            evidence_gaps=("target window or gesture thresholds are missing",),
            review_questions=(
                "Does the vocal and arrangement move from restrained to open here?",
            ),
        )
    start_s, end_s = target_window_s
    indices = np.flatnonzero(
        (features.times_s >= start_s) & (features.times_s <= end_s)
    )
    if len(indices) < 3:
        return PreservationEvaluation(
            directive_id=directive.id,
            artifact_id=artifact_id,
            status=EvaluationStatus.INDETERMINATE,
            target_window_s=target_window_s,
            evidence_gaps=("target window has too few frames",),
        )
    energy = features.energy_db[indices]
    valley_cutoff = np.percentile(energy, 30)
    valley_start, valley_end, valley_duration = _longest_true_run(
        energy <= valley_cutoff,
        features.hop_s,
    )
    min_duration = float(thresholds.gesture_valley_duration_s)
    noise_band = float(thresholds.noise_band)

    def at_least(value: float, threshold: float) -> EvaluationStatus:
        if value >= threshold + noise_band:
            return EvaluationStatus.PASS
        if value <= threshold - noise_band:
            return EvaluationStatus.FAIL
        return EvaluationStatus.INDETERMINATE

    valley_status = at_least(valley_duration, min_duration)
    valley_end_index = indices[min(valley_end, len(indices) - 1)]
    max_delay_frames = max(
        1,
        int(round(float(thresholds.gesture_max_delay_s) / features.hop_s)),
    )
    post_end = min(len(features.energy_db), valley_end_index + max_delay_frames + 1)
    baseline = float(
        np.mean(
            features.energy_db[
                indices[valley_start] : max(indices[valley_start] + 1, valley_end_index)
            ]
        )
    )
    post_peak = (
        float(np.max(features.energy_db[valley_end_index:post_end]))
        if post_end > valley_end_index
        else baseline
    )
    step = post_peak - baseline
    step_status = at_least(step, float(thresholds.gesture_min_step_db))
    boundary_tolerance = max(
        features.hop_s * 2,
        float(thresholds.gesture_max_delay_s),
    )
    edge_tolerance = features.hop_s
    boundary_distances = [
        abs(boundary.time_s - features.times_s[valley_end_index])
        for boundary in structure.boundaries
        if boundary.time_s > edge_tolerance
        and boundary.time_s < features.duration_s - edge_tolerance
    ]
    evidence_gaps: list[str] = []
    if not boundary_distances:
        boundary_status = EvaluationStatus.INDETERMINATE
        evidence_gaps.append("no interior structure boundary was detected")
    else:
        distance = min(boundary_distances)
        if distance <= max(0.0, boundary_tolerance - noise_band):
            boundary_status = EvaluationStatus.PASS
        elif distance >= boundary_tolerance + noise_band:
            boundary_status = EvaluationStatus.FAIL
        else:
            boundary_status = EvaluationStatus.INDETERMINATE
    checks = {
        "low_energy_or_vocal_valley": valley_status,
        "post_valley_energy_step": step_status,
        "independent_structure_boundary": boundary_status,
    }
    if any(value == EvaluationStatus.FAIL for value in checks.values()):
        status = EvaluationStatus.FAIL
    elif all(value == EvaluationStatus.PASS for value in checks.values()):
        status = EvaluationStatus.PASS
    else:
        status = EvaluationStatus.INDETERMINATE
    return PreservationEvaluation(
        directive_id=directive.id,
        artifact_id=artifact_id,
        status=status,
        target_window_s=target_window_s,
        evidence_gaps=tuple(evidence_gaps),
        objective_checks=checks,
        review_questions=(
            "Does the bridge feel restrained rather than merely quieter?",
            (
                "Does the chorus open in chest register without changing "
                "vocalist identity?"
            ),
            "Does the arrangement widen without adding an instrumental detour?",
        ),
    )


def preservation_enters_compliance(
    directive: PreservationDirective,
    evaluation: PreservationEvaluation,
) -> bool:
    return directive.must_preserve and evaluation.status in {
        EvaluationStatus.PASS,
        EvaluationStatus.FAIL,
        EvaluationStatus.INDETERMINATE,
        EvaluationStatus.NOT_EVALUATED,
    }


@dataclass
class ProtectedIslandTracker:
    original_artifact_id: str
    max_accepted_edits: int = 5
    credit_budget: int | None = None
    credits_spent: int = 0
    accepted_artifact_ids: list[str] | None = None
    rejected_artifact_ids: list[str] | None = None
    consecutive_rejections: int = 0
    retention_history: list[float] | None = None

    def __post_init__(self) -> None:
        self.accepted_artifact_ids = self.accepted_artifact_ids or []
        self.rejected_artifact_ids = self.rejected_artifact_ids or []
        self.retention_history = self.retention_history or []

    @property
    def next_parent_id(self) -> str:
        return (
            self.accepted_artifact_ids[-1]
            if self.accepted_artifact_ids
            else self.original_artifact_id
        )

    def record(
        self,
        artifact_id: str,
        *,
        accepted: bool,
        retention_vs_original: float,
        credit_cost: int = 0,
    ) -> None:
        if credit_cost < 0:
            raise ValueError("credit_cost cannot be negative")
        self.credits_spent += credit_cost
        self.retention_history.append(retention_vs_original)
        if accepted:
            self.accepted_artifact_ids.append(artifact_id)
            self.consecutive_rejections = 0
        else:
            self.rejected_artifact_ids.append(artifact_id)
            self.consecutive_rejections += 1

    @property
    def should_stop(self) -> bool:
        declining = (
            len(self.retention_history) >= 3
            and self.retention_history[-1]
            < self.retention_history[-2]
            < self.retention_history[-3]
        )
        return (
            self.consecutive_rejections >= 2
            or len(self.accepted_artifact_ids) >= self.max_accepted_edits
            or declining
            or (
                self.credit_budget is not None
                and self.credits_spent >= self.credit_budget
            )
        )
