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
import importlib.util
import os
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import types
from pathlib import Path
from typing import Any

import pytest
from kpip.host import clone
from kpip.host._accel import _kpip_link_tree as python_link_tree

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
    # The per-file walk, unless a test asks for the tree loops.
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
    monkeypatch.setattr(clone, "fcntl", types.SimpleNamespace(ioctl=ioctl))
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
    monkeypatch.setattr(clone, "fcntl", types.SimpleNamespace(ioctl=ioctl))
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


class FailingLinkTree:
    """The link loops' contract, failing chosen files once."""

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


LINK_TREE_SOURCE = Path(python_link_tree.__file__).with_suffix(".c")


@pytest.fixture(scope="session")
def built_link_tree(tmp_path_factory: pytest.TempPathFactory) -> types.ModuleType:
    """``_kpip_link_tree.c`` built against the test interpreter.

    The binary has it compiled in as a built-in; built here as an extension,
    the same loops run under the suite. Without a compiler the C cases skip,
    except in CI, which must run them: MSVC's ``cl`` on Windows, ``cc``
    elsewhere.
    """
    directory = tmp_path_factory.mktemp("link-tree")
    output = directory / ("_kpip_link_tree" + sysconfig.get_config_var("EXT_SUFFIX"))
    include = sysconfig.get_path("include")
    if os.name == "nt":
        compiler = shutil.which("cl")
        command = [
            "/nologo",
            "/O2",
            "/W3",
            "/WX",
            "/LD",
            f"/I{include}",
            str(LINK_TREE_SOURCE),
            f"/Fo{directory}\\",
            f"/Fe{output}",
            "/link",
            f"/LIBPATH:{Path(sys.base_prefix) / 'libs'}",
        ]
    else:
        configured = shlex.split(sysconfig.get_config_var("CC") or "cc")
        compiler = shutil.which(configured[0]) or shutil.which("cc")
        command = [
            "-O2",
            "-Wall",
            "-Werror",
            "-fPIC",
            "-I",
            include,
            str(LINK_TREE_SOURCE),
            "-o",
            str(output),
            *(
                ["-bundle", "-undefined", "dynamic_lookup"]
                if sys.platform == "darwin"
                else ["-shared"]
            ),
        ]
    if compiler is None:
        if os.environ.get("CI"):
            pytest.fail("no C compiler to build _kpip_link_tree.c with")
        pytest.skip("no C compiler")
    built = subprocess.run(
        [compiler, *command], capture_output=True, text=True, check=False
    )
    assert built.returncode == 0, built.stdout + built.stderr
    spec = importlib.util.spec_from_file_location("_kpip_link_tree", output)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["python", "c"])
def link_loops(request: pytest.FixtureRequest) -> object:
    """Each implementation of the loops: Python, and the C built in to the binary."""
    if request.param == "python":
        return python_link_tree
    return request.getfixturevalue("built_link_tree")


def test_the_tree_loops_link_the_same_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_trees: None,
    link_loops: object,
) -> None:
    link_tree_through(monkeypatch, link_loops)
    source = make_tree(tmp_path)
    (source / "pkg" / "sub").chmod(0o750)
    (source / "pkg" / "empty").mkdir()
    destination = tmp_path / "target"

    clone.clone_path(str(source), str(destination))

    for relative in FILES:
        assert same_inode(source, destination, relative)
    assert (destination / "pkg" / "empty").is_dir()
    if os.name != "nt":
        assert (destination / "pkg" / "tool").stat().st_mode & 0o777 == 0o755
        assert stat.S_IMODE((destination / "pkg" / "sub").stat().st_mode) == 0o750
        assert os.readlink(destination / "pkg" / "sub" / "alias") == "mod.py"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
def test_the_tree_loops_keep_a_read_only_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_trees: None,
    link_loops: object,
) -> None:
    link_tree_through(monkeypatch, link_loops)
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
def test_the_tree_loops_reject_a_duplicate_and_leave_nothing(
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
    copied = Path(os.fsdecode(files[1])).as_posix()
    for relative in FILES:
        assert same_inode(source, destination, relative) == (relative != copied)


def test_a_large_tree_is_linked_in_slices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_trees: None,
    link_loops: object,
) -> None:
    link_tree_through(monkeypatch, link_loops)
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
    if os.name != "nt":
        assert os.readlink(destination / "pkg" / "sub" / "alias") == "mod.py"


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


def test_windows_takes_the_whole_tree_route_in_hard_link_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clone.sys, "platform", "win32")

    assert clone._links_whole_trees((1, 1))
    monkeypatch.setattr(clone, "_link_mode", "copy")
    assert not clone._links_whole_trees((1, 1))


