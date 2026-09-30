"""Bounded background work used by the resolver's catalog prefetcher."""

from __future__ import annotations

from collections.abc import Hashable
from math import ceil
from threading import Condition, RLock
from typing import Callable, Generic, TypeVar

from kpip.core import latency

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from concurrent.futures import Future

T = TypeVar("T")
V = TypeVar("V")


class PrefetchPolicy:
    """Small adaptive scorer for independent background catalog work."""

    __slots__ = ("latency", "yield_count")

    def __init__(self) -> None:
        self.latency: dict[Hashable, float] = {}
        self.yield_count: dict[Hashable, float] = {}

    def observe(self, key: Hashable, elapsed: float, result_count: int) -> None:
        previous_latency = self.latency.get(key)
        previous_yield = self.yield_count.get(key)
        if previous_latency is None:
            self.latency[key] = elapsed
            self.yield_count[key] = float(result_count)
            return
        self.latency[key] = previous_latency * 0.75 + elapsed * 0.25
        self.yield_count[key] = (previous_yield or 0.0) * 0.75 + result_count * 0.25

    def priority(self, key: Hashable) -> float:
        latency = self.latency.get(key)
        if latency is None:
            return 0.0
        return (self.yield_count.get(key, 0.0) + 1.0) / max(latency, 1e-6)


_CPU_PER_FETCH = 0.0025
"""Interpreter time one fetch takes to handle, headers to parsed page, in
seconds: what a request's wait has to cover for another to be worth running
alongside it."""

_FEWEST_AT_ONCE = 4


def fetches_at_once(ceiling: int) -> int:
    """How many fetches to run at once, from how long the index takes.

    As many as keep the interpreter busy while requests are out: the time one
    takes, over the time handling one takes. A cold jupyter lock ran 16%
    faster with 4 at once than 32 on a link answering in 10 ms -- every extra
    thread only took turns under the interpreter lock -- and 2.3 times slower
    on one adding 100 ms, where it is the waiting that the threads overlap.
    Until the link is known, all of them.
    """
    seen = latency.typical()

    if seen is None:
        return ceiling

    return max(_FEWEST_AT_ONCE, min(ceiling, ceil(seen / _CPU_PER_FETCH)))


class _Gate:
    """Lets :func:`fetches_at_once` tasks of a pool run at a time.

    The pool keeps all its threads; those over the limit wait here, asleep,
    rather than contend for the interpreter lock -- woken when a running one
    finishes, not polling, which would contend for it again. The limit is
    read again each time, as the link becomes known.
    """

    __slots__ = ("ceiling", "condition", "running")

    def __init__(self, ceiling: int) -> None:
        self.ceiling = ceiling
        self.condition = Condition()
        self.running = 0

    def __enter__(self) -> None:
        with self.condition:
            while self.running >= fetches_at_once(self.ceiling):
                # A finishing task wakes one; the timeout only lets a limit
                # raised by a slower link be noticed without one.
                self.condition.wait(1.0)

            self.running += 1

    def __exit__(self, *exc_info: object) -> None:
        with self.condition:
            self.running -= 1
            self.condition.notify()


class Prefetcher(Generic[T, V]):
    """Submit each keyed task once and consume it deterministically."""

    def __init__(self, loader: Callable[[V], T], max_workers: int) -> None:
        from concurrent.futures import ThreadPoolExecutor

        gate = _Gate(max_workers)

        def gated(value: V) -> T:
            with gate:
                return loader(value)

        self.loader = gated
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.futures: dict[Hashable, Future[T]] = {}
        # Every key ever submitted. A key whose future was taken is not new:
        # its result is on its way to wherever its consumer keeps it, and
        # until it is there, a second request for it looked unserved and ran
        # the task again.
        self.submitted: set[Hashable] = set()
        self.lock = RLock()
        self.closed = False

    def submit(self, key: Hashable, value: V) -> bool:
        with self.lock:
            if self.closed or key in self.submitted:
                return False
            self.submitted.add(key)
            self.futures[key] = self.executor.submit(self.loader, value)
            return True

    def take(self, key: Hashable) -> Future[T] | None:
        with self.lock:
            return self.futures.pop(key, None)

    def peek(self, key: Hashable) -> Future[T] | None:
        """The pending future for ``key`` without consuming it."""
        with self.lock:
            return self.futures.get(key)

    def pending(self, key: Hashable) -> bool:
        with self.lock:
            return key in self.futures

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
