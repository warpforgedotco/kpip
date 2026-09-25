"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

from kpip.core.utils import load_snapshot, save_snapshot, versioned_bucket

import atexit
import marshal
import os
import threading

from kpip.core.packaging import Requirement, parse_requirement
from kpip.core.versions import Version
from kpip.index.source_models import CandidateMetadata

# Version 2 is one marshal file rather than an SQLite database.
NAME = f"{versioned_bucket('candidate-metadata', 2)}.snapshot"
FORMAT = "kpip-candidate-metadata"
MAX_ENTRIES = 16_384
INSTANCES: dict[str, CandidateMetadataCache] = {}
# The trailing element is the interpreter the metadata was filtered for:
# entries are marker-filtered, so a lock for 3.8 must not answer for the
# interpreter running kpip. Rows written before it was part of the key no
# longer match, which is the point -- they cannot say who they were for.
CacheKey = tuple[str, str, tuple[str, ...], str, str]
CacheValue = tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]


class CandidateMetadataCache:
    """Process-local metadata cache backed by one marshal snapshot.

    The file maps each key to its value, itself marshalled, so reading it
    builds the keys and one bytes object per entry, and only the entries a
    run asks for are decoded and validated. It used to be an SQLite table,
    read whole on first use for the same reason; the snapshot keeps that and
    drops the ``sqlite3`` import, the connection, and a ``json.dumps`` of
    the key per lookup. A flush merges this run's changes into what is on
    disk then, so concurrent runs keep each other's entries.
    """

    __slots__ = (
        "_pending_deletes",
        "_pending_puts",
        "_stored",
        "decoded",
        "dirty",
        "entries",
        "lock",
        "path",
    )

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        self.path = os.path.join(os.fspath(cache_dir), NAME)
        self.lock = threading.RLock()
        self.dirty = False

        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}

        self._pending_puts: dict[CacheKey, CacheValue] = {}
        self._pending_deletes: set[CacheKey] = set()

        # Undecoded values as stored, read on first use.
        self._stored: dict[CacheKey, bytes] | None = None
        atexit.register(self.flush)

    def _read_stored(self) -> dict[CacheKey, bytes]:
        loaded = load_snapshot(self.path)
        if (
            isinstance(loaded, tuple)
            and len(loaded) == 2
            and loaded[0] == FORMAT
            and isinstance(loaded[1], dict)
        ):
            return loaded[1]  # ty: ignore[invalid-return-type]
        return {}

    @staticmethod
    def valid_value(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 5
            and isinstance(value[0], str)
            and isinstance(value[1], str)
            and isinstance(value[2], tuple)
            and all(isinstance(item, str) for item in value[2])
            and isinstance(value[3], tuple)
            and all(isinstance(item, str) for item in value[3])
            and (value[4] is None or isinstance(value[4], str))
        )

    def _load(self, key: CacheKey) -> CacheValue | None:
        """Decode one stored value, validate it and memoize it."""
        with self.lock:
            stored = self._stored
            if stored is None:
                stored = self._stored = self._read_stored()
            blob = stored.get(key)
        if blob is None or key in self._pending_deletes:
            return None
        try:
            loaded = marshal.loads(blob)
        except Exception:  # noqa: BLE001
            loaded = None
        if not self.valid_value(loaded):
            self._discard(key)
            return None
        value: CacheValue = loaded  # ty: ignore[invalid-assignment]
        self._evict()
        self.entries[key] = value
        return value

    def _discard(self, key: CacheKey) -> None:
        """Forget an entry that failed decoding, in memory and on disk."""
        self.entries.pop(key, None)
        self.decoded.pop(key, None)
        self._pending_puts.pop(key, None)
        self._pending_deletes.add(key)
        self.dirty = True

    def _evict(self) -> None:
        """Make room for one more entry."""
        if len(self.entries) >= MAX_ENTRIES:
            evicted = next(iter(self.entries))
            self.entries.pop(evicted, None)
            self.decoded.pop(evicted, None)

    def get(self, key: CacheKey) -> CandidateMetadata | None:
        decoded = self.decoded.get(key)
        if decoded is not None:
            return decoded

        value = self.entries.get(key)
        if value is None:
            value = self._load(key)
            if value is None:
                return None

        dependencies: list[Requirement] = []
        for raw in value[2]:
            requirement = self.decode_requirement(raw)
            if requirement is None:
                self._discard(key)
                return None
            dependencies.append(requirement)

        version = self.decode_version(value[1])
        if version is None:
            self._discard(key)
            return None

        metadata = CandidateMetadata(
            name=value[0],
            version=version,
            dependencies=tuple(dependencies),
            provided_extras=frozenset(value[3]),
            requires_python=value[4],
        )
        self.decoded[key] = metadata
        return metadata

    @staticmethod
    def decode_requirement(raw: str) -> Requirement | None:
        try:
            return parse_requirement(raw)
        except ValueError:
            return None

    @staticmethod
    def decode_version(raw: str) -> Version | None:
        try:
            return Version(raw)
        except ValueError:
            return None

    def contains(self, key: CacheKey) -> bool:
        """Check for cached metadata without decoding its requirements."""
        return key in self.entries or self._load(key) is not None

    def put(self, key: CacheKey, metadata: CandidateMetadata) -> None:
        if key not in self.entries:
            self._evict()

        value = (
            metadata.name,
            str(metadata.version),
            tuple(dependency.raw for dependency in metadata.dependencies),
            tuple(sorted(metadata.provided_extras)),
            metadata.requires_python,
        )
        self.entries[key] = value
        self.decoded[key] = metadata
        self._pending_puts[key] = value
        self._pending_deletes.discard(key)
        self.dirty = True

    def flush(self) -> None:
        """Merge this run's changes into the snapshot on disk."""
        if not self.dirty:
            return
        with self.lock:
            stored = self._read_stored()
            for key in self._pending_deletes:
                stored.pop(key, None)
            for key, value in self._pending_puts.items():
                stored.pop(key, None)
                stored[key] = marshal.dumps(value)
            # The newest entries are last; beyond the cap, the oldest go.
            excess = len(stored) - MAX_ENTRIES
            if excess > 0:
                for key in list(stored)[:excess]:
                    del stored[key]
            if save_snapshot(self.path, (FORMAT, stored)):
                self._stored = stored
                self._pending_puts.clear()
                self._pending_deletes.clear()
                self.dirty = False


def get_candidate_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> CandidateMetadataCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = INSTANCES.setdefault(key, CandidateMetadataCache(key))
    return cache
