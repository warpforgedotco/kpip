"""Low-level wheel archive reading.

A ``struct`` and ``zlib`` reader over the zip container, with no first-party
dependencies and nothing host-specific in it. It lives in ``core`` because
every layer above reads wheels -- ``index`` to pull metadata out of a
candidate, ``install`` to clone one into a target, ``cli`` on the fast path --
and ``core`` is the only package all of them may import.
"""

from __future__ import annotations

import io
import os
import struct
import zlib

END_OF_CENTRAL_DIRECTORY = struct.Struct("<4s4H2LH")
CENTRAL_DIRECTORY_HEADER = struct.Struct("<4s6H3L5H2L")
LOCAL_FILE_HEADER = struct.Struct("<4s5H3L2H")
CENTRAL_DIRECTORY_SIZES = struct.Struct("<3H")
"""The name, extra and comment sizes at offset 28 of a central record."""

# What a ``metadata_only`` archive keeps: the members a metadata read opens.
_METADATA_SUFFIX_LENGTH = len(b".dist-info/METADATA")
_WHEEL_SUFFIX_LENGTH = len(b".dist-info/WHEEL")

_LOCAL_HEADER_HEADROOM = 512


class WheelhouseUnavailable(Exception):
    pass


_HAS_PREAD = hasattr(os, "pread")


