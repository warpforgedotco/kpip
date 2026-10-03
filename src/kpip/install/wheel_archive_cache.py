"""Versioned cache of unpacked wheels for fresh target installations.

The compressed artifact cache avoids downloads. This cache avoids repeating
ZIP extraction and lets supported filesystems clone immutable wheel trees into
an installation target with copy-on-write semantics.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import marshal
import os
import shutil
import stat
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Generator, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, TypeGuard, TypeVar

from kpip.core.appdirs import archive_entry_root
from kpip.core.digests import valid_sha256
from kpip.core.direct_url import DirectUrl
from kpip.core.errors import InstallationError
from kpip.core.utils import default_worker_count
from kpip.host.clone import Listing, remember_listings, tree_listing
from kpip.install.wheel_scripts import entry_point_scripts
from kpip.install.wheel_archive import (
    compiled_parts,
    copy_member_with_metadata,
    mapped_parts,
    record_metadata_internal,
    validate_member_parts,
    zip_mode,
)

if TYPE_CHECKING:
    from kpip.index.metadata_cache import MetadataIdentity

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


PYC_CACHE_PREFIX = "pyc-"
"""Siblings of ``tree/`` holding the entry's byte-compiled modules, one per
interpreter, named ``pyc-<cache tag>-<magic number>``
(:func:`kpip.install.bytecode.bytecode_key`). Each is that interpreter's own
bytecode, compiled by it the first time an install for it asks.

Kept outside ``tree/`` on purpose: ``tree/`` is described by the manifest's
``entries`` tuple, which two independent readers decode as a list of wheel
members (``wheel_install_plan_cache`` from its own receipt, and
``wheel_archive_runtime.CachedWheelTreeArchive``). Synthetic ``__pycache__``
rows would corrupt both. A sibling directory leaves the manifest untouched
and makes ``--no-compile`` a matter of simply not reading it.

Laid out by *mapped* (post-relocation) path, so it matches
:func:`kpip.install.wheel_archive.compiled_parts` exactly. A module missing
from it -- one that would not compile -- is compiled in the stage, so a miss
is never an error.
"""

_LOCK_WAIT_SECONDS = 30.0

_STALE_LOCK_SECONDS = 300.0

INSTALL_WORKERS = default_worker_count()

EXTRACT_WORKERS = min(INSTALL_WORKERS, 4)
"""Threads unpacking wheels into the archive cache, across and within wheels.

Decompression releases the interpreter lock, but everything around it -- a
member's header, its write, its record row -- takes it back, and more
threads trade it rather than unpack faster: a cold trio install unpacked in
0.93 s with the machine's 20 and in 0.74 s with 4."""
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

_EXTRACT_PERMITS = threading.BoundedSemaphore(max(1, EXTRACT_WORKERS - 1))
"""Extraction threads this process may hand out *inside* a single wheel.

Wheels are already extracted concurrently with one another, so within-wheel
parallelism must not multiply with that. Permits are taken without blocking:
a batch that already saturates the pool extracts each wheel serially, and a
lone large wheel -- the case that actually needs it -- finds them all free.
"""


ArchiveEntry = tuple[str, str, str, int]

_MemberWork = tuple["zipfile.ZipInfo", str, str, "tuple[str, str] | None"]


def loaded_layout(candidate: WheelInstallCandidate) -> object | None:
    """The candidate's layout if it is already known, without reading the
    wheel; a lazily computed layout reads as not yet known."""
    loaded = getattr(candidate, "wheel_layout_if_loaded", _UNKNOWN)
    return candidate.wheel_layout if loaded is _UNKNOWN else loaded


_UNKNOWN = object()


ArchiveSummary = tuple[
    tuple[str, ...], bool, bool, bool, tuple[tuple[str, str, bool], ...]
]
"""What planning an install reads of an archive, so it need not walk every
member: its members' top-level destinations, whether one is a module at
the top level, whether a member is listed twice, whether one is
``__pycache__`` bytecode, and its entry-point scripts as name, target and
whether a GUI script."""


