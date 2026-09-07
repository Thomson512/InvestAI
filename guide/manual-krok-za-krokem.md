# Obchodní systém krok za krokem

Praktický manuál. Účty, nastavení, hotové prompty k vložení do Claude Code nebo Cursoru.

**Jak to používat:** projdi fázi 0 (účty) ručně. Od fáze 1 dál vždycky zkopíruj prompt do svého AI nástroje, nech ho udělat práci, zkontroluj výstup, pokračuj dál.

---

## Bezpečnostní pravidlo

**Nikdy nevkládej API klíče do chatu s AI.** Ani do Claude, ani do GPT, ani do Cursoru.

Klíče patří výhradně do:
- GitHub → Settings → Secrets and variables → Actions
- Supabase → Project Settings → Edge Functions → Secrets
- lokální `.env` soubor, který je v `.gitignore`

Když ti AI řekne „pošli mi svůj klíč", je to chyba. Odmítni.

---

# FÁZE 0 — Účty

Zhruba hodina. Vše ve free tieru.

## 0.1 GitHub

1. `github.com` → Sign up
2. Vytvoř **privátní** repozitář, např. `trading-system`
3. Nainstaluj GitHub CLI: `winget install GitHub.cli` (Windows) nebo `brew install gh` (Mac)
4. `gh auth login` → GitHub.com → HTTPS → Login with a web browser
5. Ověř: `gh auth status` musí ukázat scope `repo` a `workflow`

## 0.2 Trading 212

1. `trading212.com` → registrace
2. V aplikaci přepni na **Practice / Demo** účet
3. Settings → API → vygeneruj klíč pro **demo**
4. Klíč ulož do správce hesel

Demo API base URL: `https://demo.trading212.com`

## 0.3 Alpaca

1. `alpaca.markets` → Sign up
2. Paper trading dashboard → **Generate API Key**
3. Ulož `APCA-API-KEY-ID` a `APCA-API-SECRET-KEY`

Free tier dává IEX feed s denními svíčkami. To stačí.

## 0.4 Supabase

1. `supabase.com` → New project
2. Zapiš si databázové heslo
3. Project Settings → API → zkopíruj `Project URL` a `service_role` klíč
4. Database → Extensions → zapni `pg_cron` a `pg_net`

## 0.5 Vlož klíče do GitHub

```
gh secret set T212_API_KEY --repo Thomson512/InvestAI
gh secret set ALPACA_KEY_ID --repo Thomson512/InvestAI
gh secret set ALPACA_SECRET_KEY --repo Thomson512/InvestAI
gh secret set SUPABASE_URL --repo Thomson512/InvestAI
gh secret set SUPABASE_SERVICE_KEY --repo Thomson512/InvestAI
```

Příkaz se zeptá na hodnotu. Nezadávej ji jako argument — zůstala by v historii shellu.

---

# FÁZE 1 — Strategie a univerzum

## Prompt 1.1 — Zafixování strategie

```
Zakládám automatizovaný obchodní systém. Repo: Thomson512/InvestAI.
Jazyk: Python 3.12. Komunikuj česky, stručně.

## ÚKOL
Vytvoř config/strategy.v1.json se zafixovanou strategií a skript,
který spočítá jeho hash.

## STRATEGIE — breakout s filtrem relativní síly
  breakout_lookback_days: 30
  trend_filter_sma: 150
  relative_strength_lookback_days: 63
  min_rs_excess_pct: 10
  atr_period: 14
  stop_atr_multiple: 2.0
  reward_to_risk: 4.5
  min_market_breadth_pct: 50
  max_open_positions: 8
  max_risk_per_trade_czk: 500
  max_position_value_czk: 8000
  max_new_entries_per_session: 2

## POŽADAVKY
1. JSON s poli schema_version, strategy_version, frozen_at
2. scripts/param_hash.py - sha256 kanonického JSON, deterministický
3. Test, že hash je stabilní napříč běhy
4. README sekce vysvětlující, proč se hodnoty NESMÍ měnit

## PRAVIDLA
Žádný broker kód. Žádné API volání. Jen konfigurace a hash.

Až budeš hotov, vypiš param_hash a připomeň mi ho commitnout.
```

