from __future__ import annotations

import json

import httpx

from songeval.analyzer import ProjectAnalyzer
from songeval.importers import hydrate_local_artifacts
from songeval.llm import (
    SYSTEM_PROMPT,
    DeterministicNarrator,
    OpenAICompatibleNarrator,
)


def report(minimal_manifest):
    return ProjectAnalyzer(hydrate_local_artifacts(minimal_manifest)).analyze()


def test_deterministic_narrator_is_evidence_backed(minimal_manifest):
    result = DeterministicNarrator().narrate(report(minimal_manifest))
    assert result.provider == "deterministic"
    assert len(result.evidence_hash) == 64
    assert "Recommended artifact: withheld" in result.markdown
    assert "total score" not in result.markdown.lower()


def test_openai_compatible_request_contains_contract_and_evidence(minimal_manifest):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "## 结论\n证据不足，因此暂不推荐。"}}
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    narrator = OpenAICompatibleNarrator(
        base_url="https://llm.example/v1",
        api_key="test-key",
        model="test-model",
        client=client,
    )
    result = narrator.narrate(report(minimal_manifest))
    assert result.model == "test-model"
    assert "暂不推荐" in result.markdown
    assert "Never claim that you listened" in captured["body"]["messages"][0]["content"]
    evidence_message = captured["body"]["messages"][1]["content"]
    assert "recommendation" in evidence_message
    assert "Evidence JSON" in evidence_message


def test_llm_contract_forbids_scores_and_invented_hearing():
    assert "Never claim that you listened" in SYSTEM_PROMPT
    assert "Never calculate or imply a total score" in SYSTEM_PROMPT
