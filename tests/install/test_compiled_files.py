"""Bytecode for a module installed from the archive cache."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from kpip.install import wheel_state
from kpip.install.bytecode import pyc_path
from kpip.install.wheel_archive import compiled_parts, mapped_parts


def test_a_module_missing_from_the_cached_tree_is_not_compiled_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published bytecode tree lacks only modules that would not compile:
    compiling them again at every install would only fail again."""
    tree = tmp_path / "pyc"
    tree.mkdir()
    stage = tmp_path / "stage" / "pkg"
    stage.mkdir(parents=True)
    source = stage / "broken.py"
    source.write_text("print 'python 2'\n")
    assert compiled_parts(mapped_parts("pkg/broken.py")) is not None
    assert pyc_path(os.fspath(source)) is not None

    compiled: list[object] = []
    monkeypatch.setattr(wheel_state, "bytecode_tree", lambda archive: str(tree))
    monkeypatch.setattr(wheel_state, "target_magic", lambda: b"\0\0\0\0")
    monkeypatch.setattr(wheel_state, "compile_modules", compiled.extend)

    placed = wheel_state.compiled_files(
        str(tmp_path / "stage"),
        [(str(source), "/site/pkg/broken.py", "/site/pkg/broken.py", None)],
        archive=object(),  # ty:ignore[invalid-argument-type]
        members={str(source): "pkg/broken.py"},
    )

    assert placed == []
    assert compiled == []