## Prompt 1.2 — Univerzum

```
Repo: Thomson512/InvestAI. Navazuje na Prompt 1.1.

## ÚKOL
Vytvoř config/universe.v1.json s 200-250 symboly z S&P 500.

## POŽADAVKY
1. Zdroj seznamu uveď explicitně v souboru
2. Povinná pole: constructed_at (ISO 8601 UTC), universe_hash,
   source, exclusion_criteria
3. Univerzum je FROZEN - nikdy se nemění zpětně.
   Nová verze = nový soubor v0.2.0, ne edit stávajícího.
4. Do README napiš, proč zpětná změna vytvoří survivorship bias

## PROČ 200-250, NE 50
Frekvence obchodů roste lineárně s velikostí univerza.
Při 50 symbolech vychází ~25 obchodů ročně, na statistické
vyhodnocení je potřeba ~100. To by trvalo 4 roky.
Při 250 symbolech to je ~10 měsíců.

## PRAVIDLA
Žádný broker kód, žádná exekuce.
```

---

# FÁZE 2 — Data a signál

## Prompt 2.1 — Dataset builder

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/build_dataset.py - stáhne denní svíčky z Alpaca pro celé
univerzum a uloží je jako JSON.

## POŽADAVKY
1. Alpaca API, IEX feed, denní bary, 400 sessions zpět
2. CHUNKING: max 100 symbolů na request. Alpaca víc nezvládne.
3. Selhání kteréhokoli chunku = fail-closed.
   Nikdy neukládej částečný dataset - vznikla by evidence s dírou.
4. Klíče výhradně z env: ALPACA_KEY_ID, ALPACA_SECRET_KEY
5. Výstup obsahuje: generated_at, universe_hash,
   source_dataset_sha256, latest_session
6. Stáhni i SPY jako referenci pro relativní sílu

## POZOR NA FEED
IEX zachycuje ~3 % konsolidovaného objemu. Když budeš někdy
počítat likviditní práh, kalibruj ho na IEX, ne na konsolidovaná
data. Do konfigurace prahu zapiš pole "feed": "alpaca_iex".

## PRAVIDLA
Read-only. Žádný broker, žádná exekuce.
Test s mockovanou odpovědí, aby CI neprovolávalo API.
```

## Prompt 2.2 — Vyhodnocení signálu

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/evaluate_signals.py - pro každý symbol rozhodne
BUY_CANDIDATE / REJECT podle strategie z config/strategy.v1.json.

## KRITICKÉ - POINT IN TIME
Funkce dostane index dne a smí vidět VÝHRADNĚ data do toho dne
včetně. Žádné bars[index+1:], žádné rolling okno počítané
na celé sérii dopředu.

Napiš test, který selže při look-ahead:
vezmi tentýž den, jednou vyhodnoť na plné sérii, jednou na sérii
oříznuté na tento den. Výsledek MUSÍ být identický.

Toto je nejčastější chyba v obchodních systémech a znehodnotí
veškerou evidenci, kterou pak nasbíráš.

## VÝSTUP PRO KAŽDÝ SYMBOL
  symbol, decision, tier, score
  blockers: seznam důvodů odmítnutí
  entry_price, stop_price, target_price
  atr_14, rs_excess_pct, breakout_strength

## TVRDÁ BRÁNA
Spočítej market breadth (podíl symbolů nad SMA150).
Pod min_market_breadth_pct neprojde ŽÁDNÝ kandidát.

## PRAVIDLA
Žádný broker kód. Čistá funkce nad datasetem.
```

---

# FÁZE 3 — Forward shadow

**Tady se tři měsíce jen sbírá. Neobchoduje se.**

