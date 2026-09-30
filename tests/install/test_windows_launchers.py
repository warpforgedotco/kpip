"""Windows script launchers: only the 64-bit ones ship."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.install import wheel_scripts


@pytest.mark.parametrize(
    "machine, gui, launcher",
    [
        ("AMD64", False, "t64.exe"),
        ("AMD64", True, "w64.exe"),
        ("ARM64", False, "t64-arm.exe"),
        ("ARM64", True, "w64-arm.exe"),
    ],
)
def test_a_script_is_written_on_the_launcher_for_its_machine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    gui: bool,
    launcher: str,
) -> None:
    from importlib.resources import files

    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", machine)
    monkeypatch.setattr(sys, "maxsize", 2**63 - 1)
    target = tmp_path / "tool.exe"

    wheel_scripts.write_windows_script(str(target), "print('hi')\n", gui=gui)

    expected = (files("kpip._launchers") / launcher).read_bytes()
    written = target.read_bytes()
    assert written.startswith(expected)
    with zipfile.ZipFile(target) as package:
        assert package.read("__main__.py") == b"print('hi')\n"


def test_32_bit_python_is_refused_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "maxsize", 2**31 - 1)

    with pytest.raises(InstallationError, match="64-bit Python"):
        wheel_scripts.write_windows_script(str(tmp_path / "tool.exe"), "", gui=False)

    assert not (tmp_path / "tool.exe").exists()
