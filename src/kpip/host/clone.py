"""Lightweight copy-on-write cloning primitives.

Installation hot paths import this module without pulling in the broader
filesystem utility stack.  Platform-specific fallback modules are loaded only
when the native clone operation is unavailable.

Per regular file, the order is a reflink (Linux ``FICLONE``), then a hard
link, then a plain copy.  ``KPIP_LINK_MODE`` picks the policy the way uv's
``--link-mode`` does: ``hardlink`` is the default on Linux and Windows,
``clone`` (reflink, else copy) on macOS, and ``copy`` skips both.  A hard link
shares its inode with the cache tree it came from, so anything that later
rewrites an installed file must break the link first: :func:`replace_contents`
does that for the installer's own rewrites.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Callable

    CloneFile = Callable[[bytes, bytes, int], int]

    Devices = tuple[int, int]

_FICLONE = 0x40049409

_clonefile: CloneFile | None = None

_clonefile_loaded = False


def _darwin_clone(source: str, destination: str) -> bool:
    """Clone one file or directory tree with APFS copy-on-write semantics."""

    global _clonefile, _clonefile_loaded

    if sys.platform != "darwin":
        return False

    import ctypes

    if not _clonefile_loaded:
        _clonefile_loaded = True

        try:
            function = ctypes.CDLL(None, use_errno=True).clonefile

        except (AttributeError, OSError):
            function = None

        if function is not None:
            function.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)

            function.restype = ctypes.c_int

        _clonefile = function

    function = _clonefile

    if function is None:
        return False

    if (
        function(
            os.fsencode(source),
            os.fsencode(destination),
            0,
        )
        == 0
    ):
        return True

    error = ctypes.get_errno()

    if error in {
        errno.ENOTSUP,
        errno.EXDEV,
        errno.EINVAL,
        getattr(errno, "ENOSYS", -1),
    }:
        return False

    raise OSError(error, os.strerror(error), destination)


_reflink_unsupported: set[int] = set()

"""Destination devices whose filesystem rejected FICLONE outright.

``ioctl`` is issued on the destination descriptor, so support is a property
of the destination filesystem, and one that does not change mid-run.  ext4,
overlayfs and tmpfs all answer ``EOPNOTSUPP``; without this memo every file
cloned onto them pays an open, an exclusive create, the failing ioctl and an
unlink before falling back.  Keyed by ``st_dev``: an earlier version keyed on
the destination directory to save a stat, which meant paying that failing
sequence once per directory in the tree.  :func:`clone_path` now resolves the
device pair once per call and threads it through the walk, so the per-file
check is a set lookup.
"""

_LINK_MODES = ("clone", "hardlink", "copy")

_link_mode: str | None = None

"""``KPIP_LINK_MODE`` as resolved on first use; ``None`` until then."""


def _configured_link_mode() -> str:
    """Resolve ``KPIP_LINK_MODE``, defaulting the way uv's ``--link-mode`` does.

    ``hardlink`` is the cheapest way to put a cached file into a target on a
    filesystem without copy-on-write, and it is uv's default on Linux and
    Windows.  It also means an installed file *is* the cache's file: anything
    that later rewrites it in place, without unlinking first, rewrites the
    cache too.  macOS defaults to ``clone``, where ``clonefile`` gives an
    independent copy for free.  ``clone`` still tries a reflink first on
    Linux, so btrfs and XFS get independent files at hard link cost; ``copy``
    skips both, which is what a user who edits installed files wants.
    """

    global _link_mode

    if _link_mode is None:
        value = os.environ.get("KPIP_LINK_MODE", "").strip().lower()

        if value not in _LINK_MODES:
            value = "clone" if sys.platform == "darwin" else "hardlink"

        _link_mode = value

    return _link_mode


_hardlink_unsupported: set[Devices] = set()

"""``(source, destination)`` device pairs where ``os.link`` cannot work.

A hard link needs both paths on one filesystem that supports links.  ``EXDEV``
says they are not on one, ``EPERM`` is what FAT, exFAT and some FUSE
filesystems answer, and ``EOPNOTSUPP``/``ENOSYS`` cover the rest.  None of
those change mid-run, so a device pair is judged once.  ``EMLINK`` is per
file and is not memoised.
"""


_reflink_slow: set[int] = set()

"""Devices whose FICLONE succeeds but moves data at copy speed.

