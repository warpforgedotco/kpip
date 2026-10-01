"""Hash collections and validation used across kpip packages."""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    # Every name here is only ever written in an annotation, and
    # annotations are strings in this module, so none of them needs to
    # exist at run time. ``typing`` in particular was being imported by
    # every command that hashes a file, for a type alias.
    Hash = Any


def file_hashes(path: str) -> dict[str, str]:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        buffer = bytearray(max(1, min(size, 1024 * 1024)))
        view = memoryview(buffer)
        while read := stream.readinto(buffer):
            digest.update(view[:read])
    return {"sha256": digest.hexdigest()}


class Hashes:
    """Build multiple hashes at once and check them against known values."""

    def __init__(self, hashes: dict[str, list[str]] | None = None) -> None:
        self.allowed_internal = {
            algorithm: [digest.lower() for digest in sorted(digests)]
            for algorithm, digests in (hashes or {}).items()
        }

    def __and__(self, other: Hashes) -> Hashes:
        if not other:
            return self
        if not self:
            return other
        return Hashes(
            {
                algorithm: [
                    digest
                    for digest in digests
                    if digest in self.allowed_internal.get(algorithm, [])
                ]
                for algorithm, digests in other.allowed_internal.items()
            },
        )

    @property
    def digest_count(self) -> int:
        return sum(len(digests) for digests in self.allowed_internal.values())

    @property
    def allowed_digests(self) -> frozenset[str]:
        return frozenset(
            digest for digests in self.allowed_internal.values() for digest in digests
        )

    def is_hash_allowed(self, hash_name: str, hex_digest: str) -> bool:
        return hex_digest.lower() in self.allowed_internal.get(hash_name, [])

    def __bool__(self) -> bool:
        return bool(self.allowed_internal)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hashes):
            return NotImplemented
        return self.allowed_internal == other.allowed_internal

    def __hash__(self) -> int:
        return hash(
            ",".join(
                sorted(
                    ":".join((algorithm, digest))
                    for algorithm, digest_list in self.allowed_internal.items()
                    for digest in digest_list
                ),
            ),
        )
