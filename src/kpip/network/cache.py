"""HTTP cache implementation."""

from __future__ import annotations

import os
import struct
import threading
from contextlib import contextmanager

from kpip.core.utils import ensure_dir
from kpip.network.freshness import (
    COMBINED_MAGIC,
    cache_entry_path,
    read_cache_metadata,
)
from kpip.host.filesystem import replace, set_descriptor_permissions

PRIVATE_MODE = 0o600
"""The mode a temporary entry falls back to when its own cannot be created."""


def process_umask() -> int:
    """The umask this process was started with, read once.

    ``os.umask`` is the only portable way to read it and it is also the way
    to set it, so calling it repeatedly from the worker threads that write
    cache entries would race with them.  Linux publishes the value, and the
    set-and-restore fallback runs once, before any entry is written.
    """
    global _PROCESS_UMASK
    if _PROCESS_UMASK is None:
        _PROCESS_UMASK = _read_process_umask()
    return _PROCESS_UMASK


def _read_process_umask() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    previous = os.umask(0o022)
    os.umask(previous)
    return previous


_PROCESS_UMASK: int | None = None

"""Directory under the cache directory holding the HTTP page cache."""


TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from typing import Any, BinaryIO

COMBINED_HEADER = struct.Struct(f"<{len(COMBINED_MAGIC)}sQ")


@contextmanager
def suppressed_cache_errors() -> Generator[None, None, None]:
    """If we can't access the cache then we can just skip caching and process
    as if caching wasn't enabled.
    """
    try:
        yield
    except OSError:
        pass


