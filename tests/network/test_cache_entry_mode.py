"""A cache entry is created at the mode it should end up with.

``open`` masks the mode it is given, so an entry whose mode has a bit the
umask clears must be created narrow and widened afterwards.  When the umask
clears nothing the entry wants -- the usual case -- the chmod is pure cost,
and a resolve stores thousands of entries.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator

import pytest
from kpip.network import cache as cache_module
from kpip.network.cache import PRIVATE_MODE, SafeFileCache


@pytest.fixture(autouse=True)
def fresh_umask_reading() -> Iterator[None]:
    saved = cache_module._PROCESS_UMASK
    cache_module._PROCESS_UMASK = None
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)
        cache_module._PROCESS_UMASK = saved


def entry_mode_on_disk(directory: str, key: str) -> int:
    cache = SafeFileCache(directory)
    cache.set(key, b"payload")
    path = cache.get_cache_path(key)
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize(
    "directory_mode, expected_entry_mode, expects_chmod",
    [
        (0o700, 0o600, False),
        (0o755, 0o644, False),
        (0o777, 0o666, True),
    ],
)
def test_the_entry_ends_at_the_directory_mode(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    directory_mode: int,
    expected_entry_mode: int,
    expects_chmod: bool,
) -> None:
    directory = tmp_path / f"cache-{directory_mode:o}"
    directory.mkdir(mode=directory_mode)
    os.chmod(directory, directory_mode)

    chmods: list[int] = []
    real = cache_module.set_descriptor_permissions
    monkeypatch.setattr(
        cache_module,
        "set_descriptor_permissions",
        lambda descriptor, path, mode: chmods.append(mode)
        or real(descriptor, path, mode),
    )

    assert entry_mode_on_disk(os.fspath(directory), "demo") == expected_entry_mode
    assert bool(chmods) is expects_chmod


def test_the_usual_directory_costs_no_chmod(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0755 cache directory under a 0022 umask is the common shape."""
    directory = tmp_path / "cache"
    directory.mkdir()
    os.chmod(directory, 0o755)

    cache = SafeFileCache(os.fspath(directory))

    assert cache.entry_mode() == 0o644
    assert cache.creation_mode() == 0o644, "created at its final mode"

    calls: list[int] = []
    monkeypatch.setattr(
        cache_module,
        "set_descriptor_permissions",
        lambda descriptor, path, mode: calls.append(mode),
    )
    for index in range(5):
        cache.set(f"key-{index}", b"x")

    assert calls == []


def test_a_mode_the_umask_would_clear_falls_back_to_private(tmp_path) -> None:
    directory = tmp_path / "cache"
    directory.mkdir()
    os.chmod(directory, 0o777)

    cache = SafeFileCache(os.fspath(directory))

    assert cache.entry_mode() == 0o666
    assert cache.creation_mode() == PRIVATE_MODE, "widened after creation instead"


def test_the_umask_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading it is also how it is set, so worker threads must not race."""
    readings = 0
    real = cache_module._read_process_umask

    def counting() -> int:
        nonlocal readings
        readings += 1
        return real()

    monkeypatch.setattr(cache_module, "_read_process_umask", counting)

    first = cache_module.process_umask()
    for _ in range(20):
        cache_module.process_umask()

    assert readings == 1
    assert first == cache_module.process_umask()
