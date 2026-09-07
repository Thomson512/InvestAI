# InvestAI

Automatizovaný obchodní systém. Strategie je zafixovaná v `config/strategy.v1.json`.

## Zafixovaná strategie (v1)

Breakout s filtrem relativní síly. Parametry v `config/strategy.v1.json`.
Otisk: `config/strategy.v1.sha256` (SHA-256 kanonického JSON).

Spočítání hashe:

```bash
python scripts/param_hash.py
```

## Proč se hodnoty NESMÍ měnit

Soubor `strategy.v1.json` je zmrazený kontrakt, ne pracovní draft.

- **Evidence musí zůstat porovnatelná.** Každý signál, shadow obchod i fence se váže na `param_hash`. Když změníš lookback, ATR násobek nebo risk limit, nové výsledky nelze skládat ke starým — vypadá to jako stejná strategie, ale je to jiný experiment.
- **Look-ahead a křivení historie.** Úprava parametrů po tom, co už znáš výsledek období, je in-sample fitting. Zpětná editace v1 znehodnotí celou nasbíranou evidenci.
- **Live a shadow musí být identické.** Limit rizika 500 CZK a strop pozice 8000 CZK platí stejně pro simulaci i pozdější paper/live. Tichá změna v JSON způsobí, že shadow měří jiný systém, než který pak pustíš.
- **Deterministický hash je pojistka.** `scripts/param_hash.py` počítá SHA-256 kanonického JSON (seřazené klíče, kompaktní zápis, UTF-8). Jakákoli změna hodnoty, pořadí významu polí v obsahu nebo metadat (`frozen_at`, verze) změní hash. Test ověřuje, že hash je stabilní napříč běhy a sedí na zaznamenaný otisk.

Nová sada parametrů = **nový soubor** (`strategy.v2.json`) a nový hash. Soubor v1 se po zmrazení needituje.

## Zafixované univerzum (v1)

`config/universe.v1.json` obsahuje 250 symbolů z S&P 500.

- Zdroj: Wikipedia — [List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies), snapshot 2026-09-07.
- Otisk: pole `universe_hash` (SHA-256 seřazeného seznamu symbolů).
- Univerzum je **FROZEN**. Zpětná editace v1 je zakázaná. Nová sada = nový soubor (`universe.v2.json`), ne úprava stávajícího.

### Proč 250, ne 50

Frekvence obchodů roste lineárně s velikostí univerza. Při 50 symbolech vychází ~25 obchodů ročně. Na statistické vyhodnocení je potřeba ~100 obchodů — to by trvalo 4 roky. Při 250 symbolech je to ~10 měsíců.

### Proč zpětná změna vytvoří survivorship bias

Survivorship bias vznikne, když z historie vyhodíš jména, která později dopadla špatně (vyřazení z indexu, krach, slučování), a necháš jen ta, která „přežila“.

- **Zpětný drop loserů zkreslí edge nahoru.** Breakout + RS na univerzu bez později vyřazených jmen vypadá ziskověji, než by bylo v reálu. V živém běhu ta jména ještě v seznamu byla.
- **Zpětný add vítězů je look-ahead.** Doplnit NVDA/PLTR až poté, co už víš, že rostly, není ten samý experiment. Budoucí evidence nejde skládat k minulé.
- **Stejný `universe_hash` musí sedět na dataset i na shadow.** Dataset builder si hash uloží. Když v1 potichu přepíšeš, hash se buď rozjede, nebo (hůř) zůstane a ty porovnáváš jinou historii pod stejným otiskem.
- **Index se mění dopředu, ne dozadu.** S&P 500 v roce 2027 nebude stejný jako dnes. To patří do `universe.v2.json` s novým `constructed_at` a novým hashem. Řada v1 zůstane archívem toho, na čem se sbírala první evidence.

## Dataset (IEX, fail-closed)

`scripts/build_dataset.py` stáhne denní bary z Alpaca (IEX) pro celé univerzum + `SPY` (reference pro relativní sílu). 400 sessions zpět, max 100 symbolů na request.

```bash
# klíče jen z env, nikdy do gitu
set ALPACA_KEY_ID=...
set ALPACA_SECRET_KEY=...
python scripts/build_dataset.py
```

Výstup `data/dataset.json` (gitignore) obsahuje `generated_at`, `universe_hash`, `source_dataset_sha256`, `latest_session`.

