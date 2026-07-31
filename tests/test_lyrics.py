from __future__ import annotations

import json

import pytest

from songeval.lyrics import (
    JsonTranscriptProvider,
    analyze_lyrics,
    confirm_burden_lyric_defect,
    locate_lyric_lines,
    transcript_segments_from_payload,
)
from songeval.models import TranscriptSegment


def test_transcript_only_localizes_and_requires_human_confirmation():
    locations = locate_lyric_lines(
        "[Verse]\n雨落得慢\n柳也绿得慢",
        [
            TranscriptSegment(
                start_s=1,
                end_s=2,
                text="雨落得慢",
                confidence=0.95,
            ),
            TranscriptSegment(
                start_s=2,
                end_s=3,
                text="柳也绿得慢",
                confidence=0.3,
            ),
        ],
    )
    assert locations[0].status == "located"
    assert locations[1].status == "low_confidence"
    assert all(item.requires_human_confirmation for item in locations)


def test_low_asr_confidence_only_downgrades_an_otherwise_located_line():
    location = locate_lyric_lines(
        "完全不同的目标歌词",
        [
            TranscriptSegment(
                start_s=1,
                end_s=2,
                text="没有关系的转录内容",
                confidence=0.1,
            )
        ],
    )[0]
    assert location.status in {"possible_changed", "possible_missing"}
    assert location.status != "low_confidence"


def test_repeated_lyric_lines_prefer_the_earliest_equal_match():
    locations = locate_lyric_lines(
        "同一句\n同一句",
        [
            TranscriptSegment(start_s=1, end_s=2, text="同一句", confidence=0.9),
            TranscriptSegment(start_s=2, end_s=3, text="同一句", confidence=0.9),
        ],
    )
    assert [item.start_s for item in locations] == [1, 2]


def test_asr_cannot_create_t1_without_human_confirmation():
    location = locate_lyric_lines(
        "欲折的手",
        [TranscriptSegment(start_s=1, end_s=2, text="听不清", confidence=0.2)],
    )[0]
    with pytest.raises(ValueError, match="cannot create"):
        confirm_burden_lyric_defect(
            location,
            project_id="p",
            artifact_id="a",
            brief_id="b",
            human_confirmation=False,
            description="wrong",
        )


def test_human_confirmation_requires_timing_and_description():
    untimed = locate_lyric_lines("目标行", [])[0]
    with pytest.raises(ValueError, match="requires a localized line"):
        confirm_burden_lyric_defect(
            untimed,
            project_id="p",
            artifact_id="a",
            brief_id="b",
            human_confirmation=True,
            description="heard a defect",
        )
    timed = locate_lyric_lines(
        "目标行",
        [TranscriptSegment(start_s=1, end_s=2, text="目标行", confidence=0.9)],
    )[0]
    with pytest.raises(ValueError, match="requires a description"):
        confirm_burden_lyric_defect(
            timed,
            project_id="p",
            artifact_id="a",
            brief_id="b",
            human_confirmation=True,
            description="   ",
        )


def test_human_confirmation_can_create_burden_lyric_t1():
    location = locate_lyric_lines(
        "欲折的手",
        [TranscriptSegment(start_s=1, end_s=2, text="欲折的手", confidence=0.9)],
    )[0]
    defect = confirm_burden_lyric_defect(
        location,
        project_id="p",
        artifact_id="a",
        brief_id="b",
        human_confirmation=True,
        description="human heard a wrong burden line",
    )
    assert defect.tier.value == "T1"
    assert defect.confirmed
    assert defect.evidence_source.startswith("human confirmation")


def test_whisper_json_parser_uses_word_confidence(tmp_path, tone_a):
    payload = {
        "segments": [
            {
                "start": 1.0,
                "end": 2.0,
                "text": "雨落得慢",
                "words": [
                    {"word": "雨落", "probability": 0.9},
                    {"word": "得慢", "probability": 0.8},
                ],
            }
        ]
    }
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(json.dumps(payload), encoding="utf-8")
    parsed = transcript_segments_from_payload(payload)
    assert parsed[0].confidence == pytest.approx(0.85)
    analysis = analyze_lyrics(
        project_id="p",
        artifact_id="a",
        brief_id="b",
        lyrics="雨落得慢",
        audio_path=tone_a,
        provider=JsonTranscriptProvider(transcript_path),
        provider_name="json:test",
    )
    assert analysis.locations[0].status == "located"
    assert analysis.transcript_sha256


def test_transcript_parser_rejects_non_numeric_direct_confidence():
    with pytest.raises(
        ValueError,
        match="transcript segment 0 has invalid confidence",
    ):
        transcript_segments_from_payload(
            {
                "segments": [
                    {
                        "start": 0,
                        "end": 1,
                        "text": "line",
                        "confidence": "high",
                    }
                ]
            }
        )
