"""Forward shadow: virtuální portfolio s live CZK stropy, ne % equity."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.evaluate_signals import DECISION_BUY, bar_date, evaluate_signals
from scripts.param_hash import compute_param_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = REPO_ROOT / "config" / "strategy.v1.json"
COSTS_PATH = REPO_ROOT / "config" / "costs.v1.json"
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.v1.json"
DEFAULT_DATASET = REPO_ROOT / "data" / "dataset.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "report.json"
REFERENCE_SYMBOL = "SPY"
BPS = 10_000.0


class ShadowError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def require_live_sizing(params: dict) -> dict:
    """Stropy musí být přesně ty ze strategy.v1 — žádné % equity."""
    required = {
        "max_risk_per_trade_czk": 500,
        "max_position_value_czk": 8000,
        "max_open_positions": 8,
    }
    for key, expected in required.items():
        got = params.get(key)
        if got != expected:
            raise ShadowError(f"{key} musí být {expected} (live), je {got}.")
    return {
        **required,
        "max_new_entries_per_session": int(params["max_new_entries_per_session"]),
    }


def reporting_capital_czk(sizing: dict) -> float:
    """Jen pro equity křivku / DD. Nikdy pro výpočet shares."""
    return float(sizing["max_open_positions"] * sizing["max_position_value_czk"])


def fx_usdczk(session: str, fx_cfg: dict) -> float:
    origin = date.fromisoformat(fx_cfg["origin"])
    current = date.fromisoformat(session)
    days = (current - origin).days
    drift = 1.0 + (float(fx_cfg["daily_drift_bps"]) / BPS) * days
    amp = float(fx_cfg["wobble_amp"])
    period = max(float(fx_cfg["wobble_period_days"]), 1.0)
    wobble = 1.0 + amp * math.sin(days / period)
    return float(fx_cfg["base_rate"]) * drift * wobble


def size_shares(*, entry_usd: float, stop_usd: float, fx_rate: float, sizing: dict) -> int:
    """Fixní CZK risk/notional. Žádný argument equity."""
    risk_usd = entry_usd - stop_usd
    if entry_usd <= 0 or risk_usd <= 0 or fx_rate <= 0:
        return 0
    by_risk = sizing["max_risk_per_trade_czk"] / (risk_usd * fx_rate)
    by_value = sizing["max_position_value_czk"] / (entry_usd * fx_rate)
    return int(min(by_risk, by_value))


def apply_buy_slip(price: float, slippage_bps: float) -> float:
    return price * (1.0 + slippage_bps / BPS)


def apply_sell_slip(price: float, slippage_bps: float) -> float:
    return price * (1.0 - slippage_bps / BPS)


def apply_fx_buy(rate: float, fx_fee_bps: float) -> float:
    return rate * (1.0 + fx_fee_bps / BPS)


def apply_fx_sell(rate: float, fx_fee_bps: float) -> float:
    return rate * (1.0 - fx_fee_bps / BPS)


def bar_on(bars: list[dict], session: str) -> dict | None:
    for bar in bars:
        if bar_date(bar) == session:
            return bar
    return None


def paper_gate(*, calendar_days: int, completed_trades: int, min_days: int, min_trades: int) -> dict:
    authorized = calendar_days >= min_days and completed_trades >= min_trades
    return {
        "min_days": min_days,
        "min_trades": min_trades,
        "days": calendar_days,
        "trades": completed_trades,
        "promotion_authorized": authorized,
    }


@dataclass
class Position:
    symbol: str
    shares: int
    entry_session: str
    planned_entry: float
    entry_fill: float
    stop_price: float
    target_price: float
    fx_entry_mid: float
    fx_entry_fill: float
    commission_bps: float
    slippage_bps: float
    fx_fee_bps: float
    costs_entry_czk: float


@dataclass
class Book:
    cash_czk: float
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def _entry_cost_czk(shares: int, fill_usd: float, fx_fill: float, commission_bps: float) -> float:
    notional = shares * fill_usd * fx_fill
    commission = notional * commission_bps / BPS
    return notional + commission


def _exit_proceeds_czk(shares: int, fill_usd: float, fx_fill: float, commission_bps: float) -> float:
    notional = shares * fill_usd * fx_fill
    commission = notional * commission_bps / BPS
    return notional - commission


def _close_position(
    book: Book,
    pos: Position,
    *,
    exit_session: str,
    raw_exit: float,
    reason: str,
    fx_mid: float,
    costs: dict,
) -> None:
    exit_fill = apply_sell_slip(raw_exit, pos.slippage_bps)
    fx_out = apply_fx_sell(fx_mid, pos.fx_fee_bps)
    proceeds = _exit_proceeds_czk(pos.shares, exit_fill, fx_out, pos.commission_bps)
    book.cash_czk += proceeds
    planned_risk_czk = pos.shares * (pos.planned_entry - pos.stop_price) * pos.fx_entry_mid
    pnl = proceeds - pos.costs_entry_czk
    r_multiple = pnl / planned_risk_czk if planned_risk_czk > 0 else None
    slip_entry = pos.shares * (pos.entry_fill - pos.planned_entry) * pos.fx_entry_mid
    slip_exit = pos.shares * (raw_exit - exit_fill) * fx_mid
    fx_fee = (
        pos.shares * pos.entry_fill * (pos.fx_entry_fill - pos.fx_entry_mid)
        + pos.shares * exit_fill * (fx_mid - fx_out)
    )
    fx_drift = pos.shares * exit_fill * (fx_mid - pos.fx_entry_mid)
    commission = (
        pos.shares * pos.entry_fill * pos.fx_entry_fill * pos.commission_bps / BPS
        + pos.shares * exit_fill * fx_out * pos.commission_bps / BPS
    )
    book.trades.append(
        {
            "symbol": pos.symbol,
            "entry_session": pos.entry_session,
            "exit_session": exit_session,
            "entry_price": _round(pos.entry_fill),
            "exit_price": _round(exit_fill),
            "stop_price": _round(pos.stop_price),
            "target_price": _round(pos.target_price),
            "shares": pos.shares,
            "exit_reason": reason,
            "r_multiple": _round(r_multiple),
            "realized_pnl_czk": _round(pnl, 2),
            "fx_entry": _round(pos.fx_entry_mid, 6),
            "fx_exit": _round(fx_mid, 6),
            "costs": {
                "commission_czk": _round(commission, 2),
                "slippage_czk": _round(slip_entry + slip_exit, 2),
                "fx_fee_czk": _round(fx_fee, 2),
                "fx_drift_czk": _round(fx_drift, 2),
            },
        }
    )
    del book.positions[pos.symbol]


def _manage_open(book: Book, bars_by_symbol: dict[str, list[dict]], session: str, fx_cfg: dict) -> None:
    fx_mid = fx_usdczk(session, fx_cfg)
    for symbol in list(book.positions):
        pos = book.positions[symbol]
        bar = bar_on(bars_by_symbol.get(symbol, []), session)
        if bar is None:
            continue
        raw_open = float(bar["o"])
        raw_low = float(bar["l"])
        raw_high = float(bar["h"])
        if raw_open <= pos.stop_price:
            _close_position(
                book, pos, exit_session=session, raw_exit=raw_open, reason="STOP", fx_mid=fx_mid, costs={}
            )
            continue
        if raw_open >= pos.target_price:
            _close_position(
                book, pos, exit_session=session, raw_exit=raw_open, reason="TARGET", fx_mid=fx_mid, costs={}
            )
            continue
        hit_stop = raw_low <= pos.stop_price
        hit_target = raw_high >= pos.target_price
        if hit_stop:
            # stejný bar stop+target → STOP (pesimisticky, bez look-ahead výhody)
            _close_position(
                book, pos, exit_session=session, raw_exit=pos.stop_price, reason="STOP", fx_mid=fx_mid, costs={}
            )
        elif hit_target:
            _close_position(
                book, pos, exit_session=session, raw_exit=pos.target_price, reason="TARGET", fx_mid=fx_mid, costs={}
            )


def _mark_equity(book: Book, bars_by_symbol: dict[str, list[dict]], session: str, fx_cfg: dict) -> float:
    fx_mid = fx_usdczk(session, fx_cfg)
    marked = book.cash_czk
    for pos in book.positions.values():
        bar = bar_on(bars_by_symbol.get(pos.symbol, []), session)
        price = float(bar["c"]) if bar else pos.entry_fill
        marked += pos.shares * price * fx_mid
    book.equity_curve.append(marked)
    return marked


def _select_candidates(signals: list[dict], book: Book, sizing: dict) -> list[dict]:
    free_slots = sizing["max_open_positions"] - len(book.positions)
    if free_slots <= 0:
        return []
    ranked = [
        item
        for item in signals
        if item["decision"] == DECISION_BUY
        and item["symbol"] not in book.positions
        and item["entry_price"]
        and item["stop_price"]
        and item["target_price"]
    ]
    ranked.sort(
        key=lambda item: (
            0 if item["tier"] == "A" else 1,
            -(item["score"] if item["score"] is not None else -1e9),
            item["symbol"],
        )
    )
    limit = min(int(sizing["max_new_entries_per_session"]), free_slots)
    return ranked[:limit]


def _fill_pending(
    book: Book,
    pending: list[dict],
    bars_by_symbol: dict[str, list[dict]],
    session: str,
    fx_cfg: dict,
    sizing: dict,
    costs: dict,
) -> None:
    fx_mid = fx_usdczk(session, fx_cfg)
    slip = float(costs["slippage_bps"])
    fx_fee = float(costs["fx_fee_bps"])
    commission = float(costs["commission_bps"])
    for signal in pending:
        if len(book.positions) >= sizing["max_open_positions"]:
            break
        symbol = signal["symbol"]
        if symbol in book.positions:
            continue
        bar = bar_on(bars_by_symbol.get(symbol, []), session)
        if bar is None:
            continue
        planned_entry = float(signal["entry_price"])
        stop = float(signal["stop_price"])
        target = float(signal["target_price"])
        shares = size_shares(entry_usd=planned_entry, stop_usd=stop, fx_rate=fx_mid, sizing=sizing)
        if shares < 1:
            continue
        fill = apply_buy_slip(float(bar["o"]), slip)
        fx_in = apply_fx_buy(fx_mid, fx_fee)
        cost = _entry_cost_czk(shares, fill, fx_in, commission)
        if cost > book.cash_czk:
            continue
        book.cash_czk -= cost
        book.positions[symbol] = Position(
            symbol=symbol,
            shares=shares,
            entry_session=session,
            planned_entry=planned_entry,
            entry_fill=fill,
            stop_price=stop,
            target_price=target,
            fx_entry_mid=fx_mid,
            fx_entry_fill=fx_in,
            commission_bps=commission,
            slippage_bps=slip,
            fx_fee_bps=fx_fee,
            costs_entry_czk=cost,
        )


def _summarize(book: Book, start_equity: float) -> dict:
    completed = book.trades
    winners = [t for t in completed if (t["realized_pnl_czk"] or 0) > 0]
    losers = [t for t in completed if (t["realized_pnl_czk"] or 0) <= 0]
    realized = sum(t["realized_pnl_czk"] or 0 for t in completed)
    marked = (book.equity_curve[-1] - start_equity) if book.equity_curve else 0.0
    peak = start_equity
    max_dd = 0.0
    for equity in book.equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    rs = [t["r_multiple"] for t in completed if t["r_multiple"] is not None]
    gross_win = sum(t["realized_pnl_czk"] or 0 for t in winners)
    gross_loss = abs(sum(t["realized_pnl_czk"] or 0 for t in losers))
    profit_factor = None
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = None
    return {
        "completed_trades": len(completed),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate_pct": _round(100.0 * len(winners) / len(completed), 2) if completed else None,
        "realized_pnl": _round(realized, 2),
        "marked_pnl": _round(marked, 2),
        "max_drawdown_pct": _round(max_dd, 2),
        "expectancy": _round(sum(rs) / len(rs), 4) if rs else None,
        "profit_factor": _round(profit_factor, 4) if profit_factor is not None else None,
        "open_positions": len(book.positions),
        "trades": completed,
    }


def run_scenario(
    bars_by_symbol: dict[str, list[dict]],
    params: dict,
    *,
    sizing: dict,
    costs: dict,
    fx_cfg: dict,
    symbols: list[str],
    start_index: int | None = None,
) -> dict:
    spy = bars_by_symbol.get(REFERENCE_SYMBOL) or []
    if len(spy) < int(params["trend_filter_sma"]):
        raise ShadowError("Málo SPY seancí pro shadow.")
    first = start_index if start_index is not None else int(params["trend_filter_sma"]) - 1
    start_equity = reporting_capital_czk(sizing)
    book = Book(cash_czk=start_equity)
    pending: list[dict] = []

    for idx in range(first, len(spy)):
        session = bar_date(spy[idx])
        _fill_pending(book, pending, bars_by_symbol, session, fx_cfg, sizing, costs)
        pending = []
        _manage_open(book, bars_by_symbol, session, fx_cfg)
        snapshot = evaluate_signals(bars_by_symbol, idx, params, symbols=symbols)
        pending = _select_candidates(snapshot["signals"], book, sizing)
        _mark_equity(book, bars_by_symbol, session, fx_cfg)

    return _summarize(book, start_equity)


def run_shadow(
    dataset: dict,
    params: dict,
    costs_cfg: dict,
    *,
    symbols: list[str] | None = None,
    param_hash: str | None = None,
) -> dict:
    sizing = require_live_sizing(params)
    bars = dataset["bars"]
    names = symbols if symbols is not None else sorted(s for s in bars if s != REFERENCE_SYMBOL)
    spy = bars[REFERENCE_SYMBOL]
    first_session = bar_date(spy[int(params["trend_filter_sma"]) - 1])
    last_session = bar_date(spy[-1])
    calendar_days = (date.fromisoformat(last_session) - date.fromisoformat(first_session)).days

    scenarios = {}
    for name, cost in costs_cfg["scenarios"].items():
        scenarios[name] = run_scenario(
            bars,
            params,
            sizing=sizing,
            costs=cost,
            fx_cfg=costs_cfg["fx"],
            symbols=names,
        )

    gate = paper_gate(
        calendar_days=calendar_days,
        completed_trades=scenarios["baseline"]["completed_trades"],
        min_days=int(costs_cfg["paper_gate"]["min_days"]),
        min_trades=int(costs_cfg["paper_gate"]["min_trades"]),
    )
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe_hash": dataset.get("universe_hash"),
        "source_dataset_sha256": dataset.get("source_dataset_sha256"),
        "param_hash": param_hash,
        "sizing": {
            **sizing,
            "method": "fixed_czk",
            "reporting_capital_czk": reporting_capital_czk(sizing),
            "note": "Shares z max_risk_per_trade_czk a max_position_value_czk. Ne z procent virtuální equity.",
        },
        "paper_gate": gate,
        "first_session": first_session,
        "last_session": last_session,
        "scenarios": scenarios,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward shadow s live CZK stropy.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--strategy", type=Path, default=STRATEGY_PATH)
    parser.add_argument("--costs", type=Path, default=COSTS_PATH)
    parser.add_argument("--universe", type=Path, default=UNIVERSE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    if not args.dataset.is_file():
        print(f"Chybí dataset: {args.dataset}", file=sys.stderr)
        return 1

    strategy = load_json(args.strategy)
    document = run_shadow(
        load_json(args.dataset),
        strategy["parameters"],
        load_json(args.costs),
        symbols=list(load_json(args.universe)["symbols"]) if args.universe.is_file() else None,
        param_hash=compute_param_hash(args.strategy),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    base = document["scenarios"]["baseline"]
    print(
        f"SHADOW: {base['completed_trades']} trades, {base['open_positions']} open, "
        f"PnL {base['realized_pnl']} CZK | gate={document['paper_gate']['promotion_authorized']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
