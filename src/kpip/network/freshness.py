"""Whether a cached index page is still good, asked of the cache alone."""

from __future__ import annotations

import enum
import json
import time

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


class _MissingCacheExpiry(enum.Enum):
    """Single-member enum so ``is not`` narrowing keeps the ``float | None`` type."""

    TOKEN = enum.auto()


MISSING_CACHE_EXPIRY = _MissingCacheExpiry.TOKEN

# A stored entry claiming to come from further in the future than this has
# seen the clock go backwards, and its expiry cannot be trusted either.
_CLOCK_SKEW_TOLERANCE = 60.0


def _cache_control_directives(headers: Any) -> dict[str, str | None]:
    directives: dict[str, str | None] = {}

    for part in (headers.get("Cache-Control") or "").split(","):
        name, _, value = part.strip().partition("=")

        if name:
            directives[name.lower()] = value.strip().strip('"') if value else None

    return directives


def _age(headers: Any) -> int:
    try:
        return max(0, int(headers.get("Age") or 0))
    except (TypeError, ValueError):
        return 0


def freshness_deadline(headers: Any, now: float) -> float | None:
    """When a response received at ``now`` stops being fresh; None if it must not be stored.

    RFC 9111 for a private cache: ``max-age``, else ``Expires`` measured
    against the response's own ``Date`` so a skewed clock on either side
    cancels out, both less the time the response already spent in shared
    caches (``Age``). Without either, or with ``no-cache``, the response is
    stored but stale at once: the next use revalidates it, which costs a 304
    when it has a validator. There is no heuristic freshness, so an index
    that sends no caching headers is never answered from a stale page.
    """

    directives = _cache_control_directives(headers)

    if "no-store" in directives:
        return None

    if "no-cache" in directives and directives["no-cache"] is None:
        return now

    if "max-age" in directives:
        try:
            max_age = int(directives["max-age"] or "")
        except ValueError:
            return now

        return now + max(0, max_age - _age(headers))

    expires = headers.get("Expires")

    if expires:
        import email.utils

        try:
            expires_at = email.utils.parsedate_to_datetime(expires).timestamp()
        except (TypeError, ValueError, OverflowError, IndexError):
            return now

        reference = now
        date = headers.get("Date")

        if date:
            try:
                reference = email.utils.parsedate_to_datetime(date).timestamp()
            except (TypeError, ValueError, OverflowError, IndexError):
                pass

        return now + max(0.0, expires_at - reference - _age(headers))

    return now


def metadata_is_fresh(values: Any, now: float) -> bool:
    """Whether stored cache metadata may still be served without asking the server."""

    try:
        expires_at = float(values["expires_at"])
    except (KeyError, TypeError, ValueError):
        # Entries written before every response got a deadline have none.
        return False

    stored_at = values.get("stored_at")

    if stored_at is not None:
        try:
            if now < float(stored_at) - _CLOCK_SKEW_TOLERANCE:
                return False
        except (TypeError, ValueError):
            return False

    return expires_at > now


def cached_response_is_fresh(
    cache: Any,
    remembered: dict[str, float],
    url: str,
) -> bool:
    """Check cache freshness without reading the cached response body.

    Deliberately separate from the session that usually asks it. This is a
    question about a directory of files -- has an answer been stored, and
    has it expired -- and a resolve whose pages are all still fresh asks it
    for every requirement and then never opens a socket. Answering it
    needed a ``NetworkSession``, which meant the whole vendored HTTP stack
    was imported to establish that nothing would be sent over it.
    """

    if cache is None:
        return False

    now = time.time()

    cached_expiry = remembered.get(url, MISSING_CACHE_EXPIRY)

    if cached_expiry is not MISSING_CACHE_EXPIRY:
        if cached_expiry > now:
            return True

        remembered.pop(url, None)

    metadata = cache.get(url)

    if metadata is None:
        return False

    try:
        values = json.loads(metadata)
    except (TypeError, ValueError):
        return False

    if not isinstance(values, dict) or not metadata_is_fresh(values, now):
        return False

    remembered[url] = float(values["expires_at"])

    return True
