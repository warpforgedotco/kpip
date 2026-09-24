"""Which kpip produced a cached result, for caches that outlive the process."""

from __future__ import annotations

import os
import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

_identity: tuple[object, ...] | None = None


def code_identity() -> tuple[object, ...]:
    """A value that changes whenever kpip's code does.

    ``__version__`` does not: it stays put between releases and in every
    checkout. A cache keyed on it would replay results rendered by code that
    has since changed, so this looks at the code itself instead -- the
    binary when kpip is compiled into one, else a digest of every module's
    path, size and modification time, plus the installed ``RECORD``, whose
    per-file hashes change with any content even when a reproducible build
    gives every file the same timestamp.
    """

    global _identity  # noqa: PLW0603 -- process-wide memo

    if _identity is None:
        _identity = _compute()

    return _identity


def _sha256() -> Any:
    # The interpreter's own SHA-2, not ``hashlib``: that loads OpenSSL, and a
    # lock replayed from the cache would spend more on that import than on
    # identifying the code.  The digests are the same.
    try:
        from _sha2 import sha256  # ty: ignore[unresolved-import]
    except ImportError:
        try:
            from _sha256 import sha256  # ty: ignore[unresolved-import]
        except ImportError:
            from hashlib import sha256

    return sha256()


def _installed_record(root: str) -> bytes | None:
    """The ``RECORD`` of the distribution ``root`` was installed from, if any."""

    parent = os.path.dirname(root)

    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                name = entry.name.lower()

                if name.startswith("kpip-") and name.endswith(".dist-info"):
                    with open(os.path.join(entry.path, "RECORD"), "rb") as file:
                        return file.read()
    except OSError:
        return None

    return None


def _compute() -> tuple[object, ...]:
    if "__compiled__" in globals():
        try:
            stat = os.stat(sys.executable)
        except OSError:
            return ("compiled", sys.executable)

        return ("compiled", sys.executable, stat.st_mtime_ns, stat.st_size)

    return _source_identity(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _source_identity(root: str) -> tuple[object, ...]:
    prefix = len(root) + 1
    digest = _sha256()

    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(
            name for name in subdirectories if name != "__pycache__"
        )

        for name in sorted(files):
            if not name.endswith(".py"):
                continue

            path = os.path.join(directory, name)

            try:
                stat = os.stat(path)
            except OSError:
                continue

            digest.update(
                f"{path[prefix:]}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
            )

    record = _installed_record(root)

    if record is not None:
        digest.update(record)

    return ("source", root, digest.hexdigest())