## Prompt 3.1

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/forward_shadow.py - simuluje obchody podle signálů
a vede virtuální portfolio.

## KRITICKÉ - PARAMETRY MUSÍ ODPOVÍDAT ŽIVÉMU SYSTÉMU
Použij PŘESNĚ hodnoty z config/strategy.v1.json:
max_risk_per_trade_czk 500, max_position_value_czk 8000,
max_open_positions 8.

NEPOUŽÍVEJ procenta z virtuální equity. Nejčastější chyba je,
že shadow běží na jiné velikosti pozic než živý systém a měsíc
sběru dat je pak k ničemu.

## NÁKLADOVÉ SCÉNÁŘE - tři varianty
  baseline:     commission 0 bps, slippage 5 bps,  fx_fee 15 bps
  conservative: commission 0 bps, slippage 10 bps, fx_fee 15 bps
  severe:       commission 0 bps, slippage 20 bps, fx_fee 15 bps

Modeluj i FX drift - kurz při vstupu a výstupu se liší a reálně
to posunulo ztráty z -1.0 R na -1.4 R.

## VÝSTUP report.json
  per scénář: completed_trades, winners, losers, win_rate_pct,
  realized_pnl, marked_pnl, max_drawdown_pct, expectancy,
  profit_factor
  per obchod: entry/exit session, ceny, exit_reason, r_multiple,
  costs

## PAPER GATE
  min_days: 90
  min_trades: 100
  promotion_authorized: vždy false, dokud obojí není splněno

## PRAVIDLA
Žádný broker kód. Simulace nad reálnými daty.
```

## Prompt 3.2 — Denní workflow

```
Repo: Thomson512/InvestAI.

## ÚKOL
.github/workflows/daily-shadow.yml

## POŽADAVKY
1. cron "15 21 * * 1-5" (po US close) + workflow_dispatch
2. Kroky: build_dataset -> evaluate_signals -> forward_shadow
3. Session gate: použij pandas_market_calendars, kalendář NYSE.
   O víkendu a svátku skonči no-op, NEPŘEPISUJ dataset.
4. Job summary první řádek:
     "SHADOW: <N> trades, <M> open, PnL <X> CZK"
5. Evidence commituj na větev runtime/evidence, NE do main
6. Timeout 60 minut

## PRAVIDLA
Žádný broker přístup. Žádné T212 credentials v tomto workflow.
```

**Tady se zastav na tři měsíce.** Kontroluj denně, že workflow běží. Neobchoduj.

---

# FÁZE 4 — Control plane

## Prompt 4.1

```
Repo: Thomson512/InvestAI.

## ÚKOL
supabase/migrations/0001_control_plane.sql

## TABULKY - schéma trading
  system_control(id, trading_enabled default FALSE, updated_at)
  risk_limits(id, daily_loss_limit_pct, max_drawdown_pct,
              heartbeat_max_age_min)
  daily_pnl(trade_date pk, opening_equity, current_equity,
            peak_equity, updated_at)
  trade_fences(fence_key pk, symbol, session_date, state, order_id,
               created_at)
    state in ('PENDING','SENT','CONFIRMED','UNCERTAIN','NEVER_SENT')
  scheduler_heartbeat(loop_name pk, last_seen)

Na všech zapni RLS. Bez policy - přístup jen přes service_role.

## FUNKCE can_trade()
Vrací (allowed boolean, reason text). VŽDY fail-closed.

Kontroly v pořadí, každá s vlastním reason:
  KILL_SWITCH_OFF        trading_enabled != true
  MISSING_RISK_LIMITS    chybí konfigurace
  NO_PNL_RECORD_TODAY    chybí řádek pro current_date
  INVALID_OPENING_EQUITY opening_equity <= 0
  DAILY_LOSS_LIMIT       překročen denní limit
  MAX_DRAWDOWN           překročen drawdown od peaku
  HEARTBEAT_STALE        smyčka neběží
  UNRESOLVED_FENCE       existuje fence ve stavu UNCERTAIN

