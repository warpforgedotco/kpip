"""A cached result must not outlive the code that produced it."""

from __future__ import annotations

import os
from pathlib import Path

from kpip.core.code_identity import _source_identity


def make_install(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "kpip"
    (root / "resolution").mkdir(parents=True)
    (root / "__init__.py").write_text("x = 1\n")
    (root / "resolution" / "engine.py").write_text("y = 2\n")
    return root


def test_a_same_size_edit_to_an_older_module_changes_the_identity(
    tmp_path: Path,
) -> None:
    root = make_install(tmp_path)
    older = root / "resolution" / "engine.py"
    newer = root / "__init__.py"
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(9_000_000_000, 9_000_000_000))
    before = _source_identity(str(root))

    older.write_text("y = 3\n")
    os.utime(older, ns=(2_000_000_000, 2_000_000_000))

    assert _source_identity(str(root)) != before


def test_the_identity_is_stable(tmp_path: Path) -> None:
    root = make_install(tmp_path)

    assert _source_identity(str(root)) == _source_identity(str(root))