def summarize_archive(
    tree: str, dist_info: str, entries: tuple[ArchiveEntry, ...]
) -> ArchiveSummary:
    tops: set[str] = set()

    top_module = False

    for entry in entries:
        relative = entry[0]

        top, separator, _ = relative.partition("/")

        if top.endswith(".data"):
            mapped = mapped_parts(relative)
            top = mapped[0]
            at_top_level = len(mapped) == 1
        else:
            at_top_level = not separator

        if at_top_level and top.endswith(".py"):
            top_module = True

        tops.add(os.path.normcase(top))

    scripts = entry_point_scripts(os.path.join(tree, dist_info, "entry_points.txt"))

    return (
        tuple(sorted(tops)),
        top_module,
        len({entry[0] for entry in entries}) != len(entries),
        any("__pycache__/" in entry[0] for entry in entries),
        tuple((name, target, gui) for name, (target, gui) in scripts.items()),
    )


def valid_archive_summary(summary: object) -> TypeGuard[ArchiveSummary]:
    return (
        isinstance(summary, tuple)
        and len(summary) == 5
        and isinstance(summary[0], tuple)
        and all(isinstance(top, str) for top in summary[0])
        and isinstance(summary[1], bool)
        and isinstance(summary[2], bool)
        and isinstance(summary[3], bool)
        and isinstance(summary[4], tuple)
        and all(
            isinstance(script, tuple)
            and len(script) == 3
            and isinstance(script[0], str)
            and isinstance(script[1], str)
            and isinstance(script[2], bool)
            for script in summary[4]
        )
    )


class CachedWheelArchive:
    """An unpacked wheel in the archive cache.

    ``entries`` not given are read from its manifest when first asked for,
    and ``summary`` not given is made from them.
    """

    __slots__ = ("_entries", "_summary", "digest", "dist_info", "tree")

    def __init__(
        self,
        digest: str,
        tree: str,
        dist_info: str,
        entries: tuple[ArchiveEntry, ...] | None = None,
        summary: ArchiveSummary | None = None,
    ) -> None:
        self.digest = digest

        self.tree = tree

        self.dist_info = dist_info

        self._entries = entries

        self._summary = summary

    @property
    def entries(self) -> tuple[ArchiveEntry, ...]:
        entries = self._entries

        if entries is None:
            loaded = load_archive(os.path.dirname(self.tree), self.digest)

            if loaded is None:
                raise OSError(
                    errno.ENOENT, "archive cache entry has no manifest", self.tree
                )

            entries = self._entries = loaded.entries

        return entries

    @property
    def summary(self) -> ArchiveSummary:
        summary = self._summary

        if summary is None:
            summary = self._summary = summarize_archive(
                self.tree, self.dist_info, self.entries
            )

        return summary


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
    from kpip.index.metadata_cache import get_wheel_metadata_cache, metadata_identity

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

    from kpip.index.metadata_cache import get_wheel_metadata_cache, metadata_identity

    cache = None

    identity = None

    if cache_dir is not None:
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
            value = marshal.loads(file.read())

    except EOFError, OSError, TypeError, ValueError:
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

    # The shape of its first entry, not the types of all of them: a manifest
    # is only ever written whole by kpip, renamed into a bucket versioned by
    # its format, and names its wheel's digest, checked above. Checking every
    # entry cost a warm jupyter install 12,000 of them, twice.
    if not valid_archive_entries(entries[:1]):
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

        # Only this kpip's own: another may have taken it for stale since.
        try:
            with open(path, encoding="ascii") as lock:
                own = lock.read() == str(os.getpid())

        except OSError, ValueError:
            own = False

        if own:
            try:
                os.unlink(path)

            except FileNotFoundError:
                pass


def _record_metadata(
    archive: zipfile.ZipFile, dist_info: str
) -> dict[str, tuple[str, str]]:
    try:
        text = archive.read(f"{dist_info}/RECORD").decode("utf-8")

    except KeyError, UnicodeDecodeError:
        return {}

    import csv

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


_HAS_PREAD = hasattr(os, "pread")

_LEAN_MEMBER_LIMIT = 16 * 1024 * 1024
"""Members up to this size are read whole with one ``pread``."""

_LOCAL_NAME_HEADROOM = 256
"""Bytes read past the local header on a guess at its name and extra field."""

try:
    # Compiled into the binary as a built-in; see host/_accel/_kpip_unzip.c.
    import _kpip_unzip  # ty: ignore[unresolved-import]

    _extract_loop = getattr(_kpip_unzip, "extract_members", None)
except ImportError:
    _extract_loop = None


def unpacks_without_the_lock() -> bool:
    """Whether a wheel's members are extracted in C, the interpreter lock
    released: then threads unpack wheels side by side, and beside the solve."""
    return _extract_loop is not None


