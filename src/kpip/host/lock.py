"""Advisory file lock serializing writers of one installation target.

Two installers driving the same environment concurrently can interleave
file writes -- pip maintainers demonstrated a runnable race with two wheels
shipping the same file (pypa/pip#8187) -- so the install transaction takes an
exclusive lock scoped to the target environment, the way uv locks the
target.  Advisory only: it orders cooperating kpip processes and asks
nothing of other tools.

The lock file lives under the per-user cache root, keyed by the target's
resolved path, never inside the target itself: a failed transaction must
leave an absent target absent, and the installed tree must not grow
bookkeeping files.  It is deliberately not in the temp directory: the file
has to outlive the transaction, because unlinking it on release would let
the next process create a fresh inode and hold "the" lock beside a waiter
still queued on the old one, and a scratch directory promises the opposite
of that.  Nothing here is cached data, so ``--no-cache-dir`` and
``KPIP_CACHE_DIR`` do not move it -- two installers targeting one
environment must meet at the same path whatever their cache settings say --
and ``kpip cache purge``, which reaps whole ``v*`` roots, leaves this
sibling directory alone.  The cache root is per-user, so the lock orders one
user's installers; cross-user races on a shared environment stay out of
scope.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from collections.abc import Iterator

from kpip.core.digests import sha256_hexdigest
from kpip.core.logger import get_logger
from kpip.core.appdirs import user_cache_dir

logger = get_logger(__name__)


def lock_dir() -> str:
    """The directory holding every install lock for this user."""

    return os.path.join(user_cache_dir("kpip"), "locks")


def lock_path_for(directory: str) -> str:
    """The rendezvous lock file for every installer targeting ``directory``.

    ``normcase`` is what makes two spellings of one Windows target meet:
    ``ntpath.realpath`` canonicalizes case only for a path that already
    exists, and falls back to ``abspath`` -- which keeps the caller's
    casing -- for one that does not.  An install target usually does not
    exist yet, so without the fold two installers naming it differently
    would take two locks and interleave.
    """

    digest = sha256_hexdigest(
        os.path.normcase(os.path.realpath(directory)).encode(
            "utf-8",
            "surrogatepass",
        ),
    )[:16]

    return os.path.join(lock_dir(), f"kpip-install-{digest}.lock")


def _lock_exclusive(fd: int) -> None:
    """Block until ``fd`` holds the exclusive lock."""

    if sys.platform == "win32":
        import msvcrt

        # LK_LOCK retries for ~10 seconds and then raises; loop as long as
        # the error still means "someone else holds it" so acquisition
        # blocks like flock does.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EDEADLK}:
                    continue

                raise

            else:
                return

    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def environment_write_lock(directory: str) -> Iterator[None]:
    """Hold the exclusive install lock for ``directory`` over the block.

    Never touches ``directory`` itself.  When the lock file cannot be
    created or locked (an unwritable cache directory, a filesystem without
    locks) the block runs unlocked, which is exactly the pre-lock behavior:
    the lock reduces risk where it can and never turns a working install
    into a failure.
    """

    path = lock_path_for(directory)

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

    except OSError:
        logger.debug("Could not create %s; installing without a lock", path)

        yield

        return

    try:
        try:
            _lock_exclusive(fd)

        except OSError:
            logger.debug("Could not lock %s; installing without a lock", path)

            yield

            return

        yield

    finally:
        with contextlib.suppress(OSError):
            _unlock(fd)

        os.close(fd)
