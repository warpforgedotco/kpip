from __future__ import annotations

import threading
import time

import pytest
from kpip.index.prefetch import Prefetcher, PrefetchPolicy, fetches_at_once


def test_prefetch_policy_prefers_fast_high_yield_sources() -> None:
    policy = PrefetchPolicy()
    policy.observe("slow", 1.0, 10)
    policy.observe("fast", 0.1, 10)

    assert policy.priority("fast") > policy.priority("slow")


def test_prefetcher_deduplicates_and_overlaps_work() -> None:
    lock = threading.Lock()
    active = 0
    maximum = 0
    calls: list[str] = []
    started = threading.Event()

    def load(value: str) -> str:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(value)
        started.set()
        time.sleep(0.05)
        with lock:
            active -= 1
        return value.upper()

    prefetcher = Prefetcher(load, max_workers=2)
    try:
        prefetcher.submit("first", "first")
        prefetcher.submit("first", "duplicate")
        prefetcher.submit("second", "second")
        assert started.wait(1)
        assert prefetcher.take("first").result() == "FIRST"
        assert prefetcher.take("second").result() == "SECOND"
        assert calls == ["first", "second"]
        assert maximum == 2
    finally:
        prefetcher.close()


def test_prefetcher_runs_a_key_once_though_it_was_taken() -> None:
    """Taking a result does not make its key new again: a page asked for
    between the take and its result being stored was fetched a second time."""
    calls: list[str] = []

    def load(value: str) -> str:
        calls.append(value)
        return value.upper()

    prefetcher = Prefetcher(load, max_workers=2)
    try:
        assert prefetcher.submit("page", "page")
        assert prefetcher.take("page").result() == "PAGE"

        assert not prefetcher.submit("page", "page")
        assert prefetcher.take("page") is None
        assert not prefetcher.pending("page")
    finally:
        prefetcher.close()

    assert calls == ["page"]


def test_prefetcher_propagates_loader_errors() -> None:
    def load(value: str) -> str:
        raise ValueError(value)

    prefetcher = Prefetcher(load, max_workers=1)
    try:
        prefetcher.submit("failure", "broken")
        with pytest.raises(ValueError, match="broken"):
            prefetcher.take("failure").result()
    finally:
        prefetcher.close()


@pytest.fixture
def link(monkeypatch: pytest.MonkeyPatch):
    """A link answering in ``seconds``, as the network session would note it."""
    from kpip.core import latency

    latency.reset()

    def answering_in(seconds: float) -> None:
        latency.reset()
        for _ in range(8):
            latency.observe(seconds)

    yield answering_in
    latency.reset()


def test_fetches_at_once_follow_how_long_the_index_takes(link) -> None:
    assert fetches_at_once(32) == 32  # nothing seen yet: all of them

    link(0.010)
    assert fetches_at_once(32) == 4

    link(0.050)
    assert 4 < fetches_at_once(32) < 32

    link(0.200)
    assert fetches_at_once(32) == 32


@pytest.mark.parametrize("seconds, expected", [(0.005, 4), (0.5, 16)])
def test_a_pool_runs_only_as_many_fetches_as_the_link_calls_for(
    link, seconds: float, expected: int
) -> None:
    link(seconds)
    lock = threading.Lock()
    running = 0
    peak = 0
    started = threading.Barrier(expected, timeout=5)

    def load(value: int) -> int:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            # The first ``expected`` meet here, so the peak is reached.
            if value < expected:
                started.wait()
            time.sleep(0.02)
        finally:
            with lock:
                running -= 1
        return value

    prefetcher = Prefetcher(load, max_workers=16)
    for value in range(16):
        prefetcher.submit(value, value)
    results = [prefetcher.take(value).result(timeout=10) for value in range(16)]
    prefetcher.close()

    assert results == list(range(16))
    assert peak == expected
