from __future__ import annotations

import pytest

from scripts.fence import MemoryFenceStore, STATE_UNCERTAIN
from pathlib import Path

from scripts.protective_stop_guard import (
    atr_stop_from_bars,
    choose_stop,
    fallback_stop,
    run_guard,
)
from scripts.submit_order import EXIT_STOP, EXIT_UNCERTAIN

SAFE_ENV = {
    "T212_ENVIRONMENT": "demo",
    "ENABLE_LIVE_EXECUTION": "false",
    "T212_API_KEY": "key-id-xyz",
    "T212_API_SECRET": "secret-key-xyz",
}

CONFIG = {"fallback_stop_pct": 4.0}
STRATEGY = {
    "parameters": {
        "atr_period": 14,
        "stop_atr_multiple": 2.0,
        "max_risk_per_trade_czk": 500,
        "max_position_value_czk": 8000,
        "max_open_positions": 8,
        "max_new_entries_per_session": 2,
    }
}


def _pos(symbol: str = "AAPL", qty: float = 2, price: float = 200) -> dict:
    return {
        "positions": [{"ticker": f"{symbol}_US_EQ", "quantity": qty, "current_price": price}],
        "active_orders": [],
    }


def _bars(close: float = 200.0, count: int = 20) -> list[dict]:
    rows = []
    for i in range(count):
        rows.append({"t": f"2026-08-{i+1:02d}", "h": close + 2, "l": close - 2, "c": close})
    return rows


def test_source_never_calls_can_trade() -> None:
    source = Path("scripts/protective_stop_guard.py").read_text(encoding="utf-8")
    assert "rpc/can_trade" not in source
    assert "default_can_trade" not in source
    assert "can_close_position" in source


def test_guards_before_any_post() -> None:
    def forbidden(url: str, headers: dict, body: dict) -> dict:
        raise AssertionError("POST se nesmí volat")

    with pytest.raises(RuntimeError, match="T212_ENVIRONMENT"):
        run_guard(
            symbol="AAPL",
            session_date="2026-09-08",
            snapshot=_pos(),
            dry_run=False,
            env={"ENABLE_LIVE_EXECUTION": "false", "T212_API_KEY": "x"},
            http_post=forbidden,
        )
    with pytest.raises(RuntimeError, match="ENABLE_LIVE_EXECUTION"):
        run_guard(
            symbol="AAPL",
            session_date="2026-09-08",
            snapshot=_pos(),
            dry_run=False,
            env={"T212_ENVIRONMENT": "demo", "ENABLE_LIVE_EXECUTION": "true", "T212_API_KEY": "x"},
            http_post=forbidden,
        )


def test_one_symbol_only() -> None:
    with pytest.raises(RuntimeError, match="Jeden symbol"):
        run_guard(
            symbol="AAPL,MSFT",
            session_date="2026-09-08",
            snapshot=_pos(),
            dry_run=True,
            env=SAFE_ENV,
        )


def test_noop_when_stop_exists() -> None:
    snapshot = _pos()
    snapshot["active_orders"] = [
        {"ticker": "AAPL_US_EQ", "side": "SELL", "type": "STOP", "status": "NEW"}
    ]
    posts: list = []
    result = run_guard(
        symbol="AAPL",
        session_date="2026-09-08",
        snapshot=snapshot,
        dry_run=False,
        env=SAFE_ENV,
        http_post=lambda url, headers, body: posts.append(url) or {},
    )
    assert result.outcome == "NO-OP HAS_STOP AAPL"
    assert result.exit_code == 0
    assert posts == []


def test_missing_position() -> None:
    result = run_guard(
        symbol="AAPL",
        session_date="2026-09-08",
        snapshot={"positions": [], "active_orders": []},
        dry_run=True,
        env=SAFE_ENV,
    )
    assert result.outcome == "NO_POSITION AAPL"
    assert result.exit_code == EXIT_STOP


def test_dry_run_prints_plan_without_post() -> None:
    posts: list = []
    result = run_guard(
        symbol="AAPL",
        session_date="2026-09-08",
        snapshot=_pos(),
        dry_run=True,
        env=SAFE_ENV,
        strategy=STRATEGY,
        config=CONFIG,
        bars=None,
        http_post=lambda url, headers, body: posts.append(body) or {},
    )
    assert result.dry_run is True
    assert posts == []
    assert result.plan["body"]["stopPrice"] == 192.0
    assert result.plan["method"] == "fallback_pct"
    assert result.outcome.startswith("DRY_RUN AAPL")


def test_fallback_and_atr_stop() -> None:
    assert fallback_stop(200, 4) == 192.0
    bars = _bars(200, 20)
    atr = atr_stop_from_bars(bars, period=14, multiple=2.0)
    assert atr is not None
    assert atr < 200
    price, method = choose_stop(
        last_price=200,
        bars=bars,
        atr_period=14,
        atr_multiple=2.0,
        fallback_pct=4.0,
    )
    assert method == "atr"
    assert price == atr


def test_close_blocked_skips_post() -> None:
    posts: list = []
    result = run_guard(
        symbol="AAPL",
        session_date="2026-09-08",
        snapshot=_pos(),
        dry_run=False,
        env=SAFE_ENV,
        strategy=STRATEGY,
        config=CONFIG,
        can_close_fn=lambda: (False, "NO"),
        http_post=lambda url, headers, body: posts.append(url) or {},
    )
    assert result.outcome == "CLOSE_BLOCKED:NO"
    assert posts == []


def test_places_stop_once_via_fence() -> None:
    posts: list[dict] = []

    def post(url: str, headers: dict, body: dict) -> dict:
        posts.append(body)
        return {"id": "stop-1"}

    result = run_guard(
        symbol="AAPL",
        session_date="2026-09-08",
        snapshot=_pos(),
        dry_run=False,
        env=SAFE_ENV,
        strategy=STRATEGY,
        config=CONFIG,
        can_close_fn=lambda: (True, "OK"),
        store=MemoryFenceStore(),
        http_post=post,
    )
    assert result.outcome == "STOP_PLACED AAPL id=stop-1"
    assert len(posts) == 1
    assert posts[0]["quantity"] == -2.0
    assert posts[0]["stopPrice"] == 192.0
    assert posts[0]["timeValidity"] == "GOOD_TILL_CANCEL"
    assert result.fence_key and ":AAPL:STOP:" in result.fence_key


def test_uncertain_nonzero_no_retry() -> None:
    calls = {"n": 0}

    def post(url: str, headers: dict, body: dict) -> dict:
        calls["n"] += 1
        raise TimeoutError("timed out")

    store = MemoryFenceStore()
    result = run_guard(
        symbol="MSFT",
        session_date="2026-09-08",
        snapshot=_pos("MSFT"),
        dry_run=False,
        env=SAFE_ENV,
        strategy=STRATEGY,
        config=CONFIG,
        can_close_fn=lambda: (True, "OK"),
        store=store,
        http_post=post,
    )
    assert result.exit_code == EXIT_UNCERTAIN
    assert result.exit_code != 0
    assert result.outcome.startswith("UNCERTAIN")
    assert calls["n"] == 1
    row = store.get(result.fence_key)
    assert row is not None
    assert row.state == STATE_UNCERTAIN
