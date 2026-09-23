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


def cached_response_is_fresh(
    cache: Any,
    remembered: dict[str, float | None],
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

    cached_expiry = remembered.get(url, MISSING_CACHE_EXPIRY)

    if cached_expiry is not MISSING_CACHE_EXPIRY:
        if cached_expiry is None or cached_expiry > time.time():
            return True

        remembered.pop(url, None)

    metadata = cache.get(url)

    if metadata is None:
        return False

    try:
        values = json.loads(metadata)

        expires_at = values.get("expires_at")

        expires_at_value = None if expires_at is None else float(expires_at)

        if expires_at_value is not None and expires_at_value <= time.time():
            return False

    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False

    remembered[url] = expires_at_value

    return True
