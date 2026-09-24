"""Versioned cache of unpacked wheels for fresh target installations.

The compressed artifact cache avoids downloads. This cache avoids repeating
ZIP extraction and lets supported filesystems clone immutable wheel trees into
an installation target with copy-on-write semantics.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import io
import marshal
import os
import shutil
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

from kpip.core.errors import InstallationError
from kpip.core.utils import default_worker_count, versioned_bucket
from kpip.core.wheel import validate_wheel
from kpip.install.wheel_archive import (
    compiled_parts,
    copy_member_with_metadata,
    mapped_parts,
    validate_member_parts,
    zip_mode,
)

if TYPE_CHECKING:
    import zipfile
    from typing import Protocol, TypeVar

    from kpip.core.direct_url import DirectUrl

    class WheelInstallCandidate(Protocol):
        """Read-only candidate boundary required by the archive installer."""

        @property
        def canonical_name(self) -> str: ...

        @property
        def name(self) -> str: ...

        @property
        def path(self) -> str: ...

        @property
        def source_hashes(self) -> dict[str, str] | None: ...

        @property
        def source_kind(self) -> str | None: ...

        @property
        def version(self) -> object: ...

        @property
        def wheel_layout(self) -> object | None: ...

    InstallCandidate = TypeVar("InstallCandidate", bound=WheelInstallCandidate)

    WheelRequest = tuple[str, bool, DirectUrl | None]

else:
    WheelRequest = tuple[str, bool, object | None]


ARCHIVE_CACHE_BUCKET = versioned_bucket("archive", 1, interpreter=True)

PYC_CACHE_SUBDIR = "pyc"
"""Sibling of ``tree/`` holding the entry's byte-compiled modules.

Kept outside ``tree/`` on purpose: ``tree/`` is described by the manifest's
``entries`` tuple, which two independent readers decode as a list of wheel
members (``wheel_install_plan_cache`` from its own receipt, and
``wheel_archive_runtime.CachedWheelTreeArchive``). Synthetic ``__pycache__``
rows would corrupt both. A sibling directory leaves the manifest untouched
and makes ``--no-compile`` a matter of simply not reading it.

Laid out by *mapped* (post-relocation) path, so it matches
:func:`kpip.install.wheel_archive.compiled_parts` exactly. Absent for entries
written before this cache learned to compile; the installer falls back to
compiling in the stage, so a missing directory is a miss, never an error.
"""

_LOCK_WAIT_SECONDS = 30.0

_STALE_LOCK_SECONDS = 300.0

INSTALL_WORKERS = default_worker_count()
"""Size of the install-side thread pools.

Sized to the machine rather than fixed: cloning, extraction and hashing are
filesystem- and decompression-bound, and a four-thread cap left most of a
large machine idle. See :func:`kpip.core.utils.default_worker_count`.
"""

PARALLEL_THRESHOLD = 4
"""How much work has to be waiting before a thread pool earns its overhead.

Deliberately *not* ``INSTALL_WORKERS``: the pool's size and the point at
which spinning one up pays for itself are unrelated, and tying them together
sends every batch smaller than the machine's core count down the serial path.
"""

PARALLEL_EXTRACT_MEMBERS = 64
"""Members a wheel needs before extracting it across threads is worth it."""

_EXTRACT_PERMITS = threading.BoundedSemaphore(max(1, INSTALL_WORKERS - 1))
"""Extraction threads this process may hand out *inside* a single wheel.