CHYBĚJÍCÍ DATA = FALSE. Nikdy nepředpokládej, že je vše v pořádku.

## ENTRY VS EXIT
can_trade() blokuje jen VSTUPY.
Přidej can_close_position(), která ignoruje kill switch
i denní ztrátový limit. Uzavření pozice musí být možné vždy,
jinak si při vypnutí zablokuješ výstup z otevřených pozic.

## VÝSTUP
Migrace + dotazy k ověření + README, jak ji aplikovat.
```

Migraci aplikuj v Supabase → SQL Editor. Zkopíruj **obsah souboru z repa**, ne vlastní verzi — jinak se databáze rozejde s migrační historií.

---

# FÁZE 5 — Broker, jen čtení

## Prompt 5.1

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/broker_snapshot.py - přečte stav Trading 212 DEMO účtu.

## POŽADAVKY
1. Base URL NATVRDO: https://demo.trading212.com
   Guard: když T212_ENVIRONMENT != "demo", vyhoď RuntimeError
   PŘED jakýmkoli HTTP voláním.
2. Endpointy: /api/v0/equity/account/summary,
   /api/v0/equity/portfolio, /api/v0/equity/orders
3. Klíč z env T212_API_KEY, nikdy do logu
4. Retry jen na GET, exponenciální backoff, max 3 pokusy
5. Výstup snapshot.json: account_total_value, available_to_trade,
   positions[], active_orders[], fetched_at

## GLOBÁLNÍ BLOCKERY
Funkce global_blockers(snapshot) -> list[str]

Pozice je CHRÁNĚNÁ, když má aktivní SELL příkaz typu
STOP nebo STOP_LIMIT na stejném tickeru.

Nechráněná pozice nad prahem smoke_exemption_czk (250)
vrací "unprotected_position:{ticker}".

Tohle je tvrdá brána - jedna nechráněná pozice zastaví
VŠECHNY nové vstupy. Je to správně: bez stopu neznáš
maximální riziko.

## PRAVIDLA
POUZE GET. Žádný POST, PUT ani DELETE. Zatím.
```

---

# FÁZE 6 — Fence a první příkaz

## Prompt 6.1 — Durable fence

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/fence.py - ochrana proti duplicitnímu odeslání.

## PROČ
Trading 212 nemá clientOrderId. Market příkazy nejsou idempotentní.
Když POST vyprší timeoutem, NEVÍŠ, jestli příkaz dorazil.
Automatický retry může vytvořit dvojitou pozici.

## LOGIKA
1. fence_key = f"{session_date}:{symbol}:{param_hash}"
2. INSERT do trading.trade_fences se stavem PENDING
   PŘED odesláním. Konflikt na primary key = dnes už jsme
   to zkoušeli, vrať SKIP.
3. Jediný POST. Timeout 15 s. ŽÁDNÝ retry.
4. Timeout nebo 5xx -> stav UNCERTAIN, exit nenulový, ZASTAV.
   Tohle musí vyřešit člověk.
5. Úspěch -> stav SENT, pak readback z brokera -> CONFIRMED

## KLASIFIKACE NEVER_SENT
Fence smí být uvolněna do NEVER_SENT jen při splnění OBOU
podmínek zároveň:
  a) chybí artefakt odeslání
  b) broker potvrzuje, že se pozice ani hotovost nezměnily
Jinak zůstává UNCERTAIN.

## TESTY
- fence existuje -> SKIP, žádné volání brokera
- timeout -> UNCERTAIN, nenulový exit, žádný retry
- souběžné spuštění -> jen jeden projde

