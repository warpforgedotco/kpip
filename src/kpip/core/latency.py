"""How long the index takes to answer, as this process has seen it.

The resolver's prefetchers read this to decide how many fetches to run at
once (``kpip.index.prefetch``); the network session notes each request. Only
requests that reach the network count: an answer from the cache says nothing
about the link.
"""

from __future__ import annotations

import threading
from collections import deque

_SETTLED_AFTER = 8
"""Requests to see before an estimate is given: the first few carry the
connection and TLS setup that the rest do not."""

_lock = threading.Lock()
_recent: deque[float] = deque(maxlen=32)
_typical: float | None = None


def observe(seconds: float) -> None:
    """Note one request's time to its response headers."""
    global _typical

    with _lock:
        _recent.append(seconds)

        if len(_recent) >= _SETTLED_AFTER:
            # The median of the latest: a large page or a slow mirror now and
            # then says little about the link, and an average would follow it.
            ordered = sorted(_recent)
            _typical = ordered[len(ordered) // 2]


def typical() -> float | None:
    """The recent time to a response, or None before there is a fair one."""
    return _typical


def reset() -> None:
    """Forget what was seen; for tests."""
    global _typical

    with _lock:
        _recent.clear()
        _typical = None
