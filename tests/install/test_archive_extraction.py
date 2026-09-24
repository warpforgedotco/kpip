"""Extraction into the archive cache: layout, ordering and concurrency.

The threaded path exists only to make large wheels faster, so it has to be
indistinguishable from the serial one -- same entries, same order, same
bytes on disk. These tests drive both and compare.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest
from kpip.core.utils import default_worker_count
from kpip.core.wheel import wheel_candidate
from kpip.install import wheel_archive_cache as cache_module
from kpip.install.wheel_archive_cache import prepare_cached_wheels


def _wheel_with(directory: Path, name: str, members: dict[str, str]) -> Path:
    wheel = directory / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in members.items():
            archive.writestr(path, text)
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


def _candidate(wheel: Path) -> object:
    return wheel_candidate(wheel).copy_with(
        source_hashes={"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        source_kind="wheel",
    )


def _many_member_wheel(directory: Path, name: str, count: int) -> Path:
    members = {
        f"{name}/deep/nest/mod_{index:04d}.py": f"VALUE = {index}\n"
        for index in range(count)
    }
    members[f"{name}/__init__.py"] = "\n"
    return _wheel_with(directory, name, members)


@pytest.mark.parametrize("threshold", [1, 10**9], ids=["threaded", "serial"])
def test_extraction_is_identical_threaded_and_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    threshold: int,
) -> None:
    monkeypatch.setattr(cache_module, "PARALLEL_EXTRACT_MEMBERS", threshold)

    wheel = _many_member_wheel(tmp_path, "wide", 120)
    cache_dir = tmp_path / f"cache-{threshold}"

    (archive,) = prepare_cached_wheels((_candidate(wheel),), str(cache_dir))

    expected = [
        member.filename
        for member in zipfile.ZipFile(wheel).infolist()
        if not member.is_dir()
    ]

    assert [entry[0] for entry in archive.entries] == expected, (
        "entries must follow the wheel's own member order, not completion order"
    )

    for relative, digest, size, _ in archive.entries:
        body = Path(archive.tree, *relative.split("/")).read_bytes()
        assert size == str(len(body))
        assert digest.startswith("sha256=")


def test_threaded_and_serial_extraction_agree_entry_for_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _many_member_wheel(tmp_path, "agree", 100)

    monkeypatch.setattr(cache_module, "PARALLEL_EXTRACT_MEMBERS", 10**9)
    (serial,) = prepare_cached_wheels((_candidate(wheel),), str(tmp_path / "s"))

    monkeypatch.setattr(cache_module, "PARALLEL_EXTRACT_MEMBERS", 1)
    (threaded,) = prepare_cached_wheels((_candidate(wheel),), str(tmp_path / "t"))

    assert serial.entries == threaded.entries
    assert serial.dist_info == threaded.dist_info


def test_nested_directories_are_created_once_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The layout pass memoizes directories; deep trees must still be created."""
    made: list[str] = []

    real = os.makedirs

    def counting(path: str, *args: object, **kwargs: object) -> None:
        made.append(path)
        real(path, *args, **kwargs)

    wheel = _wheel_with(
        tmp_path,
        "deeppkg",
        {
            "deeppkg/a/b/c/one.py": "1\n",
            "deeppkg/a/b/c/two.py": "2\n",
            "deeppkg/a/b/c/three.py": "3\n",
            "deeppkg/a/other.py": "4\n",
        },
    )

    monkeypatch.setattr(cache_module.os, "makedirs", counting)
    (archive,) = prepare_cached_wheels((_candidate(wheel),), str(tmp_path / "cache"))

    for relative, _, _, _ in archive.entries:
        assert Path(archive.tree, *relative.split("/")).is_file()

    # pyc/ mirrors tree/, so each is created once -- count only the tree side.
    suffix = os.path.join("tree", "deeppkg", "a", "b", "c")
    deepest = [path for path in made if path.endswith(suffix)]
    assert len(deepest) == 1, f"created the same directory {len(deepest)} times"


def test_extract_permits_are_returned(tmp_path: Path) -> None:
    """A wheel that borrows extraction threads must give them back, or the
    next large wheel in the process silently extracts serially forever."""
    wheel = _many_member_wheel(tmp_path, "permits", 80)

    for index in range(3):
        prepare_cached_wheels((_candidate(wheel),), str(tmp_path / f"cache{index}"))

    taken = cache_module._borrow_extract_workers(cache_module.INSTALL_WORKERS - 1)
    try:
        assert taken == max(0, cache_module.INSTALL_WORKERS - 1)
    finally:
        cache_module._return_extract_workers(taken)


def test_default_worker_count_scales_and_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KPIP_CONCURRENCY", raising=False)
    assert default_worker_count() >= 1
    assert default_worker_count() <= 32

    monkeypatch.setenv("KPIP_CONCURRENCY", "3")
    assert default_worker_count() == 3

    for bad in ("0", "-2", "many", ""):
        monkeypatch.setenv("KPIP_CONCURRENCY", bad)
        assert default_worker_count() >= 1, f"{bad!r} should be ignored, not fatal"


def test_byte_code_is_compiled_only_for_an_install_that_compiles(
    tmp_path: Path,
) -> None:
    """A --no-compile fill skips byte code; the first compiling install adds it."""
    wheel = _wheel_with(tmp_path, "demo", {"demo/__init__.py": "VALUE = 1\n"})
    cache = tmp_path / "cache"

    (archive,) = prepare_cached_wheels(
        (_candidate(wheel),), str(cache), pycompile=False
    )
    pyc = Path(cache_module.pyc_root(os.path.dirname(archive.tree)))
    assert not pyc.exists()

    (again,) = prepare_cached_wheels((_candidate(wheel),), str(cache), pycompile=True)
    assert again.tree == archive.tree
    assert list(pyc.rglob("*.pyc"))


def test_extracted_files_take_the_umask_and_keep_executable_bits(
    tmp_path: Path,
) -> None:
    """As pip and uv install them: 0o666 or 0o777, under the umask."""
    wheel = tmp_path / "modes-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, mode in (("modes/plain.py", 0o664), ("modes/tool.sh", 0o755)):
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, "x\n")
        archive.writestr(
            "modes-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: modes\nVersion: 1.0\n",
        )
        archive.writestr(
            "modes-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("modes-1.0.dist-info/RECORD", "")
    umask = os.umask(0)
    os.umask(umask)

    (archive,) = prepare_cached_wheels(
        (_candidate(wheel),), str(tmp_path / "cache"), pycompile=False
    )
    tree = Path(archive.tree)

    assert (tree / "modes/plain.py").stat().st_mode & 0o777 == 0o666 & ~umask
    assert (tree / "modes/tool.sh").stat().st_mode & 0o777 == 0o777 & ~umask
