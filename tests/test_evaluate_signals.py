from __future__ import annotations

from scripts.evaluate_signals import (
    BLOCK_BREADTH,
    BLOCK_BREAKOUT,
    DECISION_BUY,
    DECISION_REJECT,
    evaluate_signals,
    evaluate_symbol,
    visible_asof,
)

PARAMS = {
    "breakout_lookback_days": 30,
    "trend_filter_sma": 150,
    "relative_strength_lookback_days": 63,
    "min_rs_excess_pct": 10,
    "atr_period": 14,
    "stop_atr_multiple": 2.0,
    "reward_to_risk": 4.5,
    "min_market_breadth_pct": 50,
}


def _dated_series(closes: list[float], high_extra: float = 0.4) -> list[dict]:
    bars = []
    for i, close in enumerate(closes):
        year = 2024 + (i // 250)
        day = i % 250
        month = (day // 20) + 1
        dom = (day % 20) + 1
        bars.append(
            {
                "t": f"{year}-{month:02d}-{dom:02d}T00:00:00Z",
                "o": close,
                "h": close + high_extra,
                "l": max(0.01, close - high_extra),
                "c": close,
                "v": 1000,
            }
        )
    return bars


def _ramp(start: float, n: int, step: float) -> list[float]:
    return [start + i * step for i in range(n)]


def _strong_breakout(n: int = 180) -> list[dict]:
    closes = _ramp(80.0, n - 1, 0.15)
    prior_highs = [close + 0.4 for close in closes[-30:]]
    breakout_close = max(prior_highs) + 2.0
    closes.append(breakout_close)
    return _dated_series(closes, high_extra=0.4)


def _weak_down(n: int = 180) -> list[dict]:
    return _dated_series(_ramp(120.0, n, -0.2), high_extra=0.3)


def _spy_mild(n: int = 180) -> list[dict]:
    return _dated_series(_ramp(400.0, n, 0.02), high_extra=0.5)


def test_look_ahead_full_series_matches_truncated() -> None:
    n_asof = 170
    future = 25
    strong = _strong_breakout(n_asof)
    crash = [float(strong[-1]["c"]) * (0.7 ** (i + 1)) for i in range(future)]
    future_bars = []
    price = crash[0]
    for i, close in enumerate(crash):
        future_bars.append(
            {
                "t": f"2026-06-{i + 1:02d}T00:00:00Z",
                "o": price,
                "h": close + 0.4,
                "l": max(0.01, close - 0.4),
                "c": close,
                "v": 1000,
            }
        )
        price = close
    strong_full = strong + future_bars
    weak = _weak_down(n_asof + future)
    spy_future = _spy_mild(n_asof) + [
        {
            "t": f"2026-06-{i + 1:02d}T00:00:00Z",
            "o": 410 + i,
            "h": 420 + i,
            "l": 400 + i,
            "c": 415 + i * 3,
            "v": 1000,
        }
        for i in range(future)
    ]

    full = {"SPY": spy_future, "AAA": strong_full, "BBB": weak}
    asof_index = n_asof - 1

    truncated = {
        symbol: visible_asof(series, spy_future[asof_index]["t"][:10])
        for symbol, series in full.items()
    }
    assert len(truncated["AAA"]) == n_asof
    assert len(full["AAA"]) > n_asof

    from_full = evaluate_signals(full, asof_index, PARAMS, symbols=["AAA", "BBB"])
    from_cut = evaluate_signals(truncated, -1, PARAMS, symbols=["AAA", "BBB"])

    assert from_full["asof_session"] == from_cut["asof_session"]
    assert from_full["signals"] == from_cut["signals"]
    assert from_full["market_breadth_pct"] == from_cut["market_breadth_pct"]


def test_breadth_gate_rejects_every_candidate() -> None:
    n = 180
    # 1 strong, 3 falling → breadth 25 % < 50 %
    bars = {
        "SPY": _spy_mild(n),
        "AAA": _strong_breakout(n),
        "BBB": _weak_down(n),
        "CCC": _weak_down(n),
        "DDD": _weak_down(n),
    }
    result = evaluate_signals(bars, -1, PARAMS, symbols=["AAA", "BBB", "CCC", "DDD"])
    assert result["breadth_gate"] == "FAIL"
    assert result["candidate_count"] == 0
    assert all(item["decision"] == DECISION_REJECT for item in result["signals"])
    assert all(BLOCK_BREADTH in item["blockers"] for item in result["signals"])


def test_breakout_with_rs_and_trend_is_candidate() -> None:
    n = 180
    bars = {
        "SPY": _spy_mild(n),
        "AAA": _strong_breakout(n),
        "BBB": _strong_breakout(n),
    }
    result = evaluate_signals(bars, -1, PARAMS, symbols=["AAA", "BBB"])
    assert result["breadth_gate"] == "PASS"
    aaa = next(item for item in result["signals"] if item["symbol"] == "AAA")
    assert aaa["decision"] == DECISION_BUY
    assert aaa["blockers"] == []
    assert aaa["tier"] in {"A", "B"}
    assert aaa["entry_price"] is not None
    assert aaa["stop_price"] < aaa["entry_price"]
    assert aaa["target_price"] > aaa["entry_price"]
    risk = aaa["entry_price"] - aaa["stop_price"]
    assert abs((aaa["target_price"] - aaa["entry_price"]) / risk - 4.5) < 1e-3
    assert aaa["atr_14"] > 0
    assert aaa["rs_excess_pct"] >= 10
    assert aaa["breakout_strength"] > 0
    assert aaa["score"] is not None


def test_no_breakout_is_rejected() -> None:
    n = 180
    flat = _dated_series([100.0] * n, high_extra=2.0)
    # last close stays inside the 30d range
    bars = {"SPY": _spy_mild(n), "AAA": flat, "BBB": _strong_breakout(n)}
    result = evaluate_signals(bars, -1, PARAMS, symbols=["AAA", "BBB"])
    aaa = next(item for item in result["signals"] if item["symbol"] == "AAA")
    assert aaa["decision"] == DECISION_REJECT
    assert BLOCK_BREAKOUT in aaa["blockers"]


def test_output_fields_present() -> None:
    n = 180
    result = evaluate_signals(
        {"SPY": _spy_mild(n), "AAA": _strong_breakout(n)},
        -1,
        PARAMS,
        symbols=["AAA"],
    )
    required = {
        "symbol",
        "decision",
        "tier",
        "score",
        "blockers",
        "entry_price",
        "stop_price",
        "target_price",
        "atr_14",
        "rs_excess_pct",
        "breakout_strength",
    }
    assert required <= set(result["signals"][0])


def test_evaluate_symbol_never_reads_past_visible() -> None:
    visible = _strong_breakout(180)
    spy = _spy_mild(180)
    first = evaluate_symbol("AAA", visible, spy, PARAMS)
    extra = visible + [
        {
            "t": "2026-12-31T00:00:00Z",
            "o": 1.0,
            "h": 1.0,
            "l": 1.0,
            "c": 1.0,
            "v": 1,
        }
    ]
    # volající musí oříznout; funkce sama budoucnost nedostane
    second = evaluate_symbol("AAA", visible, spy, PARAMS)
    assert first == second
    assert extra[-1]["c"] == 1.0
