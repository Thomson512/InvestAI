from __future__ import annotations

import json
from pathlib import Path

import io
import urllib.error

from scripts.council import (
    AGENT_CRO,
    AGENT_NEWS,
    AGENT_ORDER,
    AGENT_REGIME,
    AGENT_SCANNER,
    AGENT_TECHNICAL,
    AGENT_THESIS,
    ANTHROPIC_MESSAGES_URL,
    ANTHROPIC_MODELS_URL,
    CRO_TOOL_NAME,
    OPENAI_RESPONSES_URL,
    WEB_AGENTS,
    CouncilUnavailable,
    _http_error_text,
    attach_openai_provenance,
    council_verdict,
    load_council_config,
    parse_finding,
    review_candidate,
)

CONFIG = load_council_config()
ENV = {"OPENAI_API_KEY": "test-openai", "ANTHROPIC_API_KEY": "test-anthropic"}


def _finding(agent: str, verdict: str = "APPROVE", confidence: float = 0.9) -> dict:
    return {
        "agent": agent,
        "verdict": verdict,
        "confidence": confidence,
        "thesis": f"{agent} ok",
        "risks": [],
        "sources": [{"url": "https://example.test/src"}] if agent in WEB_AGENTS else [],
        "provenance_ok": True,
    }


def _six(**overrides: tuple[str, float]) -> list[dict]:
    findings = [_finding(agent) for agent in AGENT_ORDER]
    for agent, (verdict, confidence) in overrides.items():
        for finding in findings:
            if finding["agent"] == agent:
                finding["verdict"] = verdict
                finding["confidence"] = confidence
    return findings


def test_approve_requires_cro_and_four_confident_votes() -> None:
    verdict, reason = council_verdict(_six(), CONFIG)
    assert verdict == "APPROVE"
    assert reason.startswith("6/6")


def test_cro_reject_blocks() -> None:
    verdict, _reason = council_verdict(_six(**{AGENT_CRO: ("REJECT", 0.9)}), CONFIG)
    assert verdict == "REJECT"


def test_specialist_reject_blocks() -> None:
    verdict, _reason = council_verdict(_six(**{AGENT_NEWS: ("REJECT", 0.9)}), CONFIG)
    assert verdict == "REJECT"


def test_low_confidence_does_not_count_as_approval() -> None:
    findings = _six(
        **{
            AGENT_SCANNER: ("APPROVE", 0.4),
            AGENT_REGIME: ("APPROVE", 0.4),
            AGENT_NEWS: ("APPROVE", 0.4),
        }
    )
    verdict, reason = council_verdict(findings, CONFIG)
    assert verdict == "NO_TRADE"
    assert "threshold" in reason


def test_cro_approve_below_threshold_is_reject() -> None:
    verdict, _reason = council_verdict(_six(**{AGENT_CRO: ("APPROVE", 0.5)}), CONFIG)
    assert verdict == "REJECT"


def test_incomplete_membership_is_no_trade() -> None:
    verdict, _reason = council_verdict(_six()[:-1], CONFIG)
    assert verdict == "NO_TRADE"


def test_web_agent_without_source_fails_provenance() -> None:
    findings = _six()
    for finding in findings:
        if finding["agent"] == AGENT_REGIME:
            finding["sources"] = []
    verdict, reason = council_verdict(findings, CONFIG)
    assert verdict == "NO_TRADE"
    assert "web source" in reason


def test_non_web_sources_fail() -> None:
    findings = _six()
    for finding in findings:
        if finding["agent"] == AGENT_TECHNICAL:
            finding["sources"] = [{"url": "https://example.test/nope"}]
    verdict, _reason = council_verdict(findings, CONFIG)
    assert verdict == "NO_TRADE"


def test_parse_finding_rejects_bad_confidence() -> None:
    try:
        parse_finding(
            {
                "agent": AGENT_SCANNER,
                "verdict": "APPROVE",
                "confidence": True,
                "thesis": "x",
                "risks": [],
                "sources": [],
            },
            AGENT_SCANNER,
        )
    except CouncilUnavailable:
        return
    raise AssertionError("bool confidence must fail")