A working reflink is metadata work and finishes in microseconds regardless of
file size; on some filesystems (network-backed block storage, XFS
configurations) the ioctl succeeds but behaves like a full copy, which made
uv's clone-by-default install ~30x slower than hardlinking on XFS-on-EBS
(astral-sh/uv#18259).  Successful clones of probe-sized files are timed while
a device is undecided, and a device whose measured throughput stays below
what any metadata-only clone achieves is demoted to the hard-link fallback.
Keyed by the source ``st_dev``: a successful FICLONE implies source and
destination share that device.
"""

_reflink_fast: set[int] = set()

"""Devices whose measured clone throughput proved FICLONE is metadata work."""

_reflink_probe: dict[int, tuple[int, int]] = {}

"""Per-device ``(bytes, ns)`` accumulated over timed clones while undecided.

Installer threads race on these entries without a lock: a lost update only
lengthens the probe, and ``_reflink_slow`` membership is the load-bearing
outcome.
"""

# Files below this size prove nothing: the fixed ioctl cost dominates, and a
# genuinely instant clone of tiny files would read as slow throughput.
_REFLINK_PROBE_MIN_FILE_BYTES = 128 * 1024

# Judge a device once this much time went into its timed ioctls...
_REFLINK_PROBE_MIN_NS = 25_000_000

# ...demoting it when the bytes cloned in that time imply less than ~256
# MB/s: no metadata-only clone is that slow, and a clone that behaves as a
# full copy of cache-fresh files rarely exceeds it.
_REFLINK_MIN_BYTES_PER_NS = 0.256

# A device cloning this much before reaching the time threshold implies
# multi-GB/s throughput, which no data copy explains; it is proven fast.
_REFLINK_PROVE_FAST_BYTES = 64 * 1024 * 1024


def _record_reflink_timing(device: int, size: int, elapsed_ns: int) -> None:
    """Fold one timed clone into the device's probe, deciding when ripe."""

    if device in _reflink_fast:
        return

    bytes_total, ns_total = _reflink_probe.get(device, (0, 0))

    bytes_total += size

    ns_total += elapsed_ns

    if ns_total >= _REFLINK_PROBE_MIN_NS:
        if bytes_total < ns_total * _REFLINK_MIN_BYTES_PER_NS:
            _reflink_slow.add(device)

        else:
            _reflink_fast.add(device)

    elif bytes_total >= _REFLINK_PROVE_FAST_BYTES:
        _reflink_fast.add(device)

    else:
        _reflink_probe[device] = (bytes_total, ns_total)


def _linux_reflink(
    source: str,
    destination: str,
    source_device: int,
    destination_device: int,
) -> bool:
    """Clone one regular file with the Linux FICLONE ioctl when available."""

    if not sys.platform.startswith("linux"):
        return False

    if destination_device in _reflink_unsupported or source_device in _reflink_slow:
        return False

    try:
        import fcntl

    except ImportError:
        return False

    source_fd = os.open(source, os.O_RDONLY)

    try:
        source_stat = os.fstat(source_fd)

        mode = stat.S_IMODE(source_stat.st_mode)

        probing = (
            source_stat.st_size >= _REFLINK_PROBE_MIN_FILE_BYTES
            and source_device not in _reflink_fast
        )

        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )

        try:
            if probing:
                started = time.perf_counter_ns()

            fcntl.ioctl(destination_fd, _FICLONE, source_fd)

            if probing:
                _record_reflink_timing(
                    source_device,
                    source_stat.st_size,
                    time.perf_counter_ns() - started,
                )

        except OSError as exc:
            os.close(destination_fd)

            destination_fd = -1

            try:
                os.unlink(destination)

            except FileNotFoundError:
                pass

            if exc.errno in {
                errno.ENOTTY,
                errno.ENOSYS,
                errno.EOPNOTSUPP,
            }:
                _reflink_unsupported.add(destination_device)

                return False

            if exc.errno in {errno.EXDEV, errno.EINVAL}:
                return False

            raise

        finally:
            if destination_fd >= 0:
                os.close(destination_fd)

    finally:
        os.close(source_fd)

    import shutil

    shutil.copystat(source, destination, follow_symlinks=False)

    return True


def _hardlink(
    source: str,
    destination: str,
    source_device: int,
    destination_device: int,
) -> bool:
    """Hard link one regular file into place when the filesystems allow it.

    One syscall and no data movement.  The link shares mode and timestamps
    with ``source``, so there is no ``copystat`` to pay.  Only reached when
    :func:`_configured_link_mode` is ``hardlink``.
    """

    pair = (source_device, destination_device)

    if pair in _hardlink_unsupported:
        return False

    try:
        os.link(source, destination)

    except OSError as exc:
        if exc.errno == errno.EMLINK:
            return False

        if exc.errno in {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            errno.EOPNOTSUPP,
            errno.ENOTSUP,
            errno.EINVAL,
            getattr(errno, "ENOSYS", -1),
        }:
            _hardlink_unsupported.add(pair)

            return False

        raise

    return True


