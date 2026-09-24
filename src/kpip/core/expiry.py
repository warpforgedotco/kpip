"""When a stored response may still be served, shared by the HTTP cache and
the catalog summaries that record its freshness.  Imports nothing."""

from __future__ import annotations

CLOCK_SKEW_TOLERANCE = 60.0
"""A stored entry claiming to come from further in the future than this has
seen the clock go backwards, and its expiry cannot be trusted either."""


def expiry_is_fresh(expires_at: float, stored_at: float, now: float) -> bool:
    """Whether a response stored at ``stored_at`` may still be served at ``now``."""

    return expires_at > now and now >= stored_at - CLOCK_SKEW_TOLERANCE
