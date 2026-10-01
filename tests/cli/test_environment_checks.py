"""What pip checks of the environment before it changes it, and what it
leaves behind of a build when asked."""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path

import pytest
from kpip.build.build_backend import check_build_requirements
from kpip.cli import install, uninstall
from kpip.cli.install import run_install
from kpip.cli.parsers.install import create_parser
from kpip.core import run_options
from kpip.core.errors import BuildError
from kpip.core.metadata import clear_installed_index
from kpip.core.temp_dir import build_directory
from kpip.host import environment_checks
from kpip.core.errors import CommandError
from kpip.host.environment_checks import (
    ExternallyManagedEnvironment,
    check_externally_managed,
    check_system_python,
    warn_if_run_as_root,
)


@pytest.fixture
def system_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An interpreter outside any virtual environment, its stdlib in a
    directory the test owns."""
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    interpreter = types.SimpleNamespace(
        stdlib=str(stdlib), prefix=str(tmp_path), in_virtualenv=False
    )
    monkeypatch.setattr(environment_checks, "target_interpreter", lambda: interpreter)
    return stdlib


def test_an_unmarked_environment_may_be_changed(system_python: Path) -> None:
    check_externally_managed()


def test_a_marked_environment_is_refused_with_its_distributors_words(
    system_python: Path,
) -> None:
    (system_python / "EXTERNALLY-MANAGED").write_text(
        "[externally-managed]\nError=Use apt install python3-xyz instead.\n"
    )

    with pytest.raises(ExternallyManagedEnvironment) as refused:
        check_externally_managed()

    message = str(refused.value)

    assert message.startswith("externally-managed-environment\n")
    assert "This environment is externally managed" in message
    assert "Use apt install python3-xyz instead." in message
    assert "--break-system-packages" in message
    assert "PEP 668" in message


def test_a_marker_that_says_nothing_gets_the_default_words(
    system_python: Path,
) -> None:
    (system_python / "EXTERNALLY-MANAGED").write_text("")

    with pytest.raises(ExternallyManagedEnvironment, match="is managed externally"):
        check_externally_managed()


def test_a_virtual_environment_is_never_externally_managed(
    system_python: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (system_python / "EXTERNALLY-MANAGED").write_text("")
    monkeypatch.setattr(environment_checks.target_interpreter(), "in_virtualenv", True)

    check_externally_managed()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "site"
    info = root / "simple-2.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Name: simple\nVersion: 2.0\n")
    monkeypatch.setattr(sys, "path", [str(root)])
    monkeypatch.setenv("KPIP_NO_CACHE_DIR", "1")
    clear_installed_index()
    environment = dict(os.environ)

    yield root

    os.environ.clear()
    os.environ.update(environment)
    clear_installed_index()


def test_install_refuses_a_marked_environment_unless_told_to_break_it(
    site: Path, system_python: Path, tmp_path: Path
) -> None:
    (system_python / "EXTERNALLY-MANAGED").write_text("")

    with pytest.raises(ExternallyManagedEnvironment):
        run_install(["--no-index", "simple"])

    assert run_install(["--no-index", "--break-system-packages", "simple"]) == 0


def test_install_elsewhere_is_not_checked(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--target, --prefix and --root install where the marker cannot be
    looked for, and a dry run that reports changes nothing."""
    checked: list[bool] = []
    monkeypatch.setattr(
        install, "check_externally_managed", lambda: checked.append(True)
    )

    class Stop(Exception):
        pass

    def stop(*args: object, **kwargs: object) -> None:
        raise Stop

    monkeypatch.setattr(install, "InstallOutcome", stop)

    for arguments in (
        ["--target", "/t"],
        ["--prefix", "/p"],
        ["--root", "/r"],
        ["--dry-run", "--report", "-"],
    ):
        with pytest.raises(Stop):
            run_install(["--no-index", *arguments, "simple"])

    assert checked == []

    with pytest.raises(Stop):
        run_install(["--no-index", "simple"])

    assert checked == [True]


def test_uninstall_refuses_a_marked_environment(
    site: Path, system_python: Path
) -> None:
    (system_python / "EXTERNALLY-MANAGED").write_text("")

    with pytest.raises(ExternallyManagedEnvironment):
        uninstall.run_uninstall(["-y", "absent"])


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="no user ids on this platform")
def test_root_outside_a_virtual_environment_is_warned(
    system_python: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)

    with caplog.at_level(logging.WARNING):
        warn_if_run_as_root()

    assert "as the 'root' user" in caplog.text
    assert "--root-user-action" in caplog.text

    caplog.clear()
    monkeypatch.setattr(os, "getuid", lambda: 501)

    with caplog.at_level(logging.WARNING):
        warn_if_run_as_root()

    assert caplog.text == ""


