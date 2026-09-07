"""Read-only snapshot Trading 212 DEMO. Pouze GET, žádný POST/PUT/DELETE."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
BROKER_CONFIG_PATH = REPO_ROOT / "config" / "broker.v1.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "snapshot.json"
DEMO_BASE_URL = "https://demo.trading212.com"
SUMMARY_PATH = "/api/v0/equity/account/summary"
PORTFOLIO_PATH = "/api/v0/equity/portfolio"
ORDERS_PATH = "/api/v0/equity/orders"
MAX_ATTEMPTS = 3
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
PROTECTIVE_SELL_TYPES = frozenset({"STOP", "STOP_LIMIT"})
HTTP_TIMEOUT_SEC = 30

HttpGet = Callable[[str, dict[str, str]], object]
SleepFn = Callable[[float], None]


class BrokerSnapshotError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require_demo_environment(env: dict[str, str] | None = None) -> None:
    """Guard PŘED jakýmkoli HTTP. Live URL se nikdy nesestaví."""
    source = env if env is not None else os.environ
    if source.get("T212_ENVIRONMENT") != "demo":
        raise RuntimeError("T212_ENVIRONMENT musí být 'demo'. HTTP se nevolá.")


def require_api_key(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    key = (source.get("T212_API_KEY") or "").strip()
    if not key:
        raise BrokerSnapshotError("Chybí T212_API_KEY v prostředí.")
    return key


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": api_key, "Accept": "application/json"}


def _safe_url_for_error(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _redact(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "[redacted]")


def default_http_get(url: str, headers: dict[str, str]) -> object:
    if not url.startswith(DEMO_BASE_URL):
        raise RuntimeError("HTTP mimo demo base URL je zakázané.")
    request = urllib.request.Request(url, headers=headers, method="GET")
    secret = headers.get("Authorization", "")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = _redact(exc.read().decode("utf-8", errors="replace"), secret)
        raise BrokerSnapshotError(
            f"T212 HTTP {exc.code} GET {_safe_url_for_error(url)}: {body[:200]}"
        ) from None
    except urllib.error.URLError as exc:
        raise BrokerSnapshotError(f"T212 síťové selhání GET {_safe_url_for_error(url)}") from exc
    return payload


def get_with_retry(
    url: str,
    headers: dict[str, str],
    *,
    http_get: HttpGet,
    sleep: SleepFn = time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> object:
    if not url.startswith(DEMO_BASE_URL + "/"):
        raise RuntimeError("GET jen na https://demo.trading212.com")
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return http_get(url, headers)
        except BrokerSnapshotError as exc:
            last_error = exc
            status = _status_from_error(exc)
            retryable = status is None or status in RETRYABLE_STATUS
            if (not retryable) or attempt == max_attempts - 1:
                raise
            sleep(2**attempt)
        except Exception as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                raise BrokerSnapshotError("T212 GET selhal po opakováních.") from exc
            sleep(2**attempt)
    raise BrokerSnapshotError(f"T212 GET selhal: {last_error}")


def _status_from_error(exc: BrokerSnapshotError) -> int | None:
    text = str(exc)
    if "HTTP " not in text:
        return None
    try:
        return int(text.split("HTTP ", 1)[1].split()[0])
    except (IndexError, ValueError):
        return None


def normalize_ticker(ticker: str) -> str:
    value = ticker.strip().upper()
    for suffix in ("_US_EQ", "_EQ"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _ticker_of(item: dict) -> str:
    if item.get("ticker"):
        return str(item["ticker"])
    instrument = item.get("instrument") or {}
    return str(instrument.get("ticker") or "")


def _position_value_czk(position: dict, account_currency: str) -> float | None:
    if position.get("value_czk") is not None:
        return float(position["value_czk"])
    wallet = position.get("walletImpact") or position.get("wallet_impact") or {}
    currency = str(wallet.get("currency") or account_currency or "").upper()
    if wallet.get("currentValue") is not None and currency == "CZK":
        return float(wallet["currentValue"])
    quantity = float(position.get("quantity") or 0)
    price = float(position.get("currentPrice") or position.get("current_price") or 0)
    if currency == "CZK":
        return quantity * price
    return None


def normalize_position(raw: dict, account_currency: str) -> dict:
    ticker = _ticker_of(raw)
    quantity = float(raw.get("quantity") or 0)
    price = raw.get("currentPrice", raw.get("current_price"))
    return {
        "ticker": ticker,
        "quantity": quantity,
        "current_price": float(price) if price is not None else None,
        "value_czk": _position_value_czk(raw, account_currency),
        "currency": account_currency,
    }


def normalize_order(raw: dict) -> dict:
    return {
        "id": raw.get("id"),
        "ticker": _ticker_of(raw),
        "side": str(raw.get("side") or "").upper(),
        "type": str(raw.get("type") or "").upper(),
        "status": raw.get("status"),
        "quantity": raw.get("quantity"),
        "stop_price": raw.get("stopPrice", raw.get("stop_price")),
        "limit_price": raw.get("limitPrice", raw.get("limit_price")),
    }


def is_protective_sell(order: dict, position_ticker: str) -> bool:
    if str(order.get("side") or "").upper() != "SELL":
        return False
    if str(order.get("type") or "").upper() not in PROTECTIVE_SELL_TYPES:
        return False
    return normalize_ticker(str(order.get("ticker") or "")) == normalize_ticker(position_ticker)


def smoke_exemption_czk(config: dict | None = None) -> float:
    if config and config.get("smoke_exemption_czk") is not None:
        return float(config["smoke_exemption_czk"])
    if BROKER_CONFIG_PATH.is_file():
        return float(load_json(BROKER_CONFIG_PATH)["smoke_exemption_czk"])
    return 250.0


def global_blockers(snapshot: dict, *, exemption_czk: float | None = None) -> list[str]:
    """Tvrdá brána: jedna nechráněná pozice nad prahem zastaví všechny vstupy."""
    threshold = smoke_exemption_czk() if exemption_czk is None else exemption_czk
    orders = snapshot.get("active_orders") or []
    blockers: list[str] = []
    for position in snapshot.get("positions") or []:
        ticker = str(position.get("ticker") or "")
        quantity = float(position.get("quantity") or 0)
        if not ticker or quantity == 0:
            continue
        if any(is_protective_sell(order, ticker) for order in orders):
            continue
        value = position.get("value_czk")
        if value is None:
            blockers.append(f"unprotected_position:{ticker}")
            continue
        if float(value) > threshold:
            blockers.append(f"unprotected_position:{ticker}")
    return blockers


def _as_list(payload: object) -> list:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("positions", "items", "data", "orders"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise BrokerSnapshotError("Neočekávaný tvar odpovědi (očekáván seznam).")


def fetch_snapshot(
    *,
    env: dict[str, str] | None = None,
    http_get: HttpGet | None = None,
    sleep: SleepFn = time.sleep,
    now: datetime | None = None,
    config: dict | None = None,
) -> dict:
    require_demo_environment(env)
    api_key = require_api_key(env)
    getter = http_get or default_http_get
    headers = auth_headers(api_key)
    now = now or datetime.now(timezone.utc)

    def pull(path: str) -> object:
        return get_with_retry(f"{DEMO_BASE_URL}{path}", headers, http_get=getter, sleep=sleep)

    summary_raw = pull(SUMMARY_PATH)
    if not isinstance(summary_raw, dict):
        raise BrokerSnapshotError("Summary není objekt.")
    portfolio_raw = _as_list(pull(PORTFOLIO_PATH))
    orders_raw = _as_list(pull(ORDERS_PATH))

    cash = summary_raw.get("cash") or {}
    account_currency = str(summary_raw.get("currency") or "CZK").upper()
    positions = [normalize_position(item, account_currency) for item in portfolio_raw if isinstance(item, dict)]
    orders = [normalize_order(item) for item in orders_raw if isinstance(item, dict)]
    snapshot = {
        "account_total_value": summary_raw.get("totalValue", summary_raw.get("account_total_value")),
        "available_to_trade": cash.get("availableToTrade", summary_raw.get("available_to_trade")),
        "currency": account_currency,
        "positions": positions,
        "active_orders": orders,
        "fetched_at": now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": "demo",
        "base_url": DEMO_BASE_URL,
    }
    snapshot["global_blockers"] = global_blockers(
        snapshot, exemption_czk=smoke_exemption_czk(config)
    )
    return snapshot


def write_snapshot(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GET snapshot T212 demo (read-only).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        snapshot = fetch_snapshot()
    except RuntimeError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1
    write_snapshot(args.output, snapshot)
    blockers = snapshot["global_blockers"]
    print(
        f"OK snapshot positions={len(snapshot['positions'])} "
        f"orders={len(snapshot['active_orders'])} blockers={len(blockers)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
