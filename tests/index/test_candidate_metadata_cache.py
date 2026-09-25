from __future__ import annotations

from pathlib import Path
from typing import Any

from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.candidate_metadata_cache import (
    NAME,
    CandidateMetadataCache,
    get_candidate_metadata_cache,
)
from kpip.index.source_models import CandidateMetadata


def test_candidate_metadata_cache_roundtrip(tmp_path: Path) -> None:
    cache = get_candidate_metadata_cache(tmp_path)
    key = (
        "https://files.example.test/demo.whl",
        "1.2.3",
        ("docs",),
        "sha256:abc",
    )
    metadata = CandidateMetadata(
        name="demo",
        version=Version("1.2.3"),
        dependencies=(parse_requirement("requests>=2"),),
        provided_extras=frozenset(("docs",)),
        requires_python=">=3.9",
    )

    assert not cache.contains(key)
    cache.put(key, metadata)
    assert cache.contains(key)
    cache.flush()
    loaded = CandidateMetadataCache(tmp_path).get(key)

    assert loaded is not None
    assert loaded.name == "demo"
    assert loaded.version == Version("1.2.3")
    assert loaded.dependencies[0].raw == "requests>=2"
    assert loaded.provided_extras == frozenset(("docs",))
    assert loaded.requires_python == ">=3.9"


def test_candidate_metadata_cache_defers_database_creation(tmp_path: Path) -> None:
    """A resolve that only misses must not pay to create the database."""
    key = ("https://example.test/cold.whl", "1", (), "sha256:cold")

    cache = CandidateMetadataCache(tmp_path)
    assert not cache.contains(key)
    assert cache.get(key) is None
    assert not (tmp_path / NAME).exists()

    cache.put(
        key,
        CandidateMetadata(
            name="cold",
            version=Version("1"),
            dependencies=(),
            provided_extras=frozenset(),
            requires_python=None,
        ),
    )
    cache.flush()
    assert (tmp_path / NAME).is_file()


def test_candidate_metadata_cache_validates_entries_lazily(tmp_path: Path) -> None:
    """A stored entry of the wrong shape is a miss when it is read, not an
    error when the snapshot is loaded."""
    import marshal

    from kpip.core.utils import save_snapshot
    from kpip.index.candidate_metadata_cache import FORMAT

    valid_key = ("https://example.test/valid.whl", "1", (), "sha256:valid")
    invalid_key = ("https://example.test/invalid.whl", "1", (), "sha256:bad")
    entries = {
        valid_key: ("valid", "1", (), (), None),
        invalid_key: ("invalid", "1", (42,), (), None),
    }
    save_snapshot(
        tmp_path / NAME,
        (FORMAT, {key: marshal.dumps(value) for key, value in entries.items()}),
    )

    cache = CandidateMetadataCache(tmp_path)

    assert cache.contains(valid_key)
    assert not cache.contains(invalid_key)
    assert invalid_key not in cache.entries
    cache.flush()
    assert not cache.contains(invalid_key)


def metadata(name: str) -> CandidateMetadata:
    return CandidateMetadata(
        name=name,
        version=Version("1"),
        dependencies=(parse_requirement("requests>=2"),),
        provided_extras=frozenset(),
        requires_python=None,
    )


def test_concurrent_runs_keep_each_others_entries(tmp_path: Path) -> None:
    first = CandidateMetadataCache(tmp_path)
    second = CandidateMetadataCache(tmp_path)
    one = ("https://example.test/one.whl", "1", (), "sha256:one")
    two = ("https://example.test/two.whl", "1", (), "sha256:two")

    first.put(one, metadata("one"))
    second.put(two, metadata("two"))
    first.flush()
    second.flush()

    later = CandidateMetadataCache(tmp_path)
    assert later.get(one) is not None
    assert later.get(two) is not None


def test_a_flush_keeps_the_newest_entries_up_to_the_cap(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from kpip.index import candidate_metadata_cache

    monkeypatch.setattr(candidate_metadata_cache, "MAX_ENTRIES", 2)
    cache = CandidateMetadataCache(tmp_path)
    keys = [(f"https://example.test/{n}.whl", "1", (), f"sha256:{n}") for n in range(3)]
    for key in keys:
        cache.put(key, metadata("demo"))
    cache.flush()

    later = CandidateMetadataCache(tmp_path)
    assert [later.contains(key) for key in keys] == [False, True, True]


def test_an_unreadable_snapshot_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / NAME).write_bytes(b"not marshal")
    key = ("https://example.test/x.whl", "1", (), "sha256:x")

    cache = CandidateMetadataCache(tmp_path)
    assert not cache.contains(key)
    cache.put(key, metadata("x"))
    cache.flush()
    assert CandidateMetadataCache(tmp_path).get(key) is not None
