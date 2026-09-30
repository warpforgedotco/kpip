"""The Python kpip installs for, and what it knows about it."""

from __future__ import annotations

import os
import sys
import sysconfig
import types
from pathlib import Path

import pytest
from kpip.core.errors import CommandError
from kpip.host import interpreter_facts
from kpip.host.interpreter_facts import (
    Interpreter,
    identify,
    own_interpreter,
    probe,
    target_interpreter,
)


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interpreter_facts, "_interpreters", {})
    for name in ("KPIP_PYTHON", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("scheme", sorted(sysconfig.get_scheme_names()))
def test_the_facts_answer_as_sysconfig_does(scheme: str) -> None:
    variables = {"base": "/x", "platbase": "/x"}

    interpreters = [own_interpreter()]
    if "user" not in scheme:
        # A user scheme's base comes from HOME, which each test gets its
        # own of; this process read the real one when it started.
        interpreters.append(probe(sys.executable))
    for interpreter in interpreters:
        assert interpreter.paths(scheme) == sysconfig.get_paths(scheme)
        assert interpreter.paths(scheme, variables) == sysconfig.get_paths(
            scheme, vars=variables
        )


def test_an_interpreted_kpip_installs_for_itself() -> None:
    interpreter = target_interpreter()

    assert interpreter.is_own
    assert interpreter.executable == sys.executable


def test_python_names_an_interpreter_or_an_environment(tmp_path: Path) -> None:
    """As pip's ``--python``: a file, or a directory holding one."""
    scripts = tmp_path / "env" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_text("")

    assert identify(str(python)) == str(python)
    assert identify(str(tmp_path / "env")) == str(python)
    with pytest.raises(CommandError, match="Could not locate Python interpreter"):
        identify(str(tmp_path / "missing"))


def test_python_is_probed_even_when_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []
    monkeypatch.setattr(interpreter_facts, "probe", probed.append)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KPIP_PYTHON", ".")

    target_interpreter()

    assert probed == [str(tmp_path / "bin" / "python")]


def test_a_compiled_kpip_installs_for_the_active_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []
    monkeypatch.setattr(interpreter_facts, "probe", probed.append)
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").write_text("")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))

    target_interpreter()

    assert probed == [str(tmp_path / "bin" / "python")]


def test_a_compiled_kpip_with_no_python_to_install_for_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    monkeypatch.setattr(interpreter_facts.shutil, "which", lambda name: None)

    with pytest.raises(CommandError, match="No Python interpreter to install for"):
        target_interpreter()


@pytest.mark.parametrize(
    "base_prefix, expected",
    [("/env", False), ("/usr", True)],
)
def test_an_interpreter_is_a_virtual_environments_when_its_prefixes_differ(
    base_prefix: str, expected: bool
) -> None:
    own = own_interpreter()
    facts = {name: getattr(own, name) for name in Interpreter.__slots__}
    facts.update(prefix="/env", base_prefix=base_prefix)

    assert Interpreter(facts).in_virtualenv is expected


def test_another_pythons_distributions_are_read_from_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This process's import finders speak only for the Python running kpip;
    a compiled kpip's is its own bundle, where nothing is installed."""
    from kpip.core import metadata

    site = tmp_path / "site-packages"
    info = site / "demo-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n")

    class Bundle:
        @staticmethod
        def find_distributions(context: object = None) -> list:
            return []

    monkeypatch.setattr(sys, "meta_path", [Bundle, *sys.meta_path])
    other = types.SimpleNamespace(is_own=False, path=[str(site)])
    monkeypatch.setattr(interpreter_facts, "target_interpreter", lambda: other)
    monkeypatch.setattr(metadata, "target_interpreter", lambda: other)

    names = [
        distribution.canonical_name
        for distribution in metadata.iter_installed_distributions()
    ]

    assert names == ["demo"]


def _other_python(prefix: Path) -> Interpreter:
    """This Python's facts, as if it lived at ``prefix``."""
    namespace: dict = {"__name__": "kpip_interpreter_probe"}
    exec(interpreter_facts.PROBE, namespace)  # noqa: S102
    facts = namespace["facts"]()
    facts["executable"] = str(prefix / "bin" / "python")
    facts["prefix"] = facts["base_prefix"] = str(prefix)
    for name in ("base", "platbase", "installed_base", "installed_platbase"):
        facts["config"][name] = str(prefix)
    return Interpreter(facts)


def test_another_pythons_distributions_are_local_to_its_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip.build import metadata

    inside = tmp_path / "env" / "site-packages"
    outside = tmp_path / "elsewhere"
    for site, name in ((inside, "inside"), (outside, "outside")):
        info = site / f"{name}-1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
        )
    other = types.SimpleNamespace(prefix=str(tmp_path / "env"))
    monkeypatch.setattr(metadata, "target_interpreter", lambda: other)

    store = metadata.InstalledDistributionStore(paths=[str(inside), str(outside)])
    names = [view.canonical_name for view in store.iter(local_only=True)]

    assert names == ["inside"]


def test_another_pythons_scripts_may_be_uninstalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip.install import uninstall

    other = _other_python(tmp_path / "env")
    monkeypatch.setattr(uninstall, "target_interpreter", lambda: other)
    scripts = os.path.normcase(os.path.realpath(tmp_path / "env" / "bin"))
    if os.name == "nt":
        scripts = os.path.normcase(os.path.realpath(tmp_path / "env" / "Scripts"))

    assert scripts in uninstall._script_directories(str(tmp_path / "unrelated"))


def test_another_pythons_egg_links_are_found_on_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip.build import metadata

    site = tmp_path / "site-packages"
    project = tmp_path / "project"
    info = project / "demo.egg-info"
    info.mkdir(parents=True)
    (info / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n")
    site.mkdir()
    (site / "demo.egg-link").write_text(f"{project}\n.\n")
    monkeypatch.setattr(metadata, "search_path", lambda: [str(site)])

    [view] = metadata.InstalledDistributionStore(paths=[str(project)]).iter()

    assert view.editable_project_location == str(project)
