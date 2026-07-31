from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from songeval.enums import (
    AcquisitionPath,
    Axis,
    EvaluationStatus,
    OperationType,
    Provenance,
    ReadinessStatus,
    TaskType,
)
from songeval.models import (
    AxisEvaluation,
    AxisThreshold,
    BriefRequirement,
    CandidateAssessment,
    ComplianceFloor,
    CreativeBriefVersion,
    GenerationEvent,
    OrdinalObservation,
    ProjectDecisionPolicy,
    ProjectManifest,
    ProvenanceRecord,
    ReleaseArtifact,
    Take,
)


@pytest.fixture
def provenance() -> ProvenanceRecord:
    return ProvenanceRecord(
        provenance=Provenance.DECLARED,
        source="test",
    )


def synth_song(
    path: Path,
    *,
    duration_s: float = 8.0,
    sample_rate: int = 16_000,
    gain: float = 1.0,
    prefix_s: float = 0.0,
) -> Path:
    frames = int(duration_s * sample_rate)
    t = np.arange(frames) / sample_rate
    frequency = np.where(
        t < duration_s / 3,
        220.0,
        np.where(t < 2 * duration_s / 3, 329.63, 261.63),
    )
    phase = 2 * np.pi * np.cumsum(frequency) / sample_rate
    pulse = (np.mod(t, 0.5) < 0.08).astype(float)
    envelope = 0.25 + 0.35 * pulse
    mono = gain * envelope * np.sin(phase)
    fade = min(sample_rate // 20, len(mono) // 4)
    if fade:
        mono[:fade] *= np.linspace(0, 1, fade)
        mono[-fade:] *= np.linspace(1, 0, fade)
    stereo = np.column_stack((mono, mono * 0.92))
    if prefix_s:
        prefix = np.zeros((int(prefix_s * sample_rate), 2))
        stereo = np.concatenate((prefix, stereo), axis=0)
    sf.write(path, stereo, sample_rate, subtype="PCM_16")
    return path


@pytest.fixture
def tone_a(tmp_path: Path) -> Path:
    return synth_song(tmp_path / "a.wav")


@pytest.fixture
def tone_b(tmp_path: Path) -> Path:
    path = tmp_path / "b.wav"
    sample_rate = 16_000
    duration = 8.0
    t = np.arange(int(sample_rate * duration)) / sample_rate
    frequency = np.where(t < 4.0, 246.94, 392.0)
    phase = 2 * np.pi * np.cumsum(frequency) / sample_rate
    mono = 0.35 * np.sin(phase) * (0.7 + 0.3 * (np.mod(t, 0.4) < 0.05))
    sf.write(path, np.column_stack((mono, mono * 0.8)), sample_rate, subtype="PCM_16")
    return path


@pytest.fixture
def brief(provenance: ProvenanceRecord) -> CreativeBriefVersion:
    return CreativeBriefVersion(
        id="brief_test",
        project_id="project_test",
        version="v1",
        lyrics="line one\nline two",
        requirements=(
            BriefRequirement(
                id="lyrics",
                label="lyrics",
                value="line one",
                hard=True,
                burden_bearing=True,
                provenance=provenance,
            ),
            BriefRequirement(
                id="style",
                label="style",
                value="quiet",
                hard=False,
                provenance=provenance,
            ),
        ),
        field_provenance={"lyrics": provenance},
    )


@pytest.fixture
def complete_policy() -> ProjectDecisionPolicy:
    return ProjectDecisionPolicy(
        id="policy_test",
        project_id="project_test",
        version="v1",
        declared_by_user=True,
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
            AxisThreshold(axis=Axis.COMPLIANCE, ordinal_delta=1, source="test"),
            AxisThreshold(axis=Axis.CRAFT, ordinal_delta=1, source="test"),
            AxisThreshold(
                axis=Axis.RELEASE_READINESS,
                ordinal_delta=1,
                source="test",
            ),
        ),
        max_na_ratio=0.25,
        abstention_strategy="abstain on critical unknown",
    )


def candidate(
    artifact_id: str,
    *,
    take_id: str | None = None,
    brief_id: str = "brief_test",
    compliance: int | None = 3,
    craft: int | None = 2,
    readiness: ReadinessStatus = ReadinessStatus.READY,
    defects=(),
) -> CandidateAssessment:
    readiness_value = {
        ReadinessStatus.READY: 3,
        ReadinessStatus.NEEDS_SUNO_EDIT: 2,
        ReadinessStatus.BLOCKED: 0,
        ReadinessStatus.INDETERMINATE: None,
    }[readiness]
    return CandidateAssessment(
        artifact_id=artifact_id,
        take_id=take_id or f"take_{artifact_id}",
        brief_id=brief_id,
        evaluations=(
            AxisEvaluation(
                artifact_id=artifact_id,
                axis=Axis.COMPLIANCE,
                status=EvaluationStatus.PASS
                if compliance is not None
                else EvaluationStatus.INDETERMINATE,
                observations=(
                    OrdinalObservation(
                        criterion="requirements",
                        value=compliance,
                        evidence="test",
                    ),
                ),
            ),
            AxisEvaluation(
                artifact_id=artifact_id,
                axis=Axis.CRAFT,
                status=EvaluationStatus.PASS
                if craft is not None
                else EvaluationStatus.INDETERMINATE,
                observations=(
                    OrdinalObservation(
                        criterion="structure",
                        value=craft,
                        evidence="test",
                        start_s=12.0,
                        end_s=18.0,
                    ),
                ),
            ),
            AxisEvaluation(
                artifact_id=artifact_id,
                axis=Axis.RELEASE_READINESS,
                status=EvaluationStatus.PASS
                if readiness in {ReadinessStatus.READY, ReadinessStatus.NEEDS_SUNO_EDIT}
                else EvaluationStatus.INDETERMINATE,
                readiness=readiness,
                observations=(
                    OrdinalObservation(
                        criterion="suno_only_release_path",
                        value=readiness_value,
                        evidence="test",
                    ),
                ),
            ),
            AxisEvaluation(
                artifact_id=artifact_id,
                axis=Axis.DISTINCTIVENESS,
                status=EvaluationStatus.PASS,
                observations=(
                    OrdinalObservation(
                        criterion="pitch_harmony",
                        value=2,
                        evidence="computed",
                    ),
                ),
                ignored_for_ordering=True,
            ),
        ),
        defects=tuple(defects),
    )


@pytest.fixture
def minimal_manifest(
    tmp_path: Path,
    tone_a: Path,
    brief: CreativeBriefVersion,
) -> ProjectManifest:
    event = GenerationEvent(
        id="event_test",
        project_id="project_test",
        brief_id=brief.id,
        task=TaskType.CREATE,
        raw_metadata={"unknown_platform_field": {"kept": True}},
    )
    take = Take(
        id="take_test",
        project_id="project_test",
        generation_event_id=event.id,
    )
    artifact = ReleaseArtifact(
        id="artifact_test",
        project_id="project_test",
        take_id=take.id,
        local_path=str(tone_a),
        operation=OperationType.RAW,
        acquisition_path=AcquisitionPath.UNKNOWN,
        platform_reported_duration_s=8.2,
        raw_payload={"arbitrary": 7},
    )
    return ProjectManifest(
        project_id="project_test",
        title="test",
        briefs=[brief],
        sources=[],
        generation_events=[event],
        takes=[take],
        artifacts=[artifact],
    )
