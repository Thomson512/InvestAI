import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scripts.build_dataset import (
    DatasetBuildError,
    build_dataset,
    chunk_symbols,
    request_symbols,
)
from scripts.param_hash import compute_universe_hash

FIXED_NOW = datetime(2026, 9, 7, 20, 21, tzinfo=timezone.utc)


def _bar(day: str, close: float = 10.0) -> dict:
    return {
        "t": f"{day}T04:00:00Z",
        "o": close,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": 1000,
        "n": 10,
        "vw": close,
    }


def _universe(tmp_path: Path, symbols: list[str]) -> Path:
    path = tmp_path / "universe.json"
    path.write_text(
        json.dumps(
            {
                "universe_hash": compute_universe_hash(symbols),
                "symbols": symbols,
            }
        ),
        encoding="utf-8",
    )
    return path


def _liquidity(tmp_path: Path, **overrides) -> Path:
    payload = {
        "schema_version": "1.0",
        "feed": "alpaca_iex",
        "alpaca_feed": "iex",
        "timeframe": "1Day",
        "adjustment": "all",
        "lookback_sessions": 400,
        "lookback_calendar_days": 650,
        "max_symbols_per_request": 100,
        "min_avg_daily_dollar_volume_iex": None,
    }
    payload.update(overrides)
    path = tmp_path / "liquidity.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeAlpaca:
    def __init__(self, bars: dict[str, list[dict]], fail_on_chunk: int | None = None) -> None:
        self.bars = bars
        self.fail_on_chunk = fail_on_chunk
        self.urls: list[str] = []
        self.chunk_index = 0

    def __call__(self, url: str, headers: dict[str, str]) -> dict:
        self.urls.append(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert query["feed"] == ["iex"]
        assert query["timeframe"] == ["1Day"]
        assert "APCA-API-KEY-ID" in headers
        assert "APCA-API-SECRET-KEY" in headers
        symbols = query["symbols"][0].split(",")
        if self.fail_on_chunk is not None and self.chunk_index == self.fail_on_chunk:
            self.chunk_index += 1
            raise DatasetBuildError("simulované selhání chunku")
        self.chunk_index += 1
        return {"bars": {symbol: self.bars[symbol] for symbol in symbols}, "next_page_token": None}


def test_chunks_never_exceed_100() -> None:
    symbols = [f"S{i:03d}" for i in range(251)]
    chunks = chunk_symbols(request_symbols(symbols), 100)
    assert max(len(chunk) for chunk in chunks) <= 100
    assert sum(len(chunk) for chunk in chunks) == 252
    assert chunks[-1][-1] == "SPY"


def test_build_dataset_writes_required_fields(tmp_path: Path) -> None:
    symbols = [f"S{i:03d}" for i in range(120)]
    bars = {symbol: [_bar("2026-09-03"), _bar("2026-09-04")] for symbol in symbols + ["SPY"]}
    output = tmp_path / "dataset.json"
    document = build_dataset(
        universe_path=_universe(tmp_path, symbols),
        liquidity_path=_liquidity(tmp_path),
        output_path=output,
        env={"ALPACA_KEY_ID": "test-key", "ALPACA_SECRET_KEY": "test-secret"},
        http_get=FakeAlpaca(bars),
        now=FIXED_NOW,
    )
    assert output.is_file()
    assert set(document) >= {
        "generated_at",
        "universe_hash",
        "source_dataset_sha256",
        "latest_session",
    }
    assert document["generated_at"] == "2026-09-07T20:21:00Z"
    assert document["latest_session"] == "2026-09-04"
    assert document["universe_hash"] == compute_universe_hash(symbols)
    assert document["feed"] == "alpaca_iex"
    assert "SPY" in document["bars"]
    assert len(document["bars"]) == 121
    assert len(document["source_dataset_sha256"]) == 64
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["source_dataset_sha256"] == document["source_dataset_sha256"]


def test_fail_closed_does_not_write_or_clobber(tmp_path: Path) -> None:
    symbols = [f"S{i:03d}" for i in range(120)]
    bars = {symbol: [_bar("2026-09-04")] for symbol in symbols + ["SPY"]}
    output = tmp_path / "dataset.json"
    output.write_text('{"keep": true}\n', encoding="utf-8")
    with pytest.raises(DatasetBuildError, match="selhání chunku"):
        build_dataset(
            universe_path=_universe(tmp_path, symbols),
            liquidity_path=_liquidity(tmp_path),
            output_path=output,
            env={"ALPACA_KEY_ID": "test-key", "ALPACA_SECRET_KEY": "test-secret"},
            http_get=FakeAlpaca(bars, fail_on_chunk=1),
            now=FIXED_NOW,
        )
    assert json.loads(output.read_text(encoding="utf-8")) == {"keep": True}
    assert not (tmp_path / "dataset.json.tmp").exists()


def test_missing_credentials_fail_before_http(tmp_path: Path) -> None:
    symbols = ["AAPL"]
    output = tmp_path / "dataset.json"

    def forbidden(url: str, headers: dict[str, str]) -> dict:
        raise AssertionError("CI nesmí volat Alpaca")

    with pytest.raises(DatasetBuildError, match="ALPACA_KEY_ID"):
        build_dataset(
            universe_path=_universe(tmp_path, symbols),
            liquidity_path=_liquidity(tmp_path),
            output_path=output,
            env={},
            http_get=forbidden,
            now=FIXED_NOW,
        )
    assert not output.exists()
