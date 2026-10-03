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
        # The same for every copy of one build, wherever it is moved.
        try:
            return ("build", _build_id())
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


def _build_id() -> str:
    """The ``BUILD_ID`` kpip-compile ships as data of the ``kpip`` package.

    Read through the package loader's resource reader, which
    ``importlib.resources`` itself would use, so the binary need not import
    that package and everything it brings in.
    """

    import kpip

    loader = getattr(kpip.__spec__, "loader", None)
    get_reader = getattr(loader, "get_resource_reader", None)
    reader = get_reader("kpip") if get_reader is not None else None

    if reader is None:
        from importlib.resources import files

        return files("kpip").joinpath("BUILD_ID").read_text("ascii")

    with reader.open_resource("BUILD_ID") as stream:
        return stream.read().decode("ascii")


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
