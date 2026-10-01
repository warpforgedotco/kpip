"""How the PEP 517 hook caller runs a backend."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kpip.build.pep517_hooks import BuildBackendHookCaller
from kpip.core.subprocesses import VERBOSE

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


ENVIRONMENT_BACKEND = """\
import os
import sys


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return [
        os.environ.get("PYTHONPATH"),
        sorted(key for key in os.environ if key.startswith("KPIP_")),
        sys.path,
    ]
"""


def test_an_isolated_build_does_not_inherit_the_users_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In an isolated build environment, what the user's PYTHONPATH names
    -- an old setuptools, say -- would stand in for the backend the
    environment installed, as pip keeps it from doing. Without isolation
    the build runs with what the user has, PYTHONPATH included."""
    elsewhere = str(tmp_path / "elsewhere")
    monkeypatch.setenv("PYTHONPATH", elsewhere)
    monkeypatch.setenv("KPIP_INDEX_URL", "https://example.invalid/simple")
    hook = caller(tmp_path, ENVIRONMENT_BACKEND)

    isolated = BuildBackendHookCaller(
        hook.source_dir,
        "hook_backend",
        backend_path=["backend"],
        python_executable=sys.executable,
        isolated=True,
    )
    pythonpath, kpip_variables, path = isolated.prepare_metadata_for_build_wheel(
        str(tmp_path)
    )

    assert pythonpath is None
    assert kpip_variables == []
    assert elsewhere not in path

    pythonpath, _, path = hook.prepare_metadata_for_build_wheel(str(tmp_path))

    assert pythonpath == elsewhere
    assert elsewhere in path


FAILING_BACKEND = """\
import sys


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    print("compiling _speedups.c")
    print("error: Python.h: No such file or directory", file=sys.stderr)
    raise SystemExit("error: command 'cc' failed")


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    print("reading the version")
    raise ValueError("no version found")
"""


def test_a_failing_hook_says_what_the_backend_printed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The traceback says where a backend gave up; what it printed first --
    the compiler's error -- says why, as pip shows it. At -v it is logged
    as it would be had the hook succeeded."""
    hook = caller(tmp_path, FAILING_BACKEND)

    with caplog.at_level(VERBOSE, logger="kpip.subprocessor"):
        with pytest.raises(RuntimeError) as raised:
            hook.prepare_metadata_for_build_wheel(str(tmp_path))

    message = str(raised.value)
    assert "reading the version" in message
    assert "ValueError: no version found" in message
    assert "reading the version" in caplog.messages

    # A backend that exits rather than raises.
    with pytest.raises(RuntimeError) as raised:
        hook.build_wheel(str(tmp_path))

    message = str(raised.value)
    assert "compiling _speedups.c" in message
    assert "Python.h: No such file or directory" in message
    assert "command 'cc' failed" in message
