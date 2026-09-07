from pathlib import Path

MIGRATION = Path(__file__).resolve().parent.parent / "supabase" / "migrations" / "0001_control_plane.sql"
VERIFY = Path(__file__).resolve().parent.parent / "supabase" / "verify" / "0001_control_plane.sql"

REASONS = [
    "KILL_SWITCH_OFF",
    "MISSING_RISK_LIMITS",
    "NO_PNL_RECORD_TODAY",
    "INVALID_OPENING_EQUITY",
    "DAILY_LOSS_LIMIT",
    "MAX_DRAWDOWN",
    "HEARTBEAT_STALE",
    "UNRESOLVED_FENCE",
]


def test_migration_has_tables_rls_and_no_policies() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "system_control",
        "risk_limits",
        "daily_pnl",
        "trade_fences",
        "scheduler_heartbeat",
    ):
        assert f"CREATE TABLE trading.{table}" in sql
        assert f"ALTER TABLE trading.{table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE trading.{table} FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY" not in sql
    assert "trading_enabled boolean NOT NULL DEFAULT false" in sql
    assert "PENDING" in sql and "UNCERTAIN" in sql and "NEVER_SENT" in sql


def test_can_trade_reasons_in_order() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    positions = [sql.index(f"reason := '{name}'") for name in REASONS]
    assert positions == sorted(positions)


def test_close_ignores_kill_switch_and_daily_loss() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION trading.can_close_position()" in sql
    assert "SELECT true AS allowed, 'OK'::text AS reason" in sql
    close_fn = sql.split("FUNCTION trading.can_close_position()")[1]
    assert "KILL_SWITCH" not in close_fn
    assert "daily_loss" not in close_fn.lower() or "ignoruje" in sql.lower()


def test_verify_script_covers_fail_closed_path() -> None:
    sql = VERIFY.read_text(encoding="utf-8")
    for reason in REASONS:
        assert reason in sql
    assert "can_close_position" in sql
    assert "ROLLBACK" in sql