**Fail-closed:** selhání kteréhokoli chunku = konec, částečný soubor se nezapíše. Díra v evidenci by znehodnotila shadow i pozdější live.

**Feed `alpaca_iex`:** IEX je ~3 % konsolidovaného objemu. Likviditní práh kalibruj jen na tomto feedu (`config/liquidity.v1.json`, pole `feed`). Práh ze SIP/Yahoo sem nepatří.

## Signály (point-in-time)

`scripts/evaluate_signals.py` rozhodne `BUY_CANDIDATE` / `REJECT` podle `config/strategy.v1.json`.

Funkce dostane index dne v řadě SPY a smí vidět **jen bary s datem <= tento den**. Žádné `bars[index+1:]`, žádné rolling okno na celé sérii dopředu. Look-ahead by znehodnotil celou evidenci.

```bash
python scripts/evaluate_signals.py
```

Filtry: close > SMA150, close nad 30denním high (předchozích 30 seancí), RS excess vs SPY za 63 dní >= 10 %. Stop = 2×ATR14, target = 4.5R.

**Tvrdá brána:** market breadth = podíl jmen nad SMA150. Pod `min_market_breadth_pct` (50) neprojde žádný kandidát.

## Forward shadow

`scripts/forward_shadow.py` simuluje virtuální portfolio. **Stejné CZK stropy jako live** z `strategy.v1.json`: risk 500, pozice 8000, max 8 otevřených, max 2 vstupy za seanci. Shares se nepočítají z procent equity — jinak by shadow měřil jiný systém.

Náklady (`config/costs.v1.json`): baseline / conservative / severe (slippage 5 / 10 / 20 bps, FX fee 15 bps). Kurz USD/CZK při vstupu ≠ výstupu (FX drift).

```bash
python scripts/forward_shadow.py
```

`data/report.json`: per scénář completed_trades, winners, losers, win_rate_pct, realized_pnl, marked_pnl, max_drawdown_pct, expectancy, profit_factor; per obchod entry/exit, ceny, exit_reason, r_multiple, costs.

**Paper gate:** `promotion_authorized` je true jen při ≥90 dnech **a** ≥100 obchodech. Jinak vždy false.

## Denní shadow workflow

`.github/workflows/daily-shadow.yml` — cron `15 21 * * 1-5` (po US close) + ruční `workflow_dispatch`. Timeout 60 min.

Pořadí: NYSE session gate → `build_dataset` → `evaluate_signals` → `forward_shadow`. Evidence jde na větev `runtime/evidence`, ne do `main`.

O víkendu a NYSE svátku je **no-op**: dataset se nestahuje ani nepřepisuje. Job summary začíná `SHADOW: <N> trades, <M> open, PnL <X> CZK`.

Workflow má jen Alpaca market-data secrets (`ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`). Žádný broker, žádné T212 credentials.

Tři měsíce jen sbírat. Neobchodovat.

## Control plane (Supabase)

Schéma `trading` v `supabase/migrations/0001_control_plane.sql`. Kill switch default **false**. RLS zapnuté, žádné policy — čte/píše jen `service_role`.

`can_trade()` hlídá jen **vstupy**, vždy fail-closed. Chybějící data = `allowed=false`. Důvody v pořadí: `KILL_SWITCH_OFF`, `MISSING_RISK_LIMITS`, `NO_PNL_RECORD_TODAY`, `INVALID_OPENING_EQUITY`, `DAILY_LOSS_LIMIT`, `MAX_DRAWDOWN`, `HEARTBEAT_STALE`, `UNRESOLVED_FENCE`.

`can_close_position()` ignoruje kill switch i denní ztrátu a vrací vždy `true`. Jinak by vypnutí zamklo výstup z otevřených pozic.

### Jak aplikovat

1. Supabase → SQL Editor.
2. Vlož **celý obsah** `supabase/migrations/0001_control_plane.sql` z tohoto repa. Ne vlastní přepis — databáze by se rozešla s migrací.
3. Run.
4. Stejně spusť `supabase/verify/0001_control_plane.sql`. Končí `ROLLBACK`, data neponechá. Když nějaký `RAISE EXCEPTION` spadne, migrace nesedí.
5. Pak spusť `supabase/migrations/0002_public_rpc.sql` — bez toho PostgREST `can_trade` na `/rest/v1/rpc/can_trade` nevidí (vystavuje jen `public`).

