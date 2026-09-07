-- Hned po ověření v T212 Demo.

UPDATE trading.system_control SET trading_enabled = false WHERE id = 1;

SELECT trading_enabled FROM trading.system_control WHERE id = 1;
SELECT * FROM trading.can_trade();
