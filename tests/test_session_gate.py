from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from scripts.session_gate import (
    evaluate_gate,
    nyse_session_date,
    shadow_summary_line,
)
from scripts.shadow_summary import line_from_report, write_step_summary


def test_weekend_is_closed() -> None:
    saturday = date(2026, 9, 5)
    result = evaluate_gate(saturday, is_open=lambda day: False)
    assert result["open"] is False
    assert result["reason"] == "weekend"


def test_holiday_weekday_is_closed() -> None:
    new_year = date(2026, 1, 1)
    result = evaluate_gate(new_year, is_open=lambda day: False)
    assert result["open"] is False
    assert result["reason"] == "holiday"


def test_regular_session_is_open() -> None:
    tuesday = date(2026, 9, 8)
    result = evaluate_gate(tuesday, is_open=lambda day: True)
    assert result["open"] is True
    assert result["reason"] is None


def test_gate_does_not_write_dataset(tmp_path: Path, monkeypatch) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text('{"keep": true}\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    evaluate_gate(date(2026, 9, 5), is_open=lambda day: False)
    assert dataset.read_text(encoding="utf-8") == '{"keep": true}\n'


def test_summary_first_line_format() -> None:
    line = shadow_summary_line(12, 3, -140.5)
    assert line == "SHADOW: 12 trades, 3 open, PnL -140.5 CZK"
    report_line = line_from_report(
        {
            "scenarios": {
                "baseline": {
                    "completed_trades": 4,
                    "open_positions": 1,
                    "realized_pnl": 20.0,
                }
            }
        }
    )
    assert report_line == "SHADOW: 4 trades, 1 open, PnL 20.0 CZK"


def test_step_summary_starts_with_shadow_line(tmp_path: Path, monkeypatch) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    write_step_summary("SHADOW: 0 trades, 0 open, PnL 0 CZK", ["NO-OP: NYSE closed"])
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("SHADOW: 0 trades, 0 open, PnL 0 CZK\n")


def test_session_date_uses_new_york() -> None:
    # 2026-09-08 02:30 UTC = still 2026-09-07 in New York
    now = datetime(2026, 9, 8, 2, 30, tzinfo=timezone.utc)
    assert nyse_session_date(now) == date(2026, 9, 7)
