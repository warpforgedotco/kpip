"""Which kpip produced a cached result, for caches that outlive the process."""

from __future__ import annotations

import os
from hashlib import sha256

from kpip.core.compiled import own_binary

_identity: tuple[object, ...] | None = None


def code_identity() -> tuple[object, ...]:
    """A value that changes whenever kpip's code does.

    ``__version__`` does not: it stays put between releases and in every
    checkout. A cache keyed on it would replay results rendered by code that
    has since changed, so this looks at the code itself instead -- the
    build ID kpip-compile ships in the binary, a digest of the modules it
    compiled, else a digest of every module's path, size and modification
    time.
    """

    global _identity  # noqa: PLW0603 -- process-wide memo

    if _identity is None:
        _identity = _compute()

    return _identity


def _compute() -> tuple[object, ...]:
    binary = own_binary()
    if binary is not None:
        from importlib.resources import files as package_files

        # The same for every copy of one build, wherever it is moved.
        try:
            return (
                "build",
                package_files("kpip").joinpath("BUILD_ID").read_text("ascii"),
            )
        except OSError:
            pass

        # A binary built without one: the file that was run. Not
        # sys.executable, a python beside the binary that does not exist.
        try:
            stat = os.stat(binary)
        except OSError:
            return ("compiled", binary)

        return ("compiled", binary, stat.st_mtime_ns, stat.st_size)

    return _source_identity(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _source_identity(root: str) -> tuple[object, ...]:
    prefix = len(root) + 1
    digest = sha256()

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

    return ("source", root, digest.hexdigest())
