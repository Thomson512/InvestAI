"""Point-in-time signály. Vidí jen bary do asof dne včetně."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.param_hash import compute_param_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = REPO_ROOT / "config" / "strategy.v1.json"
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.v1.json"
DEFAULT_DATASET = REPO_ROOT / "data" / "dataset.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "signals.json"
REFERENCE_SYMBOL = "SPY"

DECISION_BUY = "BUY_CANDIDATE"
DECISION_REJECT = "REJECT"

BLOCK_HISTORY = "INSUFFICIENT_HISTORY"
BLOCK_SMA = "BELOW_SMA150"
BLOCK_BREAKOUT = "NO_BREAKOUT"
BLOCK_RS = "RS_EXCESS_BELOW_MIN"
BLOCK_BREADTH = "BREADTH_BELOW_MIN"
BLOCK_STOP = "INVALID_STOP"
BLOCK_SPY = "MISSING_SPY"
BLOCK_BARS = "MISSING_BARS"


class SignalError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def bar_date(bar: dict) -> str:
    return str(bar["t"])[:10]


def visible_asof(bars: list[dict], asof_date: str) -> list[dict]:
    """Výhradně seance <= asof_date. Žádný pohled dopředu."""
    return [bar for bar in bars if bar_date(bar) <= asof_date]


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def atr_wilder(bars: list[dict], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i]["h"])
        low = float(bars[i]["l"])
        prev_close = float(bars[i - 1]["c"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return None
    atr = sum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def period_return(bars: list[dict], lookback: int) -> float | None:
    if len(bars) < lookback + 1:
        return None
    start = float(bars[-(lookback + 1)]["c"])
    end = float(bars[-1]["c"])
    if start <= 0:
        return None
    return end / start - 1.0


def prior_breakout_high(bars: list[dict], lookback: int) -> float | None:
    if len(bars) < lookback + 1:
        return None
    prior = bars[-(lookback + 1) : -1]
    return max(float(bar["h"]) for bar in prior)


def asof_date_from_spy(spy_bars: list[dict], asof_index: int) -> str:
    if not spy_bars:
        raise SignalError("Chybí SPY řada.")
    if asof_index < 0:
        asof_index = len(spy_bars) + asof_index
    if asof_index < 0 or asof_index >= len(spy_bars):
        raise SignalError(f"asof_index {asof_index} mimo rozsah SPY ({len(spy_bars)}).")
    return bar_date(spy_bars[asof_index])


def _empty_signal(symbol: str, blockers: list[str]) -> dict:
    return {
        "symbol": symbol,
        "decision": DECISION_REJECT,
        "tier": None,
        "score": None,
        "blockers": blockers,
        "entry_price": None,
        "stop_price": None,
        "target_price": None,
        "atr_14": None,
        "rs_excess_pct": None,
        "breakout_strength": None,
    }


def evaluate_symbol(
    symbol: str,
    visible: list[dict],
    spy_visible: list[dict],
    params: dict,
) -> dict:
    """Hodnotí jeden symbol jen z visible (už oříznuté na asof)."""
    if not spy_visible:
        return _empty_signal(symbol, [BLOCK_SPY])
    if not visible:
        return _empty_signal(symbol, [BLOCK_BARS])

    need = max(
        int(params["trend_filter_sma"]),
        int(params["breakout_lookback_days"]) + 1,
        int(params["relative_strength_lookback_days"]) + 1,
        int(params["atr_period"]) + 1,
    )
    blockers: list[str] = []
    if len(visible) < need or len(spy_visible) < int(params["relative_strength_lookback_days"]) + 1:
        return _empty_signal(symbol, [BLOCK_HISTORY])

    close = float(visible[-1]["c"])
    closes = [float(bar["c"]) for bar in visible]
    sma150 = sma(closes, int(params["trend_filter_sma"]))
    atr14 = atr_wilder(visible, int(params["atr_period"]))
    prior_high = prior_breakout_high(visible, int(params["breakout_lookback_days"]))
    stock_ret = period_return(visible, int(params["relative_strength_lookback_days"]))
    spy_ret = period_return(spy_visible, int(params["relative_strength_lookback_days"]))

    rs_excess_pct = None
    if stock_ret is not None and spy_ret is not None:
        rs_excess_pct = (stock_ret - spy_ret) * 100.0

    breakout_strength = None
    if prior_high is not None and prior_high > 0:
        breakout_strength = (close / prior_high - 1.0) * 100.0

    if sma150 is None or close <= sma150:
        blockers.append(BLOCK_SMA)
    if prior_high is None or close <= prior_high:
        blockers.append(BLOCK_BREAKOUT)
    if rs_excess_pct is None or rs_excess_pct < float(params["min_rs_excess_pct"]):
        blockers.append(BLOCK_RS)

    entry = close
    stop = None
    target = None
    if atr14 is not None and atr14 > 0:
        stop = entry - float(params["stop_atr_multiple"]) * atr14
        risk = entry - stop
        if risk <= 0:
            blockers.append(BLOCK_STOP)
            stop = None
            target = None
        else:
            target = entry + float(params["reward_to_risk"]) * risk
    else:
        blockers.append(BLOCK_STOP)

    score = None
    if rs_excess_pct is not None and breakout_strength is not None:
        trend_excess = 0.0
        if sma150 and sma150 > 0:
            trend_excess = max(0.0, (close / sma150 - 1.0) * 100.0)
        score = rs_excess_pct + breakout_strength + trend_excess

    decision = DECISION_BUY if not blockers else DECISION_REJECT
    tier = None
    if decision == DECISION_BUY and rs_excess_pct is not None:
        tier = "A" if rs_excess_pct >= 20.0 else "B"

    return {
        "symbol": symbol,
        "decision": decision,
        "tier": tier,
        "score": _round(score),
        "blockers": blockers,
        "entry_price": _round(entry),
        "stop_price": _round(stop),
        "target_price": _round(target),
        "atr_14": _round(atr14),
        "rs_excess_pct": _round(rs_excess_pct),
        "breakout_strength": _round(breakout_strength),
    }


def market_breadth_pct(
    bars_by_symbol: dict[str, list[dict]],
    symbols: list[str],
    asof_date: str,
    sma_period: int,
) -> float | None:
    scored = 0
    above = 0
    for symbol in symbols:
        visible = visible_asof(bars_by_symbol.get(symbol, []), asof_date)
        closes = [float(bar["c"]) for bar in visible]
        value = sma(closes, sma_period)
        if value is None:
            continue
        scored += 1
        if closes[-1] > value:
            above += 1
    if scored == 0:
        return None
    return 100.0 * above / scored


def evaluate_signals(
    bars_by_symbol: dict[str, list[dict]],
    asof_index: int,
    params: dict,
    symbols: list[str] | None = None,
) -> dict:
    spy = bars_by_symbol.get(REFERENCE_SYMBOL) or []
    asof_date = asof_date_from_spy(spy, asof_index)
    spy_visible = visible_asof(spy, asof_date)
    names = list(symbols) if symbols is not None else sorted(
        name for name in bars_by_symbol if name != REFERENCE_SYMBOL
    )

    raw = []
    for symbol in names:
        visible = visible_asof(bars_by_symbol.get(symbol, []), asof_date)
        raw.append(evaluate_symbol(symbol, visible, spy_visible, params))

    breadth = market_breadth_pct(
        bars_by_symbol, names, asof_date, int(params["trend_filter_sma"])
    )
    min_breadth = float(params["min_market_breadth_pct"])
    breadth_ok = breadth is not None and breadth >= min_breadth

    signals = []
    for signal in raw:
        if not breadth_ok and signal["decision"] == DECISION_BUY:
            signal = {
                **signal,
                "decision": DECISION_REJECT,
                "tier": None,
                "blockers": [*signal["blockers"], BLOCK_BREADTH],
            }
        elif not breadth_ok and BLOCK_BREADTH not in signal["blockers"]:
            signal = {**signal, "blockers": [*signal["blockers"], BLOCK_BREADTH]}
        signals.append(signal)

    return {
        "asof_session": asof_date,
        "asof_index": asof_index if asof_index >= 0 else len(spy) + asof_index,
        "market_breadth_pct": _round(breadth, 2),
        "breadth_gate": "PASS" if breadth_ok else "FAIL",
        "candidate_count": sum(1 for item in signals if item["decision"] == DECISION_BUY),
        "signals": signals,
    }


def evaluate_dataset(
    dataset: dict,
    params: dict,
    *,
    asof_index: int = -1,
    symbols: list[str] | None = None,
    param_hash: str | None = None,
) -> dict:
    result = evaluate_signals(dataset["bars"], asof_index, params, symbols=symbols)
    result["generated_from"] = {
        "universe_hash": dataset.get("universe_hash"),
        "source_dataset_sha256": dataset.get("source_dataset_sha256"),
        "param_hash": param_hash,
        "latest_session": dataset.get("latest_session"),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Point-in-time breakout + RS signály.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--strategy", type=Path, default=STRATEGY_PATH)
    parser.add_argument("--universe", type=Path, default=UNIVERSE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--index", type=int, default=-1, help="Index dne v SPY řadě (záporný od konce).")
    args = parser.parse_args(argv)

    if not args.dataset.is_file():
        print(f"Chybí dataset: {args.dataset}", file=sys.stderr)
        return 1

    strategy = load_json(args.strategy)
    params = strategy["parameters"]
    dataset = load_json(args.dataset)
    symbols = None
    if args.universe.is_file():
        symbols = list(load_json(args.universe)["symbols"])

    document = evaluate_dataset(
        dataset,
        params,
        asof_index=args.index,
        symbols=symbols,
        param_hash=compute_param_hash(args.strategy),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"OK signals asof={document['asof_session']} "
        f"breadth={document['market_breadth_pct']} "
        f"gate={document['breadth_gate']} "
        f"candidates={document['candidate_count']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
