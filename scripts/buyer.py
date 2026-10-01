"""Buyer řetěz: freshness → preselect → kotace → ČNB FX → finalize → council → fence/submit."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.broker_snapshot import global_blockers, normalize_ticker
from scripts.council import CouncilUnavailable, council_configured, review_candidate
from scripts.evaluate_signals import DECISION_BUY
from scripts.fence import MemoryFenceStore, OUTCOME_SKIP, STATE_CONFIRMED
from scripts.forward_shadow import require_live_sizing, size_shares
from scripts.param_hash import compute_param_hash
from scripts.session_gate import nyse_tz
from scripts.submit_order import SubmitResult, default_can_trade, submit_order

REPO_ROOT = Path(__file__).resolve().parent.parent
BUYER_CONFIG_PATH = REPO_ROOT / "config" / "buyer.v1.json"
STRATEGY_PATH = REPO_ROOT / "config" / "strategy.v1.json"
DEFAULT_SHORTLIST = REPO_ROOT / "data" / "signals.json"
DEFAULT_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"
DEFAULT_EVIDENCE = REPO_ROOT / "data" / "buyer_evidence.json"
ALPACA_QUOTES_URL = "https://data.alpaca.markets/v2/stocks/quotes/latest"
CNB_DAILY_URL = (
    "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/"
    "kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"
)
HTTP_TIMEOUT_SEC = 20
BPS = 10_000.0

OUTCOME_MARKET_CLOSED = "MARKET_CLOSED"
OUTCOME_NO_CANDIDATES = "NO_CANDIDATES"
OUTCOME_FRESHNESS_FAIL = "FRESHNESS_FAIL"
REASON_UNRESOLVED_FENCE = "UNRESOLVED_FENCE"
UNRESOLVED_FENCE_ERROR = (
    "::error::UNRESOLVED_FENCE — visí fence UNCERTAIN, nákupy stojí. "
    "SQL: SELECT fence_key, symbol, state, order_id FROM trading.trade_fences "
    "WHERE state = 'UNCERTAIN'; rozhodni CONFIRMED nebo NEVER_SENT."
)
HEALTHY_FAMILIES = frozenset(
    {OUTCOME_MARKET_CLOSED, OUTCOME_NO_CANDIDATES, "ORDER_SUBMITTED", "COUNCIL_REJECT"}
)
OUTCOME_RE = re.compile(
    r"^(MARKET_CLOSED|NO_CANDIDATES|FRESHNESS_FAIL|"
    r"GLOBAL_BLOCKER:.+|SPREAD_REJECTED:[A-Z0-9.]+|"
    r"FENCE_EXISTS:[A-Z0-9.]+|ORDER_SUBMITTED:.+|"
    r"COUNCIL_REJECT:[A-Z0-9.]+|COUNCIL_UNAVAILABLE:[A-Z0-9.]+)$"
)

HttpGet = Callable[[str, dict[str, str]], object]
CanTradeFn = Callable[[], tuple[bool, str]]


class BuyerError(RuntimeError):
    pass


@dataclass
class BuyerResult:
    outcome: str
    exit_code: int = 0
    extra_summary: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    fail_closed: str | None = None


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def is_official_outcome(outcome: str) -> bool:
    return bool(OUTCOME_RE.match(outcome))


def outcome_family(outcome: str) -> str:
    if outcome.startswith("ORDER_SUBMITTED:"):
        return "ORDER_SUBMITTED"
    if outcome.startswith("GLOBAL_BLOCKER:"):
        return "GLOBAL_BLOCKER"
    if outcome.startswith("SPREAD_REJECTED:"):
        return "SPREAD_REJECTED"
    if outcome.startswith("FENCE_EXISTS:"):
        return "FENCE_EXISTS"
    if outcome.startswith("COUNCIL_REJECT:"):
        return "COUNCIL_REJECT"
    if outcome.startswith("COUNCIL_UNAVAILABLE:"):
        return "COUNCIL_UNAVAILABLE"
    return outcome


def is_healthy_outcome(outcome: str) -> bool:
    return outcome_family(outcome) in HEALTHY_FAMILIES


def global_blocker_outcome(name: str) -> str:
    return f"GLOBAL_BLOCKER:{name}"


def spread_rejected_outcome(symbol: str) -> str:
    return f"SPREAD_REJECTED:{symbol}"


def fence_exists_outcome(symbol: str) -> str:
    return f"FENCE_EXISTS:{symbol}"


def order_submitted_outcome(order_id: str) -> str:
    return f"ORDER_SUBMITTED:{order_id}"


def council_reject_outcome(symbol: str) -> str:
    return f"COUNCIL_REJECT:{symbol}"


def council_unavailable_outcome(symbol: str) -> str:
    return f"COUNCIL_UNAVAILABLE:{symbol}"


def shortlist_close_at(asof_session: str) -> datetime:
    return datetime.combine(date.fromisoformat(asof_session), time(16, 0), tzinfo=nyse_tz())


def shortlist_age_hours(asof_session: str, now: datetime) -> float:
    close = shortlist_close_at(asof_session).astimezone(timezone.utc)
    current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return (current - close).total_seconds() / 3600.0


def freshness_ok(shortlist: dict, now: datetime, config: dict) -> bool:
    asof = shortlist.get("asof_session")
    if not asof:
        return False
    max_hours = float(config["freshness_max_age_hours"])
    if shortlist_age_hours(str(asof), now) > max_hours:
        return False
    generated = shortlist.get("generated_at")
    if generated:
        stamp = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
        current = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if (current - stamp).total_seconds() / 3600.0 > max_hours:
            return False
    return True


def buy_candidates(shortlist: dict) -> list[dict]:
    rows = [row for row in (shortlist.get("signals") or []) if row.get("decision") == DECISION_BUY]
    return sorted(rows, key=lambda row: (-float(row.get("score") or 0), str(row.get("symbol") or "")))


def held_symbols(snapshot: dict) -> set[str]:
    held: set[str] = set()
    for position in snapshot.get("positions") or []:
        if float(position.get("quantity") or 0) == 0:
            continue
        ticker = normalize_ticker(str(position.get("ticker") or ""))
        if ticker:
            held.add(ticker)
    return held


def open_position_count(snapshot: dict) -> int:
    return len(held_symbols(snapshot))


def spread_bps(bid: float, ask: float) -> float | None:
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * BPS


def parse_cnb_usdczk(text: str) -> float:
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 5:
            continue
        if parts[3].strip().upper() != "USD":
            continue
        amount = float(parts[2].replace(",", "."))
        rate = float(parts[4].replace(",", "."))
        if amount <= 0 or rate <= 0:
            raise BuyerError("CNB USD kurz neplatný.")
        return rate / amount
    raise BuyerError("CNB denní kurz neobsahuje USD.")


def fetch_cnb_usdczk(*, http_get: HttpGet | None = None, url: str = CNB_DAILY_URL) -> float:
    getter = http_get or _default_http_get
    payload = getter(url, {"Accept": "text/plain"})
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace")
    elif isinstance(payload, str):
        text = payload
    else:
        raise BuyerError("CNB vrátila neočekávaný tvar.")
    return parse_cnb_usdczk(text)


def alpaca_headers(env: dict[str, str] | None = None) -> dict[str, str]:
    source = env if env is not None else os.environ
    key_id = (source.get("ALPACA_KEY_ID") or "").strip()
    secret = (source.get("ALPACA_SECRET_KEY") or "").strip()
    if not key_id or not secret:
        raise BuyerError("Chybí ALPACA_KEY_ID nebo ALPACA_SECRET_KEY v prostředí.")
    return {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}


def fetch_quotes(
    symbols: list[str],
    *,
    env: dict[str, str] | None = None,
    http_get: HttpGet | None = None,
    feed: str = "iex",
) -> dict[str, dict]:
    if not symbols:
        return {}
    getter = http_get or _default_http_get
    query = urllib.parse.urlencode({"symbols": ",".join(symbols), "feed": feed})
    url = f"{ALPACA_QUOTES_URL}?{query}"
    headers = alpaca_headers(env) if http_get is None else {
        "APCA-API-KEY-ID": (env or {}).get("ALPACA_KEY_ID", "test"),
        "APCA-API-SECRET-KEY": (env or {}).get("ALPACA_SECRET_KEY", "test"),
    }
    payload = getter(url, headers)
    if not isinstance(payload, dict):
        raise BuyerError("Alpaca kotace: neočekávaný tvar.")
    raw = payload.get("quotes") or payload
    if not isinstance(raw, dict):
        raise BuyerError("Alpaca kotace: chybí quotes.")
    quotes: dict[str, dict] = {}
    for symbol, row in raw.items():
        if not isinstance(row, dict):
            continue
        bid = float(row.get("bp") or row.get("bid_price") or 0)
        ask = float(row.get("ap") or row.get("ask_price") or 0)
        quotes[str(symbol).upper()] = {"bid": bid, "ask": ask, "raw": row}
    return quotes


def _default_http_get(url: str, headers: dict[str, str]) -> object:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise BuyerError(f"HTTP {exc.code} GET {url}: {detail}") from None
    except urllib.error.URLError as exc:
        raise BuyerError(f"GET selhal: {exc}") from exc
    if "denni_kurz" in url or url.endswith(".txt"):
        return body.decode("utf-8", errors="replace")
    return json.loads(body.decode("utf-8"))


def finalize_plan(
    signal: dict,
    quote: dict,
    fx_rate: float,
    sizing: dict,
    params: dict,
) -> dict | None:
    ask = float(quote["ask"])
    stop = float(signal.get("stop_price") or 0)
    if ask <= 0 or stop <= 0 or stop >= ask:
        return None
    shares = size_shares(entry_usd=ask, stop_usd=stop, fx_rate=fx_rate, sizing=sizing)
    if shares < 1:
        return None
    risk = ask - stop
    target = ask + float(params["reward_to_risk"]) * risk
    return {
        "symbol": signal["symbol"],
        "quantity": float(shares),
        "entry_usd": ask,
        "stop_price": stop,
        "target_price": target,
        "fx_usdczk": fx_rate,
        "spread_bps": spread_bps(float(quote["bid"]), ask),
    }


def _fix_line(blocker: str) -> str | None:
    if not blocker.startswith("unprotected_position:"):
        return None
    symbol = normalize_ticker(blocker.split(":", 1)[1])
    return f"FIX: gh workflow run protective-stop-guard.yml -f symbol={symbol}"


def _ok(outcome: str, *, extra: list[str] | None = None, **evidence: object) -> BuyerResult:
    if not is_official_outcome(outcome):
        raise BuyerError(f"Neplatný OUTCOME: {outcome}")
    return BuyerResult(outcome=outcome, extra_summary=extra or [], evidence=dict(evidence))


def run_buyer(
    *,
    session_date: str,
    shortlist: dict | None,
    snapshot: dict,
    config: dict,
    strategy: dict,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
    can_trade_fn: CanTradeFn | None = None,
    http_get: HttpGet | None = None,
    submit_fn: Callable[..., SubmitResult] | None = None,
    council_fn: Callable[..., dict] | None = None,
    dry_run: bool = False,
) -> BuyerResult:
    now = now or datetime.now(timezone.utc)
    params = strategy["parameters"]
    sizing = require_live_sizing(params)
    param_hash = compute_param_hash()

    if shortlist is None or not freshness_ok(shortlist, now, config):
        return _ok(OUTCOME_FRESHNESS_FAIL, asof_session=(shortlist or {}).get("asof_session"))

    blockers = global_blockers(snapshot)
    if blockers:
        name = blockers[0]
        extra = [line for line in (_fix_line(name),) if line]
        return _ok(global_blocker_outcome(name), extra=extra, blockers=blockers)

    allowed, reason = (can_trade_fn or (lambda: default_can_trade(env)))()
    if not allowed:
        extra: list[str] = []
        result = _ok(global_blocker_outcome(reason), extra=extra)
        if reason == REASON_UNRESOLVED_FENCE:
            result.exit_code = 1
            result.extra_summary = [UNRESOLVED_FENCE_ERROR]
        return result

    if open_position_count(snapshot) >= int(sizing["max_open_positions"]):
        return _ok(global_blocker_outcome("max_open_positions"))

    held = held_symbols(snapshot)
    candidates = [row for row in buy_candidates(shortlist) if row.get("symbol") not in held]
    if not candidates:
        return _ok(OUTCOME_NO_CANDIDATES, asof_session=shortlist.get("asof_session"))

    symbols = [str(row["symbol"]) for row in candidates]
    quotes = fetch_quotes(
        symbols, env=env, http_get=http_get, feed=str(config.get("quote_feed") or "iex")
    )
    max_spread = float(config["max_spread_bps"])
    tradable: list[tuple[dict, dict]] = []
    first_reject: str | None = None
    for row in candidates:
        symbol = str(row["symbol"])
        quote = quotes.get(symbol.upper())
        if quote is None:
            first_reject = first_reject or symbol
            continue
        bps = spread_bps(float(quote["bid"]), float(quote["ask"]))
        if bps is None or bps > max_spread:
            first_reject = first_reject or symbol
            continue
        tradable.append((row, quote))
    if not tradable:
        return _ok(spread_rejected_outcome(first_reject or symbols[0]))

    fx_rate = fetch_cnb_usdczk(http_get=http_get, url=str(config.get("fx_url") or CNB_DAILY_URL))

    plan = None
    chosen_signal: dict | None = None
    for row, quote in tradable:
        plan = finalize_plan(row, quote, fx_rate, sizing, params)
        if plan:
            chosen_signal = row
            break
    if plan is None or chosen_signal is None:
        return _ok(OUTCOME_NO_CANDIDATES, reason="unsizable")

    source_env = env if env is not None else os.environ
    council_evidence = None
    if council_fn is not None or council_configured(source_env):
        try:
            if council_fn is not None:
                decision = council_fn(
                    plan=plan,
                    signal=chosen_signal,
                    shortlist=shortlist,
                    param_hash=param_hash,
                    env=source_env,
                )
            else:
                decision = review_candidate(
                    plan=plan,
                    signal=chosen_signal,
                    shortlist=shortlist,
                    param_hash=param_hash,
                    env=source_env,
                )
        except CouncilUnavailable as exc:
            return _ok(
                council_unavailable_outcome(str(plan["symbol"])),
                extra=[f"council: {exc}"],
                plan=plan,
                council_error=str(exc),
            )
        council_evidence = {
            "verdict": decision.get("verdict"),
            "reason": decision.get("reason"),
            "cached": bool(decision.get("cached")),
            "findings": decision.get("findings") or [],
        }
        verdict = decision.get("verdict")
        if verdict == "REJECT":
            return _ok(
                council_reject_outcome(str(plan["symbol"])),
                extra=[f"council: {decision.get('reason') or 'REJECT'}"],
                plan=plan,
                council=council_evidence,
            )
        if verdict != "APPROVE":
            return _ok(
                council_unavailable_outcome(str(plan["symbol"])),
                extra=[f"council: {decision.get('reason') or verdict}"],
                plan=plan,
                council=council_evidence,
            )

    submit = submit_fn or submit_order
    result = submit(
        symbol=plan["symbol"],
        quantity=plan["quantity"],
        stop_price=plan["stop_price"],
        session_date=session_date,
        dry_run=dry_run,
        env=env,
        snapshot=snapshot,
        can_trade_fn=can_trade_fn or (lambda: default_can_trade(env)),
        store=MemoryFenceStore() if dry_run else None,
    )
    if result.outcome == OUTCOME_SKIP or result.reason == "fence_exists":
        return _ok(fence_exists_outcome(plan["symbol"]), fence_key=result.fence_key)
    if result.outcome == STATE_CONFIRMED and result.buy_order_id:
        evidence = {
            "plan": plan,
            "fence_key": result.fence_key,
            "stop_order_id": result.stop_order_id,
            "param_hash": param_hash,
        }
        if council_evidence is not None:
            evidence["council"] = council_evidence
        return _ok(order_submitted_outcome(result.buy_order_id), **evidence)
    if result.outcome == "STOP":
        reason_text = result.reason.replace("can_trade:", "", 1)
        if result.reason.startswith("can_trade:"):
            return _ok(global_blocker_outcome(reason_text))
        if result.reason.startswith("GLOBAL_BLOCKER:"):
            return _ok(result.reason)
        return BuyerResult(
            outcome=result.reason or "STOP",
            exit_code=result.exit_code,
            fail_closed=result.reason or "STOP",
            evidence={"plan": plan, "submit": result.reason},
        )
    return BuyerResult(
        outcome=result.reason or result.outcome,
        exit_code=result.exit_code or 2,
        fail_closed=result.reason or result.outcome,
        extra_summary=[result.critical] if result.critical else [],
        evidence={"plan": plan, "submit": result.reason, "fence_key": result.fence_key},
    )


def write_evidence(path: Path, result: BuyerResult, session_date: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "outcome": result.outcome,
        "session_date": session_date,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "official": is_official_outcome(result.outcome) and result.fail_closed is None,
        "extra_summary": result.extra_summary,
        **result.evidence,
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_github_output(result: BuyerResult) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(f"outcome={result.outcome}\n")
        handle.write(f"continue={'false' if result.fail_closed else 'true'}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Buyer řetěz po otevřené NYSE seanci.")
    parser.add_argument("--session", required=True)
    parser.add_argument("--shortlist", type=Path, default=DEFAULT_SHORTLIST)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--config", type=Path, default=BUYER_CONFIG_PATH)
    parser.add_argument("--strategy", type=Path, default=STRATEGY_PATH)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--dry-run", default="false")
    args = parser.parse_args(argv)
    dry_run = str(args.dry_run).strip().lower() in {"true", "1", "yes"}
    try:
        config = load_json(args.config)
        strategy = load_json(args.strategy)
        snapshot = load_json(args.snapshot)
        shortlist = load_json(args.shortlist) if args.shortlist.is_file() else None
        result = run_buyer(
            session_date=args.session,
            shortlist=shortlist,
            snapshot=snapshot,
            config=config,
            strategy=strategy,
            dry_run=dry_run,
        )
    except RuntimeError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1
    write_evidence(args.evidence, result, args.session)
    write_github_output(result)
    for line in result.extra_summary:
        if line.startswith("::error::") or line.startswith("::warning::"):
            print(line)
    if result.fail_closed or result.exit_code:
        if result.fail_closed:
            print(f"FAIL-CLOSED: {result.fail_closed}", file=sys.stderr)
        else:
            print(f"BUYER {result.outcome}", file=sys.stderr)
        return result.exit_code or 1
    print(f"BUYER {result.outcome}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