Wheels are already extracted concurrently with one another, so within-wheel
parallelism must not multiply with that. Permits are taken without blocking:
a batch that already saturates the pool extracts each wheel serially, and a
lone large wheel -- the case that actually needs it -- finds them all free.
"""


ArchiveEntry = tuple[str, str, str, int]

_MemberWork = tuple["zipfile.ZipInfo", str, str, "tuple[str, str] | None"]


_HEX_DIGITS = "0123456789abcdefABCDEF"


def loaded_layout(candidate: WheelInstallCandidate) -> object | None:
    """The candidate's layout if it is already known, without reading the
    wheel; a lazily computed layout reads as not yet known."""
    loaded = getattr(candidate, "wheel_layout_if_loaded", _UNKNOWN)
    return candidate.wheel_layout if loaded is _UNKNOWN else loaded


_UNKNOWN = object()


def valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not value.strip(_HEX_DIGITS)


class CachedWheelArchive:
    __slots__ = ("digest", "dist_info", "entries", "tree")

    def __init__(
        self,
        digest: str,
        tree: str,
        dist_info: str,
        entries: tuple[ArchiveEntry, ...],
    ) -> None:
        self.digest = digest

        self.tree = tree

        self.dist_info = dist_info

        self.entries = entries


def supplied_wheel_digest(candidate: WheelInstallCandidate) -> str | None:
    """The SHA-256 the candidate's source vouched for, if any."""
    supplied = (
        (candidate.source_hashes or {}).get("sha256")
        if candidate.source_kind in {None, "wheel"}
        else None
    )

    if isinstance(supplied, str) and valid_sha256(supplied):
        return supplied.lower()

    return None


def prefetch_wheel_digests(
    candidates: Iterable[WheelInstallCandidate],
    cache_dir: str,
) -> tuple[str | None, ...]:
    """Load and return known digests with one database read for the batch."""
    from kpip.index.metadata_cache import (
        MetadataIdentity,
        get_wheel_metadata_cache,
        metadata_identity,
    )

    candidates = tuple(candidates)
    cache = get_wheel_metadata_cache(cache_dir)
    identities: list[MetadataIdentity | None] = []
    wanted: list[MetadataIdentity] = []
    supplied: list[str | None] = []
    for candidate in candidates:
        digest = supplied_wheel_digest(candidate)
        supplied.append(digest)
        identity = None if digest is not None else metadata_identity(candidate.path)
        identities.append(identity)
        if identity is not None:
            wanted.append(identity)

    if wanted:
        cache.prefetch_digests(wanted)

    return tuple(
        digest
        if digest is not None
        else (None if identity is None else cache.get_digest(identity))
        for digest, identity in zip(supplied, identities)
    )


def wheel_digest(candidate: WheelInstallCandidate, cache_dir: str | None = None) -> str:
    """The wheel's SHA-256: as supplied by its source, else as recorded for
    this exact file (path, size, mtime) in the metadata cache, else hashed.

    A wheel from a local wheelhouse carries no index-supplied hash, so every
    install used to read it in full to find its archive entry; the digest is
    now hashed once per file and reused while the file is unchanged.
    """
    supplied = supplied_wheel_digest(candidate)

    if supplied is not None:
        return supplied

    cache = None

    identity = None

    if cache_dir is not None:
        from kpip.index.metadata_cache import (
            get_wheel_metadata_cache,
            metadata_identity,
        )

        identity = metadata_identity(candidate.path)

        if identity is not None:
            cache = get_wheel_metadata_cache(cache_dir)

            recorded = cache.get_digest(identity)

            if recorded is not None:
                return recorded

    digest = hashlib.sha256()

    with open(candidate.path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)

    result = digest.hexdigest()

    if cache is not None and identity is not None:
        cache.put_digest(identity, result)

    return result


def archive_entry_root(cache_dir: str, digest: str) -> str:
    return os.path.join(cache_dir, ARCHIVE_CACHE_BUCKET, digest[:2], digest)


def valid_archive_entries(entries: object) -> bool:
    return isinstance(entries, tuple) and all(
        isinstance(item, tuple)
        and len(item) == 4
        and isinstance(item[0], str)
        and isinstance(item[1], str)
        and isinstance(item[2], str)
        and isinstance(item[3], int)
        for item in entries
    )


def load_archive(entry_root: str, digest: str) -> CachedWheelArchive | None:
    tree = os.path.join(entry_root, "tree")

    manifest = os.path.join(entry_root, "manifest.bin")

    if not os.path.isdir(tree) or not os.path.isfile(manifest):
        return None

    try:
        with open(manifest, "rb") as file:
            value = marshal.load(file)

    except (EOFError, OSError, TypeError, ValueError):
        return None

    if not (
        isinstance(value, tuple)
        and len(value) == 3
        and value[0] == digest
        and isinstance(value[1], str)
        and isinstance(value[2], tuple)
    ):
        return None

    entries = value[2]

    if not valid_archive_entries(entries):
        return None

    return CachedWheelArchive(digest, tree, value[1], entries)


