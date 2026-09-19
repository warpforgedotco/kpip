"""Persistent cache for metadata used by dependency resolution."""

from __future__ import annotations

from kpip.core.utils import versioned_bucket

import json
import marshal
import os
import sqlite3
from typing import cast

from kpip.core.packaging import Requirement, parse_requirement
from kpip.core.versions import Version
from kpip.index.source_models import CandidateMetadata
from kpip.index.sqlite_cache import SqliteBackedCache

NAME = f"{versioned_bucket('candidate-metadata', 1)}.sqlite"
MAX_ENTRIES = 16_384
INSTANCES: dict[str, CandidateMetadataCache] = {}
CacheKey = tuple[str, str, tuple[str, ...], str]
CacheValue = tuple[str, str, tuple[str, ...], tuple[str, ...], str | None]


class CandidateMetadataCache(SqliteBackedCache):
    """Process-local metadata cache backed by an incremental SQLite database."""

    __slots__ = (
        "_bulk",
        "_bulk_read_done",
        "_pending_deletes",
        "_pending_puts",
        "decoded",
        "entries",
    )

    SCHEMA = "CREATE TABLE IF NOT EXISTS candidate_metadata (key TEXT PRIMARY KEY, value BLOB);"

    def __init__(self, cache_dir: str | os.PathLike[str]) -> None:
        super().__init__(os.path.join(os.fspath(cache_dir), NAME))

        self.entries: dict[CacheKey, CacheValue] = {}
        self.decoded: dict[CacheKey, CandidateMetadata] = {}

        self._pending_puts: dict[CacheKey, CacheValue] = {}
        self._pending_deletes: set[CacheKey] = set()

        # Undecoded rows from the one bulk read, keyed as stored. ``None``
        # once a bulk read declined, which leaves lookups reading per key.
        self._bulk: dict[str, bytes] | None = None
        self._bulk_read_done = False

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

    def _bulk_read(self, conn: sqlite3.Connection) -> None:
        """Read every row once, so later lookups cost a dict probe.

        A resolve probes this cache once per candidate release, and a
        backtracking one probes thousands -- each a prepared statement, an
        index descent and a row fetch.  Reading the table whole amortises
        that into a single scan.  ``MAX_ENTRIES`` already caps what this
        cache will hold in memory, so a database above it keeps the
        per-key reads rather than loading more than that budget allows.
        """
        self._bulk_read_done = True
        try:
            count = conn.execute(
                "SELECT count(*) FROM candidate_metadata",
            ).fetchone()
            if count is not None and count[0] <= MAX_ENTRIES:
                self._bulk = dict(
                    conn.execute("SELECT key, value FROM candidate_metadata"),
                )
        except sqlite3.Error:
            self._bulk = None

    def _load(self, key: CacheKey) -> CacheValue | None:
        """Read one row out of the database, validate it and memoize it."""
        encoded = json.dumps(key)
        with self.lock:
            try:
                conn = self._reader()
                if conn is None:
                    return None
                if not self._bulk_read_done:
                    self._bulk_read(conn)
                bulk = self._bulk
                if bulk is not None:
                    # The bulk read holds every row, so absence is a miss.
                    # It keeps them rather than handing each out once: an
                    # entry ``_evict`` drops has to be readable again.
                    blob = bulk.get(encoded)
                    row = None if blob is None else (blob,)
                else:
                    row = conn.execute(
                        "SELECT value FROM candidate_metadata WHERE key = ?",
                        (encoded,),
                    ).fetchone()
            except sqlite3.Error:
                return None
        if row is None:
            return None
        try:
            loaded = marshal.loads(row[0])
        except Exception:  # noqa: BLE001
            loaded = None
        if not self.valid_value(loaded):
            self._discard(key)
            return None
        value = cast("CacheValue", loaded)
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

    def _flush_pending(self, conn: sqlite3.Connection) -> None:
        if self._pending_deletes:
            conn.executemany(
                "DELETE FROM candidate_metadata WHERE key = ?",
                [(json.dumps(key),) for key in self._pending_deletes],
            )
        items = [
            (json.dumps(key), marshal.dumps(value))
            for key, value in self._pending_puts.items()
        ]
        if items:
            conn.executemany(
                "INSERT OR REPLACE INTO candidate_metadata (key, value) VALUES (?, ?)",
                items,
            )

    def _clear_pending(self) -> None:
        self._pending_puts.clear()
        self._pending_deletes.clear()


def get_candidate_metadata_cache(
    cache_dir: str | os.PathLike[str],
) -> CandidateMetadataCache:
    key = os.path.abspath(os.fspath(cache_dir))
    cache = INSTANCES.get(key)
    if cache is None:
        cache = INSTANCES.setdefault(key, CandidateMetadataCache(key))
    return cache
