from __future__ import annotations

from .enums import Axis
from .models import ProjectAnalysisReport


def _time(value: float | None) -> str:
    if value is None:
        return "unknown"
    minutes, seconds = divmod(round(value, 2), 60)
    return f"{int(minutes):02d}:{seconds:05.2f}"


def render_markdown(report: ProjectAnalysisReport) -> str:
    lines = [
        f"# Song evaluation: {report.project_id}",
        "",
        f"Run: `{report.run.id}` · tool `{report.run.tool_version}`",
        "",
        "## Release decision",
        "",
        f"- Status: `{report.recommendation.status.value}`",
        (
            f"- Recommended artifact: `{report.recommendation.recommended_artifact_id}`"
            if report.recommendation.recommended_artifact_id
            else "- Recommended artifact: withheld"
        ),
        (
            f"- Alternate: `{report.recommendation.alternate_artifact_id}`"
            if report.recommendation.alternate_artifact_id
            else "- Alternate: none"
        ),
        "- Priority: "
        + (
            " > ".join(axis.value for axis in report.recommendation.priority)
            or "not declared"
        ),
        (
            "- Priority provenance: explicitly declared by user"
            if report.recommendation.user_declared_priority
            else "- Priority provenance: incomplete; formal recommendation disabled"
        ),
        f"- Confidence: `{report.recommendation.confidence.value}`",
    ]
    if report.recommendation.rationale:
        lines.extend(
            ["", "### Rationale", ""]
            + [f"- {item}" for item in report.recommendation.rationale]
        )
    if report.recommendation.alternate_costs:
        lines.extend(
            ["", "### Alternate costs", ""]
            + [f"- {item}" for item in report.recommendation.alternate_costs]
        )
    if report.recommendation.evidence_gaps:
        lines.extend(
            ["", "### Evidence gaps", ""]
            + [f"- {item}" for item in report.recommendation.evidence_gaps]
        )

    if report.source_assessments:
        lines.extend(["", "## Source-state evidence", ""])
        for assessment in report.source_assessments:
            lines.append(
                f"- `{assessment.relationship}` "
                f"({assessment.provenance.provenance.value}, "
                f"{assessment.provenance.confidence.value} confidence)"
            )
            for evidence in assessment.evidence:
                lines.append(f"  - {evidence}")
            if assessment.limitation:
                lines.append(f"  - limitation: {assessment.limitation}")

    lines.extend(["", "## Candidate views", ""])
    for assessment in report.assessments:
        lines.append(f"### `{assessment.artifact_id}`")
        lines.append("")
        if assessment.compliance_as_generated is not None:
            lines.append(
                "- compliance_as_generated: "
                f"`{assessment.compliance_as_generated.status.value}`"
            )
        if assessment.compliance_vs_target is not None:
            lines.append(
                "- compliance_vs_target: "
                f"`{assessment.compliance_vs_target.status.value}`"
            )
        for axis in Axis:
            if axis == Axis.COMPLIANCE and (
                assessment.compliance_as_generated is not None
                or assessment.compliance_vs_target is not None
            ):
                continue
            evaluation = assessment.evaluation_for(axis)
            if not evaluation:
                lines.append(f"- {axis.value}: missing")
                continue
            detail = (
                f" / {evaluation.readiness.value}"
                if evaluation.readiness is not None
                else ""
            )
            ignored = " (descriptive only)" if evaluation.ignored_for_ordering else ""
            lines.append(
                f"- {axis.value}: `{evaluation.status.value}`{detail}{ignored}"
            )
            for gap in evaluation.evidence_gaps:
                lines.append(f"  - evidence gap: {gap}")
        for defect in assessment.defects:
            location = (
                f" at {_time(defect.start_s)}–{_time(defect.end_s)}"
                if defect.start_s is not None
                else ""
            )
            common = " common-mode" if defect.common_mode else ""
            lines.append(
                f"- defect {defect.tier.value}{common}{location}: {defect.description}"
            )
        lines.append("")

    lines.extend(["## Pairwise difference hotspots", ""])
    for comparison in report.comparisons:
        lines.append(
            f"### `{comparison.artifact_a_id}` vs `{comparison.artifact_b_id}`"
        )
        lines.append("")
        if comparison.same_generation_event:
            lines.append(
                "- Same GenerationEvent: differences are sampling variance, "
                "not parameter effects."
            )
        if comparison.acquisition_warning:
            lines.append(f"- Acquisition caveat: {comparison.acquisition_warning}")
        for hotspot in comparison.hotspots:
            lines.append(
                f"- {hotspot.feature_family}: "
                f"A {_time(hotspot.a_start_s)}–{_time(hotspot.a_end_s)}, "
                f"B {_time(hotspot.b_start_s)}–{_time(hotspot.b_end_s)}"
            )
        lines.append("")

    lines.extend(["## Approximate structure maps", ""])
    for structure in report.structures:
        lines.append(f"### `{structure.artifact_id}`")
        lines.append("")
        if not structure.segments:
            lines.append("- No stable segments were detected.")
        for segment in structure.segments:
            lines.append(
                f"- group {segment.repeat_group}: "
                f"{_time(segment.start_s)}–{_time(segment.end_s)} "
                f"(group similarity {segment.similarity_to_group:.3f})"
            )
        lines.append("")

    lines.extend(["## Lyric localization", ""])
    if not report.lyric_analyses:
        lines.append(
            "- No transcript localization is attached. ASR is a locator only "
            "and cannot create a wrong-lyric defect."
        )
        lines.append("")
    for analysis in report.lyric_analyses:
        lines.append(f"### `{analysis.artifact_id}`")
        lines.append("")
        lines.append(f"- Provider: `{analysis.provider}`")
        for location in analysis.locations:
            interval = (
                f"{_time(location.start_s)}–{_time(location.end_s)}"
                if location.start_s is not None
                else "unlocated"
            )
            lines.append(
                f"- line {location.line_index + 1}: `{location.status}` at "
                f"{interval}; human confirmation required"
            )
        lines.append("")

    lines.extend(["## Reference preflight", ""])
    for preflight in report.reference_preflights:
        lines.append(f"### `{preflight.reference_segment_id}`")
        lines.append("")
        for finding in preflight.findings:
            lines.append(
                f"- [{finding.severity}] {finding.code}: {finding.message} "
                f"({finding.evidence})"
            )
        lines.append("")

    lines.extend(
        [
            "## Technical appendix",
            "",
            "These measurements are diagnostics and loudness-matching inputs. "
            "They are not quality scores and are not used to rank candidates.",
            "",
        ]
    )
    for metric in report.audio_metrics:
        lufs = metric.integrated_lufs if metric.integrated_lufs is not None else "N/A"
        lines.append(
            f"- `{metric.artifact_id}`: "
            f"duration={metric.measured_file_duration_s:.6f}s; "
            f"LUFS={lufs}; "
            f"peak={metric.peak_dbfs:.2f} dBFS; "
            f"ending={metric.ending.classification}; "
            f"final100ms={metric.ending.final_100ms_rms_dbfs:.2f} dBFS; "
            f"acquisition_degraded={str(metric.acquisition_degraded).lower()}"
        )
    if report.warnings:
        lines.extend(["", "## Run warnings", ""])
        lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines).rstrip() + "\n"