def _remove_cache_path(path: str) -> None:
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)

        else:
            shutil.rmtree(path)

    except FileNotFoundError:
        pass


@contextmanager
def _entry_lock(path: str, entry_root: str, digest: str) -> Generator[None, None, None]:
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS

    descriptor: int | None = None

    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

        except FileExistsError:
            if load_archive(entry_root, digest) is not None:
                yield

                return

            try:
                stale = time.time() - os.stat(path, follow_symlinks=False).st_mtime

            except FileNotFoundError:
                continue

            if stale > _STALE_LOCK_SECONDS:
                try:
                    os.unlink(path)

                except FileNotFoundError:
                    pass

                continue

            if time.monotonic() >= deadline:
                raise OSError(errno.EBUSY, "timed out waiting for wheel cache", path)

            time.sleep(0.05)

    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))

        yield

    finally:
        os.close(descriptor)

        try:
            os.unlink(path)

        except FileNotFoundError:
            pass


def _record_metadata(
    archive: zipfile.ZipFile, dist_info: str
) -> dict[str, tuple[str, str]]:
    try:
        text = archive.read(f"{dist_info}/RECORD").decode("utf-8")

    except (KeyError, UnicodeDecodeError):
        return {}

    result: dict[str, tuple[str, str]] = {}

    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 3 and row[1].startswith("sha256=") and row[2].isdigit():
            result[row[0]] = (row[1], row[2])

    return result


def _borrow_extract_workers(wanted: int) -> int:
    """Take up to ``wanted`` extraction threads, or as many as are spare."""
    taken = 0

    while taken < wanted and _EXTRACT_PERMITS.acquire(blocking=False):
        taken += 1

    return taken


def _return_extract_workers(count: int) -> None:
    for _ in range(count):
        _EXTRACT_PERMITS.release()


def _extract_member(
    archive: zipfile.ZipFile,
    item: _MemberWork,
) -> ArchiveEntry:
    member, relative, destination, hint = item

    mode = zip_mode(member)

    # Created with its permissions rather than chmod-ed after: a file the
    # wheel marks executable is 0o777 under the umask, any other 0o666, as
    # pip and uv install them -- one syscall fewer for every file of every
    # wheel a cold install extracts.
    metadata = copy_member_with_metadata(
        archive,
        member,
        destination,
        metadata=hint,
        creation_mode=0o777 if mode is not None and mode & 0o111 else 0o666,
    )

    return (relative, metadata[0], metadata[1], mode or 0)


def _extract_members_threaded(
    path: str,
    work: list[_MemberWork],
    workers: int,
) -> list[ArchiveEntry]:
    """Extract ``work`` across ``workers`` threads, preserving order.

    Each thread opens the wheel itself: a :class:`zipfile.ZipFile` serializes
    reads on its own lock, so sharing one would give back exactly the
    concurrency this is trying to buy. The ``ZipInfo`` records are shared --
    they describe offsets into a file both handles have open, not state of
    the handle that produced them. Decompression drops the GIL, so the
    threads do overlap.
    """
    import zipfile
    from concurrent.futures import ThreadPoolExecutor

    local = threading.local()

    opened: list[zipfile.ZipFile] = []

    lock = threading.Lock()

    def extract(item: _MemberWork) -> ArchiveEntry:
        archive = getattr(local, "archive", None)

        if archive is None:
            archive = zipfile.ZipFile(path)

            local.archive = archive

            with lock:
                opened.append(archive)

        return _extract_member(archive, item)

    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="kpip-unzip",
        ) as pool:
            return list(pool.map(extract, work))

    finally:
        for archive in opened:
            archive.close()


def pyc_root(entry_root: str) -> str:
    """The entry's byte-compiled tree, a sibling of ``tree/``."""
    return os.path.join(entry_root, PYC_CACHE_SUBDIR)


