from __future__ import annotations

import pytest

from scripts.forward_shadow import (
    Book,
    Position,
    ShadowError,
    _select_candidates,
    fx_usdczk,
    paper_gate,
    reporting_capital_czk,
    require_live_sizing,
    run_shadow,
    size_shares,
)

LIVE = {
    "breakout_lookback_days": 30,
    "trend_filter_sma": 150,
    "relative_strength_lookback_days": 63,
    "min_rs_excess_pct": 10,
    "atr_period": 14,
    "stop_atr_multiple": 2.0,
    "reward_to_risk": 4.5,
    "min_market_breadth_pct": 50,
    "max_open_positions": 8,
    "max_risk_per_trade_czk": 500,
    "max_position_value_czk": 8000,
    "max_new_entries_per_session": 2,
}

COSTS = {
    "paper_gate": {"min_days": 90, "min_trades": 100},
    "fx": {
        "pair": "USD/CZK",
        "base_rate": 21.0,
        "origin": "2024-01-01",
        "daily_drift_bps": -2.0,
        "wobble_amp": 0.0015,
        "wobble_period_days": 7,
    },
    "scenarios": {
        "baseline": {"commission_bps": 0, "slippage_bps": 5, "fx_fee_bps": 15},
        "conservative": {"commission_bps": 0, "slippage_bps": 10, "fx_fee_bps": 15},
        "severe": {"commission_bps": 0, "slippage_bps": 20, "fx_fee_bps": 15},
    },
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


def _uptrend_breakout(n: int, jump_at: int = 155, crash_at: int | None = None) -> list[dict]:
    closes = []
    price = 80.0
    for i in range(n):
        if crash_at is not None and i >= crash_at:
            price = price * 0.90
        elif i == jump_at:
            price = price + 5.0
        else:
            price = price + 0.15
        closes.append(price)
    return _dated_series(closes)


def _spy(n: int) -> list[dict]:
    return _dated_series([400.0 + i * 0.02 for i in range(n)])


def _sizing() -> dict:
    return require_live_sizing(LIVE)


def test_size_shares_uses_fixed_czk_not_equity() -> None:
    sizing = _sizing()
    shares = size_shares(entry_usd=100.0, stop_usd=90.0, fx_rate=20.0, sizing=sizing)
    # risk 10 USD * 20 = 200 CZK → 500/200 = 2; value 2000 → 8000/2000 = 4
    assert shares == 2
    again = size_shares(entry_usd=100.0, stop_usd=90.0, fx_rate=20.0, sizing=sizing)
    assert again == shares
    assert reporting_capital_czk(sizing) == 8 * 8000


def test_live_sizing_rejects_drifted_caps() -> None:
    bad = {**LIVE, "max_risk_per_trade_czk": 5000}
    with pytest.raises(ShadowError, match="max_risk_per_trade_czk"):
        require_live_sizing(bad)


def test_paper_gate_false_until_both_thresholds() -> None:
    assert paper_gate(calendar_days=89, completed_trades=500, min_days=90, min_trades=100)[
        "promotion_authorized"
    ] is False
    assert paper_gate(calendar_days=200, completed_trades=99, min_days=90, min_trades=100)[
        "promotion_authorized"
    ] is False
    assert paper_gate(calendar_days=90, completed_trades=100, min_days=90, min_trades=100)[
        "promotion_authorized"
    ] is True


def test_fx_entry_differs_from_exit() -> None:
    entry = fx_usdczk("2024-01-01", COSTS["fx"])
    exit_ = fx_usdczk("2024-04-01", COSTS["fx"])
    assert entry != exit_


def test_select_caps_two_per_session_and_eight_open() -> None:
    sizing = _sizing()
    signals = [
        {
            "symbol": f"S{i:02d}",
            "decision": "BUY_CANDIDATE",
            "tier": "A",
            "score": 20 - i,
            "entry_price": 100.0,
            "stop_price": 90.0,
            "target_price": 145.0,
        }
        for i in range(10)
    ]
    empty = Book(cash_czk=64_000)
    picked = _select_candidates(signals, empty, sizing)
    assert [item["symbol"] for item in picked] == ["S00", "S01"]

    full = Book(cash_czk=64_000)
    for i in range(8):
        full.positions[f"P{i}"] = Position(
            symbol=f"P{i}",
            shares=1,
            entry_session="2024-01-01",
            planned_entry=100,
            entry_fill=100,
            stop_price=90,
            target_price=145,
            fx_entry_mid=21,
            fx_entry_fill=21,
            commission_bps=0,
            slippage_bps=5,
            fx_fee_bps=15,
            costs_entry_czk=100,
        )
    assert _select_candidates(signals, full, sizing) == []


def test_shadow_report_fields_and_gate_closed_on_short_sample() -> None:
    n = 180
    bars = {
        "SPY": _spy(n),
        "AAA": _uptrend_breakout(n, crash_at=168),
        "BBB": _uptrend_breakout(n, crash_at=168),
    }
    report = run_shadow(
        {"bars": bars, "universe_hash": "x", "source_dataset_sha256": "y"},
        LIVE,
        COSTS,
        symbols=["AAA", "BBB"],
        param_hash="p",
    )
    assert set(report["scenarios"]) == {"baseline", "conservative", "severe"}
    base = report["scenarios"]["baseline"]
    required = {
        "completed_trades",
        "winners",
        "losers",
        "win_rate_pct",
        "realized_pnl",
        "marked_pnl",
        "max_drawdown_pct",
        "expectancy",
        "profit_factor",
    }
    assert required <= set(base)
    assert report["sizing"]["max_risk_per_trade_czk"] == 500
    assert report["sizing"]["max_position_value_czk"] == 8000
    assert report["sizing"]["max_open_positions"] == 8
    assert report["sizing"]["method"] == "fixed_czk"
    assert report["paper_gate"]["promotion_authorized"] is False
    assert base["completed_trades"] >= 1
    if base["trades"]:
        trade = base["trades"][0]
        assert {"entry_session", "exit_session", "entry_price", "exit_price", "exit_reason", "r_multiple", "costs"} <= set(
            trade
        )
        assert trade["fx_entry"] != trade["fx_exit"]
        assert set(trade["costs"]) == {
            "commission_czk",
            "slippage_czk",
            "fx_fee_czk",
            "fx_drift_czk",
        }


def test_severe_slippage_costs_at_least_as_much_as_baseline() -> None:
    n = 180
    bars = {
        "SPY": _spy(n),
        "AAA": _uptrend_breakout(n, crash_at=168),
        "BBB": _uptrend_breakout(n, crash_at=168),
        "CCC": _uptrend_breakout(n, crash_at=168),
    }
    report = run_shadow({"bars": bars}, LIVE, COSTS, symbols=["AAA", "BBB", "CCC"])
    base = report["scenarios"]["baseline"]
    severe = report["scenarios"]["severe"]
    assert base["trades"] and severe["trades"]
    base_slip = sum(t["costs"]["slippage_czk"] or 0 for t in base["trades"])
    severe_slip = sum(t["costs"]["slippage_czk"] or 0 for t in severe["trades"])
    assert severe_slip >= base_slip
