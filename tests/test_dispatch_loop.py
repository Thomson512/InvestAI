from __future__ import annotations

from pathlib import Path

BUYER = Path(".github/workflows/buyer.yml").read_text(encoding="utf-8")
EXIT = Path(".github/workflows/exit-orchestrator.yml").read_text(encoding="utf-8")
FN = Path("supabase/functions/dispatch-loop/index.ts").read_text(encoding="utf-8")
CRON = Path("supabase/migrations/0005_dispatch_cron.sql").read_text(encoding="utf-8")


def test_native_schedules_disabled() -> None:
    assert "schedule:" not in BUYER
    assert "schedule:" not in EXIT
    assert "workflow_dispatch:" in BUYER
    assert "workflow_dispatch:" in EXIT


def test_dispatch_function_calls_github_and_hides_token() -> None:
    assert "/actions/workflows/" in FN
    assert "/dispatches" in FN
    assert "GH_DISPATCH_TOKEN" in FN
    assert "console.log" in FN
    assert "token" not in FN.lower().split("console.log")[1]
    assert "buyer.yml" in FN
    assert "exit-orchestrator.yml" in FN


def test_pg_cron_schedules_buyer() -> None:
    assert "cron.schedule" in CRON
    assert "dispatch-buyer" in CRON
    assert "*/10 13-20 * * 1-5" in CRON
    assert "net.http_post" in CRON
    assert "ghp_" not in CRON
    assert "eyJ" not in CRON
    assert "dispatch_workflow('buyer.yml')" in CRON