## PRAVIDLA
Fence je v databázi, ne v souboru na runneru. Runner je efemérní.
```

## Prompt 6.2 — Odeslání příkazu

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/submit_order.py - odešle JEDEN příkaz s ochranným stopem.

## POŘADÍ OPERACÍ
1. Guard: T212_ENVIRONMENT == "demo", jinak RuntimeError
2. Guard: ENABLE_LIVE_EXECUTION == "false", jinak RuntimeError
3. can_trade() -> false znamená STOP
4. global_blockers() neprázdné znamená STOP
5. Fence PENDING
6. POST market buy
7. Readback
8. POST protective stop
9. Readback stopu
10. Fence CONFIRMED

## KRITICKÉ
Když krok 8 selže, máš nechráněnou pozici.
Zaloguj to jako CRITICAL a vypiš přesnou instrukci
pro ruční nasazení stopu. NEPOKRAČUJ dál.

## PARAMETR --dry-run
Default TRUE. Vypíše, co by odeslal, a neprovede žádný POST.
Ostrý běh vyžaduje explicitní --dry-run=false.

## PRAVIDLA
Jeden symbol na běh. Žádné dávky. Žádný retry po nejistém výsledku.
```

**První ostrý běh spusť ručně, s jedním symbolem a malou částkou.** Ověř v aplikaci Trading 212, že příkaz i stop dorazily.

---

# FÁZE 7 — Automatizace

## Prompt 7.1 — Buyer

```
Repo: Thomson512/InvestAI.

## ÚKOL
.github/workflows/buyer.yml - kompletní řetěz.

## KROKY, každý s podmínkou na výstup předchozího
1.  Session gate - NYSE kalendář. Zavřeno -> OUTCOME MARKET_CLOSED
2.  Broker snapshot
3.  Freshness gate - stáří shortlistu vs práh z configu
4.  Preselekce - global_blockers první, před kandidáty
5.  Kotace + spread filtr
6.  FX fixing z ČNB
7.  Finalizace - quantity, stop, target
8.  Fence
9.  Submit + protective stop
10. Publish summary

## DIAGNOSTIKA - PRVNÍ ŘÁDEK SUMMARY
Přesně jedna z hodnot:
  MARKET_CLOSED
  NO_CANDIDATES
  GLOBAL_BLOCKER:<jméno>
  FRESHNESS_FAIL
  SPREAD_REJECTED:<symbol>
  FENCE_EXISTS:<symbol>
  ORDER_SUBMITTED:<order_id>

Formát: "OUTCOME: <hodnota>"
Stejnou hodnotu zapiš do evidence jako pole "outcome".

## STREAK VAROVÁNÍ
Když je 24 hodin v řadě jiný výsledek než ORDER_SUBMITTED,
NO_CANDIDATES nebo MARKET_CLOSED, vyhoď ::warning::.
Job NESMÍ selhat - jen viditelně varovat.

## PROČ TO TAK
Nejnebezpečnější stav není pád. Je to workflow, které doběhne
zeleně a nic neudělá. Bez tohoto řádku můžeš mít systém týdny
mrtvý a nevědět o tom.

## ENV
  T212_ENVIRONMENT: demo
  ENABLE_LIVE_EXECUTION: "false"
```

## Prompt 7.2 — Exit orchestrator

```
Repo: Thomson512/InvestAI.

## ÚKOL
.github/workflows/exit-orchestrator.yml

## POŽADAVKY
1. Session gate - mimo tržní hodiny no-op.
   NEBĚHEJ v noci a o víkendu, nemá to co dělat.
2. Kontrola, že každá pozice má aktivní stop
3. Reconciliace pozic proti evidenci
4. UPSERT do trading.daily_pnl
5. UPDATE trading.scheduler_heartbeat

## KRITICKÉ
Tenhle workflow plní daily_pnl, na kterém závisí can_trade().
Když neběží, can_trade() vrátí NO_PNL_RECORD_TODAY a systém
neobchoduje. To je záměr, ale musí to být vidět.

Exit cesta NESMÍ být blokována přes can_trade().
Použij can_close_position().
```

## Prompt 7.3 — Externí dispatcher