_LEFT_TO_PYTHON = (0, -1, 0, 0, 0, b"", b"", 0)
"""A member the C loop hands back at once."""


def _unzip_row(item: _MemberWork) -> tuple[int, int, int, int, int, bytes, bytes, int]:
    """What the C loop needs of a member, or a row it hands straight back:
    for a member the RECORD gives no hash for, or ``_extract_member_lean``
    would not take."""
    member, _, destination, hint = item

    if (
        hint is None
        or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        or member.flag_bits & 0x1
        or member.file_size > _LEAN_MEMBER_LIMIT
        or member.compress_size > _LEAN_MEMBER_LIMIT
    ):
        return _LEFT_TO_PYTHON

    try:
        name = member.orig_filename.encode(
            "utf-8" if member.flag_bits & 0x800 else "cp437"
        )

    except UnicodeEncodeError:
        return _LEFT_TO_PYTHON

    mode = zip_mode(member)

    return (
        member.header_offset,
        member.compress_type,
        member.compress_size,
        member.file_size,
        member.CRC,
        name,
        os.fsencode(destination),
        0o777 if mode is not None and mode & 0o111 else 0o666,
    )


def _extract_members(
    archive: zipfile.ZipFile, work: list[_MemberWork], fd: int
) -> list[ArchiveEntry]:
    """Extract ``work`` in order: in the C loop where it is built in, each
    member it hands back extracted here, as ``_extract_member`` extracts it,
    raising what that raises."""
    if _extract_loop is None or fd < 0:
        return [_extract_member(archive, item, fd) for item in work]

    rows = [_unzip_row(item) for item in work]

    entries: list[ArchiveEntry] = []

    start = 0

    while start < len(work):
        _, index = _extract_loop(fd, rows, start)

        for member, relative, _, hint in work[start:index]:
            assert hint is not None

            entries.append((relative, hint[0], hint[1], zip_mode(member) or 0))

        if index < len(work):
            entries.append(_extract_member(archive, work[index], fd))

        start = index + 1

    return entries


def _extract_member_lean(fd: int, item: _MemberWork) -> ArchiveEntry | None:
    """``_extract_member`` without ``zipfile``'s read path, or None.

    ``archive.read`` opens a ``ZipExtFile``, seeks, re-parses the local
    header, builds a decompressor and a CRC tracker, and ``open`` wraps the
    destination in an ``io`` stack: thousands of bytecodes per member under
    the interpreter lock, where a cold trio install extracts 3,359 members.
    A stored or deflated member is one ``pread``, one ``zlib.decompress``
    and one ``os.write`` instead, with ``zipfile``'s checks -- the local
    header's signature and name, the size and the CRC. Anything else (an
    encrypted, oversized or otherwise compressed member) returns None for the
    ``zipfile`` path.
    """

    member, relative, destination, hint = item

    method = member.compress_type

    if (
        method not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        or member.flag_bits & 0x1
        or member.file_size > _LEAN_MEMBER_LIMIT
        or member.compress_size > _LEAN_MEMBER_LIMIT
    ):
        return None

    offset = member.header_offset

    blob = os.pread(fd, 30 + _LOCAL_NAME_HEADROOM + member.compress_size, offset)

    if len(blob) < 30 or blob[:4] != b"PK\x03\x04":
        raise zipfile.BadZipFile("Bad magic number for file header")

    name_size, extra_size = struct.unpack_from("<HH", blob, 26)

    start = 30 + name_size + extra_size

    end = start + member.compress_size

    if end > len(blob):
        blob = os.pread(fd, end, offset)

        if len(blob) < end:
            raise zipfile.BadZipFile("Truncated file data")

    local_name = blob[30 : 30 + name_size].decode(
        "utf-8" if member.flag_bits & 0x800 else "cp437"
    )

    if local_name != member.orig_filename:
        raise zipfile.BadZipFile(
            f"File name in directory {member.orig_filename!r} and header "
            f"{local_name!r} differ."
        )

    data = blob[start:end]

    if method == zipfile.ZIP_DEFLATED:
        try:
            data = zlib.decompress(data, -15, member.file_size or 1)
        except zlib.error as exc:
            raise zipfile.BadZipFile(f"Bad compressed data for {relative!r}") from exc

    if len(data) != member.file_size or zlib.crc32(data) != member.CRC:
        raise zipfile.BadZipFile(f"Bad CRC-32 for file {member.filename!r}")

    mode = zip_mode(member)

    out = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0),
        0o777 if mode is not None and mode & 0o111 else 0o666,
    )

    try:
        view = memoryview(data)

        while view:
            view = view[os.write(out, view) :]

    finally:
        os.close(out)

    if hint is None:
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest())

        hint = (f"sha256={encoded.rstrip(b'=').decode('ascii')}", str(len(data)))

    return (relative, hint[0], hint[1], mode or 0)


