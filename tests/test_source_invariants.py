from __future__ import annotations

from pathlib import Path


def test_no_aggregate_score_field_or_weighted_ranker():
    source_root = Path(__file__).parents[1] / "src"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")
    ).lower()
    for forbidden in ("total_score", "overall_score", "weighted_score"):
        assert forbidden not in source


def test_analysis_code_does_not_mutate_suno_or_publish():
    source_root = Path(__file__).parents[1] / "src"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")
    ).lower()
    assert "/generate" not in source
    assert "/publish" not in source
    assert "/credits" not in source
    assert "consume_credits" not in source


def test_blind_keyboard_shortcuts_ignore_modified_or_composing_events():
    source = (Path(__file__).parents[1] / "src/songeval/web/app.js").read_text(
        encoding="utf-8"
    )
    handler = source.split('document.addEventListener("keydown", (event) => {', 1)[1]
    shortcut_branches = handler.index('if (event.key === " ")')
    guard = handler[:shortcut_branches]
    for condition in (
        "event.isComposing",
        "event.ctrlKey",
        "event.metaKey",
        "event.altKey",
        "event.shiftKey",
    ):
        assert condition in guard


def test_blind_design_does_not_claim_loudness_was_matched():
    design = (
        Path(__file__).parents[1] / "design/claude/Suno Song Evaluator.dc.html"
    ).read_text(encoding="utf-8")
    blind_screen = design.split('<sc-if value="{{ isBlind }}"', 1)[1]
    assert "响度已匹配" not in blind_screen
    assert blind_screen.count("响度按规则处理") == 3


def test_web_ui_guards_empty_evidence_and_restored_blind_state():
    source = (Path(__file__).parents[1] / "src/songeval/web/app.js").read_text(
        encoding="utf-8"
    )
    assert 'seconds === null || seconds === undefined || seconds === ""' in source
    assert "const storedCurrent = Number(stored.__current);" in source
    assert "Number.isFinite(storedCurrent)" in source
    assert 'hotspots === null ? "未采集" : `${hotspots} 处`' in source


def test_blind_space_shortcut_preserves_button_and_link_activation():
    source = (Path(__file__).parents[1] / "src/songeval/web/app.js").read_text(
        encoding="utf-8"
    )
    handler = source.split('document.addEventListener("keydown", (event) => {', 1)[1]
    space_branch = handler.index('if (event.key === " ")')
    interactive_guard = handler[:space_branch]
    assert 'target.closest("button, a[href]")' in interactive_guard
