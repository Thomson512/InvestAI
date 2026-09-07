"""První řádek job summary: SHADOW: N trades, M open, PnL X CZK."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.session_gate import shadow_summary_line

DEFAULT_REPORT = Path(__file__).resolve().parent.parent / "data" / "report.json"


def line_from_report(report: dict) -> str:
    base = report["scenarios"]["baseline"]
    return shadow_summary_line(
        base["completed_trades"],
        base["open_positions"],
        base["realized_pnl"],
    )


def write_step_summary(first_line: str, extra_lines: list[str] | None = None) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        print(first_line)
        return
    body = first_line + "\n"
    if extra_lines:
        body += "\n".join(extra_lines) + "\n"
    Path(path).write_text(body, encoding="utf-8")
    print(first_line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--noop", action="store_true")
    parser.add_argument("--reason", type=str, default="")
    args = parser.parse_args(argv)

    extra: list[str] = []
    if args.noop:
        line = shadow_summary_line(0, 0, 0)
        extra.append(f"NO-OP: NYSE closed ({args.reason or 'weekend/holiday'}). Dataset not written.")
    else:
        if not args.report.is_file():
            print(f"Chybí report: {args.report}", file=sys.stderr)
            return 1
        line = line_from_report(json.loads(args.report.read_text(encoding="utf-8")))
    write_step_summary(line, extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
