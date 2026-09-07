from __future__ import annotations

import threading

from scripts.fence import (
    EXIT_OK,
    EXIT_UNCERTAIN,
    OUTCOME_SKIP,
    STATE_CONFIRMED,
    STATE_NEVER_SENT,
    STATE_PENDING,
    STATE_UNCERTAIN,
    MemoryFenceStore,
    can_release_never_sent,
    make_fence_key,
    release_never_sent,
    run_fenced_send,
)


def test_fence_key_format() -> None:
    assert make_fence_key("2026-09-07", "AAPL", "abc") == "2026-09-07:AAPL:abc"


def test_existing_fence_skips_without_broker_call() -> None:
    store = MemoryFenceStore()
    store.insert_pending("2026-09-07:AAPL:p", "AAPL", "2026-09-07")
    calls = {"n": 0}

    def send() -> dict:
        calls["n"] += 1
        return {"order_id": "1"}

    result = run_fenced_send(
        session_date="2026-09-07",
        symbol="AAPL",
        param_hash="p",
        store=store,
        send_once=send,
    )
    assert result.outcome == OUTCOME_SKIP
    assert result.exit_code == EXIT_OK
    assert calls["n"] == 0


def test_timeout_is_uncertain_nonzero_exit_no_retry() -> None:
    store = MemoryFenceStore()
    calls = {"n": 0}

    def send() -> dict:
        calls["n"] += 1
        raise TimeoutError("timed out")

    result = run_fenced_send(
        session_date="2026-09-07",
        symbol="MSFT",
        param_hash="p",
        store=store,
        send_once=send,
    )
    assert result.outcome == STATE_UNCERTAIN
    assert result.exit_code == EXIT_UNCERTAIN
    assert result.exit_code != 0
    assert calls["n"] == 1
    row = store.get(result.fence_key)
    assert row is not None
    assert row.state == STATE_UNCERTAIN


def test_concurrent_claim_only_one_sends() -> None:
    store = MemoryFenceStore()
    barrier = threading.Barrier(2)
    sends = {"n": 0}
    lock = threading.Lock()
    results: list = []

    def send() -> dict:
        with lock:
            sends["n"] += 1
        return {"order_id": "ok"}

    def worker() -> None:
        barrier.wait()
        results.append(
            run_fenced_send(
                session_date="2026-09-07",
                symbol="NVDA",
                param_hash="p",
                store=store,
                send_once=send,
            )
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    outcomes = sorted(item.outcome for item in results)
    assert outcomes == [STATE_CONFIRMED, OUTCOME_SKIP]
    assert sends["n"] == 1


def test_success_readback_confirms() -> None:
    store = MemoryFenceStore()
    result = run_fenced_send(
        session_date="2026-09-07",
        symbol="AAPL",
        param_hash="p",
        store=store,
        send_once=lambda: {"order_id": "99"},
        readback=lambda payload: payload["order_id"] == "99",
    )
    assert result.outcome == STATE_CONFIRMED
    assert result.exit_code == EXIT_OK
    assert store.get(result.fence_key).state == STATE_CONFIRMED


def test_never_sent_requires_both_conditions() -> None:
    store = MemoryFenceStore()
    key = "2026-09-07:AAPL:p"
    store.insert_pending(key, "AAPL", "2026-09-07")
    assert can_release_never_sent(has_send_artifact=True, broker_unchanged=True) is False
    assert can_release_never_sent(has_send_artifact=False, broker_unchanged=False) is False
    assert can_release_never_sent(has_send_artifact=False, broker_unchanged=True) is True
    assert release_never_sent(store, key, has_send_artifact=True, broker_unchanged=True) == STATE_UNCERTAIN
    assert store.get(key).state == STATE_PENDING
    assert (
        release_never_sent(store, key, has_send_artifact=False, broker_unchanged=True) == STATE_NEVER_SENT
    )
    assert store.get(key).state == STATE_NEVER_SENT
