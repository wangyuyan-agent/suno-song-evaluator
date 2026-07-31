from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audio import audio_identity
from .enums import (
    AcquisitionPath,
    PreservationIntent,
    ProtectedDimension,
    Provenance,
    TargetPlacement,
    TaskType,
)
from .importers import cache_local_audio
from .models import (
    GenerationEvent,
    PreservationDirective,
    PreservationThresholds,
    ProjectManifest,
    ProvenanceRecord,
    ReferenceSegment,
    ReleaseArtifact,
    Take,
)
from .util import slugify


@dataclass(frozen=True)
class ReferenceRegistration:
    event: GenerationEvent | None
    take: Take | None
    artifact: ReleaseArtifact
    reference: ReferenceSegment
    directive: PreservationDirective


def registered_directive_target_id(
    directive: PreservationDirective,
    artifacts: tuple[ReleaseArtifact, ...] | list[ReleaseArtifact],
) -> str | None:
    """Resolve the explicit target, including pre-field deterministic IDs."""
    if directive.target_artifact_id is not None:
        return directive.target_artifact_id
    matches = [
        artifact.id
        for artifact in artifacts
        if directive.id.startswith(
            f"directive_{slugify(artifact.id)}_{directive.preservation_intent.value}_"
        )
    ]
    return matches[0] if len(matches) == 1 else None


def register_local_reference(
    manifest: ProjectManifest,
    *,
    target_artifact_id: str,
    reference_path: str | Path,
    media_dir: str | Path,
    intent: PreservationIntent,
    start_s: float = 0.0,
    end_s: float | None = None,
    must_preserve: bool | None = None,
) -> ReferenceRegistration:
    """Register source evidence without attaching it to a generation event."""
    target = next(
        (item for item in manifest.artifacts if item.id == target_artifact_id),
        None,
    )
    if target is None:
        raise ValueError(f"unknown target artifact: {target_artifact_id}")
    requested_source = Path(reference_path).expanduser()
    if not requested_source.is_absolute():
        raise ValueError("reference audio path must be absolute")
    source = requested_source.resolve()
    if not source.is_file():
        raise ValueError(f"reference audio file does not exist: {source}")
    digest, encoding, duration = audio_identity(source)
    effective_end = duration if end_s is None else end_s
    if start_s < 0 or effective_end <= start_s or effective_end > duration + 0.002:
        raise ValueError("reference interval must lie inside the measured audio")
    destination = Path(media_dir).expanduser().resolve()
    cached = cache_local_audio(
        source,
        destination / f"reference-{digest[:16]}{source.suffix.lower()}",
    )
    target_take = next(
        (item for item in manifest.takes if item.id == target.take_id),
        None,
    )
    if target_take is None:
        raise ValueError(f"unknown take for target artifact: {target.id}")
    target_event = next(
        (
            item
            for item in manifest.generation_events
            if item.id == target_take.generation_event_id
        ),
        None,
    )
    if target_event is None:
        raise ValueError(f"unknown generation event for take: {target_take.id}")

    existing_artifact = next(
        (
            item
            for item in manifest.artifacts
            if item.file_sha256 == digest
            and item.raw_payload.get("analysis_role") == "reference_only"
        ),
        None,
    )
    event: GenerationEvent | None = None
    take: Take | None = None
    if existing_artifact is not None:
        artifact = existing_artifact
    else:
        stable = f"reference_{digest[:16]}"
        event = GenerationEvent(
            id=f"event_{stable}",
            project_id=manifest.project_id,
            brief_id=target_event.brief_id,
            task=TaskType.AUDIO_UPLOAD,
            raw_metadata={"purpose": "reference evidence only"},
        )
        take = Take(
            id=f"take_{stable}",
            project_id=manifest.project_id,
            generation_event_id=event.id,
        )
        artifact = ReleaseArtifact(
            id=f"artifact_{stable}",
            project_id=manifest.project_id,
            take_id=take.id,
            title=f"Reference {source.stem}",
            local_path=str(cached),
            file_sha256=digest,
            acquisition_path=AcquisitionPath.USER_PROVIDED_UNKNOWN,
            encoding=encoding,
            measured_file_duration_s=duration,
            raw_payload={
                "analysis_role": "reference_only",
                "source_filename": source.name,
            },
        )
    interval_key = f"{start_s:.3f}-{effective_end:.3f}"
    reference_id = f"reference_{digest[:12]}_{slugify(interval_key)}"
    reference = next(
        (
            item
            for item in manifest.references
            if item.source_artifact_id == artifact.id
            and abs(item.start_s - start_s) <= 0.001
            and abs(item.end_s - effective_end) <= 0.001
        ),
        ReferenceSegment(
            id=reference_id,
            project_id=manifest.project_id,
            source_artifact_id=artifact.id,
            title=source.stem,
            start_s=start_s,
            end_s=effective_end,
            evidence=(
                ProvenanceRecord(
                    provenance=Provenance.DECLARED,
                    source="local reference registration",
                ),
            ),
        ),
    )
    protected = {
        PreservationIntent.EXACT_AUDIO: (
            ProtectedDimension.MELODY,
            ProtectedDimension.RHYTHM,
            ProtectedDimension.HARMONY,
            ProtectedDimension.TIMBRE,
            ProtectedDimension.VOCAL_IDENTITY,
            ProtectedDimension.ARRANGEMENT,
        ),
        PreservationIntent.MELODY_RHYTHM: (
            ProtectedDimension.MELODY,
            ProtectedDimension.RHYTHM,
        ),
        PreservationIntent.STRUCTURAL_GESTURE: (
            ProtectedDimension.STRUCTURE_SHAPE,
            ProtectedDimension.ENERGY_ENVELOPE,
        ),
    }[intent]
    directive_id = (
        f"directive_{slugify(target_artifact_id)}_{intent.value}_"
        f"{digest[:12]}_{slugify(interval_key)}"
    )
    directive = next(
        (item for item in manifest.directives if item.id == directive_id),
        None,
    )
    if directive is not None:
        if must_preserve is not None and directive.must_preserve != must_preserve:
            raise ValueError(
                f"existing directive {directive.id} has "
                f"must_preserve={directive.must_preserve}, not {must_preserve}"
            )
    else:
        directive = PreservationDirective(
            id=directive_id,
            project_id=manifest.project_id,
            brief_id=target_event.brief_id,
            reference_segment_id=reference.id,
            target_artifact_id=target.id,
            preservation_intent=intent,
            protected_dimensions=protected,
            target_placement=(
                TargetPlacement.SECTION_RELATIVE
                if intent == PreservationIntent.STRUCTURAL_GESTURE
                else TargetPlacement.GLOBAL_CONDITIONER
            ),
            placement_enforceable=intent == PreservationIntent.STRUCTURAL_GESTURE,
            must_preserve=(
                must_preserve
                if must_preserve is not None
                else intent != PreservationIntent.STRUCTURAL_GESTURE
            ),
            thresholds=PreservationThresholds(),
            abstention_strategy=(
                "prefer the gesture but do not gate release"
                if intent == PreservationIntent.STRUCTURAL_GESTURE
                else "abstain unless measured retention evidence is sufficient"
            ),
        )
    return ReferenceRegistration(
        event=event,
        take=take,
        artifact=artifact,
        reference=reference,
        directive=directive,
    )
