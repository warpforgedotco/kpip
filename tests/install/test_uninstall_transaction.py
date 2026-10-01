from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from kpip.core.errors import InstallationError
from kpip.install.requirements import RequirementInstaller
from kpip.install.target import InstallTarget
from kpip.install.wheel_transaction import WheelInstaller


def wheel_internal(directory: Path) -> Path:
    path = directory / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("demo/__init__.py", "value = 1\n")
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("demo-1.0.dist-info/RECORD", "")
    return path


def install_internal(path: Path, target: Path) -> None:
    WheelInstaller(
        InstallTarget.from_options("demo", target=str(target)),
        pycompile=False,
    ).install(
        path,
    )


def test_uninstall_preserves_unrelated_and_unsafe_record_paths(tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    install_internal(wheel_internal(tmp_path), target)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    record = target / "demo-1.0.dist-info" / "RECORD"
    with record.open("a", encoding="utf-8") as file:
        file.write(f"{outside},,\n../outside.txt,,\n")

    assert RequirementInstaller().uninstall("demo", paths=[str(target)])
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_removes_symlink_without_following_target(tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    install_internal(wheel_internal(tmp_path), target)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = target / "demo-link"
    link.symlink_to(outside)
    with (target / "demo-1.0.dist-info" / "RECORD").open(
        "a",
        encoding="utf-8",
    ) as record:
        record.write("demo-link,,\n")

    assert RequirementInstaller().uninstall("demo", paths=[str(target)])
    assert not os.path.lexists(link)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_requires_record(tmp_path: Path) -> None:
    target = tmp_path / "site-packages"
    install_internal(wheel_internal(tmp_path), target)
    (target / "demo-1.0.dist-info" / "RECORD").unlink()

    with pytest.raises(InstallationError, match="no RECORD file was found"):
        RequirementInstaller().uninstall("demo", paths=[str(target)])


def test_uninstall_reads_installed_files_relative_to_the_egg_info(
    tmp_path: Path,
) -> None:
    """setuptools writes installed-files.txt relative to the .egg-info
    directory, so its rows name the package as ``../demo/...``."""
    site = tmp_path / "site-packages"
    info = site / "demo-1.0-py3.15.egg-info"
    info.mkdir(parents=True)
    (info / "PKG-INFO").write_text("Metadata-Version: 1.1\nName: demo\nVersion: 1.0\n")
    (info / "installed-files.txt").write_text(
        "../demo/__init__.py\n../demo/legacy.py\nPKG-INFO\n"
    )
    package = site / "demo"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "legacy.py").write_text("")

    assert RequirementInstaller().uninstall("demo", paths=[str(site)])

    assert not (package / "__init__.py").exists()
    assert not (package / "legacy.py").exists()
    assert not info.exists()


def _python_at(prefix: Path) -> object:
    """This Python's facts, as if it lived at ``prefix``."""
    from kpip.host import interpreter_facts

    namespace: dict = {"__name__": "kpip_interpreter_probe"}
    exec(interpreter_facts.PROBE, namespace)  # noqa: S102
    facts = namespace["facts"]()
    facts["executable"] = str(prefix / "bin" / "python")
    facts["prefix"] = facts["base_prefix"] = str(prefix)
    for name in ("base", "platbase", "installed_base", "installed_platbase"):
        facts["config"][name] = str(prefix)
    return interpreter_facts.Interpreter(facts)


def test_uninstall_removes_data_and_headers_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel's ``.data/data`` and ``.data/headers`` members are recorded as
    ``../../../share/...`` and ``../../../include/...`` rows, under the target
    interpreter's scheme rather than site-packages, and are its own files."""
    from kpip.host.locations.sysconfig_scheme import get_scheme
    from kpip.install import uninstall

    other = _python_at(tmp_path / "env")
    monkeypatch.setattr(uninstall, "target_interpreter", lambda: other)

    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo/__init__.py", "value = 1\n")
        archive.writestr("demo-1.0.data/data/share/demo/notes.txt", "notes\n")
        archive.writestr("demo-1.0.data/headers/demo.h", "/* demo */\n")
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("demo-1.0.dist-info/RECORD", "")
    scheme = get_scheme("demo", interpreter=other)
    WheelInstaller(InstallTarget.from_scheme(scheme), pycompile=False).install(wheel)
    data_file = Path(scheme.data) / "share" / "demo" / "notes.txt"
    header = Path(scheme.headers) / "demo.h"
    assert data_file.is_file()
    assert header.is_file()

    assert RequirementInstaller().uninstall("demo", paths=[scheme.purelib])

    assert not data_file.exists()
    assert not header.exists()
    assert not (Path(scheme.purelib) / "demo").exists()
