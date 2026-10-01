from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.buyer import (
    OUTCOME_FRESHNESS_FAIL,
    OUTCOME_NO_CANDIDATES,
    BuyerResult,
    fetch_quotes,
    parse_cnb_usdczk,
    run_buyer,
    shortlist_age_hours,
    spread_bps,
    write_evidence,
)
from scripts.buyer_summary import (
    outcome_line,
    streak_should_warn,
    streak_warning,
)
from scripts.param_hash import compute_param_hash
from scripts.submit_order import SubmitResult

NOW = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
STRATEGY = json.loads(Path("config/strategy.v1.json").read_text(encoding="utf-8"))
CONFIG = {
    "freshness_max_age_hours": 72,
    "max_spread_bps": 20,
    "quote_feed": "iex",
    "fx_url": "https://example.test/denni_kurz.txt",
}
CNB_TEXT = "07.09.2026 #174\nzemě|měna|množství|kód|kurz\nUSA|dolar|1|USD|21,000\n"
QUOTE_AAPL = {"quotes": {"AAPL": {"bp": 200.0, "ap": 200.1}}}


def _shortlist(*rows: dict, asof: str = "2026-09-04") -> dict:
    return {"asof_session": asof, "signals": list(rows)}


def _buy(symbol: str = "AAPL", score: float = 20.0, stop: float = 180.0) -> dict:
    return {
        "symbol": symbol,
        "decision": "BUY_CANDIDATE",
        "score": score,
        "stop_price": stop,
        "entry_price": 200.0,
        "target_price": 290.0,
    }


def _http(quotes: dict | None = None, cnb: str = CNB_TEXT):
    quotes = quotes or QUOTE_AAPL

    def getter(url: str, headers: dict[str, str]) -> object:
        if "denni_kurz" in url:
            return cnb
        if "quotes" in url:
            return quotes
        raise AssertionError(url)

    return getter


def _submitted(order_id: str = "99") -> SubmitResult:
    return SubmitResult(
        outcome="CONFIRMED",
        exit_code=0,
        dry_run=False,
        buy_order_id=order_id,
        stop_order_id="s1",
        reason="confirmed",
        fence_key="k",
    )


def test_cnb_parses_usd_comma() -> None:
    assert parse_cnb_usdczk(CNB_TEXT) == 21.0


def test_spread_bps() -> None:
    assert round(spread_bps(200.0, 200.1), 2) == 5.0
    assert spread_bps(0, 1) is None


def test_freshness_weekend_gap_ok() -> None:
    hours = shortlist_age_hours("2026-09-04", NOW)
    assert hours < 72
    assert shortlist_age_hours("2026-09-01", NOW) > 72


def test_freshness_fail_before_blockers() -> None:
    snapshot = {
        "positions": [{"ticker": "MSFT_US_EQ", "quantity": 1, "value_czk": 5000}],
        "active_orders": [],
    }
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy(), asof="2026-09-01"),
        snapshot=snapshot,
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=lambda url, headers: (_ for _ in ()).throw(AssertionError("HTTP")),
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == OUTCOME_FRESHNESS_FAIL


def test_global_blocker_before_candidates_and_quotes() -> None:
    snapshot = {
        "positions": [{"ticker": "MSFT_US_EQ", "quantity": 1, "value_czk": 5000}],
        "active_orders": [],
    }
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot=snapshot,
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=lambda url, headers: (_ for _ in ()).throw(AssertionError("quotes až po blocích")),
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == "GLOBAL_BLOCKER:unprotected_position:MSFT_US_EQ"
    assert result.extra_summary == [
        "FIX: gh workflow run protective-stop-guard.yml -f symbol=MSFT"
    ]


def test_can_trade_maps_to_global_blocker() -> None:
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (False, "KILL_SWITCH_OFF"),
        http_get=lambda url, headers: (_ for _ in ()).throw(AssertionError("HTTP")),
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == "GLOBAL_BLOCKER:KILL_SWITCH_OFF"
    assert result.exit_code == 0


def test_unresolved_fence_fails_job() -> None:
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (False, "UNRESOLVED_FENCE"),
        http_get=lambda url, headers: (_ for _ in ()).throw(AssertionError("HTTP")),
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == "GLOBAL_BLOCKER:UNRESOLVED_FENCE"
    assert result.exit_code == 1
    assert any("UNRESOLVED_FENCE" in line for line in result.extra_summary)


def test_no_candidates() -> None:
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist({"symbol": "AAPL", "decision": "REJECT", "score": 0}),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=lambda url, headers: (_ for _ in ()).throw(AssertionError("HTTP")),
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == OUTCOME_NO_CANDIDATES


def test_spread_rejected() -> None:
    wide = {"quotes": {"AAPL": {"bp": 200.0, "ap": 201.0}}}
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=_http(wide),
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == "SPREAD_REJECTED:AAPL"


