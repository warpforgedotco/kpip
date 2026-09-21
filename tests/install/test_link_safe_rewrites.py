"""Installer rewrites must not reach a hard-linked cache tree.

On filesystems without copy-on-write, ``clone_path`` hard links cached wheel
trees into the staging directory.  Anything the installer then rewrites in
place would rewrite the cache's copy as well; these rewrites must land in a
fresh inode instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from kpip.install.wheel_archive_installer import _rewrite_metadata
from kpip.install.wheel_scripts import rewrite_shebang


def test_metadata_normalization_leaves_the_cached_copy_alone(tmp_path: Path) -> None:
    cached = tmp_path / "cache" / "METADATA"
    cached.parent.mkdir()
    cached.write_text("Metadata-Version: 2.1\nName: Owner_Demo\nVersion: 1.0\n")
    installed = tmp_path / "target" / "METADATA"
    installed.parent.mkdir()
    os.link(cached, installed)

    rewritten = _rewrite_metadata(str(installed), SimpleNamespace(name="owner-demo"))

    assert rewritten is not None
    assert "Name: owner-demo\n" in installed.read_text()
    assert "Name: Owner_Demo\n" in cached.read_text()
    assert installed.stat().st_nlink == 1


def test_shebang_rewrite_leaves_the_cached_copy_alone(tmp_path: Path) -> None:
    cached = tmp_path / "cache" / "tool"
    cached.parent.mkdir()
    cached.write_bytes(b"#!python\nprint('hi')\n")
    if os.name != "nt":
        cached.chmod(0o755)
    installed = tmp_path / "target" / "tool"
    installed.parent.mkdir()
    os.link(cached, installed)

    rewrite_shebang(str(installed), "/opt/py/bin/python")

    assert installed.read_bytes() == b"#!/opt/py/bin/python\nprint('hi')\n"
    assert cached.read_bytes() == b"#!python\nprint('hi')\n"
    assert installed.stat().st_nlink == 1
    if os.name != "nt":
        assert installed.stat().st_mode & 0o777 == 0o755