def test_openai_provenance_uses_tool_urls_not_model_urls() -> None:
    finding = parse_finding(
        {
            "agent": AGENT_NEWS,
            "verdict": "APPROVE",
            "confidence": 0.8,
            "thesis": "no event",
            "risks": [],
            "sources": [],
        },
        AGENT_NEWS,
    )
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {"sources": [{"url": "https://news.example/a"}]},
            }
        ]
    }
    verified = attach_openai_provenance(finding, payload, web=True)
    assert verified["provenance_ok"] is True
    assert verified["sources"] == [{"url": "https://news.example/a"}]


def test_openai_without_tool_call_is_unavailable() -> None:
    finding = parse_finding(
        {
            "agent": AGENT_NEWS,
            "verdict": "APPROVE",
            "confidence": 0.8,
            "thesis": "no event",
            "risks": [],
            "sources": [],
        },
        AGENT_NEWS,
    )
    try:
        attach_openai_provenance(finding, {"output": []}, web=True)
    except CouncilUnavailable:
        return
    raise AssertionError("missing web search must fail")


class _Scripted:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method: str, url: str, headers: dict, body: dict | None, timeout: float) -> dict:
        self.calls += 1
        if url == ANTHROPIC_MODELS_URL:
            return {
                "data": [
                    {
                        "id": "claude-sonnet-test",
                        "display_name": "Claude Sonnet",
                        "created_at": "2026-01-01",
                    }
                ]
            }
        if url == OPENAI_RESPONSES_URL:
            assert body is not None
            head = str(body["input"]).split("\nEVIDENCE:\n", 1)[0]
            agent = next(name for name in sorted(AGENT_ORDER, key=len, reverse=True) if name in head)
            web = "tools" in body
            finding = {
                "agent": agent,
                "verdict": "APPROVE",
                "confidence": 0.9,
                "thesis": "coherent",
                "risks": [],
                "sources": [],
            }
            output: list[dict] = []
            if web:
                output.append(
                    {
                        "type": "web_search_call",
                        "action": {"sources": [{"url": f"https://example.test/{agent.replace(' ', '-')}"}]},
                    }
                )
            output.append(
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(finding)}]}
            )
            return {"output": output}
        assert body is not None
        tools = body.get("tools") or []
        if any(tool.get("name") == "web_search" for tool in tools):
            return {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "server_tool_use", "name": "web_search", "id": "1"},
                    {
                        "type": "web_search_tool_result",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://example.test/cro",
                                "title": "filing",
                            }
                        ],
                    },
                    {"type": "text", "text": "no material conflict"},
                ],
            }
        assert "tool_choice" not in body
        return {
            "content": [
                {
                    "type": "tool_use",
                    "name": CRO_TOOL_NAME,
                    "input": {
                        "agent": AGENT_CRO,
                        "verdict": "APPROVE",
                        "confidence": 0.8,
                        "thesis": "risks acceptable",
                        "risks": [],
                        "sources": [],
                    },
                }
            ]
        }


def _plan() -> dict:
    return {
        "symbol": "AAPL",
        "quantity": 1.0,
        "entry_usd": 200.1,
        "stop_price": 180.0,
        "target_price": 290.0,
        "spread_bps": 5.0,
    }


def test_review_candidate_approves_and_second_call_uses_cache(tmp_path: Path) -> None:
    transport = _Scripted()
    cache = tmp_path / "council_cache.json"
    kwargs = dict(
        plan=_plan(),
        signal={"symbol": "AAPL", "tier": "A", "score": 20},
        shortlist={"asof_session": "2026-09-04", "market_breadth_pct": 62},
        param_hash="abc",
        env=ENV,
        cache_path=cache,
        transport=transport,
        prompt_root=Path("agents"),
        config=CONFIG,
    )
    first = review_candidate(**kwargs)
    calls_after_first = transport.calls
    assert first["verdict"] == "APPROVE"
    assert first["cached"] is False
    assert calls_after_first > 0
    second = review_candidate(**kwargs)
    assert second["verdict"] == "APPROVE"
    assert second["cached"] is True
    assert transport.calls == calls_after_first
    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert "2026-09-04:AAPL:abc" in stored["decisions"]


