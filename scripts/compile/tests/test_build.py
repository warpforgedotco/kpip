from __future__ import annotations

from pathlib import Path

import pytest
from kpip_compile.build import (
    KPIP_PACKAGE,
    ONEFILE_TEMPDIR_SPEC,
    BuildOptions,
    kpip_version,
    nuitka_command,
)


def test_version_is_read_from_the_package(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text('"""kpip."""\n\n__version__ = "1.2.3"\n')

    assert kpip_version(tmp_path) == "1.2.3"


def test_missing_version_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text("")

    with pytest.raises(ValueError, match="__version__"):
        kpip_version(tmp_path)


def test_cached_onefile_uses_a_static_spec() -> None:
    command = nuitka_command(BuildOptions(platform="darwin"), "1.2.3")

    assert command[1:3] == ["-m", "nuitka"]
    assert "--mode=onefile" in command
    assert "--onefile-cache-mode=cached" in command
    assert f"--onefile-tempdir-spec={ONEFILE_TEMPDIR_SPEC}" in command
    # {VERSION} in the spec needs a product version.
    assert "--product-version=1.2.3" in command
    assert "--include-package=kpip" in command
    assert "--include-package-data=kpip" in command
    assert command[-1] == str(KPIP_PACKAGE)


def test_temporary_onefile_keeps_nuitkas_default_spec() -> None:
    command = nuitka_command(
        BuildOptions(cache_mode="temporary", platform="darwin"), "1"
    )

    assert "--onefile-cache-mode=temporary" in command
    assert not any(arg.startswith("--onefile-tempdir-spec") for arg in command)


def test_standalone_has_no_onefile_options() -> None:
    command = nuitka_command(BuildOptions(mode="standalone", platform="linux"), "1")

    assert "--mode=standalone" in command
    assert not any(arg.startswith("--onefile") for arg in command)


def test_windows_ships_the_launchers() -> None:
    command = nuitka_command(BuildOptions(platform="win32"), "1")

    assert "--output-filename=kpip.exe" in command
    assert any(
        arg.startswith("--include-data-files=") and arg.endswith("=kpip/_launchers/")
        for arg in command
    )


def test_extra_arguments_precede_the_target() -> None:
    command = nuitka_command(
        BuildOptions(platform="linux", extra_args=("--report=r.xml",)), "1"
    )

    assert command[-2:] == ["--report=r.xml", str(KPIP_PACKAGE)]


def test_a_standalone_binary_does_not_collide_with_the_package_folder() -> None:
    """The folder holds ``kpip/`` package data, so the binary cannot be ``kpip``."""
    command = nuitka_command(BuildOptions(mode="standalone", platform="darwin"), "1")

    assert "--output-filename=kpip.bin" in command
    assert "--output-filename=kpip" in nuitka_command(
        BuildOptions(platform="darwin"), "1"
    )
