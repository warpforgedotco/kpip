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
import stat
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
    # The per-file walk, unless a test asks for the compiled loops.
    monkeypatch.setattr(clone, "_link_tree", False)
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


@pytest.mark.parametrize("winner_first", [False, True])
def test_clone_skips_and_replaces_a_shared_file_in_either_order(
    tmp_path: Path, winner_first: bool
) -> None:
    """Two wheel trees shipping one file merge into one stage: the one that
    replaces it leaves its copy whichever clones first, and the other's is
    never written over it."""
    from kpip.host.clone import clone_path

    trees = {}
    for name in ("earlier", "later"):
        tree = tmp_path / name
        (tree / "pkg").mkdir(parents=True)
        (tree / "shared.py").write_text(name)
        (tree / "pkg" / f"{name}.py").write_text(name)
        trees[name] = tree
    stage = tmp_path / "stage"
    stage.mkdir()
    shared = frozenset({str(stage / "shared.py")})

    order = ("later", "earlier") if winner_first else ("earlier", "later")
    for name in order:
        if name == "later":
            clone_path(str(trees[name]), str(stage), replace=shared)
        else:
            clone_path(str(trees[name]), str(stage), skip=shared)

    assert (stage / "shared.py").read_text() == "later"
    assert sorted(path.name for path in (stage / "pkg").iterdir()) == [
        "earlier.py",
        "later.py",
    ]
    assert (trees["earlier"] / "shared.py").read_text() == "earlier"


def test_clone_still_rejects_a_duplicate_it_was_not_told_about(tmp_path: Path) -> None:
    from kpip.host.clone import clone_path

    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "shared.py").write_text(name)
    stage = tmp_path / "stage"
    stage.mkdir()

    clone_path(str(tmp_path / "a"), str(stage))
    with pytest.raises(FileExistsError):
        clone_path(str(tmp_path / "b"), str(stage))


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_a_cloned_read_only_directory_keeps_its_mode_and_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only directory is created writable so its entries can be
    linked in, then given its source mode."""
    from kpip.host.clone import clone_path

    monkeypatch.setenv("KPIP_LINK_MODE", "copy")
    source = tmp_path / "source"
    (source / "locked").mkdir(parents=True)
    (source / "locked" / "module.py").write_text("x = 1\n")
    (source / "locked").chmod(0o555)
    destination = tmp_path / "destination"
    try:
        clone_path(str(source), str(destination))

        assert (destination / "locked" / "module.py").read_text() == "x = 1\n"
        assert stat.S_IMODE((destination / "locked").stat().st_mode) == 0o555
    finally:
        (source / "locked").chmod(0o755)
        if (destination / "locked").exists():
            (destination / "locked").chmod(0o755)


def compiled_link_tree() -> types.ModuleType:
    try:
        from kpip.host import _link_tree
    except ImportError:
        pytest.skip("kpip.host._link_tree is not built")
    return _link_tree


class FailingLinkTree:
    """The compiled loops' contract in Python, failing chosen files once."""

    def __init__(self, failures: dict[int, int]) -> None:
        self.failures = dict(failures)
        self.calls: list[int] = []

    @staticmethod
    def make_directories(root: bytes, names: list, modes: list) -> tuple:
        for name, mode in zip(names, modes):
            os.mkdir(os.path.join(root, name), mode)
        return (0, len(names))

    @staticmethod
    def change_modes(root: bytes, names: list, modes: list) -> tuple:
        for name, mode in zip(names, modes):
            os.chmod(os.path.join(root, name), mode)
        return (0, len(names))

    def link_files(
        self, source: bytes, destination: bytes, names: list, start: int
    ) -> tuple:
        self.calls.append(start)
        for index in range(start, len(names)):
            if index in self.failures:
                failure = self.failures.pop(index)
                if failure == errno.EEXIST:  # what a racing writer leaves
                    Path(
                        os.fsdecode(os.path.join(destination, names[index]))
                    ).write_text("")
                return (failure, index)
            os.link(
                os.path.join(source, names[index]),
                os.path.join(destination, names[index]),
            )
        return (0, len(names))