def test_fence_exists() -> None:
    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        env={},
        http_get=_http(),
        submit_fn=lambda **kwargs: SubmitResult(
            outcome="SKIP", exit_code=0, dry_run=False, reason="fence_exists", fence_key="k"
        ),
    )
    assert result.outcome == "FENCE_EXISTS:AAPL"


def test_order_submitted() -> None:
    captured: dict = {}

    def submit(**kwargs):
        captured.update(kwargs)
        return _submitted("777")

    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        env={},
        http_get=_http(),
        submit_fn=submit,
    )
    assert result.outcome == "ORDER_SUBMITTED:777"
    assert captured["symbol"] == "AAPL"
    assert captured["quantity"] == 1.0
    assert captured["stop_price"] == 180.0
    assert captured["dry_run"] is False


def test_outcome_line_and_evidence_field(tmp_path: Path) -> None:
    line = outcome_line("ORDER_SUBMITTED:1")
    assert line == "OUTCOME: ORDER_SUBMITTED:1"
    result = BuyerResult(outcome="NO_CANDIDATES")
    path = tmp_path / "buyer_evidence.json"
    write_evidence(path, result, "2026-09-07")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["outcome"] == "NO_CANDIDATES"
    assert compute_param_hash()


def test_streak_warns_only_on_unhealthy_24h() -> None:
    now = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)
    bad = [
        {"at": "2026-09-07T10:00:00Z", "outcome": "FRESHNESS_FAIL"},
        {"at": "2026-09-07T14:00:00Z", "outcome": "GLOBAL_BLOCKER:KILL_SWITCH_OFF"},
    ]
    assert streak_should_warn(bad, now) is True
    mixed = bad + [{"at": "2026-09-07T16:00:00Z", "outcome": "NO_CANDIDATES"}]
    assert streak_should_warn(mixed, now) is False
    closed = [{"at": "2026-09-06T18:00:00Z", "outcome": "MARKET_CLOSED"}]
    assert streak_should_warn(closed, now) is False
    warning = streak_warning(bad, now)
    assert warning is not None
    assert warning.startswith("::warning::")


def test_unresolved_fence_summary_exits_nonzero(tmp_path: Path, capsys) -> None:
    from scripts.buyer_summary import main as summary_main

    evidence = tmp_path / "buyer_evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "outcome": "GLOBAL_BLOCKER:UNRESOLVED_FENCE",
                "session_date": "2026-09-30",
                "generated_at": "2026-09-30T13:40:26Z",
                "official": True,
                "extra_summary": [],
            }
        ),
        encoding="utf-8",
    )
    code = summary_main(["--evidence", str(evidence), "--history", str(tmp_path / "h.jsonl")])
    assert code == 1
    assert "UNRESOLVED_FENCE" in capsys.readouterr().out


def test_council_reject_skips_submit() -> None:
    def council(**kwargs):
        return {"verdict": "REJECT", "reason": "Claude CRO rejected the proposal", "findings": []}

    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=_http(),
        council_fn=council,
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == "COUNCIL_REJECT:AAPL"
    assert result.exit_code == 0
    assert result.evidence["council"]["verdict"] == "REJECT"


def test_council_unavailable_skips_submit() -> None:
    from scripts.council import CouncilUnavailable

    def unavailable(**kwargs):
        raise CouncilUnavailable("schema")

    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=_http(),
        council_fn=unavailable,
        submit_fn=lambda **kwargs: (_ for _ in ()).throw(AssertionError("submit")),
    )
    assert result.outcome == "COUNCIL_UNAVAILABLE:AAPL"
    assert result.exit_code == 0


def test_council_approve_submits_once_without_resizing() -> None:
    captured: dict = {}

    def council(**kwargs):
        return {"verdict": "APPROVE", "reason": "4/6", "findings": [{"agent": "Claude CRO"}]}

    def submit(**kwargs):
        captured.update(kwargs)
        return _submitted("42")

    result = run_buyer(
        session_date="2026-09-07",
        shortlist=_shortlist(_buy()),
        snapshot={"positions": [], "active_orders": []},
        config=CONFIG,
        strategy=STRATEGY,
        now=NOW,
        can_trade_fn=lambda: (True, "OK"),
        http_get=_http(),
        council_fn=council,
        submit_fn=submit,
    )
    assert result.outcome == "ORDER_SUBMITTED:42"
    assert captured["quantity"] == 1.0
    assert captured["stop_price"] == 180.0
    assert result.evidence["council"]["verdict"] == "APPROVE"


def test_fetch_quotes_uses_iex_feed() -> None:
    seen: list[str] = []

    def getter(url: str, headers: dict[str, str]) -> object:
        seen.append(url)
        return {"quotes": {"AAPL": {"bp": 1, "ap": 1.01}}}

    quotes = fetch_quotes(["AAPL"], env={"ALPACA_KEY_ID": "k", "ALPACA_SECRET_KEY": "s"}, http_get=getter)
    assert "feed=iex" in seen[0]
    assert quotes["AAPL"]["ask"] == 1.01
