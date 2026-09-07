"""Durable fence proti duplicitnímu odeslání. T212 nemá clientOrderId."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import quote

from scripts.param_hash import compute_param_hash

POST_TIMEOUT_SEC = 15
STATE_PENDING = "PENDING"
STATE_SENT = "SENT"
STATE_CONFIRMED = "CONFIRMED"
STATE_UNCERTAIN = "UNCERTAIN"
STATE_NEVER_SENT = "NEVER_SENT"
OUTCOME_SKIP = "SKIP"
EXIT_OK = 0
EXIT_UNCERTAIN = 2


class FenceError(RuntimeError):
    pass


class SendUncertain(FenceError):
    """Timeout nebo 5xx — výsledek POST je neznámý. Žádný retry."""


@dataclass
class FenceRecord:
    fence_key: str
    symbol: str
    session_date: str
    state: str
    order_id: str | None = None


@dataclass
class FenceResult:
    outcome: str
    fence_key: str
    exit_code: int
    order_id: str | None = None
    reason: str = ""


class FenceStore(Protocol):
    def get(self, fence_key: str) -> FenceRecord | None: ...
    def insert_pending(self, fence_key: str, symbol: str, session_date: str) -> bool: ...
    def set_state(self, fence_key: str, state: str, order_id: str | None = None) -> None: ...


class MemoryFenceStore:
    """Testovací store. Produkce musí být databáze — runner je efemérní."""

    def __init__(self) -> None:
        self._rows: dict[str, FenceRecord] = {}
        self._lock = threading.Lock()

    def get(self, fence_key: str) -> FenceRecord | None:
        with self._lock:
            row = self._rows.get(fence_key)
            return None if row is None else FenceRecord(**row.__dict__)

    def insert_pending(self, fence_key: str, symbol: str, session_date: str) -> bool:
        with self._lock:
            if fence_key in self._rows:
                return False
            self._rows[fence_key] = FenceRecord(
                fence_key=fence_key,
                symbol=symbol,
                session_date=session_date,
                state=STATE_PENDING,
            )
            return True

    def set_state(self, fence_key: str, state: str, order_id: str | None = None) -> None:
        with self._lock:
            row = self._rows[fence_key]
            row.state = state
            if order_id is not None:
                row.order_id = order_id


class RestFenceStore:
    """trading.trade_fences přes PostgREST. Klíč jen z env, nikdy do logu."""

    def __init__(self, url: str, service_key: str, *, http_open: Callable | None = None) -> None:
        self._url = url.rstrip("/") + "/rest/v1/trade_fences"
        self._key = service_key
        self._http_open = http_open or urllib.request.urlopen

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept-Profile": "trading",
            "Content-Profile": "trading",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _open(self, request: urllib.request.Request, timeout: float = 15) -> object:
        return self._http_open(request, timeout=timeout)

    def get(self, fence_key: str) -> FenceRecord | None:
        query = f"{self._url}?fence_key=eq.{quote(fence_key)}"
        request = urllib.request.Request(query, headers=self._headers(), method="GET")
        with self._open(request) as response:
            rows = json.loads(response.read().decode("utf-8"))
        if not rows:
            return None
        row = rows[0]
        return FenceRecord(
            fence_key=row["fence_key"],
            symbol=row["symbol"],
            session_date=str(row["session_date"]),
            state=row["state"],
            order_id=row.get("order_id"),
        )

    def insert_pending(self, fence_key: str, symbol: str, session_date: str) -> bool:
        payload = json.dumps(
            {
                "fence_key": fence_key,
                "symbol": symbol,
                "session_date": session_date,
                "state": STATE_PENDING,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url, data=payload, headers=self._headers(), method="POST"
        )
        try:
            with self._open(request) as response:
                response.read()
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                exc.read()
                return False
            raise FenceError(f"INSERT fence selhal HTTP {exc.code}") from None

    def set_state(self, fence_key: str, state: str, order_id: str | None = None) -> None:
        body: dict[str, object] = {"state": state}
        if order_id is not None:
            body["order_id"] = order_id
        request = urllib.request.Request(
            f"{self._url}?fence_key=eq.{quote(fence_key)}",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="PATCH",
        )
        with self._open(request) as response:
            response.read()


def make_fence_key(session_date: str, symbol: str, param_hash: str) -> str:
    return f"{session_date}:{symbol}:{param_hash}"


def classify_send_failure(exc: BaseException) -> bool:
    """True = UNCERTAIN (timeout/5xx). Žádný retry."""
    if isinstance(exc, (TimeoutError, SendUncertain)):
        return True
    if isinstance(exc, urllib.error.HTTPError) and exc.code >= 500:
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = str(getattr(exc, "reason", exc)).lower()
        if "timed out" in reason or "timeout" in reason:
            return True
    text = str(exc).lower()
    return "timeout" in text or "timed out" in text or "http 5" in text


def can_release_never_sent(*, has_send_artifact: bool, broker_unchanged: bool) -> bool:
    """NEVER_SENT jen když chybí artefakt odeslání A broker potvrdí beze změny."""
    return (not has_send_artifact) and broker_unchanged


def release_never_sent(
    store: FenceStore,
    fence_key: str,
    *,
    has_send_artifact: bool,
    broker_unchanged: bool,
) -> str:
    if not can_release_never_sent(
        has_send_artifact=has_send_artifact, broker_unchanged=broker_unchanged
    ):
        return STATE_UNCERTAIN
    store.set_state(fence_key, STATE_NEVER_SENT)
    return STATE_NEVER_SENT


def run_fenced_send(
    *,
    session_date: str,
    symbol: str,
    param_hash: str,
    store: FenceStore,
    send_once: Callable[[], dict],
    readback: Callable[[dict], bool] | None = None,
) -> FenceResult:
    """INSERT PENDING, jeden send, žádný retry. Timeout/5xx = UNCERTAIN."""
    fence_key = make_fence_key(session_date, symbol, param_hash)
    if not store.insert_pending(fence_key, symbol, session_date):
        return FenceResult(
            outcome=OUTCOME_SKIP,
            fence_key=fence_key,
            exit_code=EXIT_OK,
            reason="fence_exists",
        )

    calls = {"n": 0}

    def _once() -> dict:
        if calls["n"] >= 1:
            raise FenceError("Žádný retry POST.")
        calls["n"] += 1
        return send_once()

    try:
        sent = _once()
    except Exception as exc:
        store.set_state(fence_key, STATE_UNCERTAIN)
        # Po zahájení send je výsledek neznámý. Žádný retry. Člověk.
        return FenceResult(
            outcome=STATE_UNCERTAIN,
            fence_key=fence_key,
            exit_code=EXIT_UNCERTAIN,
            reason="send_uncertain",
        )

    order_id = None if sent is None else sent.get("order_id") or sent.get("id")
    order_id_text = None if order_id is None else str(order_id)
    store.set_state(fence_key, STATE_SENT, order_id=order_id_text)
    if readback is None or readback(sent):
        store.set_state(fence_key, STATE_CONFIRMED, order_id=order_id_text)
        return FenceResult(
            outcome=STATE_CONFIRMED,
            fence_key=fence_key,
            exit_code=EXIT_OK,
            order_id=order_id_text,
            reason="confirmed",
        )
    store.set_state(fence_key, STATE_UNCERTAIN, order_id=order_id_text)
    return FenceResult(
        outcome=STATE_UNCERTAIN,
        fence_key=fence_key,
        exit_code=EXIT_UNCERTAIN,
        order_id=order_id_text,
        reason="readback_failed",
    )


def store_from_env(env: dict[str, str] | None = None) -> RestFenceStore:
    source = env if env is not None else os.environ
    url = (source.get("SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        raise FenceError("Chybí SUPABASE_URL nebo SUPABASE_SERVICE_KEY.")
    return RestFenceStore(url, key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable fence (DB, ne soubor).")
    parser.add_argument("action", choices=("key", "claim", "release-never-sent"))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--param-hash", default=None)
    parser.add_argument("--has-send-artifact", action="store_true")
    parser.add_argument("--broker-unchanged", action="store_true")
    args = parser.parse_args(argv)
    param_hash = args.param_hash or compute_param_hash()
    fence_key = make_fence_key(args.session_date, args.symbol, param_hash)
    if args.action == "key":
        print(fence_key)
        return EXIT_OK
    store = store_from_env()
    if args.action == "claim":
        claimed = store.insert_pending(fence_key, args.symbol, args.session_date)
        print(OUTCOME_SKIP if not claimed else STATE_PENDING)
        return EXIT_OK
    state = release_never_sent(
        store,
        fence_key,
        has_send_artifact=args.has_send_artifact,
        broker_unchanged=args.broker_unchanged,
    )
    print(state)
    return EXIT_OK if state == STATE_NEVER_SENT else EXIT_UNCERTAIN


if __name__ == "__main__":
    sys.exit(main())
