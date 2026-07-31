from __future__ import annotations

from typing import Any

import httpx

from .models import (
    LLMNarrative,
    LLMNarrativeRequest,
    ProjectAnalysisReport,
)
from .reporting import render_markdown
from .util import canonical_json, content_hash

SYSTEM_PROMPT = """You are an evidence editor for song-candidate evaluation.
Use only the structured evidence in the user message.
Never claim that you listened to audio. Never invent timestamps, lyrics, defects,
lineage, slider values, or causal explanations.
Keep Compliance, Craft, Release Readiness, and Distinctiveness separate.
Never calculate or imply a total score, percentage, weighted ranking, or hidden
default. Distinctiveness is descriptive and is not automatically better.
If evidence is missing, say that the result is indeterminate or withheld.
Explain same-GenerationEvent differences as sampling variance, not parameter effects.
Write concise Markdown in the language requested by the user."""


def evidence_request_from_report(
    report: ProjectAnalysisReport,
) -> LLMNarrativeRequest:
    notes = tuple(
        warning
        for comparison in report.comparisons
        for warning in (
            (
                f"{comparison.artifact_a_id}/{comparison.artifact_b_id}: "
                f"{comparison.acquisition_warning}"
            )
            if comparison.acquisition_warning
            else None,
        )
        if warning
    )
    source_assessment_notes = tuple(
        (
            f"source relationship={item.relationship}; "
            f"provenance={item.provenance.provenance.value}; "
            f"confidence={item.provenance.confidence.value}; "
            f"limitation={item.limitation or 'none'}"
        )
        for item in report.source_assessments
    )
    return LLMNarrativeRequest(
        project_id=report.project_id,
        recommendation=report.recommendation,
        assessments=report.assessments,
        comparisons=report.comparisons,
        source_notes=notes + source_assessment_notes,
    )


class DeterministicNarrator:
    provider = "deterministic"
    model = "evidence-template-v1"

    def narrate(
        self, report: ProjectAnalysisReport, language: str = "zh-CN"
    ) -> LLMNarrative:
        evidence = evidence_request_from_report(report)
        return LLMNarrative(
            markdown=render_markdown(report),
            provider=self.provider,
            model=self.model,
            evidence_hash=content_hash(evidence.model_dump(mode="json")),
        )


class OpenAICompatibleNarrator:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client | None = None,
        timeout_s: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_s)
        self.provider = "openai-compatible"

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> OpenAICompatibleNarrator:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def narrate(
        self,
        report: ProjectAnalysisReport,
        language: str = "zh-CN",
    ) -> LLMNarrative:
        evidence = evidence_request_from_report(report)
        evidence_payload = evidence.model_dump(mode="json")
        response = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Output language: {language}\n"
                            f"Evidence JSON:\n{canonical_json(evidence_payload)}"
                        ),
                    },
                ],
            },
        )
        response.raise_for_status()
        payload = response.json()
        markdown = payload["choices"][0]["message"]["content"].strip()
        if not markdown:
            raise ValueError("LLM returned an empty narrative")
        disallowed = ("总分", "weighted score", "I listened", "我听了")
        if any(token.lower() in markdown.lower() for token in disallowed):
            raise ValueError("LLM narrative violated the evidence-only contract")
        return LLMNarrative(
            markdown=markdown + "\n",
            provider=self.provider,
            model=self.model,
            evidence_hash=content_hash(evidence_payload),
        )
