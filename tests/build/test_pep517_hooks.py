"""How the PEP 517 hook caller runs a backend."""

from __future__ import annotations

import sys
from pathlib import Path

from kpip.build.pep517_hooks import BuildBackendHookCaller

BACKEND = """\
import os
import sys


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    import re

    return [os.getcwd(), sys.path, os.environ.get("PYTHONPATH")]
"""


def caller(tmp_path: Path, backend: str = BACKEND) -> BuildBackendHookCaller:
    project = tmp_path / "project"
    (project / "backend").mkdir(parents=True)
    (project / "backend" / "hook_backend.py").write_text(backend, encoding="utf-8")
    return BuildBackendHookCaller(
        str(project),
        "hook_backend",
        backend_path=["backend"],
        python_executable=sys.executable,
    )


def test_the_projects_own_modules_do_not_shadow_the_standard_library(
    tmp_path: Path,
) -> None:
    """A project with a top-level ``enum.py`` builds: the hook runs in the
    project directory, but neither it nor PYTHONPATH puts the project on the
    backend's path. Only backend-path does."""
    hook = caller(tmp_path)
    project = hook.source_dir
    (Path(project) / "enum.py").write_text(
        'raise ImportError("the project shadowed enum")\n', encoding="utf-8"
    )

    cwd, path, pythonpath = hook.prepare_metadata_for_build_wheel(str(tmp_path))

    assert cwd == project
    assert "" not in path
    assert project not in path
    assert path[0] == str(Path(project) / "backend")
    assert project not in (pythonpath or "").split(":")
