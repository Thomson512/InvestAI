"""Exit orchestrator: stopy, reconciliace, daily_pnl, heartbeat. Nesmí volat can_trade()."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.broker_snapshot import is_protective_sell, normalize_ticker
from scripts.shadow_summary import write_step_summary

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SNAPSHOT = REPO_ROOT / "data" / "snapshot.json"
DEFAULT_EVIDENCE = REPO_ROOT / "data" / "exit_evidence.json"
EXIT_LOOP_NAME = "exit-orchestrator"
HTTP_TIMEOUT_SEC = 15


class ExitError(RuntimeError):
    pass


class ControlPlane(Protocol):
    def get_daily_pnl(self, trade_date: str) -> dict | None: ...
    def upsert_daily_pnl(
        self, trade_date: str, opening: float, current: float, peak: float
    ) -> None: ...
    def upsert_heartbeat(self, loop_name: str, last_seen: str) -> None: ...
    def confirmed_symbols(self) -> set[str]: ...
    def confirm_uncertain_for_symbols(self, symbols: set[str]) -> None: ...


@dataclass
class ExitResult:
    outcome: str
    exit_code: int = 0
    extra_summary: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    pnl_written: bool = False


class MemoryControlPlane:
    def __init__(self) -> None:
        self.pnl: dict[str, dict] = {}
        self.heartbeats: dict[str, str] = {}
        self.fences: set[str] = set()
        self.uncertain_symbols: set[str] = set()

    def get_daily_pnl(self, trade_date: str) -> dict | None:
        row = self.pnl.get(trade_date)
        return None if row is None else dict(row)

    def upsert_daily_pnl(
        self, trade_date: str, opening: float, current: float, peak: float
    ) -> None:
        self.pnl[trade_date] = {
            "trade_date": trade_date,
            "opening_equity": opening,
            "current_equity": current,
            "peak_equity": peak,
        }

    def upsert_heartbeat(self, loop_name: str, last_seen: str) -> None:
        self.heartbeats[loop_name] = last_seen

    def confirmed_symbols(self) -> set[str]:
        return set(self.fences)

    def confirm_uncertain_for_symbols(self, symbols: set[str]) -> None:
        self.uncertain_symbols -= {normalize_ticker(name) for name in symbols if name}


class RestControlPlane:
    """daily_pnl / heartbeat / trade_fences přes public view. Klíč nikdy do logu."""

    def __init__(self, url: str, service_key: str, *, http_open: Callable | None = None) -> None:
        self._base = url.rstrip("/") + "/rest/v1"
        self._key = service_key
        self._http_open = http_open or urllib.request.urlopen

    def _headers(self, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _open(self, request: urllib.request.Request) -> object:
        return self._http_open(request, timeout=HTTP_TIMEOUT_SEC)

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        prefer: str | None = None,
    ) -> object:
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=data,
            headers=self._headers(prefer=prefer),
            method=method,
        )
        try:
            with self._open(request) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:240]
            raise ExitError(f"Control plane HTTP {exc.code}: {detail}") from None
        if not body:
            return None
        return json.loads(body)

    def get_daily_pnl(self, trade_date: str) -> dict | None:
        rows = self._request("GET", f"/daily_pnl?trade_date=eq.{quote(trade_date)}")
        if not isinstance(rows, list) or not rows:
            return None
        return rows[0] if isinstance(rows[0], dict) else None

    def upsert_daily_pnl(
        self, trade_date: str, opening: float, current: float, peak: float
    ) -> None:
        payload = json.dumps(
            {
                "trade_date": trade_date,
                "opening_equity": opening,
                "current_equity": current,
                "peak_equity": peak,
            }
        ).encode("utf-8")
        self._request(
            "POST",
            "/daily_pnl",
            data=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def upsert_heartbeat(self, loop_name: str, last_seen: str) -> None:
        payload = json.dumps({"loop_name": loop_name, "last_seen": last_seen}).encode("utf-8")
        self._request(
            "POST",
            "/scheduler_heartbeat",
            data=payload,
            prefer="resolution=merge-duplicates,return=minimal",
        )

    def confirmed_symbols(self) -> set[str]:
        rows = self._request("GET", "/trade_fences?state=eq.CONFIRMED&select=symbol")
        if not isinstance(rows, list):
            return set()
        return {
            normalize_ticker(str(row.get("symbol") or ""))
            for row in rows
            if isinstance(row, dict) and row.get("symbol")
        }

    def confirm_uncertain_for_symbols(self, symbols: set[str]) -> None:
        payload = json.dumps({"state": "CONFIRMED"}).encode("utf-8")
        for symbol in sorted({normalize_ticker(name) for name in symbols if name}):
            self._request(
                "PATCH",
                f"/trade_fences?state=eq.UNCERTAIN&symbol=eq.{quote(symbol)}",
                data=payload,
                prefer="return=minimal",
            )


def default_can_close_position(env: dict[str, str] | None = None) -> tuple[bool, str]:
    source = env if env is not None else os.environ
    url = (source.get("SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return False, "MISSING_CONTROL_PLANE"
    endpoint = url.rstrip("/") + "/rest/v1/rpc/can_close_position"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(endpoint, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False, "CONTROL_PLANE_UNREACHABLE"
    row = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(row, dict):
        return False, "CONTROL_PLANE_UNREACHABLE"
    return bool(row.get("allowed")), str(row.get("reason") or "UNKNOWN")


def control_plane_from_env(env: dict[str, str] | None = None) -> RestControlPlane:
    source = env if env is not None else os.environ
    url = (source.get("SUPABASE_URL") or "").strip()
    key = (source.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        raise ExitError("Chybí SUPABASE_URL nebo SUPABASE_SERVICE_KEY.")
    return RestControlPlane(url, key)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def broker_symbols(snapshot: dict) -> set[str]:
    held: set[str] = set()
    for position in snapshot.get("positions") or []:
        if float(position.get("quantity") or 0) == 0:
            continue
        ticker = normalize_ticker(str(position.get("ticker") or ""))
        if ticker:
            held.add(ticker)
    return held


def missing_stops(snapshot: dict) -> list[str]:
    """Každá otevřená pozice musí mít aktivní SELL STOP. Bez smoke exemption."""
    orders = snapshot.get("active_orders") or []
    missing: list[str] = []
    for position in snapshot.get("positions") or []:
        ticker = str(position.get("ticker") or "")
        if not ticker or float(position.get("quantity") or 0) == 0:
            continue
        if any(is_protective_sell(order, ticker) for order in orders):
            continue
        missing.append(normalize_ticker(ticker))
    return missing


def evidence_symbols(book: dict | None, buyer_evidence: dict | None) -> set[str]:
    symbols: set[str] = set()
    for row in (book or {}).get("positions") or []:
        name = normalize_ticker(str(row.get("symbol") or row.get("ticker") or ""))
        if name:
            symbols.add(name)
    if buyer_evidence and str(buyer_evidence.get("outcome") or "").startswith("ORDER_SUBMITTED"):
        plan = buyer_evidence.get("plan") or {}
        name = normalize_ticker(str(plan.get("symbol") or ""))
        if name:
            symbols.add(name)
    return symbols


def reconcile(broker: set[str], expected: set[str]) -> dict:
    return {
        "matched": sorted(broker & expected),
        "broker_only": sorted(broker - expected),
        "evidence_only": sorted(expected - broker),
    }


def merge_pnl(existing: dict | None, current: float) -> tuple[float, float]:
    if existing is None:
        return current, current
    opening = float(existing["opening_equity"])
    peak = max(float(existing.get("peak_equity") or current), current)
    return opening, peak


def account_equity(snapshot: dict) -> float:
    value = snapshot.get("account_total_value")
    if value is None:
        raise ExitError("Chybí account_total_value — daily_pnl se nezapíše.")
    equity = float(value)
    if equity <= 0:
        raise ExitError("account_total_value musí být > 0.")
    return equity


def default_place_stop(
    symbol: str,
    *,
    session_date: str,
    snapshot: dict,
    dataset: dict | None = None,
):
    """Jeden fenced SELL STOP. Fence klíč STOP:… — jiný než nákupní fence."""
    from scripts.protective_stop_guard import bars_for_symbol, run_guard

    return run_guard(
        symbol=symbol,
        session_date=session_date,
        snapshot=snapshot,
        dry_run=False,
        bars=bars_for_symbol(dataset, symbol),
    )


def first_line(result: ExitResult) -> str:
    return f"EXIT: {result.outcome}"


def run_exit(
    *,
    session_date: str,
    snapshot: dict,
    store: ControlPlane,
    book: dict | None = None,
    buyer_evidence: dict | None = None,
    can_close_fn: Callable[[], tuple[bool, str]] | None = None,
    now: datetime | None = None,
    place_stop_fn: Callable[[str], object] | None = None,
) -> ExitResult:
    now = now or datetime.now(timezone.utc)
    allowed, close_reason = (can_close_fn or default_can_close_position)()
    if not allowed:
        return ExitResult(
            outcome=f"CLOSE_BLOCKED:{close_reason}",
            exit_code=1,
            evidence={"close_allowed": False, "close_reason": close_reason},
        )

    equity = account_equity(snapshot)
    unprotected = missing_stops(snapshot)
    expected = evidence_symbols(book, buyer_evidence) | store.confirmed_symbols()
    drift = reconcile(broker_symbols(snapshot), expected)

    existing = store.get_daily_pnl(session_date)
    opening, peak = merge_pnl(existing, equity)
    store.upsert_daily_pnl(session_date, opening, equity, peak)
    seen = now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_heartbeat(EXIT_LOOP_NAME, seen)

    extra: list[str] = []
    if unprotected and place_stop_fn is not None:
        try:
            guard = place_stop_fn(unprotected[0])
            extra.append(f"AUTO-STOP: {getattr(guard, 'outcome', guard)}")
        except Exception as exc:
            extra.append(f"AUTO-STOP: FAIL {exc}")
    protected = broker_symbols(snapshot) - set(unprotected)
    if protected:
        store.confirm_uncertain_for_symbols(protected)
    if unprotected:
        extra.extend(
            f"FIX: gh workflow run protective-stop-guard.yml -f symbol={name}"
            for name in unprotected
        )
        outcome = f"UNPROTECTED {' '.join(unprotected)}"
    elif drift["broker_only"] or drift["evidence_only"]:
        outcome = (
            f"DRIFT broker_only={','.join(drift['broker_only']) or '-'} "
            f"evidence_only={','.join(drift['evidence_only']) or '-'}"
        )
    else:
        outcome = (
            f"OK session={session_date} equity={equity:g} unprotected=0 drift=0"
        )

    return ExitResult(
        outcome=outcome,
        extra_summary=extra,
        pnl_written=True,
        evidence={
            "session_date": session_date,
            "close_allowed": True,
            "close_reason": close_reason,
            "unprotected": unprotected,
            "reconcile": drift,
            "daily_pnl": {
                "trade_date": session_date,
                "opening_equity": opening,
                "current_equity": equity,
                "peak_equity": peak,
            },
            "heartbeat": {"loop_name": EXIT_LOOP_NAME, "last_seen": seen},
        },
    )


def write_evidence(path: Path, result: ExitResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "outcome": result.outcome,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pnl_written": result.pnl_written,
        "extra_summary": result.extra_summary,
        **result.evidence,
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_noop_evidence(path: Path, session_date: str, reason: str) -> ExitResult:
    result = ExitResult(
        outcome="NO-OP MARKET_CLOSED",
        evidence={"session_date": session_date, "reason": reason or "weekend/holiday"},
        pnl_written=False,
    )
    write_evidence(path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exit orchestrator (PnL + heartbeat).")
    parser.add_argument("--session", required=True)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--book", type=Path, default=None)
    parser.add_argument("--buyer-evidence", type=Path, default=None)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--noop", action="store_true")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    if args.noop:
        result = write_noop_evidence(args.evidence, args.session, args.reason)
        write_step_summary(first_line(result), [f"NO-OP: {args.reason or 'NYSE closed'}. daily_pnl not written."])
        print(first_line(result))
        return 0

    try:
        snapshot = load_json(args.snapshot)
        book = load_json(args.book) if args.book and args.book.is_file() else None
        buyer = (
            load_json(args.buyer_evidence)
            if args.buyer_evidence and args.buyer_evidence.is_file()
            else None
        )
        dataset = load_json(args.dataset) if args.dataset and args.dataset.is_file() else None

        def place(symbol: str):
            return default_place_stop(
                symbol,
                session_date=args.session,
                snapshot=snapshot,
                dataset=dataset,
            )

        result = run_exit(
            session_date=args.session,
            snapshot=snapshot,
            store=control_plane_from_env(),
            book=book,
            buyer_evidence=buyer,
            place_stop_fn=place,
        )
    except RuntimeError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        write_evidence(
            args.evidence,
            ExitResult(outcome=f"FAIL-CLOSED {exc}", exit_code=1, pnl_written=False),
        )
        write_step_summary(f"EXIT: FAIL-CLOSED {exc}")
        return 1

    write_evidence(args.evidence, result)
    write_step_summary(first_line(result), result.extra_summary or None)
    print(first_line(result))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
