"""A backend hook finds its isolated build environment's scripts first."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from kpip.build.pep517_hooks import BuildBackendHookCaller

BACKEND = """\
import os


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return os.environ["PATH"]
"""


def caller(tmp_path: Path, scripts_dir: str | None) -> BuildBackendHookCaller:
    (tmp_path / "path_backend.py").write_text(BACKEND, encoding="utf-8")
    return BuildBackendHookCaller(
        str(tmp_path),
        "path_backend",
        backend_path=["."],
        python_executable=sys.executable,
        scripts_dir=scripts_dir,
    )


def test_the_scripts_dir_comes_first_on_the_hooks_path(tmp_path: Path) -> None:
    scripts = str(tmp_path / "env" / "bin")

    path = (
        caller(tmp_path, scripts)
        .prepare_metadata_for_build_wheel(str(tmp_path))
        .split(os.pathsep)
    )

    assert path[0] == scripts
    assert path[1:] == os.environ["PATH"].split(os.pathsep)


def test_without_one_the_path_is_inherited(tmp_path: Path) -> None:
    path = (
        caller(tmp_path, None)
        .prepare_metadata_for_build_wheel(str(tmp_path))
        .split(os.pathsep)
    )

    assert path == os.environ["PATH"].split(os.pathsep)