Po migraci je `trading_enabled = false`. Limity a dnešní `daily_pnl` se musí vložit explicitně, jinak `can_trade()` zůstane zavřené.

## Broker snapshot (jen čtení)

`scripts/broker_snapshot.py` čte T212 **demo** GET-only. Base URL je natvrdo `https://demo.trading212.com`. Když `T212_ENVIRONMENT != demo`, `RuntimeError` **před** HTTP.

```powershell
$env:T212_ENVIRONMENT="demo"
$env:T212_API_KEY="..."
python scripts/broker_snapshot.py
```

Výstup `data/snapshot.json`: `account_total_value`, `available_to_trade`, `positions`, `active_orders`, `fetched_at`.

`global_blockers(snapshot)` — pozice je chráněná jen aktivním SELL typu `STOP` / `STOP_LIMIT`. Nechráněná pozice nad `smoke_exemption_czk` (250) → `unprotected_position:{ticker}`. Jedna stačí k zastavení všech nových vstupů.

Klíč nikdy do logu. Žádný POST/PUT/DELETE.

## Durable fence

`scripts/fence.py` brání duplicitnímu odeslání. T212 nemá `clientOrderId`; timeout u market příkazu neříká, jestli dorazil. Retry = riziko dvojité pozice.

`fence_key = {session_date}:{symbol}:{param_hash}`

1. INSERT `PENDING` do `trading.trade_fences` **před** POSTem. Konflikt PK → `SKIP`, broker se nevolá.
2. Jediný send, timeout 15 s, **žádný retry**.
3. Timeout / 5xx → `UNCERTAIN`, nenulový exit, zastavit. Řeší člověk.
4. Úspěch → `SENT`, readback → `CONFIRMED`.
5. `NEVER_SENT` jen když zároveň chybí artefakt odeslání **a** broker potvrdí, že se pozice ani hotovost nezměnily.

Fence žije v databázi, ne v souboru na runneru.

## Submit order (jeden symbol, demo)

`scripts/submit_order.py` — market buy + ochranný SELL STOP. Default `--dry-run=true` (žádný POST). Ostrý běh: `--dry-run=false`.

Guardy **před** HTTP: `T212_ENVIRONMENT=demo`, `ENABLE_LIVE_EXECUTION=false`. Pak `can_trade()`, `global_blockers()`, fence, jeden POST buy, readback, jeden POST stop, readback, fence `CONFIRMED`.

Když stop selže: `CRITICAL`, přesný návod na ruční STOP, fence `UNCERTAIN`, nenulový exit, **žádný retry**.

```powershell
$env:T212_ENVIRONMENT="demo"
$env:ENABLE_LIVE_EXECUTION="false"
python scripts/submit_order.py --symbol AAPL --quantity 1 --stop-price 180 --session-date 2026-09-07
python scripts/submit_order.py --symbol AAPL --quantity 1 --stop-price 180 --session-date 2026-09-07 --dry-run=false
```

Jeden symbol na běh. První ostrý běh ručně, malá částka, ověř v T212 app.

## Buyer workflow

`.github/workflows/buyer.yml` — cron `*/10 13-20 * * 1-5` (UTC) + `workflow_dispatch`. Env natvrdo `T212_ENVIRONMENT=demo`, `ENABLE_LIVE_EXECUTION=false`.

Řetěz: NYSE regular hours → snapshot → freshness shortlistu (`config/buyer.v1.json`) → `global_blockers` před kandidáty → IEX kotace + spread → ČNB USD/CZK → quantity/stop/target → fence → jeden demo submit + protective stop.

První řádek job summary je přesně `OUTCOME: <hodnota>` (`MARKET_CLOSED`, `NO_CANDIDATES`, `GLOBAL_BLOCKER:…`, `FRESHNESS_FAIL`, `SPREAD_REJECTED:<symbol>`, `FENCE_EXISTS:<symbol>`, `ORDER_SUBMITTED:<order_id>`). Stejné pole `outcome` je v `runtime/evidence` (`evidence/buyer/`).

24 h v řadě jiný výsledek než `ORDER_SUBMITTED` / `NO_CANDIDATES` / `MARKET_CLOSED` → `::warning::`. Job neselže.

