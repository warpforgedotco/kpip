"""Replay a lock whose inputs, and every index page it read, are unchanged.

A lock against an index is a function of its inputs, the interpreter it is
for, the code doing it, and the index pages it read; the wheels those pages
point at are named by hash, so their metadata cannot change underneath it.
When none of that has changed the answer cannot have either, and a warm lock
can write the answer it wrote last time instead of resolving again.

Deliberately light: the pre-startup fast path asks this before anything that
resolves, and a replay must not import the resolver or the HTTP client.
"""

from __future__ import annotations

import marshal
import os
import sys
import time

from kpip.core.appdirs import http_cache_path
from kpip.core.code_identity import code_identity
from kpip.core.utils import key_bytes, versioned_bucket
from kpip.network.freshness import (
    CacheMetadataReader,
    decode_metadata,
    metadata_is_fresh,
)

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import Protocol

    class MetadataCache(Protocol):
        def get(self, key: str) -> bytes | None: ...


REPLAY_BUCKET = versioned_bucket("lock-replay", 1)
"""Directory under the cache directory holding replayable locks."""

REPLAY_FORMAT = 3

FRESH = "fresh"
"""Every page is unchanged and still fresh: the lock can be replayed."""

STALE_SAME = "stale"
"""Every page is unchanged, but some must be revalidated before replaying."""

CHANGED = "changed"
"""A page changed, or the record cannot be trusted: resolve again."""

PageValidators = tuple[str, "str | None", "str | None"]
"""A page URL with the ETag and Last-Modified it had when the lock read it."""


class ReplayRecord:
    __slots__ = ("pages", "rendered")

    def __init__(self, pages: tuple[PageValidators, ...], rendered: str) -> None:
        self.pages = pages
        self.rendered = rendered


def _plain_requirement(value: str) -> bool:
    """A requirement the index alone answers: no URL, path or archive."""

    return not (
        "://" in value
        or "/" in value
        or "\\" in value
        or value.startswith((".", "-"))
        or value.endswith((".whl", ".tar.gz", ".zip"))
        or os.path.exists(value)
    )


def _simple_requirement_file(path: str) -> bytes | None:
    """The file's requirement lines, if it only lists requirements.

    Options mean no replay. Comments and blank lines are left out, so a
    lock whose file changed only in those still replays; the requirements
    keep their order, which breaks ties in the resolve.
    """

    if os.path.basename(path).startswith("pylock") and path.endswith(".toml"):
        return None

    try:
        with open(path, "rb") as file:
            content = file.read()
    except OSError:
        return None

    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None

    requirements = []

    for line in lines:
        text = line.strip()

        # Options (-r, -c, -e, --index-url...) and continuations change what
        # the file means beyond its own lines.
        if text.startswith("-") or text.endswith("\\"):
            return None

        if not text or text.startswith("#"):
            continue

        if not _plain_requirement(text.split(";")[0].strip()):
            return None

        requirements.append(text)

    return "\n".join(requirements).encode("utf-8")


def _environment() -> tuple[object, ...]:
    """Everything outside the inputs that markers or wheel tags can see."""

    if sys.platform == "win32":
        host: tuple[object, ...] = tuple(sys.getwindowsversion()[:4])  # type: ignore[attr-defined]
        machine = os.environ.get("PROCESSOR_ARCHITECTURE", "")
    else:
        uname = os.uname()
        host = (uname.sysname, uname.release, uname.version)
        machine = uname.machine

    libc = ""
    confstr = getattr(os, "confstr", None)

    if confstr is not None:
        try:
            libc = confstr("CS_GNU_LIBC_VERSION") or ""
        except (OSError, ValueError):
            libc = ""

    return (
        sys.version,
        sys.implementation.name,
        sys.implementation.cache_tag,
        getattr(sys, "abiflags", ""),
        sys.platform,
        os.name,
        machine,
        host,
        libc,
        os.environ.get("MACOSX_DEPLOYMENT_TARGET", ""),
        os.environ.get("_PYTHON_HOST_PLATFORM", ""),
    )