def _extract_member(
    archive: zipfile.ZipFile,
    item: _MemberWork,
    fd: int = -1,
) -> ArchiveEntry:
    if fd >= 0:
        entry = _extract_member_lean(fd, item)

        if entry is not None:
            return entry

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

    local = threading.local()

    opened: list[zipfile.ZipFile] = []

    lock = threading.Lock()

    # ``pread`` needs no lock: the threads share one descriptor. Windows has
    # no ``pread``, and keeps ``zipfile``.
    fd = os.open(path, os.O_RDONLY) if _HAS_PREAD else -1

    def extract(item: _MemberWork) -> ArchiveEntry:
        if fd >= 0:
            entry = _extract_member_lean(fd, item)

            if entry is not None:
                return entry

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
        if fd >= 0:
            os.close(fd)

        for archive in opened:
            archive.close()


LISTING_NAME = "listing.bin"

_listed: set[str] = set()
"""Trees whose listings this process has handed to the clones."""

_listed_lock = threading.Lock()


def remember_tree_listings(archive: CachedWheelArchive) -> None:
    """Let clones of ``archive``'s tree link from its listing, not walk it.

    A published tree never changes, so its listing -- one per top-level
    directory, as a clone lists them -- is stored beside it the first time an
    install asks, and read from there after: walking a warm scispacy
    install's 11,000 files took a fifth of its linking threads' time. A
    listing that cannot be read or written leaves the clone walking the tree.
    """
    with _listed_lock:
        if archive.tree in _listed:
            return

        _listed.add(archive.tree)

    path = os.path.join(os.path.dirname(archive.tree), LISTING_NAME)

    listings = _read_listings(path)

    if listings is None:
        listings = {}

        try:
            with os.scandir(archive.tree) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        listings[entry.name] = tree_listing(entry.path)

        except OSError:
            return

        _write_listings(path, listings)

    remember_listings(
        {
            os.path.join(archive.tree, name): listing
            for name, listing in listings.items()
        }
    )


def _read_listings(path: str) -> dict[str, Listing] | None:
    try:
        with open(path, "rb") as file:
            value = marshal.loads(file.read())

    except EOFError, OSError, TypeError, ValueError:
        return None

    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(listing, tuple) and len(listing) == 4
        for name, listing in value.items()
    ):
        return None

    return value


def _write_listings(path: str, listings: dict[str, Listing] | BytecodeRows) -> None:
    """Publish ``listings`` whole, or not at all: a reader sees one or none."""
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".listing-", dir=os.path.dirname(path)
        )

    except OSError:
        return

    try:
        with os.fdopen(descriptor, "wb") as file:
            marshal.dump(listings, file)

        os.replace(temporary, path)

    except OSError:
        try:
            os.unlink(temporary)

        except OSError:
            pass


BytecodeRows = dict[str, tuple[tuple[str, str, str], ...]]


def bytecode_rows(archive: CachedWheelArchive) -> BytecodeRows | None:
    """For each package directory of the entry whose modules all have
    bytecode in :func:`bytecode_tree`, its ``.pyc`` files' RECORD rows: path
    in the wheel, hash and size. ``None`` without a bytecode tree.

    A module the tree lacks would not compile, as the tree is published only
    once every module was compiled, and has no row. Left out is a directory
    a ``.data`` member installs into, and one that ships ``__pycache__``
    itself: their bytecode is placed file by file. Read and hashed once, the
    first time an install asks, and stored beside the tree.
    """
    tree = bytecode_tree(archive)

    if tree is None:
        return None

    path = f"{tree}.record"

    try:
        with open(path, "rb") as file:
            rows = marshal.loads(file.read())

    except EOFError, OSError, TypeError, ValueError:
        rows = None

    if not isinstance(rows, dict) or not all(
        isinstance(top, str) and isinstance(value, tuple) for top, value in rows.items()
    ):
        rows = _bytecode_rows(archive, tree)

        _write_listings(path, rows)

    return rows


