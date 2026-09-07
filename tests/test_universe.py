import json
from pathlib import Path

from scripts.param_hash import compute_universe_hash

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "config" / "universe.v1.json"
REQUIRED_FIELDS = ("constructed_at", "universe_hash", "source", "exclusion_criteria")


def _load() -> dict:
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))


def test_universe_required_fields_and_size() -> None:
    data = _load()
    for field in REQUIRED_FIELDS:
        assert data.get(field), field
    symbols = data["symbols"]
    assert data["symbol_count"] == len(symbols)
    assert 200 <= len(symbols) <= 250
    assert len(set(symbols)) == len(symbols)
    assert symbols == sorted(symbols)
    assert data["constructed_at"].endswith("Z")


def test_universe_hash_stable_and_matches_file() -> None:
    data = _load()
    hashes = [compute_universe_hash(data["symbols"]) for _ in range(5)]
    assert len(set(hashes)) == 1
    assert hashes[0] == data["universe_hash"]
    assert len(hashes[0]) == 64