```
Repo: Thomson512/InvestAI.

## ÚKOL
supabase/functions/dispatch-loop/index.ts + pg_cron

## PROČ
GitHub Actions cron je nespolehlivý. Naměřeno na produkčním
systému: cron "40 * * * *" měl doručit 24 běhů denně, doručil 8,
z toho jen 2 v tržních hodinách. Jednou 62 hodin bez běhu.
Čím vyšší frekvence, tím horší doručování.

Externí dispatch ve stejném období: 11 doručení proti
1 nativnímu schedule.

## EDGE FUNCTION
Deno. Volá GitHub API:
  POST /repos/{owner}/{repo}/actions/workflows/{id}/dispatches
Token z env GH_DISPATCH_TOKEN, scope actions:write.
Loguje každý dispatch.

## PG_CRON
  select cron.schedule('dispatch-buyer', '*/10 13-20 * * 1-5',
    $$ select net.http_post(...) $$);

## KRITICKÉ
Vypni nativní schedule v buyer.yml, jinak dostaneš double-fire.
Idempotenci zajišťuje fence, ale dvojí běh plýtvá minutami.

## A COMMITNI TENHLE ZDROJ DO REPA
Kód, který běží v produkci a není verzovaný, je časovaná bomba.
```

---

# FÁZE 8 — Ochrana proti tichému selhání

## Prompt 8.1

```
Repo: Thomson512/InvestAI.

## ÚKOL
.github/workflows/protective-stop-guard.yml

## PROČ
Na produkčním systému jedna nechráněná pozice zablokovala
všechny nákupy na 14 dní. Každý běh hlásil success.
Nikdo si toho nevšiml, protože nic neselhalo.

## CHOVÁNÍ
Vstupy: symbol (required), dry_run (boolean, default TRUE)

1. Ověř, že pozice existuje a nemá aktivní SELL STOP.
   Když má, skonči no-op.
2. Spočítej stop podle ATR politiky.
   Osiřelá pozice nemá entry metadata - fallback procentní odstup
   z verzované konfigurace.
3. dry_run=true: vypiš plán, ŽÁDNÝ POST
4. dry_run=false: fence -> jeden POST -> readback

## GUARD RAILS
  ENABLE_LIVE_EXECUTION musí být "false"
  T212_ENVIRONMENT musí být "demo"
  jeden symbol na běh
  nejistý výsledek -> UNCERTAIN, nenulový exit, žádný retry

## NAPOJENÍ
Do buyer summary přidej při detekci blockeru druhý řádek:
  "FIX: gh workflow run protective-stop-guard.yml -f symbol=<X>"
```

---

# FÁZE 9 — Sběr dat

Rok. Nic se nemění.

**Co kontrolovat týdně:**

| | |
|---|---|
| Rozložení `OUTCOME` | kolikrát `ORDER_SUBMITTED` vs blockery |
| Počet obchodů | roste směrem ke 100? |
| Fence ve stavu `UNCERTAIN` | musí být nula |
| Heartbeat | běží dispatcher? |
| Shoda shadow vs live | drží stejné parametry? |

**Co nedělat:**

- Neměnit parametry strategie. Jakákoli změna vynuluje evidenci.
- Nepřidávat filtry, protože poslední obchody byly ztrátové. Při R:R 4.5 mají tři ze čtyř obchodů prohrát.
- Netestovat varianty. Když jich vyzkoušíš dvacet a vybereš nejlepší, ta nejlepší vypadá dobře i bez skutečné výhody.

---

# FÁZE 10 — Vyhodnocení

Po 100 obchodech.

