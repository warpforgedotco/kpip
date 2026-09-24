"""Which Python runs build backends, when kpip may not be one itself."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kpip.core import interpreter
from kpip.core.interpreter import NoBuildInterpreterError, build_interpreter
from kpip.core.packaging import set_target_python_version

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="fake interpreters are shell scripts"
)


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interpreter, "_build_interpreters", {})
    for variable in ("KPIP_BUILD_PYTHON", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.delenv(variable, raising=False)


def fake_python(directory: Path, name: str, version: str | None) -> Path:
    """An executable that answers the probe like an interpreter of ``version``, or fails."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body = f"echo {version}" if version is not None else "exit 1"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def compiled(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(interpreter, "is_compiled", lambda: True)
    monkeypatch.setenv("PATH", str(path))


def test_interpreted_kpip_builds_with_itself() -> None:
    assert build_interpreter() == sys.executable


def test_an_explicit_interpreter_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KPIP_BUILD_PYTHON", "/opt/python/bin/python3")
    compiled(monkeypatch, tmp_path)

    assert build_interpreter() == "/opt/python/bin/python3"


def test_compiled_kpip_prefers_the_target_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "bin"
    fake_python(bin_dir, "python3", "3.12")
    wanted = fake_python(bin_dir, "python3.14", "3.14")
    compiled(monkeypatch, bin_dir)
    set_target_python_version("3.14")

    try:
        assert build_interpreter() == str(wanted)
    finally:
        set_target_python_version(None)


def test_compiled_kpip_prefers_the_active_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = "%d.%d" % sys.version_info[:2]
    fake_python(tmp_path / "bin", f"python{target}", target)
    venv_python = fake_python(tmp_path / "venv" / "bin", "python", target)
    compiled(monkeypatch, tmp_path / "bin")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "venv"))

    assert build_interpreter() == str(venv_python)


def test_compiled_kpip_falls_back_to_another_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_python(tmp_path, "python", None)
    other = fake_python(tmp_path, "python3", "3.9")
    compiled(monkeypatch, tmp_path)

    assert build_interpreter() == str(other)


def test_compiled_kpip_without_a_python_says_how_to_get_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_python(tmp_path, "python3", None)
    compiled(monkeypatch, tmp_path)

    with pytest.raises(NoBuildInterpreterError) as raised:
        build_interpreter()

    assert "KPIP_BUILD_PYTHON" in str(raised.value.hint_stmt)
    assert str(tmp_path / "python3") in str(raised.value.context)


def test_another_interpreter_creates_its_own_build_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The environment comes from that interpreter, not this process's venv module."""
    from kpip.install.build_env import isolated_venv

    monkeypatch.setattr(interpreter, "is_own_interpreter", lambda executable: False)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("created the environment in-process")

    import venv

    monkeypatch.setattr(venv.EnvBuilder, "create", unexpected)

    created = isolated_venv.create_isolated_venv(
        str(tmp_path), with_pip=False, python=sys.executable
    )

    ran = subprocess.run(
        [created.python_executable, "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert os.path.realpath(ran.stdout.strip()) == os.path.realpath(tmp_path)
    assert os.path.isdir(created.lib_dirs[0])
