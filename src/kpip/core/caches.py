"""Process-global caches: one eviction policy, one registry, one reset.

Two kinds of cache exist in the core modules and each has one spelling:

* a **table** (``dict``) interns values by text inside a constructor --
  ``Version(text)``, ``parse_requirement(text)`` -- and is bounded with
  :func:`bounded_put`;
* a **memo** (:func:`memoized`, ``functools.lru_cache``'s C cache) caches a
  pure derived function.

Both are registered where they are defined, with :func:`register_table` or
:func:`register_clear`, so :func:`clear_all` empties every one of them. The
benchmarks call it between iterations; a cache that is not registered here
silently turns a cold benchmark into a warm one, which is why
``tests/core/test_cache_registry.py`` enumerates the core modules and fails
on an unregistered cache.

Eviction is a clear-all sweep at the limit rather than LRU bookkeeping:
the tables are read on hot paths where a plain ``dict`` lookup is the whole
point, and a sweep costs one rebuild of the working set every few thousand
distinct texts.

Thread safety: none of this is synchronised. Every cached value is an
immutable value type that compares equal to whatever a concurrent caller
would have built in its place, so a lost store or a sweep that races with a
lookup is a cache miss, never a wrong answer.
"""

from __future__ import annotations

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

_CLEARERS: list[Callable[[], None]] = []


def bounded_put(table: dict, key: object, value: object, limit: int) -> None:
    """Store ``value`` under ``key``, sweeping the table first once it is full."""
    if len(table) >= limit:
        table.clear()
    table[key] = value


def register_table(table: dict) -> dict:
    """Register an intern table for :func:`clear_all`; returns it for inline use."""
    _CLEARERS.append(table.clear)
    return table


def register_clear(clear: Callable[[], None]) -> None:
    """Register a ``cache_clear``-style callable for :func:`clear_all`."""
    _CLEARERS.append(clear)


def clear_all() -> None:
    """Empty every registered cache."""
    for clear in _CLEARERS:
        clear()


class CacheInfo:
    """What ``cache_info()`` of a :func:`memoized` function reports."""

    __slots__ = ("hits", "misses", "maxsize", "currsize")

    def __init__(self, hits: int, misses: int, maxsize: int, currsize: int) -> None:
        self.hits = hits
        self.misses = misses
        self.maxsize = maxsize
        self.currsize = currsize

    def __repr__(self) -> str:
        return (
            f"CacheInfo(hits={self.hits}, misses={self.misses}, "
            f"maxsize={self.maxsize}, currsize={self.currsize})"
        )


def memoized(maxsize: int) -> Callable[[Callable[..., Any]], Any]:
    """``functools.lru_cache`` that registers itself for :func:`clear_all`."""

    # The C cache ``lru_cache`` itself returns, taken directly: importing
    # ``functools`` brings ``collections``, ``operator`` and ``reprlib`` for
    # its namedtuple and wrapper helpers, some 2.5 ms of a compiled kpip's
    # start, and a memoized function is defined on every command's path.
    try:
        from _functools import _lru_cache_wrapper  # ty: ignore[unresolved-import]
    except ImportError:
        from functools import lru_cache

        def decorate(function: Callable[..., Any]) -> Any:
            wrapped = lru_cache(maxsize=maxsize)(function)
            _CLEARERS.append(wrapped.cache_clear)
            return wrapped

        return decorate

    def decorate(function: Callable[..., Any]) -> Any:
        wrapped = _lru_cache_wrapper(function, maxsize, False, CacheInfo)
        for attribute in ("__module__", "__name__", "__qualname__", "__doc__"):
            setattr(wrapped, attribute, getattr(function, attribute))
        wrapped.__wrapped__ = function
        _CLEARERS.append(wrapped.cache_clear)
        return wrapped

    return decorate
