from pathlib import Path

from scripts.param_hash import compute_param_hash

STRATEGY_PATH = Path(__file__).resolve().parent.parent / "config" / "strategy.v1.json"
RUNS = 5


def test_param_hash_stable_across_runs() -> None:
    hashes = [compute_param_hash(STRATEGY_PATH) for _ in range(RUNS)]
    assert len(set(hashes)) == 1
    assert len(hashes[0]) == 64
    assert all(c in "0123456789abcdef" for c in hashes[0])


def test_param_hash_matches_recorded_digest() -> None:
    recorded = (STRATEGY_PATH.with_suffix(".sha256")).read_text(encoding="utf-8").strip()
    assert compute_param_hash(STRATEGY_PATH) == recorded
