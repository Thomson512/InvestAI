from __future__ import annotations

import pytest

from scripts.broker_snapshot import DEMO_BASE_URL
from scripts.fence import MemoryFenceStore, STATE_UNCERTAIN
from scripts.submit_order import (
    EXIT_STOP,
    EXIT_UNCERTAIN,
    parse_dry_run,
    submit_order,
    unprotected_stop_instructions,
)

SAFE_ENV = {
    "T212_ENVIRONMENT": "demo",
    "ENABLE_LIVE_EXECUTION": "false",
    "T212_API_KEY": "key-id-xyz",
    "T212_API_SECRET": "secret-key-xyz",
}


def _ok_trade() -> tuple[bool, str]:
    return True, "OK"


def test_guards_before_any_post() -> None:
    def forbidden(url: str, headers: dict, body: dict) -> dict:
        raise AssertionError("POST se nesmí volat")

    with pytest.raises(RuntimeError, match="T212_ENVIRONMENT"):
        submit_order(
            symbol="AAPL",
            quantity=1,
            stop_price=180,
            session_date="2026-09-07",
            env={"ENABLE_LIVE_EXECUTION": "false", "T212_API_KEY": "x"},
            can_trade_fn=_ok_trade,
            snapshot={"positions": [], "active_orders": []},
            http_post=forbidden,
            dry_run=False,
        )
    with pytest.raises(RuntimeError, match="ENABLE_LIVE_EXECUTION"):
        submit_order(
            symbol="AAPL",
            quantity=1,
            stop_price=180,
            session_date="2026-09-07",
            env={"T212_ENVIRONMENT": "demo", "ENABLE_LIVE_EXECUTION": "true", "T212_API_KEY": "x"},
            can_trade_fn=_ok_trade,
            snapshot={"positions": [], "active_orders": []},
            http_post=forbidden,
            dry_run=False,
        )


def test_can_trade_false_stops_without_post() -> None:
    posts: list[str] = []
    result = submit_order(
        symbol="AAPL",
        quantity=1,
        stop_price=180,
        session_date="2026-09-07",
        env=SAFE_ENV,
        can_trade_fn=lambda: (False, "KILL_SWITCH_OFF"),
        snapshot={"positions": [], "active_orders": []},
        http_post=lambda url, headers, body: posts.append(url) or {},
        dry_run=False,
        store=MemoryFenceStore(),
    )
    assert result.outcome == "STOP"
    assert result.exit_code == EXIT_STOP
    assert posts == []


def test_global_blockers_stop_without_post() -> None:
    posts: list[str] = []
    result = submit_order(
        symbol="AAPL",
        quantity=1,
        stop_price=180,
        session_date="2026-09-07",
        env=SAFE_ENV,
        can_trade_fn=_ok_trade,
        snapshot={
            "positions": [{"ticker": "MSFT", "quantity": 1, "value_czk": 8000}],
            "active_orders": [],
        },
        http_post=lambda url, headers, body: posts.append(url) or {},
        dry_run=False,
        store=MemoryFenceStore(),
    )
    assert result.outcome == "STOP"
    assert "unprotected_position:MSFT" in result.reason
    assert posts == []


def test_dry_run_default_prints_plan_without_post_or_fence() -> None:
    store = MemoryFenceStore()
    posts: list[str] = []
    result = submit_order(
        symbol="AAPL",
        quantity=1,
        stop_price=180,
        session_date="2026-09-07",
        env=SAFE_ENV,
        can_trade_fn=_ok_trade,
        snapshot={"positions": [], "active_orders": []},
        store=store,
        http_post=lambda url, headers, body: posts.append(url) or {},
    )
    assert result.dry_run is True
    assert result.outcome == "DRY_RUN"
    assert posts == []
    assert store.get(result.fence_key) is None
    assert result.plan["market"]["url"] == f"{DEMO_BASE_URL}/api/v0/equity/orders/market"
    assert result.plan["stop"]["body"]["quantity"] == -1
    assert parse_dry_run("false") is False
    assert parse_dry_run("true") is True


def test_happy_path_buy_then_stop_confirms() -> None:
    posts: list[str] = []

    def http_post(url: str, headers: dict, body: dict) -> dict:
        posts.append(url)
        if url.endswith("/market"):
            return {"id": 11, "status": "FILLED"}
        return {"id": 22, "type": "STOP", "side": "SELL"}

    def http_get(url: str, headers: dict) -> object:
        return [
            {"id": 11, "type": "MARKET"},
            {"id": 22, "type": "STOP", "side": "SELL"},
        ]

    store = MemoryFenceStore()
    result = submit_order(
        symbol="AAPL",
        quantity=1,
        stop_price=180,
        session_date="2026-09-07",
        env=SAFE_ENV,
        can_trade_fn=_ok_trade,
        snapshot={"positions": [], "active_orders": []},
        store=store,
        http_post=http_post,
        http_get=http_get,
        dry_run=False,
        param_hash="p",
    )
    assert result.outcome == "CONFIRMED"
    assert result.exit_code == 0
    assert posts == [
        f"{DEMO_BASE_URL}/api/v0/equity/orders/market",
        f"{DEMO_BASE_URL}/api/v0/equity/orders/stop",
    ]
    assert store.get(result.fence_key).state == "CONFIRMED"


def test_stop_failure_is_critical_uncertain_no_retry() -> None:
    posts: list[str] = []

    def http_post(url: str, headers: dict, body: dict) -> dict:
        posts.append(url)
        if url.endswith("/stop"):
            raise TimeoutError("timed out")
        return {"id": 11, "status": "FILLED"}

    store = MemoryFenceStore()
    result = submit_order(
        symbol="AAPL",
        quantity=2,
        stop_price=170.5,
        session_date="2026-09-07",
        env=SAFE_ENV,
        can_trade_fn=_ok_trade,
        snapshot={"positions": [], "active_orders": []},
        store=store,
        http_post=http_post,
        http_get=lambda url, headers: [{"id": 11}],
        dry_run=False,
        param_hash="p",
    )
    assert result.outcome == "UNPROTECTED"
    assert result.exit_code == EXIT_UNCERTAIN
    assert result.critical is not None
    assert "CRITICAL" in result.critical
    assert "SELL STOP" in result.critical
    assert "170.5" in result.critical
    assert posts == [
        f"{DEMO_BASE_URL}/api/v0/equity/orders/market",
        f"{DEMO_BASE_URL}/api/v0/equity/orders/stop",
    ]
    assert store.get(result.fence_key).state == STATE_UNCERTAIN
    assert unprotected_stop_instructions(
        ticker="AAPL_US_EQ", quantity=2, stop_price=170.5, fence_key="k"
    ).startswith("CRITICAL")


def test_rejects_batch_symbols() -> None:
    with pytest.raises(Exception, match="Jeden symbol"):
        submit_order(
            symbol="AAPL,MSFT",
            quantity=1,
            stop_price=180,
            session_date="2026-09-07",
            env=SAFE_ENV,
            can_trade_fn=_ok_trade,
            snapshot={"positions": [], "active_orders": []},
            dry_run=True,
        )