```
Repo: Thomson512/InvestAI.

## ÚKOL
scripts/evaluate_gate.py - vyhodnotí forward evidenci.

## SPOČÍTEJ
  celkový počet obchodů, win rate, průměrné R vítězných
  a ztrátových, expectancy, profit factor, max drawdown,
  Sharpe, CAGR

## BREAK-EVEN WIN RATE
Spočítej skutečný, ne návrhový:
  break_even = avg_loss_R / (avg_win_R + avg_loss_R)

Návrh s R:R 4.5 předpokládá 18.2 %. Reálně to vychází kolem
24 %, protože skutečné ztráty bývají hlubší než -1.0 R
(skluz, gapy, FX) a náklady spotřebují 6-13 % rizika na obchod.

## INTERVAL SPOLEHLIVOSTI
Wilson interval pro naměřenou win rate.
Odpověz: leží break-even UVNITŘ intervalu?
Když ano, data neprokazují výhodu ani její absenci.

## VERDIKT
  EDGE_CONFIRMED    dolní mez intervalu > break-even
  NO_EDGE           horní mez intervalu < break-even
  INCONCLUSIVE      break-even uvnitř intervalu

## PRAVIDLA
Žádné přikrášlování. INCONCLUSIVE je nejčastější a legitimní
výsledek. Neznamená, že máš snížit práh gate.
```

---

# Bootstrap prompt

Když chceš, aby tě AI provedla celým procesem místo čtení tohoto dokumentu:

```
Jsi můj technický průvodce stavbou automatizovaného obchodního
systému. Jsem zkušený programátor.

## ARCHITEKTURA
GitHub Actions jako výpočet a plánování. Supabase jako control
plane. Trading 212 DEMO jako broker. Alpaca IEX jako tržní data.
ČNB jako FX. Supabase Edge Function + pg_cron jako spolehlivý
dispatcher místo GitHub cronu.

## FÁZE
0  účty a secrets
1  zafixování strategie a univerza
2  dataset a vyhodnocení signálu
3  forward shadow, 3 měsíce jen sběr
4  control plane a can_trade()
5  broker read-only a globální blockery
6  durable fence a první ruční příkaz
7  automatizace: buyer, exit orchestrator, dispatcher
8  ochrana proti tichému selhání
9  rok sběru dat
10 vyhodnocení paper gate

## TVÁ ROLE
Veď mě fázemi po jedné. Před každou se zeptej, jestli je
předchozí hotová a ověřená. Nepřeskakuj.

Ke každé fázi dej: co udělám ručně (účty, klíče, kliknutí)
a co necháme na kódu.

## NEPOKROČITELNÁ PRAVIDLA
- Nikdy po mně nechtěj API klíč. Klíče patří do GitHub Secrets.
- Fáze 3 trvá tři měsíce. Nenavrhuj ji zkrátit.
- Strategie se po zafixování nemění. Když navrhnu úpravu
  parametru kvůli špatným výsledkům, zastav mě.
- Backtest nepoužíváme. Jen forward evidence.
- Live execution zůstává vypnutá, dokud paper gate neprojde.
- Když něco nejde ověřit, řekni NEVÍM. Nedomýšlej.

## KOMUNIKACE
Česky, stručně, bez omáček. Konkrétní příkazy a kód, ne obecné
rady. Když navrhuješ soubor, napiš celý obsah.

Začni fází 0 a zeptej se, co už mám hotové.
```

---

# Nejčastější chyby

| Chyba | Následek | Prevence |
|---|---|---|
| Look-ahead ve vyhodnocení signálu | veškerá evidence neplatná | PIT test od začátku |
| Shadow s jinými parametry než live | měsíce dat o neexistujícím systému | sjednotit před fází 3 |
| Ladění parametrů podle výsledků | nadhodnocená výkonnost | zafixovat a nesahat |
| Retry po nejistém POST | duplicitní pozice | durable fence |
| Chybějící `OUTCOME` diagnostika | systém mrtvý týdny, hlásí success | fáze 7.1 |
| Spoléhání na GitHub cron | třetinová frekvence obchodů | externí dispatcher |
| Malé univerzum | 4 roky k vyhodnocení | 200-250 symbolů |
| Práh kalibrovaný na jiný feed | vyřazení likvidních titulů | `"feed"` v konfiguraci |
| Ruční migrace mimo CI | repo a DB se rozejdou | migration check |
| Backtest jako podklad pro go-live | falešná jistota | forward-only |
```
