"""Runtime wheel-archive adapters used by transactional installation."""

from __future__ import annotations

from typing import TYPE_CHECKING
import io
import os
import zipfile

from kpip.core.archive import (
    WheelArchive,
    WheelhouseUnavailable,
)
from kpip.install.wheel_archive_cache import CachedWheelArchive

if TYPE_CHECKING:
    from typing import Any
    from kpip.core.wheel import WheelCandidate


class RawWheelInfo:
    """Small member record returned by the streaming archive adapter."""

    __slots__ = ("external_attr", "file_size", "filename")

    def __init__(self, filename: str, file_size: int, external_attr: int) -> None:
        self.filename = filename

        self.file_size = file_size

        self.external_attr = external_attr

    def is_dir(self) -> bool:
        return self.filename.endswith("/")


class RawWheelArchive:
    """ZipFile-shaped adapter over the fast wheelhouse archive reader."""

    __slots__ = ("NameToInfo", "_archive", "_file", "_infos")

    def __init__(self, file: Any, archive: Any) -> None:
        self._file = file

        self._archive = archive

        self._infos = [
            RawWheelInfo(
                name,
                member[3],
                getattr(archive, "modes", {}).get(name, 0),
            )
            for name, member in archive.members.items()
        ]

        self.NameToInfo = {info.filename: info for info in self._infos}

    def infolist(self) -> list[RawWheelInfo]:
        return self._infos

    def namelist(self) -> list[str]:
        return [info.filename for info in self._infos]

    def read(self, member: str | RawWheelInfo) -> bytes:
        name = member if isinstance(member, str) else member.filename

        return self._archive.read(name)

    def open(self, member: RawWheelInfo) -> io.BytesIO:
        return io.BytesIO(self.read(member))

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> RawWheelArchive:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class CachedWheelInfo:
    """ZipInfo-shaped member backed by an immutable unpacked wheel tree."""

    __slots__ = (
        "external_attr",
        "file_size",
        "filename",
        "record_metadata",
        "source_path",
    )

    def __init__(
        self,
        tree: str,
        filename: str,
        digest: str,
        size: str,
        mode: int,
    ) -> None:
        self.filename = filename

        self.file_size = int(size)

        self.external_attr = mode << 16

        self.record_metadata = (digest, size)

        self.source_path = os.path.join(tree, *filename.split("/"))

    def is_dir(self) -> bool:
        return False


class CachedWheelTreeArchive:
    """ZipFile-shaped adapter over a :class:`CachedWheelArchive`.

    Its members are made when first asked for: an install takes most of a
    cached wheel's straight from ``entries``, the manifest's rows.
    """

    __slots__ = ("_entry_by_name", "_infos", "_name_to_info", "entries", "tree")

    def __init__(self, layout: Any) -> None:
        self.tree = layout.tree

        self.entries = layout.entries

        self._infos: list[CachedWheelInfo] | None = None

        self._name_to_info: dict[str, CachedWheelInfo] | None = None

        self._entry_by_name: dict[str, tuple[str, str, str, int]] | None = None

    def info(self, entry: tuple[str, str, str, int]) -> CachedWheelInfo:
        """The member for one row of ``entries``."""
        relative, digest, size, mode = entry

        return CachedWheelInfo(self.tree, relative, digest, size, mode)

    def infolist(self) -> list[CachedWheelInfo]:
        infos = self._infos

        if infos is None:
            infos = self._infos = [self.info(entry) for entry in self.entries]

        return infos

    @property
    def NameToInfo(self) -> dict[str, CachedWheelInfo]:
        name_to_info = self._name_to_info

        if name_to_info is None:
            name_to_info = self._name_to_info = {
                info.filename: info for info in self.infolist()
            }

        return name_to_info

    def namelist(self) -> list[str]:
        return [entry[0] for entry in self.entries]

    def read(self, member: str | CachedWheelInfo) -> bytes:
        if isinstance(member, str):
            entry_by_name = self._entry_by_name

            if entry_by_name is None:
                entry_by_name = self._entry_by_name = {
                    entry[0]: entry for entry in self.entries
                }

            member = self.info(entry_by_name[member])

        with open(member.source_path, "rb") as file:
            return file.read()

    def open(self, member: CachedWheelInfo):
        return open(member.source_path, "rb")

    def close(self) -> None:
        pass

    def __enter__(self) -> CachedWheelTreeArchive:
        return self

    def __exit__(self, *_: object) -> None:
        pass


_STREAMING_MEMBER_LIMIT = 1024 * 1024


def _raw_archive_from_layout(path: str, layout: object) -> RawWheelArchive | None:
    """Open the raw reader straight from a resolver layout, or None to decline.

    The layout already holds every member's central-directory record (name,
    compression, CRC, sizes, local header offset) and its mode bits, so the
    archive is reconstructed with no directory scan at all -- the resolver
    parsed it once, materialization reused it, and this is the third time
    the same wheel is opened on the way to being installed.
    """
    if not (isinstance(layout, tuple) and len(layout) == 3):
        return None

    raw_members = layout[1]

    if not isinstance(raw_members, tuple):
        return None

    members: dict[str, tuple[int, int, int, int, int]] = {}

    modes: dict[str, int] = {}

    for raw_member in raw_members:
        if not isinstance(raw_member, tuple) or len(raw_member) != 7:
            return None

        name, compress_type, crc, compress_size, file_size, header_offset, mode = (
            raw_member
        )

        if not (
            isinstance(name, str)
            and isinstance(compress_type, int)
            and isinstance(crc, int)
            and isinstance(compress_size, int)
            and isinstance(file_size, int)
            and isinstance(header_offset, int)
            and isinstance(mode, int)
        ):
            return None

        if compress_type not in {0, 8} or file_size > _STREAMING_MEMBER_LIMIT:
            return None

        members[name] = (compress_type, crc, compress_size, file_size, header_offset)

        modes[name] = mode

    try:
        file = open(path, "rb", buffering=0)  # noqa: SIM115

    except OSError:
        return None

    return RawWheelArchive(file, WheelArchive(file, members, modes))


def open_wheel_archive(
    path: str,
    candidate: WheelCandidate,
) -> zipfile.ZipFile | RawWheelArchive | CachedWheelTreeArchive:
    """Open a fast raw archive when its members fit the streaming contract."""

    layout = getattr(candidate, "wheel_layout", None)

    if layout is not None:
        if isinstance(layout, CachedWheelArchive):
            return CachedWheelTreeArchive(layout)

    if layout is not None:
        raw = _raw_archive_from_layout(path, layout)

        if raw is not None:
            return raw

        return zipfile.ZipFile(path)

    try:
        file = open(path, "rb", buffering=0)  # noqa: SIM115

        archive = WheelArchive(file)

    except OSError, ValueError, WheelhouseUnavailable:
        try:
            file.close()

        except UnboundLocalError:
            pass

        return zipfile.ZipFile(path)

    if any(
        member[0] not in {0, 8} or member[3] > 1024 * 1024
        for member in archive.members.values()
    ):
        file.close()

        return zipfile.ZipFile(path)

    return RawWheelArchive(file, archive)
