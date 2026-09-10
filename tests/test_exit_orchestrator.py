from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts.exit_orchestrator import (
    EXIT_LOOP_NAME,
    MemoryControlPlane,
    ExitError,
    first_line,
    merge_pnl,
    missing_stops,
    reconcile,
    run_exit,
    write_noop_evidence,
)

NOW = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)


def _snapshot(*, equity: float = 64000, positions: list | None = None, orders: list | None = None) -> dict:
    return {
        "account_total_value": equity,
        "positions": positions or [],
        "active_orders": orders or [],
    }


def _protected_msft() -> tuple[list, list]:
    positions = [{"ticker": "MSFT_US_EQ", "quantity": 1, "value_czk": 5000}]
    orders = [
        {"ticker": "MSFT_US_EQ", "side": "SELL", "type": "STOP", "status": "NEW", "stopPrice": 300}
    ]
    return positions, orders


def test_source_never_calls_can_trade() -> None:
    source = Path("scripts/exit_orchestrator.py").read_text(encoding="utf-8")
    assert "rpc/can_trade" not in source
    assert "default_can_trade" not in source
    assert "can_close_position" in source


def test_missing_stops_ignores_exemption() -> None:
    snapshot = _snapshot(
        positions=[{"ticker": "AAPL_US_EQ", "quantity": 1, "value_czk": 50}],
        orders=[],
    )
    assert missing_stops(snapshot) == ["AAPL"]


def test_reconcile_broker_vs_evidence() -> None:
    drift = reconcile({"AAPL", "MSFT"}, {"AAPL", "NVDA"})
    assert drift["matched"] == ["AAPL"]
    assert drift["broker_only"] == ["MSFT"]
    assert drift["evidence_only"] == ["NVDA"]


def test_merge_pnl_keeps_opening_and_raises_peak() -> None:
    opening, peak = merge_pnl(
        {"opening_equity": 100000, "peak_equity": 101000},
        102000,
    )
    assert opening == 100000
    assert peak == 102000
    first_open, first_peak = merge_pnl(None, 64000)
    assert first_open == 64000
    assert first_peak == 64000


def test_noop_does_not_write_pnl(tmp_path: Path) -> None:
    store = MemoryControlPlane()
    result = write_noop_evidence(tmp_path / "exit_evidence.json", "2026-09-06", "weekend")
    assert result.pnl_written is False
    assert result.outcome == "NO-OP MARKET_CLOSED"
    assert first_line(result) == "EXIT: NO-OP MARKET_CLOSED"
    assert store.pnl == {}
    assert store.heartbeats == {}


def test_close_blocked_skips_pnl_and_does_not_call_can_trade() -> None:
    store = MemoryControlPlane()
    result = run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(),
        store=store,
        can_close_fn=lambda: (False, "NO"),
        now=NOW,
    )
    assert result.exit_code == 1
    assert result.pnl_written is False
    assert store.pnl == {}
    assert result.outcome == "CLOSE_BLOCKED:NO"


def test_upserts_pnl_and_heartbeat_on_ok() -> None:
    store = MemoryControlPlane()
    store.fences.add("MSFT")
    positions, orders = _protected_msft()
    result = run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(equity=64000, positions=positions, orders=orders),
        store=store,
        can_close_fn=lambda: (True, "OK"),
        now=NOW,
    )
    assert result.pnl_written is True
    assert store.pnl["2026-09-08"]["opening_equity"] == 64000
    assert store.pnl["2026-09-08"]["current_equity"] == 64000
    assert store.heartbeats[EXIT_LOOP_NAME] == "2026-09-08T15:00:00Z"
    assert result.outcome.startswith("OK ")
    assert "unprotected=0" in result.outcome
    assert first_line(result).startswith("EXIT: OK")


def test_unprotected_still_writes_pnl() -> None:
    store = MemoryControlPlane()
    result = run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(
            positions=[{"ticker": "AAPL_US_EQ", "quantity": 2, "value_czk": 8000}],
            orders=[],
        ),
        store=store,
        can_close_fn=lambda: (True, "OK"),
        now=NOW,
    )
    assert result.pnl_written is True
    assert result.outcome == "UNPROTECTED AAPL"
    assert "protective-stop-guard.yml" in result.extra_summary[0]


def test_auto_stop_runs_for_first_unprotected() -> None:
    store = MemoryControlPlane()
    seen: list[str] = []

    class Guard:
        outcome = "STOP_PLACED AAPL id=1"

    result = run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(
            positions=[{"ticker": "AAPL_US_EQ", "quantity": 2, "value_czk": 8000, "current_price": 200}],
            orders=[],
        ),
        store=store,
        can_close_fn=lambda: (True, "OK"),
        now=NOW,
        place_stop_fn=lambda symbol: seen.append(symbol) or Guard(),
    )
    assert seen == ["AAPL"]
    assert result.pnl_written is True
    assert result.outcome == "UNPROTECTED AAPL"
    assert result.extra_summary[0] == "AUTO-STOP: STOP_PLACED AAPL id=1"


def test_confirms_uncertain_when_stop_is_on_broker() -> None:
    store = MemoryControlPlane()
    store.uncertain_symbols.add("MSFT")
    store.fences.add("MSFT")
    positions, orders = _protected_msft()
    run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(equity=64000, positions=positions, orders=orders),
        store=store,
        can_close_fn=lambda: (True, "OK"),
        now=NOW,
    )
    assert store.uncertain_symbols == set()


def test_drift_is_visible() -> None:
    store = MemoryControlPlane()
    store.fences.add("NVDA")
    positions, orders = _protected_msft()
    result = run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(positions=positions, orders=orders),
        store=store,
        can_close_fn=lambda: (True, "OK"),
        now=NOW,
    )
    assert result.pnl_written is True
    assert "DRIFT" in result.outcome
    assert "MSFT" in result.outcome
    assert "NVDA" in result.outcome


def test_missing_equity_is_fail_closed() -> None:
    store = MemoryControlPlane()
    try:
        run_exit(
            session_date="2026-09-08",
            snapshot={"positions": [], "active_orders": []},
            store=store,
            can_close_fn=lambda: (True, "OK"),
            now=NOW,
        )
    except ExitError as exc:
        assert "account_total_value" in str(exc)
        assert store.pnl == {}
        assert store.heartbeats == {}
    else:
        raise AssertionError("očekáván ExitError")


def test_buyer_evidence_feeds_expected_book() -> None:
    store = MemoryControlPlane()
    positions, orders = _protected_msft()
    result = run_exit(
        session_date="2026-09-08",
        snapshot=_snapshot(positions=positions, orders=orders),
        store=store,
        buyer_evidence={"outcome": "ORDER_SUBMITTED:1", "plan": {"symbol": "MSFT"}},
        can_close_fn=lambda: (True, "OK"),
        now=NOW,
    )
    assert result.evidence["reconcile"]["matched"] == ["MSFT"]
    assert result.outcome.startswith("OK ")
