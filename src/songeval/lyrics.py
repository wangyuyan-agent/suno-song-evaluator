from __future__ import annotations

import json
import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

from .enums import DefectTier
from .models import (
    Defect,
    LyricAnalysis,
    LyricLineLocation,
    TranscriptSegment,
)
from .util import content_hash


class TranscriptProvider(Protocol):
    def transcribe(self, path: str) -> Iterable[TranscriptSegment]: ...


def transcript_segments_from_payload(payload: object) -> tuple[TranscriptSegment, ...]:
    """Parse Whisper/MLX-Whisper style JSON without depending on one backend."""
    raw_segments = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(raw_segments, list):
        raise ValueError("transcript payload must contain a segments list")
    segments: list[TranscriptSegment] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise ValueError(f"transcript segment {index} is not an object")
        start = item.get("start", item.get("start_s"))
        end = item.get("end", item.get("end_s"))
        text = item.get("text")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise ValueError(f"transcript segment {index} has invalid timestamps")
        if not isinstance(text, str):
            raise ValueError(f"transcript segment {index} has no text")
        confidence = item.get("confidence")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise ValueError(f"transcript segment {index} has invalid confidence")
        words = item.get("words")
        if confidence is None and isinstance(words, list):
            probabilities = [
                word.get("probability")
                for word in words
                if isinstance(word, dict)
                and isinstance(word.get("probability"), (int, float))
            ]
            if probabilities:
                confidence = sum(probabilities) / len(probabilities)
        segments.append(
            TranscriptSegment(
                start_s=float(start),
                end_s=float(end),
                text=text.strip(),
                confidence=float(confidence) if confidence is not None else None,
            )
        )
    return tuple(segments)


class JsonTranscriptProvider:
    def __init__(self, transcript_path: str | Path):
        self.path = Path(transcript_path)

    def transcribe(self, _: str) -> tuple[TranscriptSegment, ...]:
        return transcript_segments_from_payload(
            json.loads(self.path.read_text(encoding="utf-8"))
        )


class MlxWhisperTranscriptProvider:
    """Optional local Apple Silicon provider; imported only when requested."""

    def __init__(
        self,
        *,
        model: str = "mlx-community/whisper-small-mlx",
        language: str | None = None,
    ):
        self.model = model
        self.language = language

    def transcribe(self, path: str) -> tuple[TranscriptSegment, ...]:
        try:
            import mlx_whisper
        except ImportError as error:
            raise RuntimeError(
                "mlx-whisper is not installed; run `uv sync --extra asr` or "
                "supply --transcript with existing Whisper JSON"
            ) from error
        options: dict[str, object] = {
            "path_or_hf_repo": self.model,
            "word_timestamps": True,
        }
        if self.language:
            options["language"] = self.language
        payload = mlx_whisper.transcribe(path, **options)
        return transcript_segments_from_payload(payload)


def analyze_lyrics(
    *,
    project_id: str,
    artifact_id: str,
    brief_id: str,
    lyrics: str,
    audio_path: str | Path,
    provider: TranscriptProvider,
    provider_name: str,
) -> LyricAnalysis:
    transcript = tuple(provider.transcribe(str(audio_path)))
    locations = locate_lyric_lines(lyrics, transcript)
    digest = content_hash([segment.model_dump(mode="json") for segment in transcript])
    return LyricAnalysis(
        project_id=project_id,
        artifact_id=artifact_id,
        brief_id=brief_id,
        provider=provider_name,
        transcript=transcript,
        locations=locations,
        transcript_sha256=digest,
    )


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()


def locate_lyric_lines(
    lyrics: str,
    transcript: Iterable[TranscriptSegment],
    *,
    located_threshold: float = 0.75,
    possible_threshold: float = 0.45,
    minimum_asr_confidence: float = 0.50,
) -> tuple[LyricLineLocation, ...]:
    """Locate lines only; this function never emits a release defect."""
    expected_lines = [
        line.strip()
        for line in lyrics.splitlines()
        if line.strip() and not line.lstrip().startswith("[")
    ]
    transcript = tuple(transcript)
    normalized_segments = [_normalize(item.text) for item in transcript]
    result: list[LyricLineLocation] = []
    search_start = 0
    for index, line in enumerate(expected_lines):
        expected = _normalize(line)
        candidates: list[tuple[float, int]] = []
        for segment_index in range(search_start, len(transcript)):
            score = SequenceMatcher(
                None,
                expected,
                normalized_segments[segment_index],
            ).ratio()
            candidates.append((score, segment_index))
        if not candidates:
            result.append(
                LyricLineLocation(
                    line_index=index,
                    expected_text=line,
                    status="unlocatable",
                )
            )
            continue
        score, segment_index = max(
            candidates,
            key=lambda item: (item[0], -item[1]),
        )
        segment = transcript[segment_index]
        if score >= located_threshold:
            status = (
                "low_confidence"
                if segment.confidence is not None
                and segment.confidence < minimum_asr_confidence
                else "located"
            )
            search_start = segment_index + 1
        elif score >= possible_threshold:
            status = "possible_changed"
        else:
            status = "possible_missing"
        result.append(
            LyricLineLocation(
                line_index=index,
                expected_text=line,
                status=status,
                start_s=segment.start_s,
                end_s=segment.end_s,
                transcript_text=segment.text,
                similarity=score,
                requires_human_confirmation=True,
            )
        )
    return tuple(result)


def confirm_burden_lyric_defect(
    location: LyricLineLocation,
    *,
    project_id: str,
    artifact_id: str,
    brief_id: str,
    human_confirmation: bool,
    description: str,
) -> Defect:
    if not human_confirmation:
        raise ValueError("ASR/forced alignment alone cannot create a lyric T1")
    if location.start_s is None or location.end_s is None:
        raise ValueError(
            "lyric T1 requires a localized line; "
            f"line {location.line_index} has status {location.status!r}"
        )
    normalized_description = description.strip()
    if not normalized_description:
        raise ValueError("lyric T1 requires a description")
    return Defect(
        project_id=project_id,
        artifact_id=artifact_id,
        brief_id=brief_id,
        code="burden_lyric_error",
        tier=DefectTier.T1,
        description=normalized_description,
        start_s=location.start_s,
        end_s=location.end_s,
        confirmed=True,
        hard_requirement=True,
        evidence_source="human confirmation after transcript localization",
    )
