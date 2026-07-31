from __future__ import annotations

from .enums import OperationType, PreservationIntent
from .models import (
    PreservationDirective,
    ReleaseArtifact,
    SunoOperationPlan,
    SunoWorkflowRecommendation,
)

REPLACE_SECTION_HELP = "https://help.suno.com/en/articles/3271873"
STUDIO_HELP = "https://help.suno.com/en/articles/7940161"
SONG_EDITOR_HELP = "https://help.suno.com/en/articles/6141505"


def _validate_directive_target(
    directive: PreservationDirective,
    target_artifact: ReleaseArtifact,
) -> None:
    if directive.project_id != target_artifact.project_id:
        raise ValueError("directive and target artifact must belong to one project")
    if (
        directive.target_artifact_id is not None
        and directive.target_artifact_id != target_artifact.id
    ):
        raise ValueError("directive was registered for a different target artifact")


def plan_structural_gesture_replace(
    *,
    directive: PreservationDirective,
    target_artifact: ReleaseArtifact,
    prompt: str,
    frozen_lyrics_excerpt: str | None = None,
) -> SunoOperationPlan:
    _validate_directive_target(directive, target_artifact)
    if directive.preservation_intent != PreservationIntent.STRUCTURAL_GESTURE:
        raise ValueError("only structural_gesture can use this Replace plan")
    if not prompt.strip():
        raise ValueError("a positive local direction is required")
    return SunoOperationPlan(
        project_id=directive.project_id,
        directive_id=directive.id,
        target_artifact_id=target_artifact.id,
        operation=OperationType.REPLACE_SECTION,
        objective=(
            "Preserve the target song and recreate only the section-relative "
            "transition gesture."
        ),
        source_handling="do_not_attach_reference_as_sample",
        selection_start_rule=(
            "Start at the beginning of the final complete Bridge phrase and a "
            "clean bar boundary; never start mid-word or mid-transient."
        ),
        selection_end_rule=(
            "End after the first complete Chorus phrase and a clean bar boundary."
        ),
        frozen_lyrics_excerpt=frozen_lyrics_excerpt,
        prompt=prompt.strip(),
        takes_per_batch=2,
        max_batches=2,
        rejection_conditions=(
            "wrong, missing, reordered, or unintelligible frozen lyric",
            "mid-line hard cut or obvious edit seam",
            "unexpected vocalist or vocal-register identity change",
            "long instrumental detour before Chorus",
            "new drums or percussion that violate the Brief",
            "surrounding protected target material regresses",
        ),
        fallback_artifact_id=target_artifact.id,
        exact_retention_claimed=False,
        external_postproduction_required=False,
        subscription_tier="pro",
        studio_available=False,
        workflow_surface="song_editor",
        source_rules=(
            "use_target_as_edit_parent",
            "do_not_attach_reference_as_sample",
        ),
        steps=(
            "Open the target song in Library or Create; choose More Actions, "
            "then Edit and Replace Section.",
            "Select from a clean boundary before the complete Bridge phrase "
            "through a clean boundary after the first complete Chorus phrase.",
            "Keep the frozen lyrics unchanged and enter only the positive local "
            "transition direction.",
            "Confirm one batch of two versions; do not choose by title or order.",
            "Download or retain both Whole Song results and rerun this evaluator.",
            "Stop after two batches; if neither passes, keep the target fallback.",
        ),
        official_sources=(
            REPLACE_SECTION_HELP,
            SONG_EDITOR_HELP,
            STUDIO_HELP,
        ),
    )


def recommend_suno_workflow(
    *,
    directive: PreservationDirective,
    target_artifact: ReleaseArtifact,
    prompt: str,
    frozen_lyrics_excerpt: str | None = None,
    subscription_tier: str = "pro",
    studio_available: bool = False,
) -> SunoWorkflowRecommendation:
    _validate_directive_target(directive, target_artifact)
    sources = (REPLACE_SECTION_HELP, SONG_EDITOR_HELP, STUDIO_HELP)
    if directive.preservation_intent == PreservationIntent.STRUCTURAL_GESTURE:
        if subscription_tier not in {"pro", "premier"}:
            return SunoWorkflowRecommendation(
                status="abstain",
                project_id=directive.project_id,
                directive_id=directive.id,
                target_artifact_id=target_artifact.id,
                requested_intent=directive.preservation_intent,
                rationale=(
                    "Replace Section requires a paid Pro or Premier subscription.",
                    "The tool will not substitute a Sample workflow because it "
                    "cannot enforce section-relative placement.",
                ),
                suggested_fallback=target_artifact.id,
                official_sources=sources,
            )
        plan = plan_structural_gesture_replace(
            directive=directive,
            target_artifact=target_artifact,
            prompt=prompt,
            frozen_lyrics_excerpt=frozen_lyrics_excerpt,
        ).model_copy(
            update={
                "subscription_tier": subscription_tier,
                "studio_available": studio_available,
                "workflow_surface": ("studio" if studio_available else "song_editor"),
            }
        )
        return SunoWorkflowRecommendation(
            status="actionable",
            project_id=directive.project_id,
            directive_id=directive.id,
            target_artifact_id=target_artifact.id,
            requested_intent=directive.preservation_intent,
            plan=plan,
            rationale=(
                "The requested intent is a section-relative structural gesture, "
                "not exact sample retention.",
                "Pro Song Editor exposes Replace Section without requiring Studio.",
                "The target remains the edit parent and the reference is not "
                "reattached as a Sample.",
            ),
            suggested_fallback=target_artifact.id,
            official_sources=sources,
        )
    return SunoWorkflowRecommendation(
        status="abstain",
        project_id=directive.project_id,
        directive_id=directive.id,
        target_artifact_id=target_artifact.id,
        requested_intent=directive.preservation_intent,
        rationale=(
            "A 16-second Sample or audio upload cannot guarantee exact placement "
            "inside a new full song.",
            "Without measured retention evidence, the tool will not relabel a "
            "structural resemblance as exact or melody-rhythm preservation.",
            (
                "Studio availability does not by itself prove that a generative "
                "edit retained the requested audio identity."
                if studio_available
                else "Studio-only multitrack operations are unavailable for the "
                f"reported subscription tier '{subscription_tier}'."
            ),
        ),
        suggested_fallback=target_artifact.id,
        official_sources=sources,
    )