def _bytecode_rows(archive: CachedWheelArchive, tree: str) -> BytecodeRows:
    rows: dict[str, list[tuple[str, str, str]]] = {}

    incomplete: set[str] = set()

    for entry in archive.entries:
        top, separator, _ = entry[0].partition("/")

        if not separator or top.endswith(".dist-info"):
            continue

        try:
            mapped = mapped_parts(entry[0])

        except InstallationError:
            continue

        if top.endswith(".data"):
            incomplete.add(mapped[0])

            continue

        found = rows.setdefault(top, [])

        if "__pycache__" in mapped:
            incomplete.add(top)

            continue

        compiled = compiled_parts(mapped)

        if compiled is None:
            continue

        try:
            with open(os.path.join(tree, *compiled), "rb") as file:
                body = file.read()

        except FileNotFoundError:
            continue

        except OSError:
            incomplete.add(top)

            continue

        found.append(("/".join(compiled), *record_metadata_internal(body)))

    return {
        top: tuple(sorted(found))
        for top, found in rows.items()
        if top not in incomplete
    }


def bytecode_tree(archive: CachedWheelArchive) -> str | None:
    """The entry's byte-compiled modules for the target interpreter, compiled
    by it the first time an install asks; ``None`` if it reads no bytecode,
    or the tree could not be made.

    Compiling holds the GIL, so batches go to the target interpreter's
    worker processes (:mod:`kpip.install.bytecode`), and installs copy the
    result rather than compiling. Compiled beside the entry and renamed into
    place, so a concurrent fill of the same entry costs a duplicate compile
    rather than a lock.

    A module that will not compile -- vendored Python 2 in a wheel, say -- is
    left out, not fatal: the install compiles that one in the stage.
    """
    from kpip.install.bytecode import bytecode_key

    key = bytecode_key()

    if key is None:
        return None

    entry_root = os.path.dirname(archive.tree)

    target = os.path.join(entry_root, f"{PYC_CACHE_PREFIX}{key}")

    if os.path.isdir(target):
        return target

    try:
        temporary = tempfile.mkdtemp(prefix=".pyc-", dir=entry_root)

    except OSError:
        return None

    try:
        # Published whole or not at all: a tree missing modules nobody took
        # would stay that way, its directory found by every later install.
        if _compile_archive_pyc(archive.tree, temporary, archive.entries):
            return None

        os.rename(temporary, target)

        temporary = ""

    except OSError:
        # Another install renamed its copy into place first.
        pass

    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)

    return target if os.path.isdir(target) else None


def _compile_archive_pyc(
    tree: str,
    destination: str,
    entries: Iterable[ArchiveEntry],
) -> list[tuple[str, str, str]]:
    """Byte-compile the entry's modules into ``destination``, each naming
    its path in the wheel: the target interpreter names its real path when
    it imports it. Returns those nobody took."""
    jobs: list[tuple[str, str, str]] = []

    for entry in entries:
        mapped = mapped_parts(entry[0])

        target = compiled_parts(mapped)

        if target is None:
            continue

        jobs.append(
            (
                os.path.join(tree, *entry[0].split("/")),
                os.path.join(destination, *target),
                "/".join(mapped),
            ),
        )

    from kpip.install.bytecode import compile_modules

    return compile_modules(jobs)


def _extracted_listings(tree: str, members: list[str]) -> dict[str, Listing]:
    """The listing of each top-level directory of a tree just extracted,
    as ``remember_tree_listings`` would walk it: its directories, parents
    first, with their modes, and its files. Extraction writes files and the
    directories holding them, and nothing else."""
    directories: dict[str, set[tuple[str, ...]]] = {}
    files: dict[str, list[bytes]] = {}

    for member in members:
        top, *parts = member.split("/")

        if not parts:
            continue

        files.setdefault(top, []).append(os.fsencode(os.path.join(*parts)))

        known = directories.setdefault(top, set())

        for end in range(1, len(parts)):
            known.add(tuple(parts[:end]))

    listings: dict[str, Listing] = {}

    for top, names in files.items():
        relatives = sorted(os.path.join(*parts) for parts in directories[top])

        listings[top] = (
            [os.fsencode(relative) for relative in relatives],
            [
                stat.S_IMODE(os.lstat(os.path.join(tree, top, relative)).st_mode)
                for relative in relatives
            ],
            names,
            [],
        )

    return listings


