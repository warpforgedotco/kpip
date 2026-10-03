"""MemberPaths must answer exactly what the per-member helpers answer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.install.target import InstallTarget
from kpip.install.wheel_archive import (
    MemberPaths,
    destination_internal_parts_text,
    validate_member_parts,
)

MEMBER_NAMES = (
    "pkg/__init__.py",
    "pkg/sub/mod.py",
    "pkg/sub/other.py",
    "pkg/sub/deep/er/file.txt",
    "top_level.py",
    "pkg-1.0.dist-info/METADATA",
    "pkg-1.0.dist-info/RECORD",
    "pkg-1.0.data/scripts/tool",
    "pkg-1.0.data/purelib/pure/mod.py",
    "pkg-1.0.data/platlib/plat.so",
    "pkg-1.0.data/headers/pkg.h",
    "pkg-1.0.data/data/share/file",
    "pkg//double/slash.py",
    "pkg/./dot/segment.py",
    "./leading/dot.py",
    "pkg/name:with:colons.py",
    "pkg/trailing.data",
    "weird.data",
    "pkg/sub/.hidden",
    "pkg/sub/..hidden",
    "pkg/sub/...",
    "unicode/ünïcode.py",
    "spaces/with spaces.py",
    "pkg/sub/.",
    "pkg/sub/",
)

ERROR_NAMES = (
    "/absolute/path.py",
    "pkg/../escape.py",
    "../escape.py",
    "pkg\\backslash.py",
    "pkg/sub\\file.py",
    "pkg-1.0.data/scripts",
    "pkg-1.0.data/unknown/file",
    "pkg-1.0.data/file",
    "pkg/sub/..",
)


def _slow(
    target: InstallTarget,
    stage_root: str,
    name: str,
) -> tuple[tuple[str, ...], str, str, str] | type[InstallationError]:
    try:
        parts = validate_member_parts(name)
        return (
            parts,
            os.path.join(stage_root, *parts),
            destination_internal_parts_text(target, parts, name),
            "/".join(parts),
        )
    except InstallationError:
        return InstallationError


def _slow_or_raise(target: InstallTarget, name: str) -> str:
    return destination_internal_parts_text(target, validate_member_parts(name), name)


def _fast(
    resolver: MemberPaths,
    name: str,
) -> tuple[tuple[str, ...], str, str, str] | type[InstallationError]:
    try:
        return resolver.resolve(name)
    except InstallationError:
        return InstallationError


@pytest.fixture
def target(tmp_path: Path) -> InstallTarget:
    root = tmp_path / "site"
    root.mkdir()
    return InstallTarget.from_options("demo", target=os.fspath(root))


@pytest.mark.parametrize("name", MEMBER_NAMES)
def test_member_paths_matches_per_member_helpers(
    target: InstallTarget,
    tmp_path: Path,
    name: str,
) -> None:
    stage_root = os.fspath(tmp_path / "stage")
    resolver = MemberPaths(target, stage_root)
    assert _fast(resolver, name) == _slow(target, stage_root, name)
    sibling = f"{name.rpartition('/')[0]}/sibling.py" if "/" in name else "sibling.py"
    assert _fast(resolver, sibling) == _slow(target, stage_root, sibling)


@pytest.mark.parametrize("name", ERROR_NAMES)
def test_member_paths_raises_like_per_member_helpers(
    target: InstallTarget,
    tmp_path: Path,
    name: str,
) -> None:
    stage_root = os.fspath(tmp_path / "stage")
    resolver = MemberPaths(target, stage_root)
    expected = _slow(target, stage_root, name)
    assert expected is InstallationError
    with pytest.raises(InstallationError) as fast_error:
        resolver.resolve(name)
    with pytest.raises(InstallationError) as slow_error:
        _slow_or_raise(target, name)
    assert str(fast_error.value) == str(slow_error.value)


def test_member_paths_shares_the_realpath_caches(
    target: InstallTarget,
    tmp_path: Path,
) -> None:
    stage_root = os.fspath(tmp_path / "stage")
    resolved_directories: dict[tuple[str, str], str] = {}
    resolved_roots: dict[str, str] = {}
    resolver = MemberPaths(
        target,
        stage_root,
        resolved_directories=resolved_directories,
        resolved_roots=resolved_roots,
    )
    resolver.resolve("pkg/sub/a.py")
    resolver.resolve("pkg/sub/b.py")
    resolver.resolve("pkg/c.py")
    assert set(resolved_roots) == {target.purelib}
    assert {key[1] for key in resolved_directories} == {
        os.path.join("pkg", "sub"),
        "pkg",
    }
    assert len(resolver.directories) == 2


def test_member_paths_escape_uses_members_full_name(
    target: InstallTarget,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(target.purelib) / "link").symlink_to(outside, target_is_directory=True)
    resolver = MemberPaths(target, os.fspath(tmp_path / "stage"))
    with pytest.raises(InstallationError, match=r"link/escaped\.py"):
        resolver.resolve("link/escaped.py")


def test_shared_resolved_roots_survive_concurrent_misses(
    target: InstallTarget,
    tmp_path: Path,
) -> None:
    """Parallel installs share target.resolved_roots_internal: concurrent
    misses may each realpath the root, but they store the same value and the
    plain-dict updates are atomic, so every worker sees one consistent answer."""
    from concurrent.futures import ThreadPoolExecutor

    from kpip.install.wheel_archive import _resolved_parent_directory

    roots: dict[str, str] = {}
    directories: dict[tuple[str, str], str] = {}

    def resolve(index: int) -> str:
        return _resolved_parent_directory(
            target.purelib,
            ("pkg", f"sub{index % 3}"),
            f"pkg/sub{index % 3}/file{index}.py",
            resolved_directories=directories,
            resolved_roots=roots,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(resolve, range(64)))

    expected_root = os.path.realpath(target.purelib)
    assert roots == {target.purelib: expected_root}
    assert set(results) == {
        os.path.join(expected_root, "pkg", f"sub{index}") for index in range(3)
    }
    assert {key[1] for key in directories} == {"pkg"} | {
        os.path.join("pkg", f"sub{index}") for index in range(3)
    }


class TestCompiledParts:
    """Where byte-compilation lands, as path parts.

    The parts are used three ways -- as a collision-trie key, as a target
    preflight path, and joined with "/" into a RECORD row -- so every one of
    them has to be a single path component.
    """

    def test_directory_comes_from_the_member_not_the_interpreter(self) -> None:
        from kpip.install.wheel_archive import compiled_parts, mapped_parts

        parts = compiled_parts(mapped_parts("pkg/sub/mod.py"))

        assert parts is not None
        assert parts[:3] == ("pkg", "sub", "__pycache__")
        assert parts[-1].startswith("mod.")
        assert parts[-1].endswith(".pyc")

    def test_scripts_are_not_compiled(self) -> None:
        from kpip.install.wheel_archive import compiled_parts, mapped_parts

        assert compiled_parts(mapped_parts("f-1.0.data/scripts/tool.py")) is None
        assert compiled_parts(mapped_parts("pkg/data.txt")) is None

    def test_the_name_is_the_target_interpreters(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Not ``cache_from_source``'s, which answers for the Python running
        kpip: compiled, the one it was built with."""
        import types

        from kpip.install import bytecode
        from kpip.install.wheel_archive import compiled_parts, mapped_parts

        other = types.SimpleNamespace(cache_tag="cpython-312", magic="00000000")
        monkeypatch.setattr(bytecode, "target_interpreter", lambda **_: other)
        monkeypatch.setattr(bytecode, "is_compiled", lambda: True)

        parts = compiled_parts(mapped_parts("pkg/sub/mod.py"))

        assert parts == ("pkg", "sub", "__pycache__", "mod.cpython-312.pyc")

    def test_an_interpreter_without_bytecode_gets_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import types

        from kpip.install import bytecode
        from kpip.install.wheel_archive import compiled_parts, mapped_parts

        other = types.SimpleNamespace(cache_tag=None, magic="00000000")
        monkeypatch.setattr(bytecode, "target_interpreter", lambda **_: other)
        monkeypatch.setattr(bytecode, "is_compiled", lambda: True)

        assert compiled_parts(mapped_parts("pkg/sub/mod.py")) is None

    def test_pycache_prefix_does_not_relocate_installed_bytecode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bytecode belongs beside the module it was built from, whatever
        ``sys.pycache_prefix`` says."""
        import sys

        from kpip.install.wheel_archive import compiled_parts, mapped_parts

        monkeypatch.setattr(sys, "pycache_prefix", "/var/pycache")

        parts = compiled_parts(mapped_parts("pkg/sub/mod.py"))

        assert parts is not None
        assert parts[:3] == ("pkg", "sub", "__pycache__")


def test_member_paths_with_caches_resolve_links_like_realpath(
    target: InstallTarget,
    tmp_path: Path,
) -> None:
    """With the shared caches a directory is resolved from the one above it;
    links inside the root, links out of it, files in a directory's place and
    directories not there yet must still resolve as realpath resolves them."""
    site = Path(target.purelib)
    (site / "pkg" / "real" / "deep").mkdir(parents=True)
    (site / "pkg" / "linked").symlink_to(
        site / "pkg" / "real", target_is_directory=True
    )
    (site / "pkg" / "afile").write_text("")
    outside = tmp_path / "outside"
    outside.mkdir()
    (site / "pkg" / "out").symlink_to(outside, target_is_directory=True)
    names = (
        "pkg/real/deep/a.py",
        "pkg/linked/deep/b.py",
        "pkg/linked/new/c.py",
        "pkg/new/newer/d.py",
        "pkg/afile/e.py",
        "pkg/out/f.py",
        "pkg/out/sub/g.py",
        "fresh/one/two/h.py",
    )
    stage_root = os.fspath(tmp_path / "stage")
    resolver = MemberPaths(
        target, stage_root, resolved_directories={}, resolved_roots={}
    )
    for name in names:
        assert _fast(resolver, name) == _slow(target, stage_root, name), name
