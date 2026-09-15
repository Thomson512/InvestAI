"""Jeden demo market buy + ochranný stop. Dry-run default. Market bez retry; stop smí 429/400."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.broker_snapshot import (
    DEMO_BASE_URL,
    ORDERS_PATH,
    auth_headers,
    global_blockers,
    require_t212_credentials,
    require_demo_environment,
)
from scripts.fence import (
    EXIT_OK,
    EXIT_UNCERTAIN,
    OUTCOME_SKIP,
    STATE_CONFIRMED,
    STATE_SENT,
    STATE_UNCERTAIN,
    FenceStore,
    MemoryFenceStore,
    make_fence_key,
    run_fenced_send,
    store_from_env,
)
from scripts.param_hash import compute_param_hash

MARKET_PATH = "/api/v0/equity/orders/market"
STOP_PATH = "/api/v0/equity/orders/stop"
POST_TIMEOUT_SEC = 15
EXIT_STOP = 1

HttpPost = Callable[[str, dict[str, str], dict], dict]
HttpGet = Callable[[str, dict[str, str]], object]
CanTradeFn = Callable[[], tuple[bool, str]]


class SubmitError(RuntimeError):
    pass


@dataclass
class SubmitResult:
    outcome: str
    exit_code: int
    dry_run: bool
    fence_key: str | None = None
    buy_order_id: str | None = None
    stop_order_id: str | None = None
    reason: str = ""
    plan: dict = field(default_factory=dict)
    critical: str | None = None


def require_live_execution_off(env: dict[str, str] | None = None) -> None:
    source = env if env is not None else os.environ
    if source.get("ENABLE_LIVE_EXECUTION") != "false":
        raise RuntimeError("ENABLE_LIVE_EXECUTION musí být 'false'.")


def require_submit_guards(env: dict[str, str] | None = None) -> None:
    require_demo_environment(env)
    require_live_execution_off(env)


def parse_dry_run(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("--dry-run musí být true nebo false")


def t212_ticker(symbol: str, ticker: str | None = None) -> str:
    if ticker:
        return ticker
    if symbol.endswith("_US_EQ"):
        return symbol
    return f"{symbol}_US_EQ"


def market_buy_plan(ticker: str, quantity: float) -> dict:
    if quantity <= 0:
        raise SubmitError("quantity musí být kladná (jeden nákup).")
    return {
        "url": f"{DEMO_BASE_URL}{MARKET_PATH}",
        "body": {"ticker": ticker, "quantity": quantity, "extendedHours": False},
    }


def protective_stop_plan(ticker: str, quantity: float, stop_price: float) -> dict:
    if stop_price <= 0:
        raise SubmitError("stop_price musí být > 0.")
    return {
        "url": f"{DEMO_BASE_URL}{STOP_PATH}",
        "body": {
            "ticker": ticker,
            "quantity": -abs(quantity),
            "stopPrice": stop_price,
            "timeValidity": "GOOD_TILL_CANCEL",
        },
    }


def unprotected_stop_instructions(
    *,
    ticker: str,
    quantity: float,
    stop_price: float,
    fence_key: str,
) -> str:
    return (
        "CRITICAL: Nechráněná pozice po selhání ochranného stopu. NEPOKRAČUJ.\n"
        f"Ruční stop v T212 DEMO pro {ticker}:\n"
        f"  1. Otevři Trading 212 Practice / Demo.\n"
        f"  2. Najdi pozici {ticker}.\n"
        f"  3. SELL STOP, quantity {quantity}, stopPrice {stop_price}.\n"
        f"  4. Ověř, že příkaz STOP je aktivní.\n"
        f"  5. Fence {fence_key} je UNCERTAIN — po aktivním STOP na brokerovi "
        "exit nastaví CONFIRMED.\n"
        "Market se neopakuje. Stop zkusí exit (jiný fence STOP:…)."
    )


def default_can_trade(env: dict[str, str] | None = None) -> tuple[bool, str]:
    source = env if env is not None else os.environ
    url = (source.get("SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return False, "MISSING_CONTROL_PLANE"
    endpoint = url.rstrip("/") + "/rest/v1/rpc/can_trade"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        endpoint, data=b"{}", headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.read()
        return False, f"CONTROL_PLANE_UNREACHABLE:{exc.code}"
    except Exception:
        return False, "CONTROL_PLANE_UNREACHABLE"
    row = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(row, dict):
        return False, "CONTROL_PLANE_UNREACHABLE"
    return bool(row.get("allowed")), str(row.get("reason") or "UNKNOWN")


def post_once(
    url: str,
    headers: dict[str, str],
    body: dict,
    *,
    http_post: HttpPost,
) -> dict:
    if not url.startswith(DEMO_BASE_URL + "/"):
        raise RuntimeError("POST jen na https://demo.trading212.com")
    return http_post(url, headers, body)


def default_http_post(url: str, headers: dict[str, str], body: dict) -> dict:
    if not url.startswith(DEMO_BASE_URL):
        raise RuntimeError("HTTP mimo demo base URL je zakázané.")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    secret = headers.get("Authorization", "")
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SEC) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace").replace(secret, "[redacted]")
        raise SubmitError(f"T212 HTTP {exc.code} POST: {payload[:200]}") from None
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).lower()
        if "timed out" in reason or "timeout" in reason:
            raise TimeoutError("T212 POST timeout 15s") from exc
        raise SubmitError("T212 POST síťové selhání") from exc


RETRYABLE_STOP_STATUS = {400, 408, 429, 500, 502, 503, 504}
STOP_SETTLE_SEC = 3
STOP_ATTEMPTS = 3


def _http_status_from_error(exc: BaseException) -> int | None:
    text = str(exc)
    if "HTTP " not in text:
        return None
    try:
        return int(text.split("HTTP ", 1)[1].split()[0])
    except (IndexError, ValueError):
        return None


def retryable_stop_error(exc: BaseException) -> bool:
    """Timeout po odeslání neretřuj — příkaz mohl projít. 429/400/5xx ano."""
    if isinstance(exc, TimeoutError):
        return False
    status = _http_status_from_error(exc)
    return status in RETRYABLE_STOP_STATUS


def _order_id(payload: dict) -> str | None:
    value = payload.get("id", payload.get("order_id"))
    return None if value is None else str(value)


def readback_order(
    order_id: str,
    *,
    http_get: HttpGet,
    headers: dict[str, str],
    expect_type: str | None = None,
) -> bool:
    url = f"{DEMO_BASE_URL}{ORDERS_PATH}"
    payload = http_get(url, headers)
    rows = payload if isinstance(payload, list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("id")) != str(order_id):
            continue
        if expect_type and str(item.get("type") or "").upper() != expect_type:
            return False
        return True
    # Market buy může být hned FILLED a zmizet z pending — id z odpovědi stačí.
    return expect_type is None


def submit_order(
    *,
    symbol: str,
    quantity: float,
    stop_price: float,
    session_date: str,
    ticker: str | None = None,
    dry_run: bool = True,
    env: dict[str, str] | None = None,
    snapshot: dict | None = None,
    can_trade_fn: CanTradeFn | None = None,
    store: FenceStore | None = None,
    http_post: HttpPost | None = None,
    http_get: HttpGet | None = None,
    param_hash: str | None = None,
    settle_sec: float = STOP_SETTLE_SEC,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_attempts: int = STOP_ATTEMPTS,
) -> SubmitResult:
    require_submit_guards(env)
    if "," in symbol or " " in symbol.strip():
        raise SubmitError("Jeden symbol na běh. Žádné dávky.")

    ticker_id = t212_ticker(symbol, ticker)
    buy = market_buy_plan(ticker_id, quantity)
    stop = protective_stop_plan(ticker_id, quantity, stop_price)
    plan = {"market": buy, "stop": stop, "symbol": symbol}
    digest = param_hash or compute_param_hash()
    fence_key = make_fence_key(session_date, symbol, digest)

    allowed, reason = (can_trade_fn or (lambda: default_can_trade(env)))()
    if not allowed:
        return SubmitResult(
            outcome="STOP",
            exit_code=EXIT_STOP,
            dry_run=dry_run,
            fence_key=fence_key,
            reason=f"can_trade:{reason}",
            plan=plan,
        )

    blockers = global_blockers(snapshot or {})
    if blockers:
        return SubmitResult(
            outcome="STOP",
            exit_code=EXIT_STOP,
            dry_run=dry_run,
            fence_key=fence_key,
            reason=f"GLOBAL_BLOCKER:{blockers[0]}",
            plan=plan,
        )

    if dry_run:
        return SubmitResult(
            outcome="DRY_RUN",
            exit_code=EXIT_OK,
            dry_run=True,
            fence_key=fence_key,
            reason="dry_run",
            plan=plan,
        )

    source = env if env is not None else os.environ
    headers = auth_headers(*require_t212_credentials(source))
    poster = http_post or default_http_post
    getter = http_get
    fence_store = store or store_from_env(source)
    posts = {"n": 0}

    def send_buy() -> dict:
        if posts["n"] >= 1:
            raise SubmitError("Žádný retry market POST.")
        posts["n"] += 1
        return post_once(buy["url"], headers, buy["body"], http_post=poster)

    def buy_readback(payload: dict) -> bool:
        order_id = _order_id(payload)
        if not order_id:
            return False
        if getter is None:
            return True
        return readback_order(order_id, http_get=getter, headers=headers)

    fenced = run_fenced_send(
        session_date=session_date,
        symbol=symbol,
        param_hash=digest,
        store=fence_store,
        send_once=send_buy,
        readback=buy_readback,
    )
    if fenced.outcome == OUTCOME_SKIP:
        return SubmitResult(
            outcome=OUTCOME_SKIP,
            exit_code=EXIT_OK,
            dry_run=False,
            fence_key=fenced.fence_key,
            reason="fence_exists",
            plan=plan,
        )
    if fenced.outcome != STATE_CONFIRMED:
        return SubmitResult(
            outcome=STATE_UNCERTAIN,
            exit_code=EXIT_UNCERTAIN,
            dry_run=False,
            fence_key=fenced.fence_key,
            buy_order_id=fenced.order_id,
            reason=fenced.reason,
            plan=plan,
        )

    # run_fenced_send označí CONFIRMED po buy readback. Stop ještě chybí — vrať SENT.
    fence_store.set_state(fenced.fence_key, STATE_SENT, order_id=fenced.order_id)

    if settle_sec > 0:
        sleep_fn(settle_sec)
    stop_payload: dict | None = None
    last_error: BaseException | None = None
    for attempt in range(max(1, stop_attempts)):
        try:
            stop_payload = post_once(stop["url"], headers, stop["body"], http_post=poster)
            last_error = None
            break
        except TimeoutError as exc:
            last_error = exc
            break
        except Exception as exc:
            last_error = exc
            if (not retryable_stop_error(exc)) or attempt >= max(1, stop_attempts) - 1:
                break
            sleep_fn(2**attempt)
    if stop_payload is None:
        critical = unprotected_stop_instructions(
            ticker=ticker_id,
            quantity=quantity,
            stop_price=stop_price,
            fence_key=fenced.fence_key,
        )
        fence_store.set_state(fenced.fence_key, STATE_UNCERTAIN, order_id=fenced.order_id)
        return SubmitResult(
            outcome="UNPROTECTED",
            exit_code=EXIT_UNCERTAIN,
            dry_run=False,
            fence_key=fenced.fence_key,
            buy_order_id=fenced.order_id,
            reason="protective_stop_failed",
            plan=plan,
            critical=critical,
        )

    stop_id = _order_id(stop_payload)
    if getter is not None and stop_id:
        if not readback_order(stop_id, http_get=getter, headers=headers, expect_type="STOP"):
            critical = unprotected_stop_instructions(
                ticker=ticker_id,
                quantity=quantity,
                stop_price=stop_price,
                fence_key=fenced.fence_key,
            )
            fence_store.set_state(fenced.fence_key, STATE_UNCERTAIN, order_id=fenced.order_id)
            return SubmitResult(
                outcome="UNPROTECTED",
                exit_code=EXIT_UNCERTAIN,
                dry_run=False,
                fence_key=fenced.fence_key,
                buy_order_id=fenced.order_id,
                stop_order_id=stop_id,
                reason="stop_readback_failed",
                plan=plan,
                critical=critical,
            )

    fence_store.set_state(fenced.fence_key, STATE_CONFIRMED, order_id=fenced.order_id)
    return SubmitResult(
        outcome=STATE_CONFIRMED,
        exit_code=EXIT_OK,
        dry_run=False,
        fence_key=fenced.fence_key,
        buy_order_id=fenced.order_id,
        stop_order_id=stop_id,
        reason="confirmed",
        plan=plan,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jeden demo nákup + ochranný stop.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--stop-price", required=True, type=float)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--snapshot", default=None)
    args = parser.parse_args(argv)
    dry_run = parse_dry_run(args.dry_run)
    snapshot = None
    if args.snapshot:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    try:
        result = submit_order(
            symbol=args.symbol,
            quantity=args.quantity,
            stop_price=args.stop_price,
            session_date=args.session_date,
            ticker=args.ticker,
            dry_run=dry_run,
            snapshot=snapshot or {"positions": [], "active_orders": []},
            store=MemoryFenceStore() if dry_run else None,
        )
    except RuntimeError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return EXIT_STOP

    if result.dry_run:
        print("DRY-RUN: žádný POST")
        print(f"MARKET {result.plan['market']['url']}")
        print(json.dumps(result.plan["market"]["body"], indent=2))
        print(f"STOP {result.plan['stop']['url']}")
        print(json.dumps(result.plan["stop"]["body"], indent=2))
    if result.critical:
        print(result.critical, file=sys.stderr)
    extra = f" fence={result.fence_key}" if result.fence_key else ""
    print(f"OUTCOME: {result.outcome} reason={result.reason}{extra}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