def _compile_archive_pyc(
    tree: str,
    destination: str,
    entries: Iterable[ArchiveEntry],
) -> int:
    """Byte-compile the entry's modules once, into ``destination``.

    Runs at fill time so that installing is a clone plus a path rewrite
    rather than a compile. Returns how many modules were compiled.

    Compiling holds the GIL, so it is the one part of extraction that threads
    cannot overlap; batches go to worker processes
    (:mod:`kpip.install.bytecode`), with anything they decline compiled here.

    A module that will not compile -- vendored Python 2 in a wheel, say -- is
    skipped, not fatal: the installer falls back to compiling that one in the
    stage, exactly as it did before this cache existed.
    """
    jobs: list[tuple[str, str, str]] = []

    created: set[str] = set()

    for entry in entries:
        mapped = mapped_parts(entry[0])

        target = compiled_parts(mapped)

        if target is None:
            continue

        output = os.path.join(destination, *target)

        parent = os.path.dirname(output)

        if parent not in created:
            os.makedirs(parent, exist_ok=True)

            created.add(parent)

        jobs.append(
            (
                os.path.join(tree, *entry[0].split("/")),
                output,
                "/".join(mapped),
            ),
        )

    from kpip.install.bytecode import compile_jobs

    for source, output, display in compile_jobs(jobs):
        _compile_one(source, output, display)

    return sum(1 for _, output, _ in jobs if os.path.exists(output))


def _compile_one(source: str, output: str, display: str) -> None:
    """Compile one module in this process, for whatever a worker declined."""
    import py_compile

    try:
        py_compile.compile(
            source,
            cfile=output,
            dfile=display,
            doraise=False,
            quiet=2,
        )

    except (OSError, ValueError, RecursionError, MemoryError):
        pass


def _extract_archive(
    candidate: WheelInstallCandidate,
    digest: str,
    entry_root: str,
    *,
    pycompile: bool,
) -> CachedWheelArchive:
    shard = os.path.dirname(entry_root)

    import tempfile

    temporary = tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=shard)

    tree = os.path.join(temporary, "tree")

    os.mkdir(tree)

    try:
        import zipfile

        with zipfile.ZipFile(candidate.path) as archive:
            layout = loaded_layout(candidate)

            if isinstance(layout, tuple) and layout and isinstance(layout[0], str):
                dist_info = layout[0]

            else:
                dist_info = validate_wheel(
                    archive,
                    os.path.basename(candidate.path)[:-4].split("-", 1)[0],
                )

            wheel_metadata = _record_metadata(archive, dist_info)

            # Validate and lay out the tree first, then write. Splitting the
            # passes keeps every directory creation on one thread -- so the
            # write pass can be threaded without racing on mkdir -- and lets
            # a directory be created once instead of once per member it holds.
            work: list[_MemberWork] = []

            seen: set[str] = set()

            created: set[str] = {tree}

            for member in archive.infolist():
                if member.is_dir():
                    continue

                parts = validate_member_parts(member.filename)

                if not parts:
                    raise InstallationError(
                        f"wheel member has an empty path: {member.filename!r}",
                    )

                relative = "/".join(parts)

                if relative in seen:
                    raise InstallationError(
                        f"Wheel {candidate.path} contains duplicate member {relative!r}",
                    )

                seen.add(relative)

                destination = os.path.join(tree, *parts)

                parent = os.path.dirname(destination)

                if parent not in created:
                    os.makedirs(parent, exist_ok=True)

                    created.add(parent)

                metadata = wheel_metadata.get(relative)

                if metadata is not None and metadata[1] != str(member.file_size):
                    metadata = None

                work.append((member, relative, destination, metadata))

            workers = (
                _borrow_extract_workers(INSTALL_WORKERS - 1)
                if len(work) >= PARALLEL_EXTRACT_MEMBERS
                else 0
            )

            try:
                entries: list[ArchiveEntry] = (
                    _extract_members_threaded(candidate.path, work, workers + 1)
                    if workers
                    else [_extract_member(archive, item) for item in work]
                )

            finally:
                _return_extract_workers(workers)

        if f"{dist_info}/RECORD" not in seen:
            raise InstallationError(
                f"Wheel {candidate.path} has no valid dist-info metadata",
            )

        if pycompile:
            _compile_archive_pyc(
                tree,
                os.path.join(temporary, PYC_CACHE_SUBDIR),
                entries,
            )

        manifest = (
            digest,
            dist_info,
            tuple(entries),
        )

        with open(os.path.join(temporary, "manifest.bin"), "wb") as file:
            marshal.dump(manifest, file)

        _remove_cache_path(entry_root)

        os.rename(temporary, entry_root)

        temporary = ""

        loaded = load_archive(entry_root, digest)

        if loaded is None:
            raise OSError(
                errno.EIO, "failed to publish wheel archive cache", entry_root
            )

        return loaded

    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare_cached_wheel(
    candidate: WheelInstallCandidate,
    cache_dir: str,
    *,
    pycompile: bool = True,
) -> CachedWheelArchive:
    """The wheel's archive cache entry, filled if need be.

    Its modules are byte-compiled only for an install that compiles them:
    compiling holds the GIL, and a cold ``--no-compile`` install of jupyter
    spent more on byte code it never used than on extracting. An entry filled
    without is compiled the first time a compiling install asks for it.
    """
    layout = loaded_layout(candidate)

    if isinstance(layout, CachedWheelArchive):
        archive = layout

    else:
        archive = _cached_wheel(candidate, cache_dir, pycompile=pycompile)

    if pycompile:
        _ensure_pyc(archive)

    return archive


