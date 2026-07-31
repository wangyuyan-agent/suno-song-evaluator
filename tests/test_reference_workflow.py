from __future__ import annotations

import pytest

from songeval.enums import PreservationIntent
from songeval.reference_workflow import (
    register_local_reference,
    registered_directive_target_id,
)


def test_local_reference_registration_keeps_reference_out_of_generation(
    minimal_manifest,
    tone_b,
    tmp_path,
):
    registration = register_local_reference(
        minimal_manifest,
        target_artifact_id="artifact_test",
        reference_path=tone_b,
        media_dir=tmp_path / "references",
        intent=PreservationIntent.STRUCTURAL_GESTURE,
        start_s=1.0,
        end_s=5.0,
    )
    assert registration.artifact.raw_payload["analysis_role"] == "reference_only"
    assert registration.reference.source_artifact_id == registration.artifact.id
    assert registration.reference.start_s == 1.0
    assert registration.reference.end_s == 5.0
    assert registration.directive.reference_segment_id == registration.reference.id
    assert registration.directive.target_artifact_id == "artifact_test"
    assert not registration.directive.must_preserve
    assert registration.directive.placement_enforceable
    assert registration.event is not None
    assert registration.event.source_material_ids == ()
    legacy = registration.directive.model_copy(update={"target_artifact_id": None})
    assert (
        registered_directive_target_id(legacy, minimal_manifest.artifacts)
        == "artifact_test"
    )


def test_distinct_intervals_get_distinct_preservation_directives(
    minimal_manifest,
    tone_b,
    tmp_path,
):
    first = register_local_reference(
        minimal_manifest,
        target_artifact_id="artifact_test",
        reference_path=tone_b,
        media_dir=tmp_path / "references",
        intent=PreservationIntent.STRUCTURAL_GESTURE,
        start_s=0.0,
        end_s=2.0,
    )
    second_manifest = minimal_manifest.model_copy(
        update={
            "generation_events": [
                *minimal_manifest.generation_events,
                first.event,
            ],
            "takes": [*minimal_manifest.takes, first.take],
            "artifacts": [*minimal_manifest.artifacts, first.artifact],
            "references": [first.reference],
            "directives": [first.directive],
        }
    )
    second = register_local_reference(
        second_manifest,
        target_artifact_id="artifact_test",
        reference_path=tone_b,
        media_dir=tmp_path / "references",
        intent=PreservationIntent.STRUCTURAL_GESTURE,
        start_s=2.0,
        end_s=4.0,
    )
    assert second.reference.id != first.reference.id
    assert second.directive.id != first.directive.id
    assert second.directive.reference_segment_id == second.reference.id


def test_reused_directive_rejects_conflicting_must_preserve(
    minimal_manifest,
    tone_b,
    tmp_path,
):
    first = register_local_reference(
        minimal_manifest,
        target_artifact_id="artifact_test",
        reference_path=tone_b,
        media_dir=tmp_path / "references",
        intent=PreservationIntent.STRUCTURAL_GESTURE,
        start_s=0.0,
        end_s=2.0,
        must_preserve=False,
    )
    with_reference = minimal_manifest.model_copy(
        update={
            "generation_events": [*minimal_manifest.generation_events, first.event],
            "takes": [*minimal_manifest.takes, first.take],
            "artifacts": [*minimal_manifest.artifacts, first.artifact],
            "references": [first.reference],
            "directives": [first.directive],
        }
    )
    with pytest.raises(ValueError, match="must_preserve=False, not True"):
        register_local_reference(
            with_reference,
            target_artifact_id="artifact_test",
            reference_path=tone_b,
            media_dir=tmp_path / "references",
            intent=PreservationIntent.STRUCTURAL_GESTURE,
            start_s=0.0,
            end_s=2.0,
            must_preserve=True,
        )


def test_reference_registration_reports_missing_target_take(
    minimal_manifest,
    tone_b,
    tmp_path,
):
    broken_target = minimal_manifest.artifacts[0].model_copy(
        update={"take_id": "missing-take"}
    )
    broken = minimal_manifest.model_copy(update={"artifacts": [broken_target]})
    with pytest.raises(ValueError, match="unknown take for target artifact"):
        register_local_reference(
            broken,
            target_artifact_id=broken_target.id,
            reference_path=tone_b,
            media_dir=tmp_path / "references",
            intent=PreservationIntent.STRUCTURAL_GESTURE,
            start_s=0.0,
            end_s=2.0,
        )