@pytest.fixture
def whole_trees(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the one-pass route: pretend this device pair never reflinks."""
    monkeypatch.setattr(
        clone,
        "_links_whole_trees",
        lambda devices: devices not in clone._hardlink_unsupported,
    )


def link_tree_through(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    monkeypatch.setattr(clone, "_link_tree", module)


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_the_compiled_loops_link_the_same_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None
) -> None:
    link_tree_through(monkeypatch, compiled_link_tree())
    source = make_tree(tmp_path)
    (source / "pkg" / "sub").chmod(0o750)
    (source / "pkg" / "empty").mkdir()
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    for relative in FILES:
        assert same_inode(source, destination, relative)
    assert (destination / "pkg" / "tool").stat().st_mode & 0o777 == 0o755
    assert stat.S_IMODE((destination / "pkg" / "sub").stat().st_mode) == 0o750
    assert (destination / "pkg" / "empty").is_dir()
    assert os.readlink(destination / "pkg" / "sub" / "alias") == "mod.py"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_the_compiled_loops_keep_a_read_only_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None
) -> None:
    link_tree_through(monkeypatch, compiled_link_tree())
    source = tmp_path / "source"
    (source / "locked" / "inner").mkdir(parents=True)
    (source / "locked" / "inner" / "module.py").write_text("x = 1\n")
    (source / "locked" / "inner").chmod(0o555)
    (source / "locked").chmod(0o555)
    destination = tmp_path / "destination"
    try:
        clone.clone_path(str(source), str(destination))

        assert (destination / "locked" / "inner" / "module.py").read_text() == "x = 1\n"
        assert stat.S_IMODE((destination / "locked").stat().st_mode) == 0o555
        assert stat.S_IMODE((destination / "locked" / "inner").stat().st_mode) == 0o555
    finally:
        for root in (source, destination):
            for path in (root / "locked", root / "locked" / "inner"):
                if path.exists():
                    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_the_compiled_loops_reject_a_duplicate_and_leave_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None
) -> None:
    """A clash inside a new directory -- another writer racing it -- fails
    as the per-file walk fails, and the half-made directory is removed."""
    link_tree_through(monkeypatch, FailingLinkTree({1: errno.EEXIST}))
    source = make_tree(tmp_path)
    destination = tmp_path / "target"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        clone.clone_path(str(source / "pkg"), str(destination / "pkg"))

    assert not (destination / "pkg").exists()


def test_a_refused_link_copies_the_rest_without_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None
) -> None:
    fake = FailingLinkTree({0: errno.EXDEV})
    link_tree_through(monkeypatch, fake)
    link = os.link

    def refuse(source: str, destination: str, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(clone.os, "link", refuse)
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    monkeypatch.setattr(clone.os, "link", link)
    # The first file's verdict holds for the pair: the loop is not asked again.
    assert fake.calls == [0]
    assert len(clone._hardlink_unsupported) == 1
    for relative in FILES:
        assert not same_inode(source, destination, relative)
        assert (destination / relative).read_bytes() == (source / relative).read_bytes()


def test_a_link_count_limit_copies_one_file_and_resumes_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None
) -> None:
    fake = FailingLinkTree({1: errno.EMLINK})
    link_tree_through(monkeypatch, fake)
    link = os.link

    def at_limit(source: str, destination: str, **kwargs: object) -> None:
        raise OSError(errno.EMLINK, "too many links")

    source = make_tree(tmp_path)
    destination = tmp_path / "target"
    monkeypatch.setattr(clone.os, "link", at_limit)
    fake_link = FailingLinkTree.link_files

    def link_files(self, *args):  # the loop links with the real call
        monkeypatch.setattr(clone.os, "link", link)
        try:
            return fake_link(self, *args)
        finally:
            monkeypatch.setattr(clone.os, "link", at_limit)

    monkeypatch.setattr(FailingLinkTree, "link_files", link_files)

    clone.clone_path(str(source), str(destination))

    assert fake.calls == [0, 2]
    assert not clone._hardlink_unsupported
    files = clone._list_tree(os.fsencode(source))[2]
    copied = os.fsdecode(files[1])
    for relative in FILES:
        assert same_inode(source, destination, relative) == (relative != copied)


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
@pytest.mark.parametrize("loops", ["python", "compiled"])
def test_a_large_tree_is_linked_in_slices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None, loops: str
) -> None:
    link_tree_through(
        monkeypatch,
        clone._PythonLinkTree if loops == "python" else compiled_link_tree(),
    )
    monkeypatch.setattr(clone, "_SPLIT_FILES", 2)
    monkeypatch.setattr(clone, "_SPLIT_WORKERS", 2)
    split: list[int] = []
    link_files = clone._link_files

    def counted(link_tree, source, destination, files, devices):
        split.append(len(files))
        link_files(link_tree, source, destination, files, devices)

    monkeypatch.setattr(clone, "_link_files", counted)
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    assert sorted(split) == [1, 2]
    for relative in FILES:
        assert same_inode(source, destination, relative)
    assert os.readlink(destination / "pkg" / "sub" / "alias") == "mod.py"


@pytest.mark.skipif(os.name == "nt", reason="POSIX only")
def test_a_failed_slice_fails_the_clone_once_every_slice_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_trees: None
) -> None:
    link_tree_through(monkeypatch, FailingLinkTree({0: errno.EEXIST}))
    monkeypatch.setattr(clone, "_SPLIT_FILES", 2)
    monkeypatch.setattr(clone, "_SPLIT_WORKERS", 2)
    source = make_tree(tmp_path)
    destination = tmp_path / "target"

    with pytest.raises(FileExistsError):
        clone.clone_path(str(source), str(destination))

    assert not destination.exists()


def test_the_whole_tree_route_is_posix_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clone.os, "name", "nt")

    assert not clone._links_whole_trees((1, 1))