def clone_path(source: str, destination: str) -> None:
    """Copy a cache path using copy-on-write cloning whenever possible.

    Both paths must be absent from concurrent mutation. If ``destination`` is
    an existing directory, directory contents are merged while duplicate files
    are rejected.

    Regular files that cannot be cloned are hard linked on Linux and Windows,
    sharing their inode with ``source``, and copied when that fails or when
    ``KPIP_LINK_MODE`` is ``clone`` or ``copy``.
    """

    _clone(os.fspath(source), os.fspath(destination), None)


def _devices(source: str, destination: str, destination_exists: bool) -> Devices:
    """Resolve the ``(source, destination)`` device pair once per clone.

    Every entry below ``source`` shares its device: a cache tree contains no
    mount points.  An absent ``destination`` lands on its parent's device, and
    the directories created beneath it inherit that.
    """

    if destination_exists:
        parent = destination

    else:
        parent = os.path.dirname(destination) or os.curdir

    return os.lstat(source).st_dev, os.stat(parent).st_dev


def _clone(source: str, destination: str, devices: Devices | None) -> None:
    destination_exists = os.path.lexists(destination)

    if not destination_exists:
        try:
            if _darwin_clone(source, destination):
                return

        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise

            destination_exists = True

    if devices is None:
        devices = _devices(source, destination, destination_exists)

    if not destination_exists:
        source_is_link = os.path.islink(source)

        return _clone_absent(
            source,
            destination,
            os.path.isdir(source) and not source_is_link,
            source_is_link,
            devices,
        )

    if not (
        os.path.isdir(source)
        and not os.path.islink(source)
        and os.path.isdir(destination)
        and not os.path.islink(destination)
    ):
        raise FileExistsError(
            errno.EEXIST,
            "copy-on-write destination already exists",
            destination,
        )

    with os.scandir(source) as entries:
        for entry in entries:
            _clone(
                os.path.join(source, entry.name),
                os.path.join(destination, entry.name),
                devices,
            )


def _clone_absent(
    source: str,
    destination: str,
    is_directory: bool,
    is_symlink: bool,
    devices: Devices,
) -> None:
    """Clone ``source`` onto a ``destination`` known not to exist.

    ``is_directory`` and ``is_symlink`` are passed in because the directory
    walk below already has them from ``scandir``, which answers both from the
    ``readdir`` result.  Re-deriving them per entry -- as recursing through
    ``clone_path`` did -- costs an ``lexists``, an ``isdir`` and an ``islink``
    on every file in the tree.
    """

    if is_directory:
        source_mode = stat.S_IMODE(os.stat(source).st_mode)

        try:
            os.mkdir(destination, source_mode | stat.S_IWUSR | stat.S_IXUSR)

        except FileExistsError:
            return _clone(source, destination, devices)

        import shutil

        try:
            with os.scandir(source) as entries:
                for entry in entries:
                    _clone_absent(
                        entry.path,
                        os.path.join(destination, entry.name),
                        entry.is_dir(follow_symlinks=False),
                        entry.is_symlink(),
                        devices,
                    )

            # Restores the source mode, including the owner write and search
            # bits added above.  A read-only source directory cannot be
            # created read-only up front: its own children could not then be
            # written into it.  Only owner bits are added, so the directory is
            # never briefly more permissive to anyone else.
            shutil.copystat(source, destination, follow_symlinks=False)

        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)

            raise

        return

    if is_symlink:
        os.symlink(os.readlink(source), destination)

        return

    source_device, destination_device = devices

    mode = _configured_link_mode()

    if mode != "copy" and _linux_reflink(
        source, destination, source_device, destination_device
    ):
        return

    if mode == "hardlink" and _hardlink(
        source, destination, source_device, destination_device
    ):
        return

    import shutil

    shutil.copy2(source, destination, follow_symlinks=False)


def replace_contents(path: str, contents: bytes) -> None:
    """Rewrite ``path`` in a fresh inode, keeping its mode.

    An installed file may be a hard link into a cache tree; opening it for
    writing would rewrite the cache too.  Unlinking first leaves the cache's
    inode untouched, and the explicit ``chmod`` restores the mode regardless
    of the umask.
    """

    mode = stat.S_IMODE(os.lstat(path).st_mode)

    os.unlink(path)

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)

    with os.fdopen(descriptor, "wb") as file:
        file.write(contents)

    os.chmod(path, mode)
