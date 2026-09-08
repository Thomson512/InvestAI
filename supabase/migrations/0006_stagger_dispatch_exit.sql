-- Posun dispatch-exit o 1 minutu proti buyeru.
-- Stejná sekunda: T212 429 + race na runtime/evidence.

DO $$
DECLARE
  j record;
BEGIN
  FOR j IN SELECT jobid FROM cron.job WHERE jobname = 'dispatch-exit'
  LOOP
    PERFORM cron.unschedule(j.jobid);
  END LOOP;
END
$$;

SELECT cron.schedule(
  'dispatch-exit',
  '1,11,21,31,41,51 13-20 * * 1-5',
  $$SELECT trading.dispatch_workflow('exit-orchestrator.yml')$$
);
