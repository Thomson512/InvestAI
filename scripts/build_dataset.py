"""Stáhne denní IEX bary z Alpaca. Fail-closed — částečný dataset se neukládá."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from scripts.param_hash import canonical_dumps, compute_universe_hash

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "config" / "universe.v1.json"
LIQUIDITY_PATH = REPO_ROOT / "config" / "liquidity.v1.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "dataset.json"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
REFERENCE_SYMBOL = "SPY"
HTTP_TIMEOUT_SEC = 30

HttpGet = Callable[[str, dict[str, str]], dict]


class DatasetBuildError(RuntimeError):
    """Selhání buildu. Dataset se nesmí zapsat."""


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def require_credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    source = env if env is not None else os.environ
    key_id = (source.get("ALPACA_KEY_ID") or "").strip()
    secret = (source.get("ALPACA_SECRET_KEY") or "").strip()
    if not key_id or not secret:
        raise DatasetBuildError("Chybí ALPACA_KEY_ID nebo ALPACA_SECRET_KEY v prostředí.")
    return key_id, secret


def chunk_symbols(symbols: list[str], size: int) -> list[list[str]]:
    if size < 1:
        raise DatasetBuildError("max_symbols_per_request musí být >= 1.")
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


def request_symbols(universe_symbols: list[str]) -> list[str]:
    ordered = list(universe_symbols)
    if REFERENCE_SYMBOL not in ordered:
        ordered.append(REFERENCE_SYMBOL)
    return ordered


def lookback_start(calendar_days: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now.date() - timedelta(days=calendar_days)).isoformat()


def default_http_get(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DatasetBuildError(f"Alpaca HTTP {exc.code} pro {url}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise DatasetBuildError(f"Alpaca síťové selhání: {exc}") from exc
    if not isinstance(payload, dict):
        raise DatasetBuildError("Alpaca vrátila neočekávaný payload.")
    return payload


def build_bars_url(
    symbols: list[str],
    *,
    start: str,
    feed: str,
    timeframe: str,
    adjustment: str,
    page_token: str | None = None,
) -> str:
    query = {
        "symbols": ",".join(symbols),
        "timeframe": timeframe,
        "start": start,
        "feed": feed,
        "adjustment": adjustment,
        "limit": "10000",
        "sort": "asc",
    }
    if page_token:
        query["page_token"] = page_token
    return f"{ALPACA_BARS_URL}?{urllib.parse.urlencode(query)}"


def normalize_bar(bar: dict) -> dict:
    out = {
        "t": bar["t"],
        "o": float(bar["o"]),
        "h": float(bar["h"]),
        "l": float(bar["l"]),
        "c": float(bar["c"]),
        "v": int(bar["v"]),
    }
    if "n" in bar:
        out["n"] = int(bar["n"])
    if "vw" in bar:
        out["vw"] = float(bar["vw"])
    return out


def fetch_chunk(
    symbols: list[str],
    *,
    start: str,
    feed: str,
    timeframe: str,
    adjustment: str,
    headers: dict[str, str],
    http_get: HttpGet,
) -> dict[str, list[dict]]:
    merged: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    page_token: str | None = None
    while True:
        url = build_bars_url(
            symbols,
            start=start,
            feed=feed,
            timeframe=timeframe,
            adjustment=adjustment,
            page_token=page_token,
        )
        payload = http_get(url, headers)
        bars = payload.get("bars") or {}
        if not isinstance(bars, dict):
            raise DatasetBuildError("Chunk vrátil neplatné pole bars.")
        for symbol, series in bars.items():
            if symbol not in merged:
                continue
            if not isinstance(series, list):
                raise DatasetBuildError(f"Neplatná řada pro {symbol}.")
            merged[symbol].extend(series)
        page_token = payload.get("next_page_token") or None
        if not page_token:
            break
    missing = [symbol for symbol, series in merged.items() if not series]
    if missing:
        raise DatasetBuildError(
            f"Chunk bez dat pro {len(missing)} symbolů (první: {missing[:5]})."
        )
    return merged


def trim_sessions(series: list[dict], lookback_sessions: int) -> list[dict]:
    normalized = [normalize_bar(bar) for bar in series]
    normalized.sort(key=lambda bar: bar["t"])
    return normalized[-lookback_sessions:]


def session_date(timestamp: str) -> str:
    return timestamp[:10]


def hash_bars(bars: dict[str, list[dict]]) -> str:
    payload = canonical_dumps({"bars": {symbol: bars[symbol] for symbol in sorted(bars)}})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def build_dataset(
    *,
    universe_path: Path = UNIVERSE_PATH,
    liquidity_path: Path = LIQUIDITY_PATH,
    output_path: Path = DEFAULT_OUTPUT,
    env: dict[str, str] | None = None,
    http_get: HttpGet = default_http_get,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    universe = load_json(universe_path)
    liquidity = load_json(liquidity_path)
    symbols = list(universe["symbols"])
    expected_hash = compute_universe_hash(symbols)
    if universe.get("universe_hash") != expected_hash:
        raise DatasetBuildError("universe_hash v souboru nesedí na seznam symbolů.")

    feed = liquidity["alpaca_feed"]
    if liquidity.get("feed") != "alpaca_iex" or feed != "iex":
        raise DatasetBuildError("Likviditní konfigurace musí mít feed=alpaca_iex / alpaca_feed=iex.")

    key_id, secret = require_credentials(env)
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }
    start = lookback_start(int(liquidity["lookback_calendar_days"]), now=now)
    lookback_sessions = int(liquidity["lookback_sessions"])
    chunk_size = int(liquidity["max_symbols_per_request"])
    if chunk_size > 100:
        raise DatasetBuildError("Alpaca chunk max 100 symbolů. Sniž max_symbols_per_request.")

    wanted = request_symbols(symbols)
    chunks = chunk_symbols(wanted, chunk_size)
    merged: dict[str, list[dict]] = {}
    try:
        for chunk in chunks:
            part = fetch_chunk(
                chunk,
                start=start,
                feed=feed,
                timeframe=liquidity["timeframe"],
                adjustment=liquidity["adjustment"],
                headers=headers,
                http_get=http_get,
            )
            merged.update(part)
    except Exception as exc:
        if isinstance(exc, DatasetBuildError):
            raise
        raise DatasetBuildError(f"Selhání chunku: {exc}") from exc

    if set(merged) != set(wanted):
        raise DatasetBuildError("Spojený dataset nemá kompletní sadu symbolů.")

    bars = {symbol: trim_sessions(series, lookback_sessions) for symbol, series in merged.items()}
    if not bars[REFERENCE_SYMBOL]:
        raise DatasetBuildError("Chybí SPY reference.")

    document = {
        "generated_at": now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe_hash": expected_hash,
        "source_dataset_sha256": hash_bars(bars),
        "latest_session": session_date(bars[REFERENCE_SYMBOL][-1]["t"]),
        "feed": "alpaca_iex",
        "lookback_sessions": lookback_sessions,
        "symbol_count": len(bars),
        "bars": bars,
    }
    write_atomic(output_path, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stáhne denní IEX bary (fail-closed).")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = build_dataset(output_path=args.output)
    except DatasetBuildError as exc:
        print(f"FAIL-CLOSED: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK dataset {args.output} session={document['latest_session']} "
        f"symbols={document['symbol_count']} sha256={document['source_dataset_sha256']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
