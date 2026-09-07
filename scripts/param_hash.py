"""SHA-256 kanonického strategy JSON. Deterministický napříč běhy."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY_PATH = REPO_ROOT / "config" / "strategy.v1.json"


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_dumps(data: object) -> str:
    """Kompaktní JSON se seřazenými klíči. Stejný vstup = stejný řetězec."""
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_param_hash(path: Path | None = None) -> str:
    data = load_json(path or DEFAULT_STRATEGY_PATH)
    payload = canonical_dumps(data).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_universe_hash(symbols: list[str]) -> str:
    """SHA-256 seřazeného seznamu symbolů. universe_hash se do vstupu nepočítá."""
    payload = canonical_dumps({"symbols": sorted(symbols)}).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SHA-256 kanonického strategy JSON")
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_STRATEGY_PATH,
        help="Cesta k JSON (výchozí: config/strategy.v1.json)",
    )
    args = parser.parse_args(argv)
    print(compute_param_hash(args.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