class SafeFileCache:
    """A file based cache which is safe to use even when the target directory may
    not be accessible or writable.

    Entries written through ``set_with_body`` are one self-contained file:
    a fixed header naming the metadata length, the metadata, then the body.
    One atomic replacement per store, one open per read, and no window in
    which another process can observe metadata without its body.

    Entries whose body must remain a raw standalone file -- artifact bodies
    that callers hard-link into place via ``get_body_path`` -- keep the
    split layout: a metadata file beside a ``.body`` companion, written by
    ``set``/``set_body``/``set_body_from_io``. Readers accept both layouts,
    so caches written by earlier versions keep working, and earlier
    versions treat combined entries as misses.

    Cache writes are not fsynced: an entry lost to a crash only costs a
    refetch, and the fsync would dominate the write.
    """

    def __init__(self, directory: str) -> None:
        assert directory is not None, "Cache directory must not be None."
        super().__init__()
        self.directory = directory
        # Directories this cache has already created, so an entry pays for
        # its directory once, not a makedirs walk per write.  Threads race
        # on the set harmlessly: a lost update costs one extra makedirs.
        self._known_directories: set[str] = set()
        # Entry files take the cache directory's permissions, read once.
        self._entry_mode: int | None = None

        self._creation_mode: int | None = None
        self._temporary_serial = 0

    def get_cache_path(self, name: str) -> str:
        """Where the entry for ``name`` lives: one fan-out level, 256 wide.

        The layout used to nest five single-character levels, pip's shape,
        which spreads a large cache thinly but makes a fresh cache create a
        directory or three for nearly every entry it stores: 9,218 mkdirs and
        the stats behind them for the 3,372 entries of one cold resolve, each
        a release and re-acquire of the interpreter lock behind the resolver.
        One level keeps a cache of a hundred thousand entries at a few
        hundred files per directory, which any filesystem in use serves
        directly, and a fresh cache creates at most 256 directories.  The
        HTTP cache bucket's version was bumped with this change, so an older
        cache is left where it is rather than mixed with.
        """
        return cache_entry_path(self.directory, name)

    @staticmethod
    def read_combined_header(file: BinaryIO) -> int | None:
        """The metadata length when ``file`` starts a combined entry."""
        header = file.read(COMBINED_HEADER.size)
        if len(header) != COMBINED_HEADER.size:
            return None
        magic, metadata_length = COMBINED_HEADER.unpack(header)
        if magic != COMBINED_MAGIC:
            return None
        return metadata_length

    def get(self, key: str) -> bytes | None:
        return read_cache_metadata(self.directory, key)

    def get_atomic(self, key: str) -> bytes | None:
        """Read a self-contained entry written with one atomic replacement."""
        path = self.get_cache_path(key) + ".atomic"
        with suppressed_cache_errors():
            with open(path, "rb") as file:
                return file.read()
        return None

    def entry_mode(self) -> int:
        """The mode entry files are created with: the cache directory's."""
        mode = self._entry_mode
        if mode is None:
            mode = self._entry_mode = os.stat(self.directory).st_mode & 0o666 | 0o600
        return mode

    def create_temporary(self, path: str) -> tuple[int, str]:
        """Open a private temporary beside ``path``, returning it and its name."""
        directory = os.path.dirname(path)
        if directory not in self._known_directories:
            ensure_dir(directory)
            self._known_directories.add(directory)

        self._temporary_serial += 1
        temporary = (
            f"{path}.{os.getpid()}-{threading.get_ident()}-{self._temporary_serial}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            self.creation_mode(),
        )
        return descriptor, temporary

    def creation_mode(self) -> int:
        """The mode to create a temporary with, avoiding a chmod if it can.

        ``open`` masks the mode it is given, so a mode with a bit the umask
        clears has to be created narrow and widened afterwards.  When the
        umask clears nothing the entry wants -- the usual case, a 0755 cache
        directory under a 0022 umask asking for 0644 -- the file is created
        at its final mode and the chmod disappears, which is one syscall per
        entry across thousands of them.
        """
        mode = self._creation_mode
        if mode is None:
            wanted = self.entry_mode()
            mode = self._creation_mode = (
                wanted if wanted & ~process_umask() == wanted else PRIVATE_MODE
            )
        return mode

    def widen_to_entry_mode(self, descriptor: int, path: str) -> None:
        """Give a temporary the cache directory's mode, if that widens it.

        ``creation_mode`` already gave it the mode it wants unless the umask
        would have masked a bit off, so this is the narrow fallback rather
        than the usual path, and a resolve stores thousands of entries.
        """
        mode = self.entry_mode()
        if mode != self.creation_mode():
            set_descriptor_permissions(descriptor, path, mode)

    def write_to_file(self, path: str, writer_func: Callable[[BinaryIO], Any]) -> None:
        """Write an entry atomically, with the cache directory's permissions.

        For a caller that streams rather than handing over one blob; a blob
        goes through :meth:`write_internal`, which needs no file object.

        A resolve stores thousands of entries from worker threads, and every
        syscall a write makes is a release and re-acquire of the interpreter
        lock behind the resolver.  This is the shortest sequence that keeps
        the same guarantees: the directory is created once per directory
        rather than walked per write, the file is opened exclusively under
        a name no other thread or process uses, its mode is widened from one
        stat of the cache directory rather than chmod-ed per write, and the
        finished file is renamed into place.
        """
        with suppressed_cache_errors():
            descriptor, temporary = self.create_temporary(path)
            try:
                self.widen_to_entry_mode(descriptor, temporary)
                with os.fdopen(descriptor, "wb") as f:
                    descriptor = -1
                    writer_func(f)
                replace(temporary, path)
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                with suppressed_cache_errors():
                    os.unlink(temporary)
                raise

    def write_internal(self, path: str, data: bytes) -> None:
        """Write one blob atomically, straight to the descriptor.

        Almost every entry is a single payload of bytes, and wrapping the
        descriptor in a ``BufferedWriter`` for one write costs the wrapper,
        a copy into its buffer and a flush, per entry.
        """
        with suppressed_cache_errors():
            descriptor, temporary = self.create_temporary(path)
            try:
                self.widen_to_entry_mode(descriptor, temporary)
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.close(descriptor)
            except BaseException:
                os.close(descriptor)
                with suppressed_cache_errors():
                    os.unlink(temporary)
                raise
            try:
                replace(temporary, path)
            except BaseException:
                with suppressed_cache_errors():
                    os.unlink(temporary)
                raise

    def write_from_io(self, path: str, source_file: BinaryIO) -> None:
        import shutil

        self.write_to_file(path, lambda f: shutil.copyfileobj(source_file, f))

    def set(self, key: str, value: bytes) -> None:
        """Set an entry's metadata, preserving the body it is stored with."""
        path = self.get_cache_path(key)
        body: bytes | None = None
        with suppressed_cache_errors():
            with open(path, "rb", buffering=0) as file:
                metadata_length = self.read_combined_header(file)
                if metadata_length is not None:
                    file.seek(metadata_length, os.SEEK_CUR)
                    body = file.read()
        if body is not None:
            self.write_combined(path, value, body)
        else:
            self.write_internal(path, value)

    def set_atomic(self, key: str, value: bytes) -> None:
        """Write a self-contained entry that needs no companion body file."""
        self.write_internal(self.get_cache_path(key) + ".atomic", value)

    def patch_atomic(self, key: str, prefix: bytes, offset: int, data: bytes) -> None:
        """Overwrite ``data`` at ``offset`` of an atomic entry starting with ``prefix``.

        In place, without the rewrite and rename of ``set_atomic``: for a
        few bytes the entry keeps outside its checksum and validates on its
        own. An entry that is absent, or is not ``prefix``'s kind, is left
        alone.
        """
        path = self.get_cache_path(key) + ".atomic"
        with suppressed_cache_errors():
            with open(path, "r+b", buffering=0) as file:
                if file.read(len(prefix)) == prefix:
                    file.seek(offset)
                    file.write(data)

    def delete(self, key: str) -> None:
        path = self.get_cache_path(key)
        with suppressed_cache_errors():
            os.remove(path)
        with suppressed_cache_errors():
            os.remove(path + ".body")
        with suppressed_cache_errors():
            os.remove(path + ".atomic")

    def get_with_body(self, key: str) -> tuple[bytes | None, BinaryIO | None]:
        """Read the metadata and open the body with one path computation.

        The returned file is positioned at the body, whichever layout the
        entry uses.
        """
        metadata_path = self.get_cache_path(key)
        with suppressed_cache_errors():
            # Unbuffered: the header reads stay two small direct reads, and
            # the caller's body read becomes one presized readall from the
            # offset instead of a buffered drain-and-join that copies the
            # body twice more.
            file = open(metadata_path, "rb", buffering=0)
            try:
                metadata_length = self.read_combined_header(file)
                if metadata_length is not None:
                    return file.read(metadata_length), file
                file.seek(0)
                metadata = file.read()
            except BaseException:
                file.close()
                raise
            file.close()
            return metadata, open(metadata_path + ".body", "rb", buffering=0)
        return None, None

    def get_body(self, key: str) -> BinaryIO | None:
        metadata_path = self.get_cache_path(key)
        with suppressed_cache_errors():
            file = open(metadata_path, "rb", buffering=0)
            try:
                metadata_length = self.read_combined_header(file)
                if metadata_length is not None:
                    file.seek(metadata_length, os.SEEK_CUR)
                    return file
            except BaseException:
                file.close()
                raise
            file.close()
            return open(metadata_path + ".body", "rb", buffering=0)
        return None

    def get_body_path(self, key: str) -> str | None:
        """Return the immutable body path without opening or copying it.

        Only split-layout entries have a standalone body file; a combined
        entry returns ``None`` and callers fall back to ``get_body``.
        """
        metadata_path = self.get_cache_path(key)
        body_path = metadata_path + ".body"
        with suppressed_cache_errors():
            with open(metadata_path, "rb", buffering=0) as file:
                if self.read_combined_header(file) is not None:
                    return None
            os.stat(body_path)
            return body_path
        return None

    def write_combined(self, path: str, metadata: bytes, body: bytes) -> None:
        header = COMBINED_HEADER.pack(COMBINED_MAGIC, len(metadata))
        # One write: three would be three syscalls through an unbuffered path
        # and the same three, plus the buffer, through a buffered one.
        self.write_to_file(path, lambda f: f.write(b"".join((header, metadata, body))))

    def set_with_body(self, key: str, metadata: bytes, body: bytes) -> None:
        """Atomically replace an entry's metadata and body together."""
        path = self.get_cache_path(key)
        self.write_combined(path, metadata, body)
        with suppressed_cache_errors():
            os.remove(path + ".body")

    def demote_combined(self, path: str) -> None:
        """Rewrite a combined entry as a bare metadata file.

        Called after a standalone ``.body`` is written for a key whose
        entry was combined, so readers see the new body instead of the
        embedded one.
        """
        metadata: bytes | None = None
        with suppressed_cache_errors():
            with open(path, "rb", buffering=0) as file:
                metadata_length = self.read_combined_header(file)
                if metadata_length is None:
                    return
                metadata = file.read(metadata_length)
        if metadata is not None:
            self.write_internal(path, metadata)

    def set_body(self, key: str, body: bytes) -> None:
        path = self.get_cache_path(key)
        self.write_internal(path + ".body", body)
        self.demote_combined(path)

    def set_body_from_io(self, key: str, body_file: BinaryIO) -> None:
        """Set the body of the cache entry from a file object."""
        path = self.get_cache_path(key)
        self.write_from_io(path + ".body", body_file)
        self.demote_combined(path)