def _extract_archive(
    candidate: WheelInstallCandidate,
    digest: str,
    entry_root: str,
) -> CachedWheelArchive:
    shard = os.path.dirname(entry_root)

    temporary = tempfile.mkdtemp(prefix=f".{digest[:12]}-", dir=shard)

    tree = os.path.join(temporary, "tree")

    os.mkdir(tree)

    try:
        with zipfile.ZipFile(candidate.path) as archive:
            layout = loaded_layout(candidate)

            if isinstance(layout, tuple) and layout and isinstance(layout[0], str):
                dist_info = layout[0]

            else:
                from kpip.core.wheel import validate_wheel

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
                _borrow_extract_workers(EXTRACT_WORKERS - 1)
                if len(work) >= PARALLEL_EXTRACT_MEMBERS
                else 0
            )

            try:
                if workers:
                    entries: list[ArchiveEntry] = _extract_members_threaded(
                        candidate.path, work, workers + 1
                    )

                else:
                    fd = os.open(candidate.path, os.O_RDONLY) if _HAS_PREAD else -1

                    try:
                        entries = _extract_members(archive, work, fd)

                    finally:
                        if fd >= 0:
                            os.close(fd)

            finally:
                _return_extract_workers(workers)

        if f"{dist_info}/RECORD" not in seen:
            raise InstallationError(
                f"Wheel {candidate.path} has no valid dist-info metadata",
            )

        manifest = (
            digest,
            dist_info,
            tuple(entries),
        )

        with open(os.path.join(temporary, "manifest.bin"), "wb") as file:
            marshal.dump(manifest, file)

        _write_listings(
            os.path.join(temporary, LISTING_NAME),
            _extracted_listings(tree, [item[1] for item in work]),
        )

        # Another kpip may have published it meanwhile -- one that took this
        # entry's lock for stale while this one worked. Its entry is whole,
        # and others may be reading it: keep it, and drop this copy.
        published = load_archive(entry_root, digest)

        if published is not None:
            return published

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

    Its modules are byte-compiled, for the target interpreter, only for an
    install that compiles them: a cold ``--no-compile`` install of jupyter
    spent more on byte code it never used than on extracting.
    """
    layout = loaded_layout(candidate)

    if isinstance(layout, CachedWheelArchive):
        archive = layout

    else:
        archive = _cached_wheel(candidate, cache_dir)

    if pycompile:
        bytecode_tree(archive)

    return archive


def _cached_wheel(
    candidate: WheelInstallCandidate,
    cache_dir: str,
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

        return _extract_archive(candidate, digest, entry_root)


def prepare_cached_wheels(
    candidates: tuple[WheelInstallCandidate, ...],
    cache_dir: str,
    *,
    pycompile: bool = True,
) -> tuple[CachedWheelArchive, ...]:
    # Loaded already, e.g. by the install's preparation, which found each
    # cached: neither their digests nor their manifests are read again.
    layouts = [loaded_layout(candidate) for candidate in candidates]
    loaded = [layout for layout in layouts if isinstance(layout, CachedWheelArchive)]
    if len(loaded) == len(candidates):
        if pycompile:
            for archive in loaded:
                bytecode_tree(archive)
        return tuple(loaded)

    digests = prefetch_wheel_digests(candidates, cache_dir)

    # Cache hits are metadata reads and marshal decoding under the GIL. A
    # fresh thread pool adds scheduling and shutdown overhead without making
    # that work parallel, so take the straight-line path when every digest
    # and archive are already present. Cold extraction remains threaded.
    cached_archives: list[CachedWheelArchive] = []
    for layout, digest in zip(layouts, digests, strict=True):
        if isinstance(layout, CachedWheelArchive):
            cached_archives.append(layout)
            continue
        if digest is None:
            break
        cached = load_archive(archive_entry_root(cache_dir, digest), digest)
        if cached is None:
            break
        cached_archives.append(cached)
    if len(cached_archives) == len(candidates):
        if pycompile:
            for archive in cached_archives:
                bytecode_tree(archive)
        return tuple(cached_archives)

    if len(candidates) < PARALLEL_THRESHOLD:
        return tuple(
            prepare_cached_wheel(candidate, cache_dir, pycompile=pycompile)
            for candidate in candidates
        )

    with ThreadPoolExecutor(
        max_workers=min(EXTRACT_WORKERS, len(candidates)),
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
