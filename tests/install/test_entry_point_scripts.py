"""Console and GUI scripts are the ones pip writes, byte for byte."""

from __future__ import annotations

import io
import os
import sys
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.install.wheel_scripts import (
    entry_point_scripts,
    generate_entry_point_files,
    scripts_not_on_path_message,
    warn_about_scripts_not_on_path,
    windows_launcher,
)
from pip._internal.operations.install.wheel import (
    PipScriptMaker,
    get_console_script_specs,
    message_about_scripts_not_on_PATH,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX scripts")

ENTRY_POINTS = {
    "tool": ("tool.cli:main", False),
    "dotted": ("tool.cli:App.run", False),
    "flagged": ("tool.cli:main [extra]", False),
    "windowed": ("tool.gui:main", True),
}


def pip_scripts(
    destination: Path, executable: str | None
) -> dict[str, tuple[bytes, int]]:
    maker = PipScriptMaker(None, str(destination))
    if executable is not None:
        maker.executable = executable
    maker.clobber = True
    maker.variants = {""}
    maker.set_mode = True
    for name, (target, gui) in ENTRY_POINTS.items():
        maker.make(f"{name} = {target}", {"gui": True} if gui else None)
    return {
        path.name: (path.read_bytes(), path.stat().st_mode)
        for path in destination.iterdir()
    }


def kpip_scripts(
    destination: Path, executable: str | None
) -> dict[str, tuple[bytes, int]]:
    written = generate_entry_point_files(ENTRY_POINTS, str(destination), executable)
    assert {mode for _, mode in written} == {
        Path(path).stat().st_mode for path, _ in written
    }
    return {
        path.name: (path.read_bytes(), path.stat().st_mode)
        for path in destination.iterdir()
    }


@posix_only
@pytest.mark.parametrize(
    "executable",
    [
        None,
        "/env/bin/python",
        "/env with space/bin/python",
        "/" + "long/" * 40 + "python",
    ],
    ids=["default", "named", "space", "long"],
)
def test_scripts_match_pips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executable: str | None
) -> None:
    monkeypatch.delenv("KPIP_SCRIPT_PYTHON", raising=False)

    assert kpip_scripts(tmp_path / "kpip", executable) == pip_scripts(
        tmp_path / "pip", executable
    )


@posix_only
def test_default_interpreter_with_a_space_is_quoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KPIP_SCRIPT_PYTHON", raising=False)
    monkeypatch.setattr(sys, "executable", "/env with space/bin/python")

    assert kpip_scripts(tmp_path / "kpip", None) == pip_scripts(tmp_path / "pip", None)


def test_an_entry_point_without_a_callable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InstallationError, match="A callable suffix is required"):
        generate_entry_point_files({"tool": ("tool.cli", False)}, str(tmp_path))


@pytest.mark.parametrize("name", ["../escape", ".", ".."])
def test_a_script_outside_the_scripts_directory_is_refused(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(InstallationError, match="outside the scripts directory"):
        generate_entry_point_files({name: ("tool:main", False)}, str(tmp_path))


@pytest.mark.parametrize("gui", [False, True])
def test_a_windows_launcher_carries_its_shebang_before_the_script(
    gui: bool,
) -> None:
    """distlib's layout: launcher, then the shebang it reads, then the zip
    it runs as ``__main__.py``."""
    head = b'#!"C:\\Program Files\\Python\\python.exe"\n'

    launcher = windows_launcher(b"print(1)\n", head, gui=gui)

    kind = "w" if gui else "t"
    stub = next(
        resource.read_bytes()
        for resource in files("kpip._launchers").iterdir()
        if resource.name.startswith(kind)
        and resource.name.endswith(".exe")
        and launcher.startswith(resource.read_bytes())
    )
    rest = launcher[len(stub) :]
    assert rest.startswith(head)
    with zipfile.ZipFile(io.BytesIO(rest[len(head) :])) as archive:
        assert archive.read("__main__.py") == b"print(1)\n"


@pytest.mark.parametrize(
    "ensurepip", [None, "install", "altinstall"], ids=["plain", "install", "altinstall"]
)
def test_pip_and_easy_install_get_this_pythons_versioned_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ensurepip: str | None
) -> None:
    if ensurepip is None:
        monkeypatch.delenv("ENSUREPIP_OPTIONS", raising=False)
    else:
        monkeypatch.setenv("ENSUREPIP_OPTIONS", ensurepip)
    console = {
        "pip": "pip._internal.cli.main:main",
        "pip3": "pip._internal.cli.main:main",
        "pip3.8": "pip._internal.cli.main:main",
        "easy_install": "setuptools.command.easy_install:main",
        "easy_install-3.8": "setuptools.command.easy_install:main",
        "other": "other:main",
    }
    entry_points = tmp_path / "entry_points.txt"
    entry_points.write_text(
        "[console_scripts]\n"
        + "".join(f"{name} = {target}\n" for name, target in console.items())
        + "[gui_scripts]\nwindowed = other:gui\n"
    )

    scripts = entry_point_scripts(str(entry_points))

    expected = dict(spec.split(" = ", 1) for spec in get_console_script_specs(console))
    assert {name: target for name, (target, gui) in scripts.items() if not gui} == (
        expected
    )
    assert scripts["windowed"] == ("other:gui", True)


@pytest.mark.parametrize(
    "scripts, path",
    [
        (["/opt/tools/bin/one"], "/usr/bin"),
        (
            ["/opt/tools/bin/one", "/opt/tools/bin/two", "/srv/bin/three"],
            "~/bin:/usr/bin",
        ),
        (["/usr/bin/one"], "/usr/bin"),
        ([], "/usr/bin"),
    ],
    ids=["one", "several", "on-path", "none"],
)
def test_the_not_on_path_warning_is_pips(
    monkeypatch: pytest.MonkeyPatch, scripts: list[str], path: str
) -> None:
    monkeypatch.setenv("PATH", path)
    monkeypatch.setattr(sys, "executable", "/env/bin/python")

    assert scripts_not_on_path_message(
        scripts, "/env/bin/python"
    ) == message_about_scripts_not_on_PATH(scripts)


def entry_point_wheel(path: Path) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "tool-1.0.dist-info/entry_points.txt",
            "[console_scripts]\ntool = tool:main\n[gui_scripts]\nwindowed = tool:gui\n",
        )
    return str(path)


def test_a_wheel_whose_scripts_land_off_path_is_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    wheel = entry_point_wheel(tmp_path / "tool-1.0-py3-none-any.whl")
    scripts = tmp_path / "bin"

    with caplog.at_level("WARNING"):
        warn_about_scripts_not_on_path([wheel], str(scripts), "/env/bin/python")

    suffix = ".exe" if os.name == "nt" else ""
    assert caplog.messages == [
        f"The script tool{suffix} is installed in '{scripts.resolve()}' which is not "
        "on PATH.\nConsider adding this directory to PATH or, if you prefer to "
        "suppress this warning, use --no-warn-script-location."
    ]


def test_scripts_beside_the_interpreter_draw_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    wheel = entry_point_wheel(tmp_path / "tool-1.0-py3-none-any.whl")
    scripts = tmp_path / "bin"

    with caplog.at_level("WARNING"):
        warn_about_scripts_not_on_path([wheel], str(scripts), str(scripts / "python"))

    assert caplog.messages == []
