"""Which kpip produced a cached result, for caches that outlive the process."""

from __future__ import annotations

import os
import sys

_identity: tuple[object, ...] | None = None


def code_identity() -> tuple[object, ...]:
    """A value that changes whenever kpip's code does.

    ``__version__`` does not: it stays put between releases and in every
    checkout. A cache keyed on it would replay results rendered by code that
    has since changed, so this looks at the code itself instead -- the
    binary when kpip is compiled into one, else every module's size and
    modification time, a walk of a few hundred ``stat`` calls.
    """

    global _identity  # noqa: PLW0603 -- process-wide memo

    if _identity is None:
        _identity = _compute()

    return _identity


def _compute() -> tuple[object, ...]:
    if "__compiled__" in globals():
        try:
            stat = os.stat(sys.executable)
        except OSError:
            return ("compiled", sys.executable)

        return ("compiled", sys.executable, stat.st_mtime_ns, stat.st_size)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    newest = 0
    total_size = 0
    count = 0

    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name != "__pycache__"]

        for name in files:
            if not name.endswith(".py"):
                continue

            try:
                stat = os.stat(os.path.join(directory, name))
            except OSError:
                continue

            newest = max(newest, stat.st_mtime_ns)
            total_size += stat.st_size
            count += 1

    return ("source", root, count, total_size, newest)