def replay_key(
    *,
    requirements: Sequence[str],
    requirement_files: Sequence[str],
    constraint_files: Sequence[str],
    index_urls: Sequence[str],
    no_binary: Iterable[str] = (),
    no_build_isolation: bool = False,
    python_version: str | None = None,
    previous_lock: str = "",
) -> bytes | None:
    """What a replayable lock is keyed on, or None if this lock is not one.

    ``previous_lock`` is ``previous_lock_digest`` of the lock this one starts
    from, whose versions it prefers.

    The command line and the fast path both call this with the options as
    given, so they agree without parsing requirement files the same way:
    files are keyed on their bytes, and any file that does more than list
    requirements is declined.
    """

    if not requirements and not requirement_files:
        return None

    if not all(
        _plain_requirement(value.split(";")[0].strip()) for value in requirements
    ):
        return None

    files = []

    for path in (*requirement_files, *constraint_files):
        content = _simple_requirement_file(path)

        if content is None:
            return None

        files.append(content)

    key = (
        REPLAY_FORMAT,
        code_identity(),
        _environment(),
        tuple(index_urls),
        tuple(requirements),
        tuple(files[: len(requirement_files)]),
        tuple(files[len(requirement_files) :]),
        tuple(sorted(no_binary)),
        no_build_isolation,
        python_version or "",
        previous_lock,
    )

    return key_bytes(key)


def _digest(value: bytes) -> str:
    digest = 14695981039346656037

    for byte in value:
        digest = (digest ^ byte) * 1099511628211 & 0xFFFFFFFFFFFFFFFF

    return f"{digest:016x}"


def record_path(cache_dir: str, key: bytes) -> str:
    return os.path.join(cache_dir, REPLAY_BUCKET, f"{_digest(key)}.cache")


def load_record(cache_dir: str, key: bytes) -> ReplayRecord | None:
    """The record stored for ``key``; anything unreadable or foreign is a miss."""

    try:
        with open(record_path(cache_dir, key), "rb") as file:
            stored_key, pages, rendered = marshal.loads(file.read())
    except (OSError, EOFError, TypeError, ValueError):
        return None

    if (
        stored_key != key
        or not isinstance(rendered, str)
        or not isinstance(pages, tuple)
    ):
        return None

    return ReplayRecord(pages, rendered)


def save_record(
    cache_dir: str,
    key: bytes,
    pages: tuple[PageValidators, ...],
    rendered: str,
) -> None:
    path = record_path(cache_dir, key)
    temporary = f"{path}.{os.getpid()}.tmp"

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(temporary, "wb") as file:
            file.write(marshal.dumps((key, pages, rendered)))

        os.replace(temporary, path)
    except (OSError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _page_metadata(http_cache: MetadataCache, url: str) -> dict[str, object] | None:
    raw = http_cache.get(url)

    return None if raw is None else decode_metadata(raw)


def page_validators(
    http_cache: MetadataCache, urls: Iterable[str]
) -> tuple[PageValidators, ...] | None:
    """What each page looked like when it was read; None if one cannot be checked later."""

    pages = []

    for url in sorted(urls):
        values = _page_metadata(http_cache, url)

        if values is None:
            return None

        etag = values.get("etag")
        last_modified = values.get("last_modified")
        etag = etag if isinstance(etag, str) else None
        last_modified = last_modified if isinstance(last_modified, str) else None

        if not etag and not last_modified:
            return None

        pages.append((url, etag, last_modified))

    return tuple(pages)


def page_state(http_cache: MetadataCache, pages: Iterable[PageValidators]) -> str:
    """Whether every page is as the record saw it, and whether it is still fresh."""

    now = time.time()
    state = FRESH

    for url, etag, last_modified in pages:
        values = _page_metadata(http_cache, url)

        if (
            values is None
            or values.get("etag") != etag
            or values.get("last_modified") != last_modified
        ):
            return CHANGED

        if not metadata_is_fresh(values, now):
            state = STALE_SAME

    return state


def stale_pages(
    http_cache: MetadataCache, pages: Iterable[PageValidators]
) -> list[str]:
    """The recorded pages that must be asked about again before a replay."""

    now = time.time()
    stale = []

    for url, _, _ in pages:
        values = _page_metadata(http_cache, url)

        if values is None or not metadata_is_fresh(values, now):
            stale.append(url)

    return stale


def open_http_cache(cache_dir: str) -> CacheMetadataReader:
    return CacheMetadataReader(http_cache_path(cache_dir))
