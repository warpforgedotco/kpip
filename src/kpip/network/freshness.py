"""Whether a cached index page is still good, asked of the cache alone."""

from __future__ import annotations

import json
import os
import time

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


COMBINED_MAGIC = b"kpip-http-cache:1\n"
"""Starts an entry holding its metadata and body in one file."""

_COMBINED_HEADER_SIZE = len(COMBINED_MAGIC) + 8


def _sha224_hexdigest(data: bytes) -> str:
    # The interpreter's own SHA-2, not ``hashlib``: that loads OpenSSL, and a
    # lock replayed from the cache would spend more on that import than on
    # every page it checks.  The digests are the same.
    try:
        from _sha2 import sha224  # ty: ignore[unresolved-import]
    except ImportError:
        try:
            from _sha256 import sha224  # ty: ignore[unresolved-import]
        except ImportError:
            from hashlib import sha224

    return sha224(data).hexdigest()


def cache_entry_path(directory: str, key: str) -> str:
    """Where the entry for ``key`` lives: one fan-out level, 256 wide."""

    hashed = _sha224_hexdigest(key.encode())
    return os.path.join(directory, hashed[:2], hashed)


def read_cache_metadata(directory: str, key: str) -> bytes | None:
    """The stored metadata for ``key``, without its body; None when absent.

    A combined entry carries both behind a header; the older layout keeps
    them in two files, and is only an entry while its body is there too.
    """

    path = cache_entry_path(directory, key)

    try:
        with open(path, "rb", buffering=0) as file:
            head = file.read(_COMBINED_HEADER_SIZE)

            if len(head) == _COMBINED_HEADER_SIZE and head.startswith(COMBINED_MAGIC):
                length = int.from_bytes(head[len(COMBINED_MAGIC) :], "little")
                return file.read(length)

            metadata = head + file.read()

        os.stat(path + ".body")
    except OSError:
        return None

    return metadata


class CacheMetadataReader:
    """Just enough of the HTTP cache to ask what it holds, cheap to import."""

    __slots__ = ("directory",)

    def __init__(self, directory: str) -> None:
        self.directory = directory

    def get(self, key: str) -> bytes | None:
        return read_cache_metadata(self.directory, key)


# A stored entry claiming to come from further in the future than this has
# seen the clock go backwards, and its expiry cannot be trusted either.
_CLOCK_SKEW_TOLERANCE = 60.0


def _cache_control_directives(headers: Any) -> dict[str, str | None]:
    directives: dict[str, str | None] = {}

    # Repeated header lines arrive joined with commas, so this also sees a
    # directive given twice across lines.
    for part in (headers.get("Cache-Control") or "").split(","):
        name, _, value = part.strip().partition("=")

        if name:
            # RFC 9111 4.2.1: of a repeated directive, the first counts, so
            # a later one cannot lengthen what an earlier one allowed.
            directives.setdefault(
                name.lower(), value.strip().strip('"') if value else None
            )

    return directives


def _http_date(value: Any) -> float | None:
    if not value:
        return None

    import email.utils

    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError, IndexError):
        return None


def _current_age(headers: Any, now: float) -> float:
    """How old the response already was on arrival (RFC 9111 4.2.3).

    The larger of what shared caches report in ``Age`` and how long ago the
    origin dated it. A client clock that runs ahead only makes responses look
    older, which costs revalidations rather than serving stale pages.
    """

    try:
        age = max(0.0, float(int(headers.get("Age") or 0)))
    except (TypeError, ValueError):
        age = 0.0

    date = _http_date(headers.get("Date"))

    if date is not None:
        age = max(age, now - date)

    return age


def freshness_deadline(headers: Any, now: float) -> float | None:
    """When a response received at ``now`` stops being fresh; None if it must not be stored.

    RFC 9111 for a private cache: the lifetime from ``max-age``, else from
    ``Expires`` measured against the response's own ``Date`` so a skewed
    clock on either side cancels out, less the age the response arrived
    with. Without either, or with ``no-cache``, the response is stored but
    stale at once: the next use revalidates it, which costs a 304 when it has
    a validator. There is no heuristic freshness, so an index that sends no
    caching headers is never answered from a stale page.
    """

    directives = _cache_control_directives(headers)

    if "no-store" in directives:
        return None

    # Qualified or not: a qualified no-cache only allows reuse without the
    # fields it names, and a cached response is served with every header.
    if "no-cache" in directives:
        return now

    if "max-age" in directives:
        try:
            lifetime = float(int(directives["max-age"] or ""))
        except ValueError:
            return now

    else:
        expires = headers.get("Expires")

        if not expires:
            return now

        expires_at = _http_date(expires)

        if expires_at is None:
            return now

        date = _http_date(headers.get("Date"))
        lifetime = expires_at - (date if date is not None else now)

    return now + max(0.0, lifetime - _current_age(headers, now))


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
    remembered: dict[str, tuple[float, float]],
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

    # The expiry is remembered with when it was stored, so a clock that has
    # since gone backwards is caught here just as it is on disk.
    remembered_entry = remembered.get(url)

    if remembered_entry is not None:
        expires_at, stored_at = remembered_entry

        if expires_at > now and now >= stored_at - _CLOCK_SKEW_TOLERANCE:
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

    stored_at = values.get("stored_at")
    remembered[url] = (
        float(values["expires_at"]),
        now if stored_at is None else float(stored_at),
    )

    return True
