"""Per-file fallbacks when copy-on-write cloning is unavailable.

Linux has no directory-level clone: ``clone_path`` walks the tree and, for
every regular file, tries ``FICLONE``, then a hard link (the default there,
as in uv), then a copy.  ext4 rejects ``FICLONE``, so on the most common
Linux filesystem those fallbacks decide what a warm install costs.  These
tests force each path with the platform calls stubbed, so they run
everywhere.
"""

from __future__ import annotations

import errno
import os
import sys
import types
from pathlib import Path

import pytest
from kpip.host import clone

FILES = ("pkg/__init__.py", "pkg/sub/mod.py", "pkg/tool")


@pytest.fixture(autouse=True)
def fresh_link_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clone, "_reflink_unsupported", set())
    monkeypatch.setattr(clone, "_hardlink_unsupported", set())
    monkeypatch.setattr(clone, "_reflink_slow", set())
    monkeypatch.setattr(clone, "_reflink_fast", set())
    monkeypatch.setattr(clone, "_reflink_probe", {})
    monkeypatch.setattr(clone, "_darwin_clone", lambda source, destination: False)
    monkeypatch.setattr(clone, "_link_mode", None)
    monkeypatch.setenv("KPIP_LINK_MODE", "hardlink")


@pytest.fixture
def no_reflink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clone, "_linux_reflink", lambda *args: False)


def make_tree(root: Path) -> Path:
    source = root / "tree"
    (source / "pkg" / "sub").mkdir(parents=True)
    (source / "pkg" / "__init__.py").write_bytes(b"top\n")
    (source / "pkg" / "sub" / "mod.py").write_bytes(b"nested\n")
    script = source / "pkg" / "tool"
    script.write_bytes(b"#!python\n")
    if os.name != "nt":
        script.chmod(0o755)
        os.symlink("mod.py", source / "pkg" / "sub" / "alias")
    return source


def same_inode(source: Path, destination: Path, relative: str) -> bool:
    return os.stat(source / relative).st_ino == os.stat(destination / relative).st_ino


def test_files_are_hard_linked_when_cloning_is_unavailable(
    tmp_path: Path, no_reflink: None
) -> None:
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    for relative in FILES:
        assert same_inode(source, destination, relative)
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()
    if os.name != "nt":
        assert (destination / "pkg" / "tool").stat().st_mode & 0o777 == 0o755
        assert os.readlink(destination / "pkg" / "sub" / "alias") == "mod.py"
    assert not clone._hardlink_unsupported


@pytest.mark.parametrize(
    "platform, expected",
    [("darwin", "clone"), ("linux", "hardlink"), ("win32", "hardlink")],
)
def test_the_default_link_mode_follows_uv(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: str
) -> None:
    monkeypatch.delenv("KPIP_LINK_MODE")
    monkeypatch.setattr(sys, "platform", platform)

    assert clone._configured_link_mode() == expected


def test_an_unknown_link_mode_falls_back_to_the_platform_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPIP_LINK_MODE", "symlink")
    monkeypatch.setattr(sys, "platform", "linux")

    assert clone._configured_link_mode() == "hardlink"


def test_clone_mode_copies_so_installed_files_stay_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_reflink: None
) -> None:
    monkeypatch.setenv("KPIP_LINK_MODE", "clone")
    monkeypatch.setattr(
        clone.os, "link", lambda source, destination: pytest.fail("linked")
    )
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    for relative in FILES:
        assert not same_inode(source, destination, relative)
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()
    (destination / FILES[0]).write_bytes(b"edited\n")
    assert (source / FILES[0]).read_bytes() == b"top\n"


def test_copy_mode_skips_the_reflink_and_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPIP_LINK_MODE", "copy")
    monkeypatch.setattr(clone, "_linux_reflink", lambda *args: pytest.fail("reflinked"))
    monkeypatch.setattr(
        clone.os, "link", lambda source, destination: pytest.fail("linked")
    )
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    for relative in FILES:
        assert not same_inode(source, destination, relative)


def test_a_filesystem_without_hard_links_is_judged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_reflink: None
) -> None:
    attempts: list[str] = []

    def refuse(source: str, destination: str) -> None:
        attempts.append(destination)
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(clone.os, "link", refuse)
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    assert len(attempts) == 1
    assert len(clone._hardlink_unsupported) == 1
    for relative in FILES:
        assert not same_inode(source, destination, relative)
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()


def test_a_link_count_limit_copies_the_file_without_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_reflink: None
) -> None:
    attempts: list[str] = []

    def refuse(source: str, destination: str) -> None:
        attempts.append(destination)
        raise OSError(errno.EMLINK, "too many links")

    monkeypatch.setattr(clone.os, "link", refuse)
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    # Every file was still offered a link: EMLINK says nothing about the next one.
    assert len(attempts) == len(FILES)
    assert not clone._hardlink_unsupported
    for relative in FILES:
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()


def test_a_filesystem_without_ficlone_is_judged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ioctls: list[int] = []

    def ioctl(descriptor: int, request: int, argument: int) -> None:
        ioctls.append(request)
        raise OSError(errno.EOPNOTSUPP, "operation not supported")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(ioctl=ioctl))
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    # ext4 answers EOPNOTSUPP once per device, not once per directory.
    assert ioctls == [clone._FICLONE]
    assert len(clone._reflink_unsupported) == 1
    for relative in FILES:
        assert same_inode(source, destination, relative)


def test_a_second_clone_onto_a_judged_device_never_touches_the_ioctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ioctl(descriptor: int, request: int, argument: int) -> None:
        raise AssertionError("FICLONE retried on a device that rejected it")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(ioctl=ioctl))
    clone._reflink_unsupported.add(os.stat(tmp_path).st_dev)
    source = make_tree(tmp_path)

    clone.clone_path(str(source), str(tmp_path / "target"))

    assert same_inode(source, tmp_path / "target", FILES[0])


def test_replace_contents_breaks_the_link_and_keeps_the_mode(tmp_path: Path) -> None:
    cached = tmp_path / "cached"
    cached.write_bytes(b"before\n")
    if os.name != "nt":
        cached.chmod(0o755)
    installed = tmp_path / "installed"
    os.link(cached, installed)

    clone.replace_contents(str(installed), b"after\n")

    assert installed.read_bytes() == b"after\n"
    assert cached.read_bytes() == b"before\n"
    assert installed.stat().st_nlink == 1
    if os.name != "nt":
        assert installed.stat().st_mode & 0o777 == 0o755
