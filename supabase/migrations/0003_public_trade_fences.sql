-- PostgREST ve výchozím stavu vidí jen schema public.
-- Accept-Profile: trading → HTTP 406 (PGRST106).
-- Tenký view, aby /rest/v1/trade_fences šlo přes service_role.

CREATE OR REPLACE VIEW public.trade_fences
WITH (security_invoker = true)
AS
SELECT
  fence_key,
  symbol,
  session_date,
  state,
  order_id,
  created_at
FROM trading.trade_fences;

REVOKE ALL ON TABLE public.trade_fences FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON TABLE public.trade_fences FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON TABLE public.trade_fences FROM authenticated;
  END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.trade_fences TO service_role;