def test_root_user_action_is_warn_unless_told_to_ignore() -> None:
    assert create_parser().parse_args(["x"]).root_user_action == "warn"
    assert (
        create_parser()
        .parse_args(["x", "--root-user-action", "ignore"])
        .root_user_action
        == "ignore"
    )


def test_build_requirements_the_environment_lacks_are_named(site: Path) -> None:
    check_build_requirements("/project", ["simple>=1", 'absent; sys_platform == "x"'])

    with pytest.raises(BuildError) as missing:
        check_build_requirements("/project", ["simple", "absent>=1", "gone"])

    assert str(missing.value) == (
        "Some build dependencies for /project are missing: 'absent>=1', 'gone'."
    )

    with pytest.raises(BuildError) as conflicting:
        check_build_requirements("/project", ["simple>=3", "absent"])

    assert str(conflicting.value) == (
        "Some build dependencies for /project conflict with the backend "
        "dependencies: simple 2.0 is incompatible with simple>=3."
    )


def test_no_clean_keeps_what_a_build_worked_in() -> None:
    with build_directory("kpip-test-build-") as removed:
        assert os.path.isdir(removed)

    assert not os.path.exists(removed)

    run_options.current.no_clean = True

    with build_directory("kpip-test-build-") as kept:
        pass

    assert os.path.isdir(kept)
    os.rmdir(kept)


def test_build_requirements_are_checked_in_the_build_interpreters_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiled, the build interpreter is never the Python running kpip:
    its own sys.path and markers are what the check reads."""
    import types

    from kpip.build import build_backend
    from kpip.core.packaging import default_environment

    other_site = tmp_path / "other-site"
    info = other_site / "simple-2.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Name: simple\nVersion: 2.0\n")
    markers = {**default_environment(), "sys_platform": "elsewhere"}
    other = types.SimpleNamespace(is_own=False, path=[str(other_site)], markers=markers)
    monkeypatch.setattr(build_backend, "build_interpreter", lambda: "/opt/python")
    monkeypatch.setattr(build_backend, "interpreter_at", lambda executable: other)
    clear_installed_index()

    check_build_requirements(
        "/project", ["simple>=1", 'absent; sys_platform != "elsewhere"']
    )

    with pytest.raises(BuildError, match="missing: 'gone'"):
        check_build_requirements("/project", ["simple", "gone"])


@pytest.fixture
def python_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A compiled kpip with no environment, its target the Python on PATH."""
    interpreter = types.SimpleNamespace(
        executable="/usr/bin/python3", in_virtualenv=False
    )
    monkeypatch.setattr(environment_checks, "is_compiled", lambda: True)
    monkeypatch.setattr(environment_checks, "active_environments", list)
    monkeypatch.setattr(environment_checks, "target_interpreter", lambda: interpreter)
    for name in ("KPIP_PYTHON", "KPIP_SYSTEM_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    return interpreter


def test_a_python_on_path_is_changed_only_with_system(python_on_path) -> None:
    """As uv: nobody chose it, and it is often the system's own."""
    with pytest.raises(CommandError, match="/usr/bin/python3 on PATH.*--system"):
        check_system_python(False)

    check_system_python(True)


def test_system_can_be_said_by_the_environment(
    python_on_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPIP_SYSTEM_PYTHON", "1")

    check_system_python(False)


def test_a_chosen_python_needs_no_system(
    python_on_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python_on_path.in_virtualenv = True
    check_system_python(False)

    python_on_path.in_virtualenv = False
    monkeypatch.setattr(
        environment_checks, "active_environments", lambda: [("conda", "/conda")]
    )
    check_system_python(False)

    monkeypatch.setattr(environment_checks, "active_environments", list)
    monkeypatch.setenv("KPIP_PYTHON", "/usr/bin/python3")
    check_system_python(False)


def test_install_refuses_a_python_on_path_before_writing(
    python_on_path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kpip.cli.main import main

    assert main(["install", "--no-index", "demo"]) != 0
    assert "pass --system" in capsys.readouterr().err
    assert main(["uninstall", "-y", "demo"]) != 0
    assert "pass --system" in capsys.readouterr().err
