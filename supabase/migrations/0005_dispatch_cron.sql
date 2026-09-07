-- pg_cron volá Edge Function dispatch-loop. Tajemství jen ve Vault, ne v tomto souboru.
-- Dashboard → Edge Functions → dispatch-loop secrets: GH_DISPATCH_TOKEN (actions:write)
-- Vault secrets: dispatch_loop_url, dispatch_loop_key

CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

CREATE OR REPLACE FUNCTION trading.dispatch_workflow(p_workflow text)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, vault, net, pg_temp
AS $$
DECLARE
  v_url text;
  v_key text;
  v_id bigint;
BEGIN
  IF p_workflow NOT IN ('buyer.yml', 'exit-orchestrator.yml') THEN
    RAISE EXCEPTION 'Workflow mimo allowlist: %', p_workflow;
  END IF;

  SELECT ds.decrypted_secret INTO v_url
  FROM vault.decrypted_secrets ds
  WHERE ds.name = 'dispatch_loop_url';

  SELECT ds.decrypted_secret INTO v_key
  FROM vault.decrypted_secrets ds
  WHERE ds.name = 'dispatch_loop_key';

  IF v_url IS NULL OR btrim(v_url) = '' OR v_key IS NULL OR btrim(v_key) = '' THEN
    RAISE EXCEPTION 'Chybí Vault secret dispatch_loop_url nebo dispatch_loop_key';
  END IF;

  SELECT net.http_post(
    url := v_url,
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'Authorization', 'Bearer ' || v_key
    ),
    body := jsonb_build_object('workflow', p_workflow)
  ) INTO v_id;

  RETURN v_id;
END;
$$;

REVOKE ALL ON FUNCTION trading.dispatch_workflow(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION trading.dispatch_workflow(text) TO postgres;

DO $$
DECLARE
  j record;
BEGIN
  FOR j IN SELECT jobid FROM cron.job WHERE jobname IN ('dispatch-buyer', 'dispatch-exit')
  LOOP
    PERFORM cron.unschedule(j.jobid);
  END LOOP;
END
$$;

SELECT cron.schedule(
  'dispatch-buyer',
  '*/10 13-20 * * 1-5',
  $$SELECT trading.dispatch_workflow('buyer.yml')$$
);

SELECT cron.schedule(
  'dispatch-exit',
  '*/10 13-20 * * 1-5',
  $$SELECT trading.dispatch_workflow('exit-orchestrator.yml')$$
);
