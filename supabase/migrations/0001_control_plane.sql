-- Control plane: fail-closed vstupy, výstupy vždy možné.
-- Přístup jen service_role (RLS zapnuté, žádné policy).

CREATE SCHEMA IF NOT EXISTS trading;

REVOKE ALL ON SCHEMA trading FROM PUBLIC;
GRANT USAGE ON SCHEMA trading TO service_role;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON SCHEMA trading FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON SCHEMA trading FROM authenticated;
  END IF;
END
$$;

CREATE TYPE trading.fence_state AS ENUM (
  'PENDING',
  'SENT',
  'CONFIRMED',
  'UNCERTAIN',
  'NEVER_SENT'
);

CREATE TABLE trading.system_control (
  id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  trading_enabled boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trading.risk_limits (
  id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  daily_loss_limit_pct numeric NOT NULL CHECK (daily_loss_limit_pct > 0),
  max_drawdown_pct numeric NOT NULL CHECK (max_drawdown_pct > 0),
  heartbeat_max_age_min integer NOT NULL CHECK (heartbeat_max_age_min > 0)
);

CREATE TABLE trading.daily_pnl (
  trade_date date PRIMARY KEY,
  opening_equity numeric NOT NULL,
  current_equity numeric NOT NULL,
  peak_equity numeric NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trading.trade_fences (
  fence_key text PRIMARY KEY,
  symbol text NOT NULL,
  session_date date NOT NULL,
  state trading.fence_state NOT NULL,
  order_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trading.scheduler_heartbeat (
  loop_name text PRIMARY KEY,
  last_seen timestamptz NOT NULL
);

CREATE INDEX trade_fences_state_idx ON trading.trade_fences (state);
CREATE INDEX trade_fences_session_idx ON trading.trade_fences (session_date);

CREATE OR REPLACE FUNCTION trading.touch_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE TRIGGER system_control_touch
BEFORE UPDATE ON trading.system_control
FOR EACH ROW EXECUTE FUNCTION trading.touch_updated_at();

CREATE TRIGGER daily_pnl_touch
BEFORE UPDATE ON trading.daily_pnl
FOR EACH ROW EXECUTE FUNCTION trading.touch_updated_at();

ALTER TABLE trading.system_control ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading.system_control FORCE ROW LEVEL SECURITY;
ALTER TABLE trading.risk_limits ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading.risk_limits FORCE ROW LEVEL SECURITY;
ALTER TABLE trading.daily_pnl ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading.daily_pnl FORCE ROW LEVEL SECURITY;
ALTER TABLE trading.trade_fences ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading.trade_fences FORCE ROW LEVEL SECURITY;
ALTER TABLE trading.scheduler_heartbeat ENABLE ROW LEVEL SECURITY;
ALTER TABLE trading.scheduler_heartbeat FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE trading.system_control FROM PUBLIC;
REVOKE ALL ON TABLE trading.risk_limits FROM PUBLIC;
REVOKE ALL ON TABLE trading.daily_pnl FROM PUBLIC;
REVOKE ALL ON TABLE trading.trade_fences FROM PUBLIC;
REVOKE ALL ON TABLE trading.scheduler_heartbeat FROM PUBLIC;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trading.system_control TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trading.risk_limits TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trading.daily_pnl TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trading.trade_fences TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE trading.scheduler_heartbeat TO service_role;

INSERT INTO trading.system_control (id, trading_enabled)
VALUES (1, false)
ON CONFLICT (id) DO NOTHING;

-- Obchodní den v America/New_York (US equity). Chybějící řádek = fail-closed.
CREATE OR REPLACE FUNCTION trading.session_date()
RETURNS date
LANGUAGE sql
STABLE
SET search_path = pg_catalog, pg_temp
AS $$
  SELECT (timezone('America/New_York', now()))::date;
$$;

CREATE OR REPLACE FUNCTION trading.can_trade()
RETURNS TABLE (allowed boolean, reason text)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, trading, pg_temp
AS $$
DECLARE
  v_enabled boolean;
  v_limits trading.risk_limits%ROWTYPE;
  v_pnl trading.daily_pnl%ROWTYPE;
  v_today date;
  v_loss_pct numeric;
  v_dd_pct numeric;
  v_fresh integer;
BEGIN
  v_today := trading.session_date();

  SELECT sc.trading_enabled INTO v_enabled
  FROM trading.system_control sc
  WHERE sc.id = 1;

  IF v_enabled IS DISTINCT FROM true THEN
    allowed := false;
    reason := 'KILL_SWITCH_OFF';
    RETURN NEXT;
    RETURN;
  END IF;

  SELECT * INTO v_limits
  FROM trading.risk_limits rl
  WHERE rl.id = 1;

  IF NOT FOUND
     OR v_limits.daily_loss_limit_pct IS NULL
     OR v_limits.max_drawdown_pct IS NULL
     OR v_limits.heartbeat_max_age_min IS NULL
     OR v_limits.daily_loss_limit_pct <= 0
     OR v_limits.max_drawdown_pct <= 0
     OR v_limits.heartbeat_max_age_min <= 0 THEN
    allowed := false;
    reason := 'MISSING_RISK_LIMITS';
    RETURN NEXT;
    RETURN;
  END IF;

  SELECT * INTO v_pnl
  FROM trading.daily_pnl p
  WHERE p.trade_date = v_today;

  IF NOT FOUND THEN
    allowed := false;
    reason := 'NO_PNL_RECORD_TODAY';
    RETURN NEXT;
    RETURN;
  END IF;

  IF v_pnl.opening_equity IS NULL OR v_pnl.opening_equity <= 0 THEN
    allowed := false;
    reason := 'INVALID_OPENING_EQUITY';
    RETURN NEXT;
    RETURN;
  END IF;

  IF v_pnl.current_equity IS NULL THEN
    allowed := false;
    reason := 'DAILY_LOSS_LIMIT';
    RETURN NEXT;
    RETURN;
  END IF;

  v_loss_pct := (v_pnl.opening_equity - v_pnl.current_equity)
                / v_pnl.opening_equity * 100;
  IF v_loss_pct >= v_limits.daily_loss_limit_pct THEN
    allowed := false;
    reason := 'DAILY_LOSS_LIMIT';
    RETURN NEXT;
    RETURN;
  END IF;

  IF v_pnl.peak_equity IS NULL OR v_pnl.peak_equity <= 0 OR v_pnl.current_equity IS NULL THEN
    allowed := false;
    reason := 'MAX_DRAWDOWN';
    RETURN NEXT;
    RETURN;
  END IF;

  v_dd_pct := (v_pnl.peak_equity - v_pnl.current_equity)
              / v_pnl.peak_equity * 100;
  IF v_dd_pct >= v_limits.max_drawdown_pct THEN
    allowed := false;
    reason := 'MAX_DRAWDOWN';
    RETURN NEXT;
    RETURN;
  END IF;

  SELECT count(*) INTO v_fresh
  FROM trading.scheduler_heartbeat h
  WHERE h.last_seen IS NOT NULL
    AND h.last_seen <= now()
    AND h.last_seen >= now() - make_interval(mins => v_limits.heartbeat_max_age_min);

  IF v_fresh IS NULL OR v_fresh = 0 THEN
    allowed := false;
    reason := 'HEARTBEAT_STALE';
    RETURN NEXT;
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM trading.trade_fences f
    WHERE f.state = 'UNCERTAIN'
  ) THEN
    allowed := false;
    reason := 'UNRESOLVED_FENCE';
    RETURN NEXT;
    RETURN;
  END IF;

  allowed := true;
  reason := 'OK';
  RETURN NEXT;
END;
$$;

-- Výstup z pozice: ignoruje kill switch i denní ztrátu. Vždy dovoleno.
-- Jinak by vypnutí trading_enabled zamklo otevřené pozice.
CREATE OR REPLACE FUNCTION trading.can_close_position()
RETURNS TABLE (allowed boolean, reason text)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
  SELECT true AS allowed, 'OK'::text AS reason;
$$;

REVOKE ALL ON FUNCTION trading.session_date() FROM PUBLIC;
REVOKE ALL ON FUNCTION trading.can_trade() FROM PUBLIC;
REVOKE ALL ON FUNCTION trading.can_close_position() FROM PUBLIC;
REVOKE ALL ON FUNCTION trading.touch_updated_at() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION trading.session_date() TO service_role;
GRANT EXECUTE ON FUNCTION trading.can_trade() TO service_role;
GRANT EXECUTE ON FUNCTION trading.can_close_position() TO service_role;
