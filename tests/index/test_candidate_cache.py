from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from kpip.core.hashes import file_hashes
from kpip.core.versions import Version
from kpip.index.cache import wheel_cache_path
from kpip.index.candidate_cache import (
    built_wheel_cache_key,
    cache_built_wheel,
    cached_wheel_for_link,
)
from kpip.index.links import Link
from kpip.index.source_models import CandidateRecord

from ..wheel_helpers import make_wheel


def source_candidate(source: Path) -> CandidateRecord:
    return CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_path(os.fspath(source), source_url=None),
    )


def cache_key(
    candidate: CandidateRecord,
    source_hashes: dict[str, str],
    *,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
    target_key: str | None = "host-target",
) -> str:
    result = built_wheel_cache_key(
        candidate,
        source_hashes=source_hashes,
        config_settings=config_settings,
        build_constraints=build_constraints,
        build_isolation=build_isolation,
        target_key=target_key,
    )
    assert result is not None
    return result


def test_built_wheel_cache_publishes_and_loads_complete_entry(tmp_path: Path) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"source archive")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    wheel = make_wheel(tmp_path, "demo", "demo", "1.0")
    cache = tmp_path / "cache"

    cache_built_wheel(
        cache,
        candidate,
        os.fspath(wheel),
        key,
        source_hashes=hashes,
    )

    cached = cached_wheel_for_link(cache, candidate, key)
    assert cached is not None
    cached_path, cached_hashes, built = cached
    assert Path(cached_path).read_bytes() == wheel.read_bytes()
    assert cached_hashes == hashes
    assert built.name == "demo"
    assert built.version == Version("1.0")
    assert sorted(Path(wheel_cache_path(os.fspath(cache), key)).iterdir()) == [
        Path(wheel_cache_path(os.fspath(cache), key)) / "demo-1.0-py3-none-any.whl",
        Path(wheel_cache_path(os.fspath(cache), key)) / "origin.json",
    ]


def test_built_wheel_cache_uses_discovered_name_for_unnamed_direct_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "master.zip"
    source.write_bytes(b"source archive")
    candidate = CandidateRecord(
        name="master.zip",
        version=Version("0"),
        link=Link.from_path(os.fspath(source), source_url=None),
    )
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    wheel = make_wheel(tmp_path, "pip-test-package", "pip_test_package", "0.1.1")
    cache = tmp_path / "cache"

    cache_built_wheel(
        cache,
        candidate,
        os.fspath(wheel),
        key,
        source_hashes=hashes,
        candidate_name_is_authoritative=False,
    )

    cached = cached_wheel_for_link(
        cache,
        candidate,
        key,
        candidate_name_is_authoritative=False,
    )
    assert cached is not None
    _, _, built = cached
    assert built.name == "pip-test-package"
    assert built.version == Version("0.1.1")


def test_built_wheel_cache_rejects_unexpected_authoritative_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"source archive")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    wheel = make_wheel(tmp_path, "other", "other", "1.0")
    cache = tmp_path / "cache"

    cache_built_wheel(
        cache,
        candidate,
        os.fspath(wheel),
        key,
        source_hashes=hashes,
    )

    assert not Path(wheel_cache_path(os.fspath(cache), key)).exists()


def test_built_wheel_cache_key_covers_build_inputs(tmp_path: Path) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"first source")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    baseline = cache_key(candidate, hashes)

    constraint = tmp_path / "constraints.txt"
    constraint.write_text("setuptools==1\n", encoding="utf-8")
    constrained = cache_key(
        candidate,
        hashes,
        build_constraints=[os.fspath(constraint)],
    )
    constraint.write_text("setuptools==2\n", encoding="utf-8")

    assert cache_key(candidate, {"sha256": "different"}) != baseline
    assert cache_key(candidate, hashes, config_settings={"mode": "fast"}) != baseline
    assert constrained != baseline
    assert (
        cache_key(
            candidate,
            hashes,
            build_constraints=[os.fspath(constraint)],
        )
        != constrained
    )
    assert cache_key(candidate, hashes, target_key="other-target") != baseline
    assert cache_key(candidate, hashes, build_isolation=False) != baseline


@pytest.mark.parametrize("origin", [None, b"{"])
def test_incomplete_built_wheel_cache_entry_is_a_miss(
    tmp_path: Path,
    origin: bytes | None,
) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"source archive")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    entry = Path(wheel_cache_path(os.fspath(tmp_path / "cache"), key))
    entry.mkdir(parents=True)
    make_wheel(entry, "demo", "demo", "1.0")
    if origin is not None:
        entry.joinpath("origin.json").write_bytes(origin)

    assert cached_wheel_for_link(tmp_path / "cache", candidate, key) is None
    assert not entry.exists()


def test_corrupt_built_wheel_cache_entry_is_a_miss(tmp_path: Path) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"source archive")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    entry = Path(wheel_cache_path(os.fspath(tmp_path / "cache"), key))
    entry.mkdir(parents=True)
    entry.joinpath("demo-1.0-py3-none-any.whl").write_bytes(b"not a wheel")
    entry.joinpath("origin.json").write_text(
        json.dumps(
            {
                "archive_info": {"hashes": hashes},
                "cache_key_hash": hashlib.sha256(key.encode()).hexdigest(),
            },
        ),
        encoding="utf-8",
    )

    assert cached_wheel_for_link(tmp_path / "cache", candidate, key) is None
    assert not entry.exists()


def test_failed_built_wheel_cache_write_is_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"source archive")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    wheel = make_wheel(tmp_path, "demo", "demo", "1.0")
    cache = tmp_path / "cache"

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only cache")

    # Patched on ``shutil`` itself: the cache imports it where it stages a
    # built wheel rather than at module scope, so there is no name to reach
    # through here, and a resolve that stages nothing never loads it.
    monkeypatch.setattr("shutil.copy2", fail_copy)

    cache_built_wheel(
        cache,
        candidate,
        os.fspath(wheel),
        key,
        source_hashes=hashes,
    )

    assert not Path(wheel_cache_path(os.fspath(cache), key)).exists()


def test_invalid_built_wheel_is_not_cached(tmp_path: Path) -> None:
    source = tmp_path / "demo-1.0.tar.gz"
    source.write_bytes(b"source archive")
    candidate = source_candidate(source)
    hashes = file_hashes(source)
    key = cache_key(candidate, hashes)
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    wheel.write_bytes(b"not a wheel")
    cache = tmp_path / "cache"

    cache_built_wheel(
        cache,
        candidate,
        os.fspath(wheel),
        key,
        source_hashes=hashes,
    )

    assert not Path(wheel_cache_path(os.fspath(cache), key)).exists()
