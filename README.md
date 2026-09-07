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

