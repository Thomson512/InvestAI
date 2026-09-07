-- SMOKE TEST. Po ověření příkazu v T212 Demo hned spusť smoke_disable.sql.
-- Neprodukuje live. Jen povolí can_trade() na jeden ruční --dry-run=false.

INSERT INTO trading.risk_limits (id, daily_loss_limit_pct, max_drawdown_pct, heartbeat_max_age_min)
VALUES (1, 5.0, 15.0, 60)
ON CONFLICT (id) DO UPDATE SET
  daily_loss_limit_pct = EXCLUDED.daily_loss_limit_pct,
  max_drawdown_pct = EXCLUDED.max_drawdown_pct,
  heartbeat_max_age_min = EXCLUDED.heartbeat_max_age_min;

INSERT INTO trading.daily_pnl (trade_date, opening_equity, current_equity, peak_equity)
VALUES (trading.session_date(), 100000, 100000, 100000)
ON CONFLICT (trade_date) DO UPDATE SET
  opening_equity = 100000,
  current_equity = 100000,
  peak_equity = 100000;

INSERT INTO trading.scheduler_heartbeat (loop_name, last_seen)
VALUES ('live_loop', now())
ON CONFLICT (loop_name) DO UPDATE SET last_seen = now();

UPDATE trading.system_control SET trading_enabled = true WHERE id = 1;

SELECT * FROM trading.can_trade();
