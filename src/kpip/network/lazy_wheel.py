"""Lazy ZIP over HTTP"""

from __future__ import annotations

__all__ = [
    "HTTPRangeRequestUnsupported",
    "dist_from_wheel_url",
    "metadata_text_from_wheel_url",
]

import shutil
from bisect import bisect_left, bisect_right
from collections.abc import Generator
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from types import TracebackType
from zipfile import BadZipFile, ZipFile

from kpip._vendor.urllib3.exceptions import DecodeError
from kpip.build.metadata import MetadataDistribution
from kpip.core.http_contracts import HttpResponse, HttpStatusError, raise_for_status
from kpip.network.exceptions import InvalidWheel
from kpip.network.session import NetworkSession

CONTENT_CHUNK_SIZE = 10 * 1024

# The opening suffix request reads more than the per-read chunk: a large
# wheel's central directory routinely exceeds 10 KiB, and the extra tail
# bytes are cheaper than the range round-trip they save.
TAIL_CHUNK_SIZE = 64 * 1024


class HTTPRangeRequestUnsupported(Exception):
    pass


def dist_from_wheel_url(
    name: str,
    url: str,
    session: NetworkSession,
) -> MetadataDistribution:
    """Return a distribution object from the given wheel URL.

    This uses HTTP range requests to only fetch the portion of the wheel
    containing metadata, just enough for the object to be constructed.
    If such requests are not supported, HTTPRangeRequestUnsupported
    is raised.
    """
    try:
        with LazyZipOverHTTP(url, session) as zf:
            with ZipFile(zf) as archive:
                return MetadataDistribution.from_wheel_archive(archive, name, zf.name)
    except (BadZipFile, DecodeError) as exc:
        raise InvalidWheel(url, name) from exc


def metadata_text_from_wheel_url(
    name: str,
    url: str,
    session: NetworkSession,
) -> str:
    """The ``METADATA`` of a remote wheel, without downloading the wheel.

    Reads the zip's central directory and that one member over HTTP range
    requests. Returns the text so callers can parse it with the same reader
    they use for a PEP 658 sidecar rather than taking on a second shape.

    Raises :class:`HTTPRangeRequestUnsupported` if the host will not serve
    ranges, and whatever the archive or the wheel itself raises otherwise.
    """
    from kpip.core.wheel import read_wheel_archive_member, validate_wheel

    with LazyZipOverHTTP(url, session) as lazy, ZipFile(lazy) as archive:
        info_dir = validate_wheel(archive, name)

        return read_wheel_archive_member(
            archive,
            f"{info_dir}/METADATA",
        ).decode("utf-8", "replace")


