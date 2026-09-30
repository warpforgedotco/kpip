"""Windows script launchers: only the 64-bit ones ship."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.install import wheel_scripts


@pytest.fixture(autouse=True)
def windows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.delenv("PROCESSOR_ARCHITECTURE", raising=False)


@pytest.mark.parametrize(
    "environment, gui, launcher",
    [
        ({"PROCESSOR_ARCHITECTURE": "AMD64"}, False, "t64.exe"),
        ({"PROCESSOR_ARCHITECTURE": "AMD64"}, True, "w64.exe"),
        ({"PROCESSOR_ARCHITECTURE": "ARM64"}, False, "t64-arm.exe"),
        ({"PROCESSOR_ARCHITECTURE": "ARM64"}, True, "w64-arm.exe"),
        # A 32-bit Python on 64-bit Windows, which sees x86 and is told the
        # real machine separately.
        (
            {"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "AMD64"},
            False,
            "t64.exe",
        ),
        (
            {"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "ARM64"},
            True,
            "w64-arm.exe",
        ),
    ],
)
def test_a_script_is_written_on_the_launcher_for_the_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    gui: bool,
    launcher: str,
) -> None:
    from importlib.resources import files

    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    target = tmp_path / "tool.exe"

    wheel_scripts.write_windows_script(str(target), "print('hi')\n", gui=gui)

    expected = (files("kpip._launchers") / launcher).read_bytes()
    assert target.read_bytes().startswith(expected)
    with zipfile.ZipFile(target) as package:
        assert package.read("__main__.py") == b"print('hi')\n"


def test_32_bit_windows_is_refused_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "x86")
    monkeypatch.setattr(sys, "maxsize", 2**31 - 1)

    with pytest.raises(InstallationError, match="64-bit Windows"):
        wheel_scripts.write_windows_script(str(tmp_path / "tool.exe"), "", gui=False)

    assert not (tmp_path / "tool.exe").exists()


def test_a_64_bit_python_told_nothing_assumes_x64() -> None:
    assert wheel_scripts.windows_machine() == ("AMD64" if sys.maxsize > 2**32 else "")