def _cached_wheel(
    candidate: WheelInstallCandidate,
    cache_dir: str,
    *,
    pycompile: bool,
) -> CachedWheelArchive:
    digest = wheel_digest(candidate, cache_dir)

    entry_root = archive_entry_root(cache_dir, digest)

    cached = load_archive(entry_root, digest)

    if cached is not None:
        return cached

    shard = os.path.dirname(entry_root)

    os.makedirs(shard, exist_ok=True)

    lock = f"{entry_root}.lock"

    with _entry_lock(lock, entry_root, digest):
        cached = load_archive(entry_root, digest)

        if cached is not None:
            return cached

        return _extract_archive(candidate, digest, entry_root, pycompile=pycompile)


def _ensure_pyc(archive: CachedWheelArchive) -> None:
    """Byte-compile an entry filled without, once, for every later install.

    Compiled beside the entry and renamed into place, so a concurrent fill
    of the same entry costs a duplicate compile rather than a lock.
    """

    entry_root = os.path.dirname(archive.tree)

    target = pyc_root(entry_root)

    if os.path.isdir(target):
        return

    import tempfile

    try:
        temporary = tempfile.mkdtemp(prefix=".pyc-", dir=entry_root)
    except OSError:
        return

    try:
        _compile_archive_pyc(archive.tree, temporary, archive.entries)
        os.rename(temporary, target)
        temporary = ""
    except OSError:
        pass
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare_cached_wheels(
    candidates: tuple[WheelInstallCandidate, ...],
    cache_dir: str,
    *,
    pycompile: bool = True,
) -> tuple[CachedWheelArchive, ...]:
    digests = prefetch_wheel_digests(candidates, cache_dir)

    # Cache hits are metadata reads and marshal decoding under the GIL. A
    # fresh thread pool adds scheduling and shutdown overhead without making
    # that work parallel, so take the straight-line path when every digest
    # and archive are already present. Cold extraction remains threaded.
    cached_archives: list[CachedWheelArchive] = []
    for digest in digests:
        if digest is None:
            break
        cached = load_archive(archive_entry_root(cache_dir, digest), digest)
        if cached is None:
            break
        cached_archives.append(cached)
    if len(cached_archives) == len(candidates):
        if pycompile:
            for archive in cached_archives:
                _ensure_pyc(archive)
        return tuple(cached_archives)

    if len(candidates) < PARALLEL_THRESHOLD:
        return tuple(
            prepare_cached_wheel(candidate, cache_dir, pycompile=pycompile)
            for candidate in candidates
        )

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(
        max_workers=min(INSTALL_WORKERS, len(candidates)),
        thread_name_prefix="kpip-archive",
    ) as pool:
        return tuple(
            pool.map(
                lambda candidate: prepare_cached_wheel(
                    candidate, cache_dir, pycompile=pycompile
                ),
                candidates,
            ),
        )
