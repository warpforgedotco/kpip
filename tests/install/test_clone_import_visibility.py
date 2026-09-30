"""A package installed into a directory already on ``sys.path`` must import.

CPython's ``FileFinder`` caches a directory's listing and re-reads it only
when that directory's mtime changes. Copy-on-write cloning does not
necessarily update an mtime -- it can carry the source's -- so an installer
built on it can leave a live interpreter unable to see what it just wrote.
uv hit exactly this and bumps site-packages explicitly afterwards.

kpip's clone route stages a whole tree and renames it over the target, which
is a different shape, so whether the hazard reaches it is a question about
this code rather than about cloning in general. These tests answer it by
importing across a real install rather than by reasoning about mtimes.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import zipfile
from pathlib import Path

import pytest
from kpip.core.wheel import wheel_candidate
from kpip.install.target import InstallTarget
from kpip.install.wheel_transaction import install_wheels_transactionally


def _wheel(directory: Path, name: str, module: str, body: str) -> Path:
    wheel = directory / f"{name.replace('-', '_')}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{module}/__init__.py", body)
        archive.writestr(
            f"{name}-1.0.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
        )
        archive.writestr(
            f"{name}-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{name}-1.0.dist-info/RECORD", "")
    return wheel


def _install(wheel: Path, name: str, target: Path, cache_dir: Path) -> None:
    """Install one wheel, asserting the clone route is what handled it."""
    from kpip.core.appdirs import ARCHIVE_CACHE_BUCKET

    candidate = wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )
    install_wheels_transactionally(
        [(wheel, True, None)],
        target=InstallTarget.from_options(name, target=str(target)),
        pycompile=True,
        lookup_existing=False,
        candidates=[candidate],
        cache_dir=str(cache_dir),
    )

    assert (cache_dir / ARCHIVE_CACHE_BUCKET).is_dir(), (
        "the clone route was not taken, so this proves nothing about cloning"
    )


@pytest.fixture
def _clean_import_state() -> object:
    """Undo everything these tests do to the interpreter they run in."""
    path = list(sys.path)
    modules = set(sys.modules)
    cache = dict(sys.path_importer_cache)
    yield
    sys.path[:] = path
    for name in set(sys.modules) - modules:
        del sys.modules[name]
    sys.path_importer_cache.clear()
    sys.path_importer_cache.update(cache)


@pytest.mark.usefixtures("_clean_import_state")
def test_second_install_is_importable_without_invalidating_caches(
    tmp_path: Path,
) -> None:
    """The case uv had to fix: import, install alongside, import again.

    The first import gives the target a live ``FileFinder`` holding a cached
    listing. If the install leaves the directory's mtime alone, the second
    import cannot see the package that is now sitting there.
    """
    target = tmp_path / "target"
    cache_dir = tmp_path / "cache"

    first = _wheel(tmp_path, "vis-one", "vis_one", "VALUE = 1\n")
    second = _wheel(tmp_path, "vis-two", "vis_two", "VALUE = 2\n")

    _install(first, "vis-one", target, cache_dir)

    sys.path.insert(0, str(target))

    assert importlib.import_module("vis_one").VALUE == 1

    assert str(target) in sys.path_importer_cache, (
        "the target has no live finder, so this test would prove nothing"
    )

    _install(second, "vis-two", target, cache_dir)

    # Deliberately no importlib.invalidate_caches() -- that is the point.
    assert importlib.import_module("vis_two").VALUE == 2


@pytest.mark.usefixtures("_clean_import_state")
def test_a_module_added_to_an_existing_package_is_importable(
    tmp_path: Path,
) -> None:
    """The same question one level down, where the clone merges into a
    directory that already exists rather than creating one."""
    target = tmp_path / "target"
    cache_dir = tmp_path / "cache"

    base = _wheel(tmp_path, "share-base", "shared_pkg", "VALUE = 1\n")

    extra = tmp_path / "share_extra-1.0-py3-none-any.whl"
    with zipfile.ZipFile(extra, "w") as archive:
        archive.writestr("shared_pkg/later.py", "LATER = 2\n")
        archive.writestr(
            "share-extra-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: share-extra\nVersion: 1.0\n",
        )
        archive.writestr(
            "share-extra-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("share-extra-1.0.dist-info/RECORD", "")

    _install(base, "share-base", target, cache_dir)

    sys.path.insert(0, str(target))

    assert importlib.import_module("shared_pkg").VALUE == 1

    _install(extra, "share-extra", target, cache_dir)

    assert importlib.import_module("shared_pkg.later").LATER == 2


def test_the_target_directory_mtime_advances_across_an_install(
    tmp_path: Path,
) -> None:
    """The mechanism behind the two tests above, asserted directly.

    Stated separately so that a regression says *why* imports broke rather
    than only that they did.
    """
    target = tmp_path / "target"
    cache_dir = tmp_path / "cache"

    first = _wheel(tmp_path, "stamp-one", "stamp_one", "VALUE = 1\n")
    second = _wheel(tmp_path, "stamp-two", "stamp_two", "VALUE = 2\n")

    _install(first, "stamp-one", target, cache_dir)

    before = target.stat().st_mtime_ns

    _install(second, "stamp-two", target, cache_dir)

    assert target.stat().st_mtime_ns != before, (
        "the target kept its old mtime, so a live FileFinder would not re-read it"
    )


@pytest.mark.usefixtures("_clean_import_state")
def test_the_visibility_tests_would_notice_a_stale_mtime(tmp_path: Path) -> None:
    """Proof that the two tests above can fail.

    They assert an import succeeds, which a passing run alone cannot
    distinguish from ``FileFinder`` never having cached anything. Putting the
    target's mtime back to what it was before the second install reproduces
    the state kpip would be in if cloning carried the source's timestamp --
    and the import must then fail.
    """
    import os

    target = tmp_path / "target"
    cache_dir = tmp_path / "cache"

    first = _wheel(tmp_path, "stale-one", "stale_one", "VALUE = 1\n")
    second = _wheel(tmp_path, "stale-two", "stale_two", "VALUE = 2\n")

    _install(first, "stale-one", target, cache_dir)

    sys.path.insert(0, str(target))

    assert importlib.import_module("stale_one").VALUE == 1

    stamp = target.stat().st_mtime_ns

    _install(second, "stale-two", target, cache_dir)

    os.utime(target, ns=(stamp, stamp))

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("stale_two")

    # And it comes back the moment the directory looks changed again.
    importlib.invalidate_caches()

    assert importlib.import_module("stale_two").VALUE == 2
