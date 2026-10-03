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
from kpip.install import bytecode
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
    entry_root = Path(os.path.dirname(archive.tree))
    assert not list(entry_root.glob(f"{cache_module.PYC_CACHE_PREFIX}*"))

    (again,) = prepare_cached_wheels((_candidate(wheel),), str(cache), pycompile=True)
    assert again.tree == archive.tree
    assert list(entry_root.glob(f"{cache_module.PYC_CACHE_PREFIX}*/**/*.pyc"))


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


def _lean_work(tmp_path: Path, wheel: Path, name: str) -> tuple:
    with zipfile.ZipFile(wheel) as archive:
        member = archive.getinfo(name)
    return (member, name, str(tmp_path / "out.bin"), None)


@pytest.mark.skipif(not cache_module._HAS_PREAD, reason="no pread")
@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_lean_extraction_writes_what_zipfile_reads(tmp_path: Path, method: int) -> None:
    """A member read with one pread and one zlib call is zipfile's bytes,
    with the RECORD hash and size of them."""
    wheel = tmp_path / "lean-1.0-py3-none-any.whl"
    payload = b"x = 1\n" * 5000
    with zipfile.ZipFile(wheel, "w", compression=method) as archive:
        archive.writestr("lean/mod.py", payload)

    fd = os.open(wheel, os.O_RDONLY)
    try:
        entry = cache_module._extract_member_lean(
            fd, _lean_work(tmp_path, wheel, "lean/mod.py")
        )
    finally:
        os.close(fd)

    assert entry is not None
    assert (tmp_path / "out.bin").read_bytes() == payload
    import base64

    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    assert entry[1:3] == (f"sha256={digest.decode()}", str(len(payload)))


@pytest.mark.skipif(not cache_module._HAS_PREAD, reason="no pread")
def test_lean_extraction_refuses_a_corrupt_member(tmp_path: Path) -> None:
    wheel = tmp_path / "bad-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("bad/mod.py", b"original contents")
    wheel.write_bytes(wheel.read_bytes().replace(b"original", b"tampered"))

    fd = os.open(wheel, os.O_RDONLY)
    try:
        with pytest.raises(zipfile.BadZipFile, match="CRC"):
            cache_module._extract_member_lean(
                fd, _lean_work(tmp_path, wheel, "bad/mod.py")
            )
    finally:
        os.close(fd)


@pytest.mark.skipif(not cache_module._HAS_PREAD, reason="no pread")
def test_lean_extraction_refuses_a_local_name_the_directory_does_not_list(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "odd-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("odd/aaa.py", b"x")
    raw = wheel.read_bytes()
    # The local header's copy of the name only: the central directory's
    # copy comes after it.
    local = raw.index(b"odd/aaa.py")
    wheel.write_bytes(raw[:local] + b"odd/zzz.py" + raw[local + 10 :])

    fd = os.open(wheel, os.O_RDONLY)
    try:
        with pytest.raises(zipfile.BadZipFile, match="differ"):
            cache_module._extract_member_lean(
                fd, _lean_work(tmp_path, wheel, "odd/aaa.py")
            )
    finally:
        os.close(fd)


@pytest.mark.skipif(not cache_module._HAS_PREAD, reason="no pread")
def test_lean_extraction_leaves_other_compression_to_zipfile(tmp_path: Path) -> None:
    wheel = tmp_path / "bz-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("bz/mod.py", b"x = 1\n")

    fd = os.open(wheel, os.O_RDONLY)
    try:
        assert (
            cache_module._extract_member_lean(
                fd, _lean_work(tmp_path, wheel, "bz/mod.py")
            )
            is None
        )
    finally:
        os.close(fd)


def test_an_incomplete_bytecode_tree_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modules nobody took would stay missing for every later install."""
    wheel = _wheel_with(tmp_path, "demo", {"demo/__init__.py": "VALUE = 1\n"})
    (archive,) = prepare_cached_wheels(
        (_candidate(wheel),), str(tmp_path / "cache"), pycompile=False
    )
    monkeypatch.setattr(bytecode, "compile_modules", lambda jobs: jobs)

    assert cache_module.bytecode_tree(archive) is None
    entry_root = Path(os.path.dirname(archive.tree))
    assert not list(entry_root.glob(f"{cache_module.PYC_CACHE_PREFIX}*"))


def test_extraction_stores_the_listing_a_walk_of_its_tree_finds(
    tmp_path: Path,
) -> None:
    """A cold install read each new tree's listing by walking it; extraction
    already knows every directory and file it wrote, and stores the listing
    beside the tree for the install to read instead."""
    from kpip.host.clone import tree_listing

    wheel = _wheel_with(
        tmp_path,
        "listed",
        {
            "listed/__init__.py": "\n",
            "listed/a/b/c/deep.py": "1\n",
            "listed/a/side.py": "2\n",
            "listed/z/last.py": "3\n",
            "other/thing.py": "4\n",
            "toplevel.py": "5\n",
        },
    )

    (archive,) = prepare_cached_wheels((_candidate(wheel),), str(tmp_path / "cache"))

    stored = cache_module._read_listings(
        os.path.join(os.path.dirname(archive.tree), cache_module.LISTING_NAME)
    )

    assert stored is not None
    tops = sorted(entry.name for entry in os.scandir(archive.tree) if entry.is_dir())
    assert sorted(stored) == tops
    for top in tops:
        directories, modes, files, symlinks = stored[top]
        walked = tree_listing(os.path.join(archive.tree, top))
        assert dict(zip(directories, modes)) == dict(zip(walked[0], walked[1]))
        assert sorted(files) == sorted(walked[2])
        assert symlinks == walked[3] == []
        for index, directory in enumerate(directories):
            parent = os.path.dirname(directory)
            assert not parent or parent in directories[:index]
