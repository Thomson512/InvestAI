-- PostgREST vidí jen public. daily_pnl a heartbeat pro exit orchestrator.

CREATE OR REPLACE VIEW public.daily_pnl
WITH (security_invoker = true)
AS
SELECT
  trade_date,
  opening_equity,
  current_equity,
  peak_equity,
  updated_at
FROM trading.daily_pnl;

CREATE OR REPLACE VIEW public.scheduler_heartbeat
WITH (security_invoker = true)
AS
SELECT loop_name, last_seen
FROM trading.scheduler_heartbeat;

REVOKE ALL ON TABLE public.daily_pnl FROM PUBLIC;
REVOKE ALL ON TABLE public.scheduler_heartbeat FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE public.daily_pnl FROM anon;
    REVOKE ALL ON TABLE public.scheduler_heartbeat FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON TABLE public.daily_pnl FROM authenticated;
    REVOKE ALL ON TABLE public.scheduler_heartbeat FROM authenticated;
  END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.daily_pnl TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.scheduler_heartbeat TO service_role;
