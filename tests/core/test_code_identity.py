"""A cached result must not outlive the code that produced it."""

from __future__ import annotations

import os
from pathlib import Path

from kpip.core.code_identity import _source_identity


def make_install(tmp_path: Path, record: bytes) -> Path:
    root = tmp_path / "site-packages" / "kpip"
    (root / "resolution").mkdir(parents=True)
    (root / "__init__.py").write_text("x = 1\n")
    (root / "resolution" / "engine.py").write_text("y = 2\n")
    dist_info = tmp_path / "site-packages" / "kpip-0.0.1.dist-info"
    dist_info.mkdir()
    (dist_info / "RECORD").write_bytes(record)
    return root


def test_a_same_size_edit_to_an_older_module_changes_the_identity(
    tmp_path: Path,
) -> None:
    root = make_install(tmp_path, b"")
    older = root / "resolution" / "engine.py"
    newer = root / "__init__.py"
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(9_000_000_000, 9_000_000_000))
    before = _source_identity(str(root))

    older.write_text("y = 3\n")
    os.utime(older, ns=(2_000_000_000, 2_000_000_000))

    assert _source_identity(str(root)) != before


def test_a_reproducible_build_is_told_apart_by_its_record(tmp_path: Path) -> None:
    """Fixed timestamps and equal sizes; only the RECORD hashes differ."""
    first = make_install(tmp_path / "a", b"kpip/resolution/engine.py,sha256=aaa,6\n")
    second = make_install(tmp_path / "b", b"kpip/resolution/engine.py,sha256=bbb,6\n")

    for root in (first, second):
        for path in root.rglob("*.py"):
            os.utime(path, ns=(315_532_800_000_000_000, 315_532_800_000_000_000))

    assert _source_identity(str(first))[2] != _source_identity(str(second))[2]


def test_the_identity_is_stable(tmp_path: Path) -> None:
    root = make_install(tmp_path, b"record")

    assert _source_identity(str(root)) == _source_identity(str(root))
