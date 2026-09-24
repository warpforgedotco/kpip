"""Consolidated core utilities: python environment, filesystem, and context helpers."""

from __future__ import annotations

import errno
import marshal
import os
import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


AuthInfo = tuple[str | None, str | None]


def enum(*sequential: str, **named: str) -> Any:
    values: dict[str, object] = dict(zip(sequential, range(len(sequential))), **named)
    values["reverse_mapping"] = {value: key for key, value in values.items()}
    return type("Enum", (), values)


class ExecutionContext:
    __slots__ = ("version",)

    def __init__(self) -> None:
        self.version: str | None = None


context = ExecutionContext()


def configure(*, version: str | None = None) -> None:
    if version is not None:
        context.version = version


def current_version() -> str | None:
    return context.version


CURRENT_PYTHON_VERSION_INFO = sys.version_info
CURRENT_PYTHON_VERSION = (
    f"{CURRENT_PYTHON_VERSION_INFO.major}.{CURRENT_PYTHON_VERSION_INFO.minor}"
)
CURRENT_PYTHON_VERSION_DIGITS = CURRENT_PYTHON_VERSION.replace(".", "")
CURRENT_PYTHON_VERSION_FULL = ".".join(
    str(part) for part in CURRENT_PYTHON_VERSION_INFO[:3]
)
CURRENT_PYTHON_MAJOR_TAG = f"py{CURRENT_PYTHON_VERSION_INFO.major}"
CURRENT_PYTHON_FULL_TAG = f"py{CURRENT_PYTHON_VERSION_DIGITS}"

CACHE_INTERPRETER_TAG = f"{sys.implementation.name}-{CURRENT_PYTHON_VERSION_DIGITS}"

CACHE_VERSION = 1
"""Version of the cache *layout*, not of any store's format.

Every persisted cache lives under the ``v<CACHE_VERSION>`` directory of the
cache root (``core/appdirs.py:versioned_cache_dir``). Bumping it retires the
whole tree at once, which is what ``kpip cache purge`` keys on -- it removes
every ``v*`` directory without knowing the stores -- and is the escape hatch
for a change that crosses all of them.

Individual stores carry their own version inside that directory (see
:func:`versioned_bucket`), so a format change to one does not discard the
others. There is no migration code either way: a store of another version is
simply never read."""

CACHE_VERSION_TAG = f"v{CACHE_VERSION}"


def versioned_bucket(name: str, version: int, *, interpreter: bool = False) -> str:
    """A cache store's on-disk name, carrying its own format version.

    Stores are versioned one at a time because they cost wildly different
    amounts to refill. Re-parsing an index page is one request; re-extracting
    every wheel, or rebuilding one from source, is not. Sharing a single
    version would make the cheap ones dictate when the expensive ones are
    thrown away -- uv's index cache has been bumped 24 times while its
    unpacked-wheel cache has never moved.

    Bumping one is the entire migration: the new name is a store this kpip
    has never written, and the old one is inert until ``kpip cache purge``.

    ``interpreter`` appends the interpreter tag for stores whose payload is
    not portable across interpreters -- ``marshal`` data, byte-compiled
    modules -- so those are scoped per interpreter as well as per version.
    """
    tag = f"-{CACHE_INTERPRETER_TAG}" if interpreter else ""

    return f"{name}-v{version}{tag}"


def key_bytes(key: object) -> bytes:
    """``key`` -- tuples of strings, bytes, numbers, booleans, None -- as bytes
    that depend only on its value.

    Not ``marshal``: it flags and back-references any object that has more
    than one reference, so two equal keys built differently -- one sharing
    a string with another tuple -- serialize differently, and a cache
    keyed on the bytes misses.
    """

    return repr(key).encode("utf-8")


def default_worker_count() -> int:
    """How many threads a machine-sized pool should use.

    Install work is filesystem- and decompression-bound rather than pure
    Python, so a small multiple of the available cores beats a fixed number
    on a large machine and avoids oversubscribing a small one. Callers still
    cap this by how much work they actually have.

    ``KPIP_CONCURRENCY`` overrides it; a value that is not a positive integer
    is ignored rather than fatal.
    """
    override = os.environ.get("KPIP_CONCURRENCY")

    if override:
        try:
            requested = int(override)

        except ValueError:
            requested = 0

        if requested > 0:
            return requested

    available = getattr(os, "process_cpu_count", None)

    cores = available() if available is not None else os.cpu_count()

    return min(32, (cores or 1) + 4)


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise


def display_path(path: str) -> str:
    if not os.path.isabs(path):
        return path
    try:
        relative = os.path.relpath(path, os.getcwd())
    except ValueError:
        return path
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return path
    return os.path.join(".", relative)


def load_snapshot(path: str | os.PathLike[str]) -> object | None:
    """Load a marshal snapshot, treating missing or corrupt data as empty."""
    try:
        with open(path, "rb") as stream:
            return marshal.load(stream)
    except (EOFError, OSError, TypeError, ValueError):
        return None


def save_snapshot(path: str | os.PathLike[str], payload: object) -> bool:
    """Atomically write a marshal snapshot and report whether it succeeded."""
    path = os.fspath(path)
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(temporary, "wb") as stream:
            marshal.dump(payload, stream)  # ty: ignore[invalid-argument-type]
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
