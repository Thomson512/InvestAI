"""Ochranný SELL STOP pro jeden symbol. Dry-run default. Žádný can_trade()."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.broker_snapshot import is_protective_sell, normalize_ticker, require_t212_credentials
from scripts.evaluate_signals import atr_wilder
from scripts.exit_orchestrator import default_can_close_position
from scripts.fence import (
    EXIT_OK,
    EXIT_UNCERTAIN,
    MemoryFenceStore,
    OUTCOME_SKIP,
    STATE_CONFIRMED,
    STATE_UNCERTAIN,
    FenceStore,
    run_fenced_send,
    store_from_env,
)
from scripts.param_hash import compute_param_hash
from scripts.session_gate import nyse_session_date
from scripts.shadow_summary import write_step_summary
from scripts.submit_order import (
    EXIT_STOP,
    HttpGet,
    HttpPost,
    SubmitError,
    _order_id,
    auth_headers,
    default_http_post,
    parse_dry_run,
    post_once,
    protective_stop_plan,
    readback_order,
    require_submit_guards,
    t212_ticker,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = REPO_ROOT / "config" / "strategy.v1.json"
GUARD_CONFIG_PATH = REPO_ROOT / "config" / "protective_stop.v1.json"
DEFAULT_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"
DEFAULT_DATASET = REPO_ROOT / "data" / "dataset.json"

CanCloseFn = Callable[[], tuple[bool, str]]


class GuardError(RuntimeError):
    pass


@dataclass
class GuardResult:
    outcome: str
    exit_code: int
    dry_run: bool
    extra_summary: list[str] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    fence_key: str | None = None
    stop_order_id: str | None = None


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require_one_symbol(symbol: str) -> str:
    value = symbol.strip().upper()
    if not value or "," in value or " " in value:
        raise GuardError("Jeden symbol na běh. Žádné dávky.")
    return normalize_ticker(value)


def find_position(snapshot: dict, symbol: str) -> dict | None:
    want = normalize_ticker(symbol)
    for row in snapshot.get("positions") or []:
        if float(row.get("quantity") or 0) == 0:
            continue
        if normalize_ticker(str(row.get("ticker") or "")) == want:
            return row
    return None


def position_has_stop(snapshot: dict, symbol: str) -> bool:
    ticker = t212_ticker(symbol)
    for order in snapshot.get("active_orders") or []:
        if is_protective_sell(order, ticker):
            return True
    return False


def atr_stop_from_bars(bars: list[dict], *, period: int, multiple: float) -> float | None:
    atr = atr_wilder(bars, period)
    if atr is None or atr <= 0 or not bars:
        return None
    last = float(bars[-1]["c"])
    stop = last - multiple * atr
    if stop <= 0 or stop >= last:
        return None
    return stop


def fallback_stop(last_price: float, fallback_pct: float) -> float:
    if last_price <= 0 or fallback_pct <= 0 or fallback_pct >= 100:
        raise GuardError("Neplatný fallback stop.")
    stop = last_price * (1.0 - fallback_pct / 100.0)
    if stop <= 0 or stop >= last_price:
        raise GuardError("Fallback stop musí být pod aktuální cenou.")
    return stop


def choose_stop(
    *,
    last_price: float,
    bars: list[dict] | None,
    atr_period: int,
    atr_multiple: float,
    fallback_pct: float,
) -> tuple[float, str]:
    atr_stop = atr_stop_from_bars(bars or [], period=atr_period, multiple=atr_multiple)
    if atr_stop is not None and atr_stop < last_price:
        return atr_stop, "atr"
    return fallback_stop(last_price, fallback_pct), "fallback_pct"


def last_price_of(position: dict) -> float:
    price = position.get("current_price")
    if price is None:
        raise GuardError("Pozice nemá current_price.")
    value = float(price)
    if value <= 0:
        raise GuardError("current_price musí být > 0.")
    return value


def first_line(result: GuardResult) -> str:
    return f"STOP-GUARD: {result.outcome}"


def run_guard(
    *,
    symbol: str,
    session_date: str,
    snapshot: dict,
    dry_run: bool = True,
    strategy: dict | None = None,
    config: dict | None = None,
    bars: list[dict] | None = None,
    env: dict[str, str] | None = None,
    can_close_fn: CanCloseFn | None = None,
    store: FenceStore | None = None,
    http_post: HttpPost | None = None,
    http_get: HttpGet | None = None,
) -> GuardResult:
    require_submit_guards(env)
    symbol = require_one_symbol(symbol)
    params = (strategy or load_json(STRATEGY_PATH))["parameters"]
    guard_cfg = config or load_json(GUARD_CONFIG_PATH)
    position = find_position(snapshot, symbol)
    if position is None:
        return GuardResult(outcome=f"NO_POSITION {symbol}", exit_code=EXIT_STOP, dry_run=dry_run)
    if position_has_stop(snapshot, symbol):
        return GuardResult(outcome=f"NO-OP HAS_STOP {symbol}", exit_code=EXIT_OK, dry_run=dry_run)

    last = last_price_of(position)
    stop_price, method = choose_stop(
        last_price=last,
        bars=bars,
        atr_period=int(params["atr_period"]),
        atr_multiple=float(params["stop_atr_multiple"]),
        fallback_pct=float(guard_cfg["fallback_stop_pct"]),
    )
    quantity = abs(float(position["quantity"]))
    ticker = t212_ticker(symbol)
    plan = protective_stop_plan(ticker, quantity, stop_price)
    plan["method"] = method
    plan["last_price"] = last

    if dry_run:
        return GuardResult(
            outcome=f"DRY_RUN {symbol} stop={stop_price:g} via={method}",
            exit_code=EXIT_OK,
            dry_run=True,
            plan=plan,
        )

    allowed, reason = (can_close_fn or default_can_close_position)()
    if not allowed:
        return GuardResult(
            outcome=f"CLOSE_BLOCKED:{reason}",
            exit_code=EXIT_STOP,
            dry_run=False,
            plan=plan,
        )

    digest = f"STOP:{compute_param_hash()}"
    source = env if env is not None else os.environ
    headers = auth_headers(*require_t212_credentials(source))
    poster = http_post or default_http_post
    posts = {"n": 0}

    def send_stop() -> dict:
        if posts["n"] >= 1:
            raise SubmitError("Žádný retry stop POST.")
        posts["n"] += 1
        return post_once(plan["url"], headers, plan["body"], http_post=poster)

    def stop_readback(payload: dict) -> bool:
        order_id = _order_id(payload)
        if not order_id:
            return False
        if http_get is None:
            return True
        return readback_order(order_id, http_get=http_get, headers=headers, expect_type="STOP")

    fence_store = store or store_from_env(source)
    fenced = run_fenced_send(
        session_date=session_date,
        symbol=symbol,
        param_hash=digest,
        store=fence_store,
        send_once=send_stop,
        readback=stop_readback,
    )
    if fenced.outcome == OUTCOME_SKIP:
        return GuardResult(
            outcome=f"FENCE_EXISTS {symbol}",
            exit_code=EXIT_OK,
            dry_run=False,
            plan=plan,
            fence_key=fenced.fence_key,
        )
    if fenced.outcome != STATE_CONFIRMED:
        return GuardResult(
            outcome=f"UNCERTAIN {symbol} {fenced.reason}",
            exit_code=fenced.exit_code or EXIT_UNCERTAIN,
            dry_run=False,
            plan=plan,
            fence_key=fenced.fence_key,
            stop_order_id=fenced.order_id,
        )
    return GuardResult(
        outcome=f"STOP_PLACED {symbol} id={fenced.order_id}",
        exit_code=EXIT_OK,
        dry_run=False,
        plan=plan,
        fence_key=fenced.fence_key,
        stop_order_id=fenced.order_id,
    )


def bars_for_symbol(dataset: dict | None, symbol: str) -> list[dict] | None:
    if not dataset:
        return None
    bars = (dataset.get("bars") or {}).get(symbol)
    return list(bars) if isinstance(bars, list) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Protective SELL STOP pro jeden symbol.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--dry-run", default="true")
    parser.add_argument("--session", default=None)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args(argv)
    dry_run = parse_dry_run(args.dry_run)
    session = args.session or nyse_session_date().isoformat()
    try:
        snapshot = load_json(args.snapshot)
        dataset = load_json(args.dataset) if args.dataset.is_file() else None
        symbol = require_one_symbol(args.symbol)
        result = run_guard(
            symbol=symbol,
            session_date=session,
            snapshot=snapshot,
            dry_run=dry_run,
            bars=bars_for_symbol(dataset, symbol),
        )
    except RuntimeError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        write_step_summary(f"STOP-GUARD: FAIL-CLOSED {exc}")
        return EXIT_STOP

    extra = []
    if result.dry_run and result.plan:
        extra.append("DRY-RUN: žádný POST")
        extra.append(json.dumps(result.plan.get("body") or result.plan, indent=2))
    write_step_summary(first_line(result), extra or None)
    print(first_line(result))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
