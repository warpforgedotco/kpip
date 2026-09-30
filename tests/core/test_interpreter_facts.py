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