def test_http_error_keeps_provider_message() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "tools.0.custom.input_schema: additionalProperties requires strict",
            },
        }
    ).encode()
    exc = urllib.error.HTTPError(
        "https://api.anthropic.com/v1/messages",
        400,
        "Bad Request",
        hdrs=None,
        fp=io.BytesIO(body),
    )
    text = _http_error_text(exc, ANTHROPIC_MESSAGES_URL)
    assert text.startswith("HTTP 400 api.anthropic.com:")
    assert "additionalProperties" in text


def test_pause_turn_replays_assistant_without_a_new_user_message(tmp_path: Path) -> None:
    class _Pausing:
        def __init__(self) -> None:
            self.bodies: list[dict] = []
            self._paused = False

        def request(self, method: str, url: str, headers: dict, body: dict | None, timeout: float) -> dict:
            if url == ANTHROPIC_MODELS_URL:
                return {
                    "data": [
                        {
                            "id": "claude-sonnet-test",
                            "display_name": "Claude Sonnet",
                            "created_at": "2026-01-01",
                        }
                    ]
                }
            if url == OPENAI_RESPONSES_URL:
                assert body is not None
                head = str(body["input"]).split("\nEVIDENCE:\n", 1)[0]
                agent = next(name for name in sorted(AGENT_ORDER, key=len, reverse=True) if name in head)
                output: list[dict] = []
                if "tools" in body:
                    output.append(
                        {
                            "type": "web_search_call",
                            "action": {"sources": [{"url": "https://example.test/openai"}]},
                        }
                    )
                output.append(
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "agent": agent,
                                        "verdict": "APPROVE",
                                        "confidence": 0.9,
                                        "thesis": "coherent",
                                        "risks": [],
                                        "sources": [],
                                    }
                                ),
                            }
                        ],
                    }
                )
                return {"output": output}
            assert body is not None
            self.bodies.append(body)
            tools = body.get("tools") or []
            if any(tool.get("name") == "web_search" for tool in tools):
                assert tools[0]["allowed_callers"] == ["direct"]
                if not self._paused:
                    self._paused = True
                    return {
                        "stop_reason": "pause_turn",
                        "content": [
                            {"type": "server_tool_use", "name": "web_search", "id": "1"},
                            {
                                "type": "web_search_tool_result",
                                "content": [
                                    {
                                        "type": "web_search_result",
                                        "url": "https://example.test/cro",
                                        "title": "filing",
                                        "encrypted_content": "keep-me",
                                    }
                                ],
                            },
                        ],
                    }
                roles = [message["role"] for message in body["messages"]]
                assert roles == ["user", "assistant"]
                assert body["messages"][1]["content"][1]["content"][0]["encrypted_content"] == "keep-me"
                return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}
            assert tools[0]["strict"] is True
            assert "tool_choice" not in body
            return {
                "content": [
                    {
                        "type": "tool_use",
                        "name": CRO_TOOL_NAME,
                        "input": {
                            "agent": AGENT_CRO,
                            "verdict": "APPROVE",
                            "confidence": 0.8,
                            "thesis": "risks acceptable",
                            "risks": [],
                            "sources": [],
                        },
                    }
                ]
            }

    decision = review_candidate(
        plan=_plan(),
        signal={"symbol": "AAPL"},
        shortlist={"asof_session": "2026-10-06"},
        param_hash="abc",
        env=ENV,
        cache_path=tmp_path / "cache.json",
        transport=_Pausing(),
        prompt_root=Path("agents"),
        config=CONFIG,
    )
    assert decision["verdict"] == "APPROVE"
