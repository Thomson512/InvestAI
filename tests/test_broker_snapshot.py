from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.broker_snapshot import (
    DEMO_BASE_URL,
    BrokerSnapshotError,
    fetch_snapshot,
    get_with_retry,
    global_blockers,
)


FIXED = datetime(2026, 9, 7, 20, 40, tzinfo=timezone.utc)
DEMO_ENV = {"T212_ENVIRONMENT": "demo", "T212_API_KEY": "secret-key-xyz"}


def _http_payloads() -> dict[str, object]:
    return {
        f"{DEMO_BASE_URL}/api/v0/equity/account/summary": {
            "currency": "CZK",
            "totalValue": 64000,
            "cash": {"availableToTrade": 12000},
        },
        f"{DEMO_BASE_URL}/api/v0/equity/portfolio": [
            {
                "ticker": "AAPL_US_EQ",
                "quantity": 2,
                "currentPrice": 200,
                "walletImpact": {"currency": "CZK", "currentValue": 8400},
            }
        ],
        f"{DEMO_BASE_URL}/api/v0/equity/orders": [
            {
                "id": 1,
                "ticker": "AAPL_US_EQ",
                "side": "SELL",
                "type": "STOP",
                "status": "NEW",
                "stopPrice": 180,
            }
        ],
    }


class Recorder:
    def __init__(self, payloads: dict[str, object] | None = None) -> None:
        self.payloads = payloads or _http_payloads()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> object:
        self.calls.append((url, headers))
        assert url.startswith(DEMO_BASE_URL)
        return self.payloads[url]


def test_guard_rejects_non_demo_before_http() -> None:
    def forbidden(url: str, headers: dict[str, str]) -> object:
        raise AssertionError("HTTP se nesmí volat")

    with pytest.raises(RuntimeError, match="T212_ENVIRONMENT"):
        fetch_snapshot(env={"T212_ENVIRONMENT": "live", "T212_API_KEY": "x"}, http_get=forbidden)
    with pytest.raises(RuntimeError, match="T212_ENVIRONMENT"):
        fetch_snapshot(env={"T212_API_KEY": "x"}, http_get=forbidden)


def test_fetch_snapshot_get_only_demo_fields() -> None:
    rec = Recorder()
    snap = fetch_snapshot(env=DEMO_ENV, http_get=rec, sleep=lambda _: None, now=FIXED)
    assert {url for url, _ in rec.calls} == {
        f"{DEMO_BASE_URL}/api/v0/equity/account/summary",
        f"{DEMO_BASE_URL}/api/v0/equity/portfolio",
        f"{DEMO_BASE_URL}/api/v0/equity/orders",
    }
    assert all(url.startswith(DEMO_BASE_URL) for url, _ in rec.calls)
    assert snap["account_total_value"] == 64000
    assert snap["available_to_trade"] == 12000
    assert snap["fetched_at"] == "2026-09-07T20:40:00Z"
    assert snap["positions"][0]["ticker"] == "AAPL_US_EQ"
    assert snap["active_orders"][0]["type"] == "STOP"
    assert snap["global_blockers"] == []


def test_global_blockers_unprotected_above_exemption() -> None:
    snapshot = {
        "positions": [{"ticker": "MSFT_US_EQ", "quantity": 1, "value_czk": 8000}],
        "active_orders": [],
    }
    assert global_blockers(snapshot, exemption_czk=250) == ["unprotected_position:MSFT_US_EQ"]


def test_stop_and_stop_limit_protect_same_ticker() -> None:
    position = {"ticker": "AAPL", "quantity": 1, "value_czk": 8000}
    stop = {"ticker": "AAPL_US_EQ", "side": "SELL", "type": "STOP"}
    stop_limit = {"ticker": "AAPL_US_EQ", "side": "SELL", "type": "STOP_LIMIT"}
    assert global_blockers({"positions": [position], "active_orders": [stop]}, exemption_czk=250) == []
    assert global_blockers({"positions": [position], "active_orders": [stop_limit]}, exemption_czk=250) == []


def test_limit_or_buy_does_not_protect() -> None:
    position = {"ticker": "NVDA", "quantity": 1, "value_czk": 8000}
    limit_sell = {"ticker": "NVDA", "side": "SELL", "type": "LIMIT"}
    buy_stop = {"ticker": "NVDA", "side": "BUY", "type": "STOP"}
    assert global_blockers({"positions": [position], "active_orders": [limit_sell]}, exemption_czk=250) == [
        "unprotected_position:NVDA"
    ]
    assert global_blockers({"positions": [position], "active_orders": [buy_stop]}, exemption_czk=250) == [
        "unprotected_position:NVDA"
    ]


def test_smoke_exemption_allows_tiny_unprotected() -> None:
    snapshot = {
        "positions": [{"ticker": "AAA", "quantity": 1, "value_czk": 250}],
        "active_orders": [],
    }
    assert global_blockers(snapshot, exemption_czk=250) == []
    snapshot["positions"][0]["value_czk"] = 250.01
    assert global_blockers(snapshot, exemption_czk=250) == ["unprotected_position:AAA"]


def test_retry_get_three_attempts_then_ok() -> None:
    attempts = {"n": 0}

    def flaky(url: str, headers: dict[str, str]) -> object:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise BrokerSnapshotError("T212 HTTP 429 GET https://demo.trading212.com/x")
        return {"ok": True}

    sleeps: list[float] = []
    out = get_with_retry(
        f"{DEMO_BASE_URL}/api/v0/equity/orders",
        {"Authorization": "secret-key-xyz"},
        http_get=flaky,
        sleep=sleeps.append,
    )
    assert out == {"ok": True}
    assert attempts["n"] == 3
    assert sleeps == [1, 2]


def test_no_retry_on_401_and_key_not_in_error() -> None:
    def unauthorized(url: str, headers: dict[str, str]) -> object:
        raise BrokerSnapshotError("T212 HTTP 401 GET https://demo.trading212.com/x: denied")

    with pytest.raises(BrokerSnapshotError, match="401") as exc:
        get_with_retry(
            f"{DEMO_BASE_URL}/api/v0/equity/orders",
            {"Authorization": "secret-key-xyz"},
            http_get=unauthorized,
            sleep=lambda _: None,
        )
    assert "secret-key-xyz" not in str(exc.value)


def test_source_has_no_mutating_http() -> None:
    source = Path(__file__).resolve().parent.parent / "scripts" / "broker_snapshot.py"
    text = source.read_text(encoding="utf-8")
    assert "method=\"POST\"" not in text
    assert "method=\"PUT\"" not in text
    assert "method=\"DELETE\"" not in text
    assert "method=\"GET\"" in text
