"""AI council: šest rolí, verdikt skládá Python. Ceny a velikost zůstávají v Quantu.

Bez OPENAI_API_KEY a ANTHROPIC_API_KEY se rada nevolá. Chyba provideru, schéma
nebo chybějící člen = žádný obchod. Model nesmí změnit stop, target ani quantity.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
COUNCIL_CONFIG_PATH = REPO_ROOT / "config" / "council.v1.json"
PROMPT_ROOT = REPO_ROOT / "agents"
DEFAULT_CACHE_PATH = REPO_ROOT / "data" / "council_cache.json"

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
CRO_TOOL_NAME = "submit_investai_cro_finding"

VERDICT_APPROVE = "APPROVE"
VERDICT_REJECT = "REJECT"
VERDICT_NO_TRADE = "NO_TRADE"
ALLOWED_VERDICTS = frozenset({VERDICT_APPROVE, VERDICT_REJECT, VERDICT_NO_TRADE})

AGENT_SCANNER = "Scanner Agent"
AGENT_REGIME = "Market Regime Agent"
AGENT_NEWS = "News and Event Agent"
AGENT_TECHNICAL = "Technical Context Agent"
AGENT_THESIS = "Thesis Agent"
AGENT_CRO = "Claude CRO"

AGENT_ORDER = (
    AGENT_SCANNER,
    AGENT_REGIME,
    AGENT_NEWS,
    AGENT_TECHNICAL,
    AGENT_THESIS,
    AGENT_CRO,
)
WEB_AGENTS = frozenset({AGENT_REGIME, AGENT_NEWS, AGENT_CRO})
OPENAI_AGENTS = {
    AGENT_SCANNER: "gpt/scanner_prompt.md",
    AGENT_REGIME: "gpt/market_regime_prompt.md",
    AGENT_NEWS: "gpt/news_event_prompt.md",
    AGENT_TECHNICAL: "gpt/technical_context_prompt.md",
    AGENT_THESIS: "gpt/thesis_prompt.md",
}


class CouncilUnavailable(RuntimeError):
    """Provider, schéma nebo provenance. Buyer z toho udělá COUNCIL_UNAVAILABLE."""


def council_configured(env: Mapping[str, str]) -> bool:
    return bool(
        (env.get("OPENAI_API_KEY") or "").strip()
        and (env.get("ANTHROPIC_API_KEY") or "").strip()
    )


def load_council_config(path: Path | None = None) -> dict:
    return json.loads((path or COUNCIL_CONFIG_PATH).read_text(encoding="utf-8"))


def cache_key(asof_session: str, symbol: str, param_hash: str) -> str:
    return f"{asof_session}:{symbol}:{param_hash}"


def build_packet(
    plan: Mapping[str, Any],
    signal: Mapping[str, Any],
    shortlist: Mapping[str, Any],
    param_hash: str,
) -> dict:
    """Jen pole, která už spočítal Python. Model je nesmí přepisovat."""
    return {
        "symbol": plan["symbol"],
        "asof_session": shortlist.get("asof_session"),
        "param_hash": param_hash,
        "tier": signal.get("tier"),
        "score": signal.get("score"),
        "rs_excess_pct": signal.get("rs_excess_pct"),
        "breakout_strength": signal.get("breakout_strength"),
        "atr_14": signal.get("atr_14"),
        "entry_usd": plan["entry_usd"],
        "stop_price": plan["stop_price"],
        "target_price": plan["target_price"],
        "quantity": plan["quantity"],
        "spread_bps": plan["spread_bps"],
        "market_breadth_pct": shortlist.get("market_breadth_pct"),
        "breadth_gate": shortlist.get("breadth_gate"),
        "authority": (
            "entry_usd, stop_price, target_price and quantity are authoritative Python "
            "outputs. Do not recompute or replace them."
        ),
    }


def _finding_schema(agent: str) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "agent": {"type": "string", "enum": [agent]},
            "verdict": {"type": "string", "enum": sorted(ALLOWED_VERDICTS)},
            "confidence": {"type": "number"},
            "thesis": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
        "required": ["agent", "verdict", "confidence", "thesis", "risks", "sources"],
    }


def _as_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CouncilUnavailable("confidence musí být číslo 0 až 1.")
    number = float(value)
    if number < 0.0 or number > 1.0:
        raise CouncilUnavailable("confidence mimo interval 0 až 1.")
    return number


def parse_finding(payload: object, expected_agent: str) -> dict:
    if not isinstance(payload, dict):
        raise CouncilUnavailable(f"{expected_agent}: finding není objekt.")
    agent = payload.get("agent")
    if agent != expected_agent:
        raise CouncilUnavailable(f"Agent identity: čekám {expected_agent}, dostal {agent}.")
    verdict = payload.get("verdict")
    if verdict not in ALLOWED_VERDICTS:
        raise CouncilUnavailable(f"{expected_agent}: neplatný verdict.")
    thesis = payload.get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        raise CouncilUnavailable(f"{expected_agent}: chybí thesis.")
    risks = payload.get("risks")
    if not isinstance(risks, list) or any(not isinstance(item, str) for item in risks):
        raise CouncilUnavailable(f"{expected_agent}: risks musí být seznam textů.")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise CouncilUnavailable(f"{expected_agent}: sources musí být seznam.")
    urls: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            raise CouncilUnavailable(f"{expected_agent}: source není objekt.")
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise CouncilUnavailable(f"{expected_agent}: source url není http(s).")
        urls.append(url)
    return {
        "agent": expected_agent,
        "verdict": verdict,
        "confidence": _as_confidence(payload.get("confidence")),
        "thesis": thesis.strip()[:800],
        "risks": [item.strip() for item in risks[:5] if item.strip()],
        "sources": [{"url": url} for url in urls],
        "provenance_ok": False,
    }


def council_verdict(findings: list[dict], config: Mapping[str, Any]) -> tuple[str, str]:
    """Jediný verdikt rady. Membership, CRO veto, pak práh schválení."""
    names = [finding["agent"] for finding in findings]
    if len(names) != len(AGENT_ORDER) or set(names) != set(AGENT_ORDER):
        return VERDICT_NO_TRADE, "Council membership is incomplete, duplicated, or unexpected"
    if any(not finding.get("provenance_ok") for finding in findings):
        return VERDICT_NO_TRADE, "Required web provenance is missing or invalid"
    for finding in findings:
        web = finding["agent"] in WEB_AGENTS
        has_sources = bool(finding.get("sources"))
        if web and not has_sources:
            return VERDICT_NO_TRADE, f"{finding['agent']} has no verified web source"
        if not web and has_sources:
            return VERDICT_NO_TRADE, f"{finding['agent']} returned sources without a web tool"

    min_confidence = float(config["min_approval_confidence"])
    min_approvals = int(config["min_approvals"])
    cro = next(finding for finding in findings if finding["agent"] == AGENT_CRO)
    if cro["verdict"] == VERDICT_REJECT:
        return VERDICT_REJECT, "Claude CRO rejected the proposal"
    if cro["verdict"] == VERDICT_NO_TRADE:
        return VERDICT_NO_TRADE, "Claude CRO found insufficient evidence"
    if cro["verdict"] != VERDICT_APPROVE or cro["confidence"] < min_confidence:
        return VERDICT_REJECT, "Claude CRO approval is below the confidence threshold"

    specialists = [finding for finding in findings if finding["agent"] != AGENT_CRO]
    if any(finding["verdict"] == VERDICT_REJECT for finding in specialists):
        return VERDICT_REJECT, "At least one specialist rejected the proposal"
    if any(finding["verdict"] == VERDICT_NO_TRADE for finding in specialists):
        return VERDICT_NO_TRADE, "At least one specialist reported insufficient evidence"

    approvals = [
        finding
        for finding in findings
        if finding["verdict"] == VERDICT_APPROVE and finding["confidence"] >= min_confidence
    ]
    if len(approvals) >= min_approvals:
        return (
            VERDICT_APPROVE,
            f"{len(approvals)}/6 approved with Claude CRO and confidence >= {min_confidence:.2f}",
        )
    return VERDICT_NO_TRADE, "Council did not reach the approval threshold"


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _openai_tool_urls(payload: Mapping[str, Any]) -> tuple[int, list[str]]:
    call_count = 0
    urls: dict[str, str] = {}
    output = payload.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        call_count += 1
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        candidates = []
        action_url = action.get("url")
        if isinstance(action_url, str):
            candidates.append(action_url)
        for source in action.get("sources") if isinstance(action.get("sources"), list) else []:
            if isinstance(source, dict) and isinstance(source.get("url"), str):
                candidates.append(source["url"])
        for url in candidates:
            if url.startswith(("http://", "https://")):
                urls.setdefault(_normalize_url(url), url)
    return call_count, [urls[key] for key in sorted(urls)]


def _extract_openai_text(payload: Mapping[str, Any]) -> str:
    output = payload.get("output")
    for item in reversed(output if isinstance(output, list) else []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        for part in reversed(content if isinstance(content, list) else []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    raise CouncilUnavailable("OpenAI response has no output_text.")


def attach_openai_provenance(finding: dict, payload: Mapping[str, Any], *, web: bool) -> dict:
    if not web:
        if finding["sources"]:
            raise CouncilUnavailable(f"{finding['agent']} returned sources without a web tool.")
        return {**finding, "provenance_ok": True, "sources": []}
    call_count, urls = _openai_tool_urls(payload)
    if call_count < 1 or not urls:
        raise CouncilUnavailable(f"{finding['agent']} produced no verifiable web search.")
    return {
        **finding,
        "provenance_ok": True,
        "sources": [{"url": url} for url in urls],
    }


def _claude_web_sources(payload: Mapping[str, Any]) -> tuple[int, list[dict[str, str]]]:
    content = payload.get("content")
    if not isinstance(content, list):
        raise CouncilUnavailable("Anthropic web response has no content array.")
    call_count = sum(
        1
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "server_tool_use"
        and block.get("name") == "web_search"
    )
    sources: dict[str, dict[str, str]] = {}
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
            continue
        result_content = block.get("content")
        if isinstance(result_content, dict) and result_content.get("error_code"):
            if result_content.get("error_code") == "max_uses_exceeded":
                continue
            raise CouncilUnavailable(
                f"Anthropic web search error: {result_content.get('error_code')}"
            )
        for result in result_content if isinstance(result_content, list) else []:
            if not isinstance(result, dict) or result.get("type") != "web_search_result":
                continue
            url = result.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            sources.setdefault(
                _normalize_url(url),
                {"url": url, "title": str(result.get("title") or url)},
            )
    return call_count, [sources[key] for key in sorted(sources)]


def _extract_claude_tool_input(payload: Mapping[str, Any]) -> dict:
    content = payload.get("content")
    if not isinstance(content, list):
        raise CouncilUnavailable("Anthropic finding has no content array.")
    matches = [
        block
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and block.get("name") == CRO_TOOL_NAME
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("input"), dict):
        raise CouncilUnavailable("Anthropic CRO did not submit exactly one finding.")
    return matches[0]["input"]


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
        timeout: float,
    ) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={**dict(headers), "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise CouncilUnavailable(f"HTTP {exc.code} {urlsplit(url).netloc}") from None
        except urllib.error.URLError as exc:
            raise CouncilUnavailable(f"HTTP failed {urlsplit(url).netloc}: {exc.reason}") from None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise CouncilUnavailable(f"Non-JSON response from {urlsplit(url).netloc}") from exc
        if not isinstance(parsed, dict):
            raise CouncilUnavailable(f"Unexpected response from {urlsplit(url).netloc}")
        return parsed


def _load_prompt(prompt_root: Path, relative: str) -> str:
    return (prompt_root / relative).read_text(encoding="utf-8").strip()


def _openai_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}


def _select_claude_model(configured: str, models_response: Mapping[str, Any]) -> str:
    raw = models_response.get("data")
    if not isinstance(raw, list):
        raise CouncilUnavailable("Anthropic models response has no data array.")
    models = [item for item in raw if isinstance(item, dict) and isinstance(item.get("id"), str)]
    available = {str(item["id"]) for item in models}
    if configured != "auto":
        if configured not in available:
            raise CouncilUnavailable("Configured Anthropic model is not available.")
        return configured
    sonnets = [
        item
        for item in models
        if "sonnet" in f"{item.get('id', '')} {item.get('display_name', '')}".lower()
    ]
    if not sonnets:
        raise CouncilUnavailable("No Anthropic Sonnet model is available.")
    selected = max(sonnets, key=lambda item: (str(item.get("created_at", "")), str(item["id"])))
    return str(selected["id"])


def _call_openai(
    *,
    agent: str,
    prompt: str,
    evidence: Mapping[str, Any],
    web: bool,
    model: str,
    api_key: str,
    timeout: float,
    max_searches: int,
    transport: UrllibTransport,
) -> dict:
    source_rule = (
        "Use web search. Set sources to an empty array; Python attaches tool URLs."
        if web
        else "You have no web tool. Set sources to an empty array."
    )
    body: dict[str, Any] = {
        "model": model,
        "store": False,
        "input": (
            f"{prompt}\n\n{source_rule}\n"
            "Return only the structured finding. Do not invent prices or position size.\n\n"
            f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "investai_finding",
                "strict": True,
                "schema": _finding_schema(agent),
            }
        },
    }
    if web:
        body["tools"] = [{"type": "web_search"}]
        body["include"] = ["web_search_call.action.sources"]
        body["max_tool_calls"] = max_searches
    print(f"AGENT_START={agent}", flush=True)
    payload = transport.request(
        "POST",
        OPENAI_RESPONSES_URL,
        _openai_headers(api_key),
        body,
        timeout,
    )
    text = _extract_openai_text(payload)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CouncilUnavailable(f"{agent} returned non-JSON finding.") from exc
    finding = parse_finding(parsed, agent)
    verified = attach_openai_provenance(finding, payload, web=web)
    print(f"AGENT_DONE={agent}", flush=True)
    return verified


def _claude_research_text(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        str(part.get("text", "")).strip()
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(text for text in texts if text).strip()


def _call_claude(
    *,
    prompt: str,
    evidence: Mapping[str, Any],
    model: str,
    api_key: str,
    timeout: float,
    max_searches: int,
    transport: UrllibTransport,
) -> dict:
    print(f"AGENT_START={AGENT_CRO}", flush=True)
    headers = _anthropic_headers(api_key)
    research_messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Search the public web for reasons this candidate should not be bought. "
                "Do not recompute prices or size.\n\n"
                f"CRO ROLE:\n{prompt}\n\n"
                f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"
            ),
        }
    ]
    research: dict[str, Any] | None = None
    for _ in range(2):
        research = transport.request(
            "POST",
            ANTHROPIC_MESSAGES_URL,
            headers,
            {
                "model": model,
                "max_tokens": 2000,
                "system": (
                    "You are the InvestAI risk-officer research pass. Use web search. "
                    "You have no broker authority."
                ),
                "messages": research_messages,
                "tools": [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": max_searches,
                    }
                ],
            },
            timeout,
        )
        if research.get("stop_reason") != "pause_turn":
            break
        research_messages.append({"role": "assistant", "content": research.get("content", [])})
        research_messages.append({"role": "user", "content": "Continue the web research."})
    if research is None or research.get("stop_reason") == "pause_turn":
        raise CouncilUnavailable("Anthropic web research did not finish.")
    call_count, sources = _claude_web_sources(research)
    if call_count < 1 or not sources:
        raise CouncilUnavailable("Claude CRO produced no verifiable web search.")
    summary = _claude_research_text(research) or "Web research completed."
    finding_payload = transport.request(
        "POST",
        ANTHROPIC_MESSAGES_URL,
        headers,
        {
            "model": model,
            "max_tokens": 1500,
            "system": (
                "Submit exactly one structured CRO finding. Set sources to an empty array. "
                "Do not recompute price, stop, target, or size. "
                "APPROVE only at confidence >= 0.65; otherwise REJECT."
            ),
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nVERIFIED_WEB_RESEARCH:\n"
                        f"{json.dumps({'summary': summary, 'sources': sources}, ensure_ascii=False, sort_keys=True)}\n\n"
                        f"EVIDENCE:\n{json.dumps(evidence, ensure_ascii=False, sort_keys=True)}"
                    ),
                }
            ],
            "tools": [
                {
                    "name": CRO_TOOL_NAME,
                    "description": "Submit the InvestAI CRO finding.",
                    "input_schema": _finding_schema(AGENT_CRO),
                }
            ],
            "tool_choice": {"type": "tool", "name": CRO_TOOL_NAME},
        },
        timeout,
    )
    finding = parse_finding(_extract_claude_tool_input(finding_payload), AGENT_CRO)
    if finding["sources"]:
        raise CouncilUnavailable("Claude CRO returned sources; Python attaches provenance.")
    print(f"AGENT_DONE={AGENT_CRO}", flush=True)
    return {**finding, "provenance_ok": True, "sources": [{"url": row["url"]} for row in sources]}


def _public_finding(finding: Mapping[str, Any]) -> dict:
    return {
        "agent": finding["agent"],
        "verdict": finding["verdict"],
        "confidence": finding["confidence"],
        "thesis": finding["thesis"],
        "risks": list(finding.get("risks") or []),
        "sources": list(finding.get("sources") or []),
    }


def _read_cache(path: Path) -> dict:
    if not path.is_file():
        return {"decisions": {}}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("decisions"), dict):
        return {"decisions": {}}
    return document


def _write_cache(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def review_candidate(
    *,
    plan: Mapping[str, Any],
    signal: Mapping[str, Any],
    shortlist: Mapping[str, Any],
    param_hash: str,
    env: Mapping[str, str] | None = None,
    cache_path: Path | None = None,
    transport: UrllibTransport | None = None,
    prompt_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict:
    """Jedna rada pro jeden plán. Cache platí, dokud se nezmění signální seance."""
    source = env if env is not None else os.environ
    if not council_configured(source):
        raise CouncilUnavailable("Chybí OPENAI_API_KEY nebo ANTHROPIC_API_KEY.")
    resolved = dict(config or load_council_config())
    packet = build_packet(plan, signal, shortlist, param_hash)
    asof = str(packet.get("asof_session") or "")
    symbol = str(packet["symbol"])
    key = cache_key(asof, symbol, param_hash) if asof else ""
    store_path = cache_path if cache_path is not None else DEFAULT_CACHE_PATH
    cache = _read_cache(store_path)
    cached = cache["decisions"].get(key) if key else None
    if isinstance(cached, dict) and cached.get("verdict") in ALLOWED_VERDICTS:
        return {**cached, "cached": True}

    client = transport or UrllibTransport()
    prompts = prompt_root or PROMPT_ROOT
    timeout = float(resolved["timeout_seconds"])
    max_searches = int(resolved["web_search_max_uses"])
    openai_model = (source.get("OPENAI_COUNCIL_MODEL") or str(resolved["openai_model"])).strip()
    claude_configured = (source.get("CLAUDE_CRO_MODEL") or str(resolved["claude_model"])).strip()
    openai_key = str(source["OPENAI_API_KEY"]).strip()
    anthropic_key = str(source["ANTHROPIC_API_KEY"]).strip()

    base_evidence = {"quant_packet": packet}
    scanner = _call_openai(
        agent=AGENT_SCANNER,
        prompt=_load_prompt(prompts, OPENAI_AGENTS[AGENT_SCANNER]),
        evidence=base_evidence,
        web=False,
        model=openai_model,
        api_key=openai_key,
        timeout=timeout,
        max_searches=max_searches,
        transport=client,
    )
    regime = _call_openai(
        agent=AGENT_REGIME,
        prompt=_load_prompt(prompts, OPENAI_AGENTS[AGENT_REGIME]),
        evidence=base_evidence,
        web=True,
        model=openai_model,
        api_key=openai_key,
        timeout=timeout,
        max_searches=max_searches,
        transport=client,
    )
    news = _call_openai(
        agent=AGENT_NEWS,
        prompt=_load_prompt(prompts, OPENAI_AGENTS[AGENT_NEWS]),
        evidence=base_evidence,
        web=True,
        model=openai_model,
        api_key=openai_key,
        timeout=timeout,
        max_searches=max_searches,
        transport=client,
    )
    technical = _call_openai(
        agent=AGENT_TECHNICAL,
        prompt=_load_prompt(prompts, OPENAI_AGENTS[AGENT_TECHNICAL]),
        evidence=base_evidence,
        web=False,
        model=openai_model,
        api_key=openai_key,
        timeout=timeout,
        max_searches=max_searches,
        transport=client,
    )
    specialists = [_public_finding(item) for item in (scanner, regime, news, technical)]
    thesis = _call_openai(
        agent=AGENT_THESIS,
        prompt=_load_prompt(prompts, OPENAI_AGENTS[AGENT_THESIS]),
        evidence={**base_evidence, "specialists": specialists},
        web=False,
        model=openai_model,
        api_key=openai_key,
        timeout=timeout,
        max_searches=max_searches,
        transport=client,
    )
    models = client.request(
        "GET",
        ANTHROPIC_MODELS_URL,
        _anthropic_headers(anthropic_key),
        None,
        timeout,
    )
    claude_model = _select_claude_model(claude_configured or "auto", models)
    cro = _call_claude(
        prompt=_load_prompt(prompts, "claude/trading_cro_prompt.md"),
        evidence={
            **base_evidence,
            "specialists": [*specialists, _public_finding(thesis)],
        },
        model=claude_model,
        api_key=anthropic_key,
        timeout=timeout,
        max_searches=max_searches,
        transport=client,
    )
    findings = [scanner, regime, news, technical, thesis, cro]
    verdict, reason = council_verdict(findings, resolved)
    decision = {
        "verdict": verdict,
        "reason": reason,
        "symbol": symbol,
        "asof_session": asof,
        "param_hash": param_hash,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cached": False,
        "findings": [_public_finding(item) for item in findings],
    }
    if key and verdict in ALLOWED_VERDICTS:
        cache["decisions"][key] = {k: v for k, v in decision.items() if k != "cached"}
        _write_cache(store_path, cache)
    return decision
