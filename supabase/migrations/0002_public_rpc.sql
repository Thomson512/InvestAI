-- PostgREST ve výchozím stavu vidí jen schema public.
-- Wrappery, aby can_trade() šlo volat jako /rest/v1/rpc/can_trade.

CREATE OR REPLACE FUNCTION public.can_trade()
RETURNS TABLE (allowed boolean, reason text)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, trading, public, pg_temp
AS $$
  SELECT * FROM trading.can_trade();
$$;

CREATE OR REPLACE FUNCTION public.can_close_position()
RETURNS TABLE (allowed boolean, reason text)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, trading, public, pg_temp
AS $$
  SELECT * FROM trading.can_close_position();
$$;

REVOKE ALL ON FUNCTION public.can_trade() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.can_close_position() FROM PUBLIC;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    REVOKE ALL ON FUNCTION public.can_trade() FROM anon;
    REVOKE ALL ON FUNCTION public.can_close_position() FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    REVOKE ALL ON FUNCTION public.can_trade() FROM authenticated;
    REVOKE ALL ON FUNCTION public.can_close_position() FROM authenticated;
  END IF;
END
$$;

GRANT EXECUTE ON FUNCTION public.can_trade() TO service_role;
GRANT EXECUTE ON FUNCTION public.can_close_position() TO service_role;
