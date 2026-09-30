from __future__ import annotations

import importlib.machinery
import shutil
from pathlib import Path

import pytest
from kpip_compile.extensions import (
    EXTENSIONS,
    POSIX_ONLY,
    SOURCE_ROOT,
    build_extensions,
    extensions_for,
    optimization_args,
)


def test_every_extension_names_its_own_source() -> None:
    for name, path in EXTENSIONS.items():
        assert (SOURCE_ROOT / path).is_file()
        # The spelled-out name is the one the source's location implies.
        assert name == path.removesuffix(".py").replace("/", ".")


def test_a_windows_build_leaves_out_the_posix_extensions() -> None:
    assert POSIX_ONLY <= EXTENSIONS.keys()
    assert extensions_for("linux") == EXTENSIONS
    assert extensions_for("win32").keys() == EXTENSIONS.keys() - POSIX_ONLY


@pytest.mark.parametrize(
    ("compiler_type", "expected"),
    [("unix", ["-O3"]), ("mingw32", ["-O3"]), ("msvc", ["/O2"])],
)
def test_optimization_is_requested_explicitly(
    compiler_type: str, expected: list[str]
) -> None:
    assert optimization_args(compiler_type) == expected


def test_builds_beside_the_source_and_leaves_no_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in EXTENSIONS.values():
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SOURCE_ROOT / path, destination)
    # A CFLAGS without optimization must not decide the build's.
    monkeypatch.setenv("CFLAGS", "-O0")
    before = Path.cwd()

    built = build_extensions(tmp_path, force=True)

    # It works from relative paths, to keep Windows object paths short, and
    # hands the caller's working directory back.
    assert Path.cwd() == before

    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    # Those this platform builds: Windows leaves out the POSIX-only ones.
    selected = extensions_for()
    assert len(built) == len(selected)
    for artifact, path in zip(built, selected.values()):
        assert artifact.is_file()
        assert artifact.parent == (tmp_path / path).parent
        assert artifact.name.endswith(suffixes)
    assert not list(tmp_path.rglob("*.c"))
