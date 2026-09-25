"""A cache snapshot serves an entry only while its file is the one it saw."""

from __future__ import annotations

import builtins
import os
from pathlib import Path
from typing import Any

import pytest
from kpip.network.cache import SafeFileCache

SNAPSHOT = "entries.snapshot"


def snapshotting(directory: Path) -> SafeFileCache:
    cache = SafeFileCache(os.fspath(directory))
    cache.use_snapshot(SNAPSHOT, "kept:")
    return cache


def saved(directory: Path, entries: dict[str, bytes]) -> None:
    writer = SafeFileCache(os.fspath(directory))
    for key, value in entries.items():
        writer.set_atomic(key, value)
    reader = snapshotting(directory)
    for key in entries:
        reader.get_atomic(key)
    reader.save_snapshot()


def no_opens(monkeypatch: Any) -> list[str]:
    opened: list[str] = []
    real_open = builtins.open

    def recording_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    return opened


def test_an_unchanged_entry_is_served_without_opening_its_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    saved(tmp_path, {"kept:a": b"alpha", "kept:b": b"beta"})
    cache = snapshotting(tmp_path)
    opened = no_opens(monkeypatch)

    assert cache.get_atomic("kept:a") == b"alpha"
    assert cache.get_atomic("kept:b") == b"beta"
    assert opened == []


def test_an_entry_replaced_by_another_process_is_read_afresh(tmp_path: Path) -> None:
    saved(tmp_path, {"kept:a": b"alpha"})
    SafeFileCache(os.fspath(tmp_path)).set_atomic("kept:a", b"omega")

    assert snapshotting(tmp_path).get_atomic("kept:a") == b"omega"


def test_an_entry_patched_in_place_is_read_afresh(tmp_path: Path) -> None:
    saved(tmp_path, {"kept:a": b"alpha"})
    path = SafeFileCache(os.fspath(tmp_path)).get_cache_path("kept:a") + ".atomic"
    status = os.stat(path)
    with open(path, "r+b") as file:
        file.write(b"ALPHA")
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1))

    assert snapshotting(tmp_path).get_atomic("kept:a") == b"ALPHA"


def test_a_removed_entry_is_a_miss(tmp_path: Path) -> None:
    saved(tmp_path, {"kept:a": b"alpha"})
    SafeFileCache(os.fspath(tmp_path)).delete("kept:a")

    assert snapshotting(tmp_path).get_atomic("kept:a") is None


def test_an_entry_this_process_changes_is_left_out_of_the_snapshot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    saved(tmp_path, {"kept:a": b"alpha", "kept:b": b"beta"})
    cache = snapshotting(tmp_path)
    cache.get_atomic("kept:a")
    cache.get_atomic("kept:b")
    cache.patch_atomic("kept:a", b"al", 2, b"P")
    cache.save_snapshot()

    later = snapshotting(tmp_path)
    opened = no_opens(monkeypatch)
    assert later.get_atomic("kept:a") == b"alPha"
    assert later.get_atomic("kept:b") == b"beta"
    assert len(opened) == 1


def test_other_entries_bypass_the_snapshot(tmp_path: Path) -> None:
    SafeFileCache(os.fspath(tmp_path)).set_atomic("other:a", b"alpha")
    cache = snapshotting(tmp_path)

    assert cache.get_atomic("other:a") == b"alpha"
    cache.save_snapshot()
    assert not (tmp_path / SNAPSHOT).exists()


def test_a_repeated_read_writes_nothing(tmp_path: Path) -> None:
    saved(tmp_path, {"kept:a": b"alpha"})
    before = os.stat(tmp_path / SNAPSHOT)
    cache = snapshotting(tmp_path)
    cache.get_atomic("kept:a")
    cache.save_snapshot()

    after = os.stat(tmp_path / SNAPSHOT)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_the_snapshot_keeps_only_what_the_last_save_read(tmp_path: Path) -> None:
    saved(tmp_path, {"kept:a": b"alpha", "kept:b": b"beta"})
    cache = snapshotting(tmp_path)
    cache.get_atomic("kept:a")
    cache.save_snapshot()

    later = snapshotting(tmp_path)
    assert set(later._snapshot) == {"kept:a"}


@pytest.mark.parametrize("content", [b"", b"not marshal", b"\xe9\x00\x00\x00"])
def test_an_unreadable_snapshot_is_ignored(tmp_path: Path, content: bytes) -> None:
    SafeFileCache(os.fspath(tmp_path)).set_atomic("kept:a", b"alpha")
    (tmp_path / SNAPSHOT).write_bytes(content)

    assert snapshotting(tmp_path).get_atomic("kept:a") == b"alpha"