class WheelArchive:
    __slots__ = (
        "_fd",
        "_metadata_only",
        "_tail",
        "_tail_start",
        "file",
        "members",
        "modes",
        "needs_zipfile",
    )

    def __init__(self, file, members=None, modes=None, *, metadata_only=False) -> None:
        self.file = file
        self._fd = file.fileno() if _HAS_PREAD and isinstance(file, io.FileIO) else -1
        self._metadata_only = metadata_only
        self.members: dict[str, tuple[int, int, int, int, int]] = (
            {} if members is None else members
        )
        self.modes: dict[str, int] = {} if modes is None else modes
        self._tail = b""
        self._tail_start = 0
        self.needs_zipfile = False
        if members is None:
            self.read_central_directory()

    def _read_at(self, offset: int, size: int) -> bytes:
        """Read ``size`` bytes at ``offset``: one pread on a file descriptor."""
        fd = self._fd
        if fd < 0:
            self.file.seek(offset)
            return self.file.read(size)
        data = os.pread(fd, size, offset)
        if len(data) < size:
            chunks = [data]
            got = len(data)
            while got < size:
                chunk = os.pread(fd, size - got, offset + got)
                if not chunk:
                    break
                chunks.append(chunk)
                got += len(chunk)
            data = b"".join(chunks)
        return data

    def read_central_directory(self) -> None:
        if self._fd >= 0:
            size = os.fstat(self._fd).st_size
        else:
            self.file.seek(0, 2)
            size = self.file.tell()
        tail_size = min(size, 22 + 65535)
        tail_start = size - tail_size
        tail = self._read_at(tail_start, tail_size)
        self._tail = tail
        self._tail_start = tail_start
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0 or marker + 22 > len(tail):
            raise WheelhouseUnavailable
        _, _, _, _, entries, directory_size, directory_offset, _ = (
            END_OF_CENTRAL_DIRECTORY.unpack_from(tail, marker)
        )
        if (
            entries == 0xFFFF
            or directory_size == 0xFFFFFFFF
            or directory_offset == 0xFFFFFFFF
        ):
            raise WheelhouseUnavailable
        if directory_offset + directory_size > size:
            raise WheelhouseUnavailable
        if directory_offset >= tail_start:
            start = directory_offset - tail_start
            directory = tail[start : start + directory_size]
        else:
            directory = self._read_at(directory_offset, directory_size)
        if len(directory) != directory_size:
            raise WheelhouseUnavailable
        directory_end = len(directory)
        unpack_record = CENTRAL_DIRECTORY_HEADER.unpack_from
        if self._metadata_only:
            self._read_metadata_records(directory, entries, unpack_record)
            return
        offset = 0
        for _ in range(entries):
            if offset + 46 > directory_end:
                raise WheelhouseUnavailable
            (
                signature,
                _,
                _,
                flags,
                compression,
                _,
                _,
                crc,
                compressed_size,
                uncompressed_size,
                name_size,
                extra_size,
                comment_size,
                _,
                _,
                external_attr,
                local_offset,
            ) = unpack_record(directory, offset)
            if signature != b"PK\x01\x02":
                raise WheelhouseUnavailable
            if (
                flags & 1
                or compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise WheelhouseUnavailable
            name_end = offset + 46 + name_size
            record_end = name_end + extra_size + comment_size
            if record_end > directory_end:
                raise WheelhouseUnavailable
            name_bytes = directory[offset + 46 : name_end]
            offset = record_end
            member = (
                compression,
                crc,
                compressed_size,
                uncompressed_size,
                local_offset,
            )
            if name_bytes.isascii():
                name = name_bytes.decode("ascii")
            else:
                try:
                    name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
                except UnicodeDecodeError as exc:
                    raise WheelhouseUnavailable from exc
            if compression != 8 and compression != 0:
                self.needs_zipfile = True
            if name in self.members:
                raise WheelhouseUnavailable
            self.members[name] = member
            self.modes[name] = external_attr

    def _read_metadata_records(self, directory, entries, unpack_record) -> None:
        """Collect only the ``.dist-info`` members a metadata read opens.

        A wheel's central directory has one record per shipped file -- for a
        large project, thousands -- and a metadata read opens two of them.
        Walking past a record needs only the three sizes that give its
        length, so the full record is unpacked, decoded and validated for
        the members this archive can actually hand back, and the rest cost
        one small unpack each.
        """
        directory_end = len(directory)
        unpack_sizes = CENTRAL_DIRECTORY_SIZES.unpack_from
        # ``startswith`` at an offset tests the buffer in place, so a member
        # this read skips never materialises its name.
        starts_with = directory.startswith
        offset = 0
        for _ in range(entries):
            if offset + 46 > directory_end:
                raise WheelhouseUnavailable
            if not starts_with(b"PK\x01\x02", offset):
                raise WheelhouseUnavailable
            name_size, extra_size, comment_size = unpack_sizes(directory, offset + 28)
            name_end = offset + 46 + name_size
            record_end = name_end + extra_size + comment_size
            if record_end > directory_end:
                raise WheelhouseUnavailable
            record_start = offset
            offset = record_end
            if not (
                (
                    name_size > _METADATA_SUFFIX_LENGTH
                    and starts_with(
                        b".dist-info/METADATA",
                        name_end - _METADATA_SUFFIX_LENGTH,
                    )
                )
                or (
                    name_size > _WHEEL_SUFFIX_LENGTH
                    and starts_with(
                        b".dist-info/WHEEL",
                        name_end - _WHEEL_SUFFIX_LENGTH,
                    )
                )
            ):
                continue
            name_bytes = directory[record_start + 46 : name_end]
            if name_bytes.count(b"/") != 1:
                continue
            (
                _,
                _,
                _,
                flags,
                compression,
                _,
                _,
                crc,
                compressed_size,
                uncompressed_size,
                _,
                _,
                _,
                _,
                _,
                external_attr,
                local_offset,
            ) = unpack_record(directory, record_start)
            if (
                flags & 1
                or compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise WheelhouseUnavailable
            if name_bytes.isascii():
                name = name_bytes.decode("ascii")
            else:
                try:
                    name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
                except UnicodeDecodeError as exc:
                    raise WheelhouseUnavailable from exc
            if compression != 8 and compression != 0:
                self.needs_zipfile = True
            if name in self.members:
                raise WheelhouseUnavailable
            self.members[name] = (
                compression,
                crc,
                compressed_size,
                uncompressed_size,
                local_offset,
            )
            self.modes[name] = external_attr

    def namelist(self) -> list[str]:
        return list(self.members)

    def read(self, name: str) -> bytes:
        try:
            member = self.members[name]
        except KeyError as exc:
            raise WheelhouseUnavailable from exc
        return self.read_member(member)

    def read_member(self, member: tuple[int, int, int, int, int]) -> bytes:
        compression, crc, compressed_size, uncompressed_size, local_offset = member
        if self._tail and local_offset >= self._tail_start:
            base = local_offset - self._tail_start
            tail = self._tail
            header = tail[base : base + 30]
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            _, _, _, _, _, _, _, _, _, name_size, extra_size = LOCAL_FILE_HEADER.unpack(
                header,
            )
            start = base + 30 + name_size + extra_size
            data = tail[start : start + compressed_size]
        else:
            blob = self._read_at(
                local_offset,
                30 + _LOCAL_HEADER_HEADROOM + compressed_size,
            )
            header = blob[:30]
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            _, _, _, _, _, _, _, _, _, name_size, extra_size = LOCAL_FILE_HEADER.unpack(
                header,
            )
            start = 30 + name_size + extra_size
            end = start + compressed_size
            if end <= len(blob):
                data = blob[start:end]
            else:
                data = self._read_at(local_offset + start, compressed_size)
        if len(data) != compressed_size:
            raise WheelhouseUnavailable
        if compression == 0:
            result = data
        elif compression == 8:
            try:
                result = zlib.decompress(data, -15)
            except zlib.error as exc:
                raise WheelhouseUnavailable from exc
        else:
            raise WheelhouseUnavailable
        if len(result) != uncompressed_size or zlib.crc32(result) & 0xFFFFFFFF != crc:
            raise WheelhouseUnavailable
        return result

    def read_many(
        self,
        names: list[str],
        *,
        ordered_input: bool = False,
    ) -> list[bytes]:
        """Read members while returning the requested order."""
        members = [self.members[name] for name in names]
        in_archive_order = ordered_input or all(
            left[4] <= right[4] for left, right in zip(members, members[1:])
        )
        if in_archive_order:
            ordered_names = names
            ordered_members = members
        else:
            ordered = sorted(zip(members, names), key=lambda item: item[0][4])
            ordered_members = [member for member, _ in ordered]
            ordered_names = [name for _, name in ordered]
        ordered_results: list[bytes] = []
        unordered_results: dict[str, bytes] | None = None if in_archive_order else {}
        tail_source = io.BytesIO(self._tail) if self._tail else None
        position = -1
        for name, member in zip(ordered_names, ordered_members):
            compression, crc, compressed_size, uncompressed_size, local_offset = member
            if tail_source is not None and local_offset >= self._tail_start:
                source = tail_source
                source.seek(local_offset - self._tail_start)
            else:
                source = self.file
                if local_offset != position:
                    source.seek(local_offset)
            header = source.read(30)
            if len(header) != 30 or header[:4] != b"PK\x03\x04":
                raise WheelhouseUnavailable
            (_, _, _, _, _, _, _, _, _, name_size, extra_size) = (
                LOCAL_FILE_HEADER.unpack(header)
            )
            source.seek(name_size + extra_size, 1)
            data = source.read(compressed_size)
            if len(data) != compressed_size:
                raise WheelhouseUnavailable
            if compression == 0:
                result = data
            elif compression == 8:
                try:
                    result = zlib.decompress(data, -15)
                except zlib.error as exc:
                    raise WheelhouseUnavailable from exc
            else:
                raise WheelhouseUnavailable
            if (
                len(result) != uncompressed_size
                or zlib.crc32(result) & 0xFFFFFFFF != crc
            ):
                raise WheelhouseUnavailable
            if in_archive_order:
                ordered_results.append(result)
            else:
                assert unordered_results is not None
                unordered_results[name] = result
            if source is self.file:
                position = local_offset + 30 + name_size + extra_size + compressed_size
        if in_archive_order:
            return ordered_results
        assert unordered_results is not None
        return [unordered_results[name] for name in names]
