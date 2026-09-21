"""``SafeFileCache.write_to_file`` keeps its guarantees on the shorter path.

A resolve stores thousands of entries from worker threads, and every syscall
a write makes is a release and re-acquire of the interpreter lock behind the
resolver. The write path was cut from a makedirs walk, a temporary file, a
stat and a chmod per entry to one directory creation per directory, one
exclusive open, one permission set and one rename. What must not change:
entries take the cache directory's mode, a failed write leaves nothing
behind, and concurrent writers never see each other's temporaries.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
from kpip.network.cache import SafeFileCache


def test_entries_take_the_cache_directory_mode(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX modes")
    directory = tmp_path / "cache"
    directory.mkdir()
    directory.chmod(0o755)
    cache = SafeFileCache(os.fspath(directory))

    cache.set_with_body("key", b"value", b"body")
    cache.set_atomic("other", b"value")

    assert cache.get("key") == b"value"
    assert cache.get_atomic("other") == b"value"
    for key, suffix in (("key", ""), ("other", ".atomic")):
        mode = os.stat(cache.get_cache_path(key) + suffix).st_mode & 0o777
        assert mode == 0o644


def test_a_failed_write_leaves_no_temporary_behind(tmp_path: Path) -> None:
    cache = SafeFileCache(os.fspath(tmp_path))
    path = cache.get_cache_path("key")

    def explode(_file: object) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        cache.write_to_file(path, explode)

    assert not os.path.exists(path)
    assert [name for name in os.listdir(os.path.dirname(path))] == []


def test_the_combined_entry_is_one_write(tmp_path: Path) -> None:
    cache = SafeFileCache(os.fspath(tmp_path))
    writes: list[int] = []
    real = cache.write_to_file

    def counting(path: str, writer: object) -> None:
        def wrapped(file: object) -> None:
            original = file.write  # type: ignore[attr-defined]

            def write(data: bytes) -> int:
                writes.append(len(data))
                return original(data)

            file.write = write  # type: ignore[attr-defined]
            writer(file)  # type: ignore[operator]

        real(path, wrapped)

    cache.write_to_file = counting  # type: ignore[method-assign]
    cache.set_with_body("key", b"meta", b"body")

    assert len(writes) == 1
    assert cache.get("key") == b"meta"
    assert cache.get_body("key").read() == b"body"  # type: ignore[union-attr]


def test_concurrent_writers_each_land_their_own_entry(tmp_path: Path) -> None:
    cache = SafeFileCache(os.fspath(tmp_path))
    start = threading.Event()

    def writer(index: int) -> None:
        start.wait(5)
        for round_ in range(20):
            cache.set_atomic(f"key-{index}", f"{index}:{round_}".encode())

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(30)

    for index in range(8):
        assert cache.get_atomic(f"key-{index}") == f"{index}:19".encode()
    leftovers = [
        name
        for _root, _dirs, files in os.walk(tmp_path)
        for name in files
        if name.endswith(".tmp")
    ]
    assert leftovers == []
