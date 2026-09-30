"""Unpack wheels into the archive cache on subinterpreters, where Python has them.

Unpacking a wheel is Python around every member -- its header, its write, its
record row -- and on threads that work shares one interpreter lock with
everything else a command does. A cold install resolves (Python: compiling
index pages, solving) and unpacks, and under one lock the two only take turns.
From Python 3.14 an ``InterpreterPoolExecutor`` runs each job in a
subinterpreter with a lock of its own: four of them unpacked jupyter's 93 wheels
in 0.31 s where four threads took 0.74 s, and beside 0.54 s of Python work in
the main interpreter they finished in 0.68 s, where threads took 1.43 s.

A job crosses as a path and a digest and comes back as the entry's path; the
main interpreter reads the entry's manifest from the cache, as it would have
after unpacking it itself. A job that fails for any reason is done again in the
main interpreter, which raises what failed, so errors read as they always have.
Without subinterpreters -- before 3.14, PyPy, a compiled kpip built without
bytecode for them, or ``KPIP_SUBINTERPRETERS=0`` -- :func:`start_archive_workers`
returns None and wheels unpack on threads as before.
"""

from __future__ import annotations

import _imp
import os
import sys
import threading

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from kpip.install.wheel_archive_cache import CachedWheelArchive

WORKERS = 4
"""Subinterpreters unpacking at once; each is an interpreter lock of its own."""


class _Wheel:
    """What unpacking reads of a candidate: its file and the digest naming it.

    The name and version the candidate protocol asks for are the filename's;
    filling an entry reads neither.
    """

    __slots__ = (
        "canonical_name",
        "name",
        "path",
        "source_hashes",
        "source_kind",
        "version",
        "wheel_layout",
    )

    def __init__(self, path: str, sha256: str) -> None:
        name, _, rest = os.path.basename(path)[:-4].partition("-")
        self.path = path
        self.name = name
        self.canonical_name = name.lower().replace("_", "-")
        self.version = rest.partition("-")[0]
        self.source_hashes = {"sha256": sha256}
        self.source_kind = "wheel"
        self.wheel_layout = None


def unpack_in_worker(path: str, sha256: str, cache_dir: str, pycompile: bool) -> str:
    """Fill the wheel's archive cache entry; its directory. Runs in a worker."""
    from kpip.install.wheel_archive_cache import prepare_cached_wheel

    archive = prepare_cached_wheel(_Wheel(path, sha256), cache_dir, pycompile=pycompile)
    return os.path.dirname(archive.tree)


def _import_kpip() -> None:
    """Import what a job needs, so a worker is ready before its first wheel."""
    import kpip.install.wheel_archive_cache  # noqa: F401


def _available() -> bool:
    if os.environ.get("KPIP_SUBINTERPRETERS", "").strip() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False

    if sys.version_info < (3, 14) or sys.implementation.name != "cpython":
        return False

    from kpip.core.interpreter import is_compiled

    # A compiled kpip's modules are compiled into the binary, where a new
    # interpreter cannot import them; it could not even start, for want of
    # "encodings". A binary built for workers carries their bytecode in its
    # frozen table (kpip_compile.workers), found by the standard import system.
    return not is_compiled() or _imp.is_frozen(__name__)


class ArchiveWorkers:
    """A pool of subinterpreters unpacking wheels for one command.

    Started by the first wheel that is not in the archive cache yet: a warm
    install finds every wheel there and starts none -- starting four and
    importing kpip into each cost a warm jupyter install 0.2 s.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        self._executor: Executor | None = None

        self._closed = False

    def _started(self) -> Executor | None:
        with self._lock:
            if self._executor is None and not self._closed:
                try:
                    from concurrent.futures import InterpreterPoolExecutor

                    executor = InterpreterPoolExecutor(max_workers=WORKERS)

                    # Each worker imports kpip before its first wheel.
                    for _ in range(WORKERS):
                        executor.submit(_import_kpip)

                except Exception:  # noqa: BLE001 - threads, as before
                    self._closed = True

                    return None

                self._executor = executor

            return self._executor

    def prepare(
        self,
        candidate: object,
        cache_dir: str,
        *,
        pycompile: bool,
    ) -> CachedWheelArchive:
        """``prepare_cached_wheel(candidate, cache_dir)``, unpacked in a worker."""
        from kpip.install.wheel_archive_cache import (
            CachedWheelArchive,
            _ensure_pyc,
            archive_entry_root,
            load_archive,
            loaded_layout,
            prepare_cached_wheel,
            wheel_digest,
        )

        archive = None

        if not isinstance(loaded_layout(candidate), CachedWheelArchive):  # ty: ignore[invalid-argument-type]
            try:
                digest = wheel_digest(candidate, cache_dir)  # ty: ignore[invalid-argument-type]

                entry_root = archive_entry_root(cache_dir, digest)

                archive = load_archive(entry_root, digest)

                if archive is not None:
                    # Already unpacked: nothing for a worker to do.
                    if pycompile:
                        _ensure_pyc(archive)

                    return archive

                executor = self._started()

                if executor is not None:
                    archive = load_archive(
                        executor.submit(
                            unpack_in_worker,
                            os.fspath(candidate.path),  # ty: ignore[unresolved-attribute]
                            digest,
                            cache_dir,
                            pycompile,
                        ).result(),
                        digest,
                    )

            except Exception:  # noqa: BLE001 - done again below, which raises it
                archive = None

        if archive is None:
            return prepare_cached_wheel(candidate, cache_dir, pycompile=pycompile)  # ty: ignore[invalid-argument-type]

        return archive

    def close(self) -> None:
        with self._lock:
            self._closed = True

            executor = self._executor

        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


def start_archive_workers() -> ArchiveWorkers | None:
    """A pool of subinterpreters, started by its first wheel, or None where
    there are none."""
    if not _available():
        return None

    return ArchiveWorkers()
