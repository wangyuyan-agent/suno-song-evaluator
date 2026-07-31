from __future__ import annotations

import pytest
from pydantic import ValidationError

from songeval.enums import (
    AcquisitionPath,
    Axis,
    EvidenceInheritance,
    OperationType,
    PreservationIntent,
    ProtectedDimension,
    Provenance,
    TargetPlacement,
)
from songeval.models import (
    ArtifactEdge,
    ComplianceFloor,
    CreativeBriefVersion,
    PreservationDirective,
    PreservationThresholds,
    ProjectDecisionPolicy,
    ProvenanceRecord,
    ReleaseArtifact,
)


def test_brief_is_immutable_and_hash_changes_by_content(brief):
    with pytest.raises(ValidationError):
        brief.lyrics = "changed"
    changed = CreativeBriefVersion(
        project_id=brief.project_id,
        version="v2",
        lyrics="changed",
    )
    assert changed.content_sha256 != brief.content_sha256


def test_brief_rejects_forged_hash():
    with pytest.raises(ValidationError, match="content hash"):
        CreativeBriefVersion(
            project_id="p",
            version="v1",
            lyrics="text",
            content_sha256="bad",
        )


def test_duration_fields_are_separate_and_mismatch_is_derived():
    artifact = ReleaseArtifact(
        project_id="p",
        take_id="t",
        platform_reported_duration_s=10.1,
        measured_file_duration_s=10.3,
    )
    assert artifact.platform_reported_duration_s == 10.1
    assert artifact.measured_file_duration_s == 10.3
    assert artifact.duration_mismatch_s == pytest.approx(0.2)
    assert artifact.has_duration_mismatch


def test_explicit_duration_mismatch_is_preserved_when_durations_are_unknown():
    artifact = ReleaseArtifact(
        project_id="p",
        take_id="t",
        duration_mismatch_s=0.25,
    )
    assert artifact.duration_mismatch_s == pytest.approx(0.25)
    assert artifact.has_duration_mismatch


def test_unknown_acquisition_disables_format_sensitive_claims():
    artifact = ReleaseArtifact(
        project_id="p",
        take_id="t",
        acquisition_path=AcquisitionPath.UNKNOWN,
    )
    assert not artifact.format_sensitive_comparison_allowed


def test_global_conditioner_cannot_claim_position_enforcement():
    with pytest.raises(ValidationError, match="cannot enforce placement"):
        PreservationDirective(
            project_id="p",
            brief_id="b",
            reference_segment_id="r",
            preservation_intent=PreservationIntent.MELODY_RHYTHM,
            protected_dimensions=(ProtectedDimension.MELODY,),
            target_placement=TargetPlacement.GLOBAL_CONDITIONER,
            placement_enforceable=True,
            must_preserve=False,
            thresholds=PreservationThresholds(),
            abstention_strategy="abstain",
        )


def test_incomplete_policy_cannot_be_marked_declared():
    with pytest.raises(ValidationError, match="declared policy must be complete"):
        ProjectDecisionPolicy(
            project_id="p",
            version="v1",
            declared_by_user=True,
            axis_priority=(Axis.COMPLIANCE,),
        )


def test_complete_policy_is_version_hashed(complete_policy):
    assert complete_policy.declared_by_user
    assert len(complete_policy.content_sha256) == 64
    assert complete_policy.compliance_floor == ComplianceFloor(
        reject_confirmed_t1=True,
        burden_lyrics_must_pass=True,
        hard_requirements_must_pass=True,
        abstain_on_critical_unknown=True,
    )


def test_generated_region_cannot_inherit_evidence():
    with pytest.raises(ValidationError, match="generated regions"):
        ArtifactEdge(
            project_id="p",
            parent_artifact_id="a",
            child_artifact_id="b",
            operation=OperationType.REPLACE_SECTION,
            evidence_inheritance={
                "generated_region": EvidenceInheritance.INHERIT_PRESERVED_REGION
            },
            provenance=ProvenanceRecord(provenance=Provenance.DECLARED),
        )


def test_edge_rejects_self_loop():
    with pytest.raises(ValidationError, match="self-edge"):
        ArtifactEdge(
            project_id="p",
            parent_artifact_id="a",
            child_artifact_id="a",
            operation=OperationType.CROP,
            provenance=ProvenanceRecord(provenance=Provenance.DECLARED),
        )