class LazyZipOverHTTP:
    """File-like object mapped to a ZIP file over HTTP.

    This uses HTTP range requests to lazily fetch the file's content,
    which is supposed to be fed to ZipFile.  If such requests are not
    supported by the server, raise HTTPRangeRequestUnsupported
    during initialization.
    """

    def __init__(
        self,
        url: str,
        session: NetworkSession,
        chunk_size: int = CONTENT_CHUNK_SIZE,
    ) -> None:
        self.session_internal, self.url_internal, self.chunk_size_internal = (
            session,
            url,
            chunk_size,
        )
        self.left_internal: list[int] = []
        self.right_internal: list[int] = []

        # One suffix range request replaces the old HEAD-then-GET probe: a
        # 206 both proves range support and delivers the tail holding the
        # central directory, so the typical wheel costs one request here
        # instead of two, and a host that serves ranges without advertising
        # Accept-Ranges now works.
        tail = session.get(
            url,
            headers={
                "Range": f"bytes=-{TAIL_CHUNK_SIZE}",
                "Cache-Control": "no-cache",
            },
            stream=True,
        )
        try:
            raise_for_status(tail)
        except HttpStatusError as exc:
            response = exc.response
            if response is not None:
                response.drain_conn()
                response.close()
                if response.status == 416:
                    # The suffix asked for more bytes than the wheel holds,
                    # so the host serves ranges and the file is tiny: one
                    # plain GET fetches all of it.
                    self.load_entire_file()
                    self.check_zip()
                    return
            raise

        if tail.status != 206:
            # The host ignored the Range header and is streaming the whole
            # body; close without draining -- the body could be the entire
            # wheel -- and report the host as range-less.
            tail.close()
            raise HTTPRangeRequestUnsupported("range request is not supported")

        self.load_tail_response(tail)
        self.check_zip()

    def load_tail_response(self, tail: HttpResponse) -> None:
        """Take total length and the tail bytes from a 206 suffix response."""
        content_range = tail.headers.get("Content-Range", "")
        spec = content_range.partition("bytes ")[2]
        range_text, _, total_text = spec.partition("/")
        start_text = range_text.partition("-")[0]
        try:
            length = int(total_text)
            start = int(start_text)
        except ValueError:
            tail.close()
            unparseable = f"could not parse Content-Range {content_range!r}"
            raise HTTPRangeRequestUnsupported(unparseable) from None
        self.length_internal = length
        self.file_internal = NamedTemporaryFile()
        self.truncate(length)
        self.seek(start)
        shutil.copyfileobj(tail, self.file_internal, self.chunk_size_internal)
        end = self.tell() - 1
        if end >= start:
            self.left_internal = [start]
            self.right_internal = [end]
        self.seek(0)

    def load_entire_file(self) -> None:
        """Fetch the whole file with one plain GET (wheels below chunk size)."""
        response = self.session_internal.get(
            self.url_internal,
            headers={"Cache-Control": "no-cache"},
            stream=True,
        )
        try:
            raise_for_status(response)
        except HttpStatusError:
            response.drain_conn()
            response.close()
            raise
        self.file_internal = NamedTemporaryFile()
        shutil.copyfileobj(response, self.file_internal, self.chunk_size_internal)
        self.length_internal = self.tell()
        if self.length_internal > 0:
            self.left_internal = [0]
            self.right_internal = [self.length_internal - 1]
        self.seek(0)

    @property
    def mode(self) -> str:
        """Opening mode, which is always rb."""
        return "rb"

    @property
    def name(self) -> str:
        """Path to the underlying file."""
        return self.file_internal.name

    def seekable(self) -> bool:
        """Return whether random access is supported, which is True."""
        return True

    def close(self) -> None:
        """Close the file."""
        self.file_internal.close()

    @property
    def closed(self) -> bool:
        """Whether the file is closed."""
        return self.file_internal.closed

    def read(self, size: int = -1) -> bytes:
        """Read up to size bytes from the object and return them.

        As a convenience, if size is unspecified or -1,
        all bytes until EOF are returned.  Fewer than
        size bytes may be returned if EOF is reached.
        """
        download_size = max(size, self.chunk_size_internal)
        start, length = self.tell(), self.length_internal
        stop = length if size < 0 else min(start + download_size, length)
        start = max(0, stop - download_size)
        self.download_internal(start, stop - 1)
        return self.file_internal.read(size)

    def readable(self) -> bool:
        """Return whether the file is readable, which is True."""
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        """Change stream position and return the new absolute position.

        Seek to offset relative position indicated by whence:
        * 0: Start of stream (the default).  pos should be >= 0;
        * 1: Current position - pos may be negative;
        * 2: End of stream - pos usually negative.
        """
        return self.file_internal.seek(offset, whence)

    def tell(self) -> int:
        """Return the current position."""
        return self.file_internal.tell()

    def truncate(self, size: int | None = None) -> int:
        """Resize the stream to the given size in bytes.

        If size is unspecified resize to the current position.
        The current stream position isn't changed.

        Return the new file size.
        """
        return self.file_internal.truncate(size)

    def writable(self) -> bool:
        """Return False."""
        return False

    def __enter__(self) -> LazyZipOverHTTP:
        self.file_internal.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self.file_internal.__exit__(exc_type, exc_value, traceback)

    @contextmanager
    def stay(self) -> Generator[None, None, None]:
        """Return a context manager keeping the position.

        At the end of the block, seek back to original position.
        """
        pos = self.tell()
        try:
            yield
        finally:
            self.seek(pos)

    def check_zip(self) -> None:
        """Check and download until the file is a valid ZIP."""
        end = self.length_internal - 1
        for start in reversed(range(0, end, self.chunk_size_internal)):
            try:
                self.download_internal(start, end)
            except HttpStatusError as exc:
                if exc.response is not None and exc.response.status == 416:
                    raise InvalidWheel(self.name, "unknown") from exc
                raise
            with self.stay():
                try:
                    ZipFile(self)
                except BadZipFile:
                    pass
                else:
                    break

    def stream_response(
        self,
        start: int,
        end: int,
    ) -> HttpResponse:
        """Return HTTP response to a range request from start to end."""
        return self.session_internal.get(
            self.url_internal,
            headers={
                "Range": f"bytes={start}-{end}",
                "Cache-Control": "no-cache",
            },
            stream=True,
        )

    def merge(
        self,
        start: int,
        end: int,
        left: int,
        right: int,
    ) -> Generator[tuple[int, int], None, None]:
        """Return a generator of intervals to be fetched.

        Args:
            start (int): Start of needed interval
            end (int): End of needed interval
            left (int): Index of first overlapping downloaded data
            right (int): Index after last overlapping downloaded data

        """
        lslice, rslice = self.left_internal[left:right], self.right_internal[left:right]
        i = start = min([start] + lslice[:1])
        end = max([end] + rslice[-1:])
        for j, k in zip(lslice, rslice):
            if j > i:
                yield i, j - 1
            i = k + 1
        if i <= end:
            yield i, end
        self.left_internal[left:right], self.right_internal[left:right] = [start], [end]

    def download_internal(self, start: int, end: int) -> None:
        """Download bytes from start to end inclusively."""
        with self.stay():
            left = bisect_left(self.right_internal, start)
            right = bisect_right(self.left_internal, end)
            for start, end in self.merge(start, end, left, right):
                response = self.stream_response(start, end)
                try:
                    raise_for_status(response)
                except HttpStatusError:
                    # Streamed; unread data would keep the connection
                    # checked out of the pool and the socket open.
                    response.drain_conn()
                    response.close()
                    raise
                self.seek(start)
                shutil.copyfileobj(
                    response,
                    self.file_internal,
                    self.chunk_size_internal,
                )