def test_the_loops_stop_at_the_first_failure_with_its_errno(
    tmp_path: Path, link_loops: Any
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    for name in ("a", "b", "c"):
        (source / name).write_text(name)
    (destination / "b").write_text("racing writer")
    names = [b"a", b"b", b"c"]

    assert link_loops.link_files(
        os.fsencode(source), os.fsencode(destination), names, 0
    ) == (errno.EEXIST, 1)
    assert link_loops.link_files(
        os.fsencode(source), os.fsencode(destination), names, 2
    ) == (0, 3)
    assert link_loops.link_files(
        os.fsencode(source), os.fsencode(destination), names, 3
    ) == (0, 3)
    assert same_inode(source, destination, "c")
    if os.name != "nt":
        # Windows reports a name past its limit as a missing path.
        assert link_loops.link_files(
            os.fsencode(source), os.fsencode(destination), [b"x" * 5000], 0
        ) == (errno.ENAMETOOLONG, 0)

    root = os.fsencode(destination)
    nested = os.path.join(b"d", b"e")
    assert link_loops.make_directories(root, [b"d", nested], [0o755, 0o700]) == (0, 2)
    assert link_loops.make_directories(root, [b"f", b"d"], [0o755, 0o755]) == (
        errno.EEXIST,
        1,
    )
    assert link_loops.change_modes(root, [nested, b"missing"], [0o550, 0o550]) == (
        errno.ENOENT,
        1,
    )
    # Read-only, as each sees it: on Windows, the attribute os.chmod sets.
    assert stat.S_IMODE((destination / "d" / "e").stat().st_mode) == (
        0o555 if os.name == "nt" else 0o550
    )
    assert link_loops.change_modes(root, [nested], [0o755]) == (0, 1)


def test_the_loops_refuse_names_the_os_module_refuses(
    tmp_path: Path, link_loops: Any
) -> None:
    root = os.fsencode(tmp_path)

    with pytest.raises(TypeError):
        link_loops.make_directories(root, ["text"], [0o755])
    with pytest.raises(ValueError):
        link_loops.link_files(root, root, [b"nul\0name"], 0)


def test_the_loops_link_across_directories_in_any_order(
    tmp_path: Path, link_loops: Any
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    relative = ["a/1", "b/1", "a/2", "top", "a/deep/3", "b/2", "missing/4", "b/3"]
    for name in relative:
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(name)
    for directory in ("a", "a/deep", "b"):
        (destination / directory).mkdir(parents=True, exist_ok=True)
    names = [os.fsencode(os.path.join(*name.split("/"))) for name in relative]
    roots = (os.fsencode(source), os.fsencode(destination))

    assert link_loops.link_files(*roots, names, 0) == (errno.ENOENT, 6)
    assert link_loops.link_files(*roots, names, 7) == (0, 8)

    for name in relative:
        if name != "missing/4":
            assert same_inode(source, destination, name)
    assert not (destination / "missing").exists()


BYTECODE = ("__pycache__/mod.cpython-311.pyc", "sub/__pycache__/deep.cpython-311.pyc")


def bytecode_and_modules(root: Path) -> tuple[Path, Path]:
    """Bytecode for two modules, and the directory their modules were
    cloned into, without its ``__pycache__`` directories."""
    source = root / "bytecode"
    destination = root / "pkg"
    for relative in BYTECODE:
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_bytes(relative.encode())
    (destination / "sub").mkdir(parents=True)
    return source, destination


def test_link_into_links_named_files_with_the_tree_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    whole_trees: None,
    link_loops: object,
) -> None:
    link_tree_through(monkeypatch, link_loops)
    source, destination = bytecode_and_modules(tmp_path)

    clone.link_into(str(source), str(destination), list(BYTECODE))

    for relative in BYTECODE:
        assert same_inode(source, destination, relative)


def test_link_into_clones_each_named_file_without_the_tree_loops(
    tmp_path: Path, no_reflink: None
) -> None:
    source, destination = bytecode_and_modules(tmp_path)

    clone.link_into(str(source), str(destination), list(BYTECODE))

    for relative in BYTECODE:
        assert (destination / relative).read_bytes() == relative.encode()


def test_remove_tree_removes_everything_and_follows_no_link(
    tmp_path: Path, link_loops: object
) -> None:
    outside = tmp_path / "outside"
    (outside / "kept").mkdir(parents=True)
    (outside / "kept" / "file").write_text("keep me")
    tree = make_tree(tmp_path)
    for index in range(50):
        (tree / "pkg" / "sub" / f"many{index}.py").write_text(str(index))
    (tree / "pkg" / "deep" / "er" / "est").mkdir(parents=True)
    (tree / "pkg" / "sub" / "many0.py").chmod(0o444)
    try:
        (tree / "pkg" / "link-out").symlink_to(outside, target_is_directory=True)
    except OSError:
        # Windows without the privilege to make links.
        pass

    remove_tree = getattr(link_loops, "remove_tree")
    assert remove_tree(os.fsencode(tree)) == 0

    assert not tree.exists()
    assert (outside / "kept" / "file").read_text() == "keep me"


def test_clone_remove_tree_falls_back_without_the_loop(tmp_path: Path) -> None:
    tree = make_tree(tmp_path)

    clone.remove_tree(str(tree))

    assert not tree.exists()
