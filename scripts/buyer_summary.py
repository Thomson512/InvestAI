"""První řádek job summary: OUTCOME: <hodnota>. Streak varování bez pádu jobu."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.buyer import (
    DEFAULT_EVIDENCE,
    OUTCOME_MARKET_CLOSED,
    UNRESOLVED_FENCE_ERROR,
    is_healthy_outcome,
    is_official_outcome,
)
from scripts.shadow_summary import write_step_summary

DEFAULT_HISTORY = Path(__file__).resolve().parent.parent / "data" / "buyer_history.jsonl"
STREAK_HOURS = 24


def outcome_line(outcome: str) -> str:
    return f"OUTCOME: {outcome}"


def parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_history(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def append_history(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def streak_should_warn(history: list[dict], now: datetime, hours: int = STREAK_HOURS) -> bool:
    cutoff = now - timedelta(hours=hours)
    recent = []
    for row in history:
        stamp = row.get("at") or row.get("generated_at")
        if not stamp:
            continue
        if parse_stamp(str(stamp)) >= cutoff:
            recent.append(row)
    if not recent:
        return False
    return all(not is_healthy_outcome(str(row.get("outcome") or "")) for row in recent)


def streak_warning(history: list[dict], now: datetime) -> str | None:
    if not streak_should_warn(history, now):
        return None
    last = str(history[-1].get("outcome") or "?") if history else "?"
    return (
        f"::warning::Buyer streak: {STREAK_HOURS}h bez ORDER_SUBMITTED / "
        f"NO_CANDIDATES / MARKET_CLOSED (poslední: {last})"
    )


def write_closed_evidence(path: Path, session_date: str, now: datetime) -> dict:
    document = {
        "outcome": OUTCOME_MARKET_CLOSED,
        "session_date": session_date,
        "generated_at": now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "official": True,
        "extra_summary": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Buyer job summary + streak warning.")
    parser.add_argument("--outcome", default=None)
    parser.add_argument("--session", default="")
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Jen zapsat evidence, summary až v dalším kroku.",
    )
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.outcome:
        if not is_official_outcome(args.outcome):
            print(f"Neplatný OUTCOME: {args.outcome}", file=sys.stderr)
            return 1
        document = write_closed_evidence(args.evidence, args.session, now)
        document["outcome"] = args.outcome
        args.evidence.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if args.prepare_only:
            return 0
    elif args.evidence.is_file():
        document = json.loads(args.evidence.read_text(encoding="utf-8"))
    else:
        print(f"Chybí evidence: {args.evidence}", file=sys.stderr)
        return 1

    outcome = str(document.get("outcome") or "")
    extra = [str(line) for line in (document.get("extra_summary") or []) if line]
    official = bool(document.get("official", is_official_outcome(outcome)))

    if official and is_official_outcome(outcome):
        first = outcome_line(outcome)
        append_history(
            args.history,
            {
                "at": document.get("generated_at") or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outcome": outcome,
                "session_date": document.get("session_date"),
            },
        )
    else:
        first = f"FAIL-CLOSED: {outcome}"

    history = load_history(args.history)
    warning = streak_warning(history, now)
    if warning:
        print(warning)
        extra = [*extra, warning]
    if "UNRESOLVED_FENCE" in outcome:
        print(UNRESOLVED_FENCE_ERROR)
        extra = [*extra, UNRESOLVED_FENCE_ERROR]

    write_step_summary(first, extra or None)
    if "UNRESOLVED_FENCE" in outcome:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
