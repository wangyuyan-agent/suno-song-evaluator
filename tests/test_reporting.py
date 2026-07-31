from songeval.models import ProjectAnalysisReport
from songeval.reporting import _time, render_markdown


def test_time_rounding_rolls_over_to_the_next_minute():
    assert _time(59.999) == "01:00.00"


def test_report_names_an_undeclared_empty_priority(minimal_manifest):
    report = ProjectAnalysisReport.model_validate(
        {
            "project_id": minimal_manifest.project_id,
            "run": {
                "project_id": minimal_manifest.project_id,
                "tool_version": "test",
            },
            "source_assessments": [],
            "audio_metrics": [],
            "structures": [],
            "comparisons": [],
            "reference_segments": [],
            "reference_preflights": [],
            "assessments": [],
            "recommendation": {
                "status": "abstain",
                "recommended_artifact_id": None,
                "alternate_artifact_id": None,
                "policy_id": None,
                "priority": [],
                "user_declared_priority": False,
                "rationale": [],
                "alternate_costs": [],
                "confidence": "indeterminate",
                "evidence_gaps": [],
                "ignored_axes": [],
            },
        }
    )
    assert "- Priority: not declared" in render_markdown(report)
