"""NYSE session gate. Zavřeno = no-op, dataset se nesmí přepsat."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLOSED_REASONS = ("weekend", "holiday")


class SessionGateError(RuntimeError):
    pass


def nyse_tz() -> ZoneInfo:
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError as exc:
        raise SessionGateError("Chybí tzdata (IANA). pip install tzdata") from exc


def nyse_session_date(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(nyse_tz()).date()


def nyse_is_open(day: date, *, is_open: Callable[[date], bool] | None = None) -> bool:
    if is_open is not None:
        return is_open(day)
    try:
        import pandas_market_calendars as mcal
    except ImportError as exc:
        raise SessionGateError("Chybí pandas_market_calendars.") from exc
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(start_date=day.isoformat(), end_date=day.isoformat())
    return not schedule.empty


def write_github_output(open_session: bool, session: date, reason: str | None) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return
    with Path(output).open("a", encoding="utf-8") as handle:
        handle.write(
            f"open={'true' if open_session else 'false'}\n"
            f"session={session.isoformat()}\n"
            f"reason={reason or ''}\n"
        )


def evaluate_gate(day: date, *, is_open: Callable[[date], bool] | None = None) -> dict:
    open_session = nyse_is_open(day, is_open=is_open)
    reason = None
    if not open_session:
        reason = "weekend" if day.weekday() >= 5 else "holiday"
    return {
        "open": open_session,
        "session": day.isoformat(),
        "reason": reason,
    }


def shadow_summary_line(completed_trades: int, open_positions: int, pnl_czk: object) -> str:
    return f"SHADOW: {completed_trades} trades, {open_positions} open, PnL {pnl_czk} CZK"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NYSE session gate (no-op mimo seanci).")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (default: dnes ET)")
    args = parser.parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else nyse_session_date()
    result = evaluate_gate(day)
    write_github_output(result["open"], day, result["reason"])
    if result["open"]:
        print(f"OPEN {result['session']}")
        return 0
    print(f"CLOSED {result['session']} reason={result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
