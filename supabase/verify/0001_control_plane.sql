-- Ověření control plane. Celé v transakci, na konci ROLLBACK.
-- Spouštěj v SQL Editoru pod service_role / postgres (obchází RLS).

BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'trading') THEN
    RAISE EXCEPTION 'schema trading missing';
  END IF;
END
$$;

-- RLS zapnuté, žádné policy
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'trading'
      AND c.relkind = 'r'
      AND c.relrowsecurity IS NOT TRUE
  LOOP
    RAISE EXCEPTION 'RLS off: %', r.relname;
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'trading'
  ) THEN
    RAISE EXCEPTION 'unexpected RLS policy on trading.*';
  END IF;
END
$$;

DELETE FROM trading.trade_fences;
DELETE FROM trading.scheduler_heartbeat;
DELETE FROM trading.daily_pnl;
DELETE FROM trading.risk_limits;
UPDATE trading.system_control SET trading_enabled = false WHERE id = 1;

-- 1) kill switch
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'KILL_SWITCH_OFF' THEN
    RAISE EXCEPTION 'expected KILL_SWITCH_OFF, got % %', r.allowed, r.reason;
  END IF;
END
$$;

-- výstup musí jít i při vypnutém switchi
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_close_position();
  IF r.allowed IS NOT TRUE OR r.reason <> 'OK' THEN
    RAISE EXCEPTION 'can_close_position must stay open, got % %', r.allowed, r.reason;
  END IF;
END
$$;

UPDATE trading.system_control SET trading_enabled = true WHERE id = 1;

-- 2) chybí limity
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'MISSING_RISK_LIMITS' THEN
    RAISE EXCEPTION 'expected MISSING_RISK_LIMITS, got % %', r.allowed, r.reason;
  END IF;
END
$$;

INSERT INTO trading.risk_limits (id, daily_loss_limit_pct, max_drawdown_pct, heartbeat_max_age_min)
VALUES (1, 2.0, 8.0, 15);

-- 3) chybí dnešní PnL
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'NO_PNL_RECORD_TODAY' THEN
    RAISE EXCEPTION 'expected NO_PNL_RECORD_TODAY, got % %', r.allowed, r.reason;
  END IF;
END
$$;

INSERT INTO trading.daily_pnl (trade_date, opening_equity, current_equity, peak_equity)
VALUES (trading.session_date(), 0, 0, 0);

-- 4) neplatné opening equity
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'INVALID_OPENING_EQUITY' THEN
    RAISE EXCEPTION 'expected INVALID_OPENING_EQUITY, got % %', r.allowed, r.reason;
  END IF;
END
$$;

UPDATE trading.daily_pnl
SET opening_equity = 100000, current_equity = 97000, peak_equity = 100000
WHERE trade_date = trading.session_date();

-- 5) denní ztráta 3 % >= 2 %
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'DAILY_LOSS_LIMIT' THEN
    RAISE EXCEPTION 'expected DAILY_LOSS_LIMIT, got % %', r.allowed, r.reason;
  END IF;
END
$$;

-- výstup i po denním limitu
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_close_position();
  IF r.allowed IS NOT TRUE THEN
    RAISE EXCEPTION 'close blocked after daily loss';
  END IF;
END
$$;

UPDATE trading.daily_pnl
SET current_equity = 99000, peak_equity = 110000
WHERE trade_date = trading.session_date();

-- 6) drawdown (110000-99000)/110000 = 10 % >= 8 %
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'MAX_DRAWDOWN' THEN
    RAISE EXCEPTION 'expected MAX_DRAWDOWN, got % %', r.allowed, r.reason;
  END IF;
END
$$;

UPDATE trading.daily_pnl
SET current_equity = 100000, peak_equity = 100000
WHERE trade_date = trading.session_date();

-- 7) heartbeat chybí / stale
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'HEARTBEAT_STALE' THEN
    RAISE EXCEPTION 'expected HEARTBEAT_STALE (missing), got % %', r.allowed, r.reason;
  END IF;
END
$$;

INSERT INTO trading.scheduler_heartbeat (loop_name, last_seen)
VALUES ('live_loop', now() - interval '2 hours');

DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'HEARTBEAT_STALE' THEN
    RAISE EXCEPTION 'expected HEARTBEAT_STALE (old), got % %', r.allowed, r.reason;
  END IF;
END
$$;

UPDATE trading.scheduler_heartbeat SET last_seen = now() WHERE loop_name = 'live_loop';

INSERT INTO trading.trade_fences (fence_key, symbol, session_date, state)
VALUES ('2026-09-07:AAPL:test', 'AAPL', trading.session_date(), 'UNCERTAIN');

-- 8) unresolved fence
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed OR r.reason <> 'UNRESOLVED_FENCE' THEN
    RAISE EXCEPTION 'expected UNRESOLVED_FENCE, got % %', r.allowed, r.reason;
  END IF;
END
$$;

UPDATE trading.trade_fences SET state = 'CONFIRMED' WHERE fence_key = '2026-09-07:AAPL:test';

-- 9) zelená
DO $$
DECLARE
  r record;
BEGIN
  SELECT * INTO r FROM trading.can_trade();
  IF r.allowed IS NOT TRUE OR r.reason <> 'OK' THEN
    RAISE EXCEPTION 'expected OK, got % %', r.allowed, r.reason;
  END IF;
END
$$;

ROLLBACK;
