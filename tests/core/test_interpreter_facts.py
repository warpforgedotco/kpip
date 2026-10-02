"""The Python kpip installs for, and what it knows about it."""

from __future__ import annotations

import os
import shutil
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
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interpreter_facts, "_interpreters", {})
    for name in (
        "KPIP_PYTHON",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "_CONDA_ROOT",
        "KPIP_SYSTEM_PYTHON",
    ):
        monkeypatch.delenv(name, raising=False)
    # Not the checkout's .venv, which a compiled kpip would install into.
    monkeypatch.chdir(tmp_path)


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
    monkeypatch.setattr(shutil, "which", lambda name: None)

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


def test_a_python_without_user_schemes_is_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MonolithPy, iOS and WASI have no user base, so sysconfig defines no
    # user scheme and refuses to name a preferred one.
    def get_preferred_scheme(key: str) -> str:
        if key == "user":
            raise ValueError(
                "'user' returned 'posix_user', which is not a valid scheme "
                "on this platform"
            )
        return "posix_prefix"

    monkeypatch.setattr(sysconfig, "get_preferred_scheme", get_preferred_scheme)
    namespace: dict = {"__name__": "kpip_interpreter_probe"}
    exec(interpreter_facts.PROBE, namespace)  # noqa: S102

    assert namespace["facts"]()["preferred"] == {
        "prefix": "posix_prefix",
        "home": "posix_prefix",
    }


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


def test_a_checkout_goes_under_the_target_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compiled, sys.prefix is kpip's own bundle: no place for a checkout."""
    from kpip.install import metadata

    fetched = tmp_path / "fetched"
    fetched.mkdir()
    (fetched / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    locator = types.SimpleNamespace(ensure_local=lambda url: str(fetched))
    monkeypatch.setattr(metadata, "ArtifactLocator", lambda: locator)
    monkeypatch.setattr(metadata, "release_checkout", lambda path: None)
    other = types.SimpleNamespace(prefix=str(tmp_path / "env"))
    monkeypatch.setattr(metadata, "target_interpreter", lambda: other)

    source, _, _ = metadata.prepare_editable_source(
        "git+https://example.invalid/demo.git#egg=demo", prepare_metadata=False
    )

    assert source == str(tmp_path / "env" / "src" / "demo")
    assert (tmp_path / "env" / "src" / "demo" / "pyproject.toml").is_file()


def test_the_site_configuration_is_the_target_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip.cli import config

    other = types.SimpleNamespace(prefix=str(tmp_path / "env"))
    monkeypatch.setattr(config, "target_interpreter", lambda **_: other)

    [site] = [
        location.path
        for location in config.config_locations()
        if location.kind == "site"
    ]

    assert site == str(tmp_path / "env" / config.CONFIG_BASENAME)


def test_a_compiled_kpip_names_its_binary_and_the_python_it_installs_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a path inside its bundle, nor the Python it was built with."""
    import kpip
    from kpip.cli import entrypoint

    binary = tmp_path / "kpip"
    binary.touch()
    other = types.SimpleNamespace(major_minor="3.11")
    monkeypatch.setattr(entrypoint, "own_binary", lambda: str(binary))
    monkeypatch.setattr(entrypoint, "target_interpreter", lambda **_: other)

    entrypoint.print_version()

    assert capsys.readouterr().out == (
        f"kpip {kpip.__version__} from {os.path.realpath(binary)} (python 3.11)\n"
    )


def test_a_python3_that_does_not_answer_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows, python3 is often the Microsoft Store's stub."""
    stub = tmp_path / "python3"
    stub.write_text("#!/bin/sh\nexit 9009\n")
    stub.chmod(0o755)
    real = tmp_path / "python"
    real.symlink_to(sys.executable)
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("KPIP_PYTHON", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert target_interpreter().version == tuple(sys.version_info[:3])


def test_a_windows_conda_environment_keeps_python_at_its_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "python.exe").write_text("")
    monkeypatch.setattr(interpreter_facts.os, "name", "nt")

    assert identify(str(tmp_path)) == str(tmp_path / "python.exe")


def test_a_projects_own_modules_do_not_stand_in_for_the_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed')\n")
    (tmp_path / "platform.py").write_text("raise SystemExit('shadowed')\n")
    monkeypatch.chdir(tmp_path)

    assert probe(sys.executable).version == tuple(sys.version_info[:3])


@pytest.fixture
def no_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A compiled kpip on a machine with no Python to install for."""
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX", "KPIP_PYTHON"):
        monkeypatch.delenv(variable, raising=False)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return tmp_path


def test_listing_a_path_needs_no_python(
    no_python: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kpip.cli.main import main

    site = no_python / "site"
    info = site / "demo-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n")

    assert main(["list", "--path", str(site), "--format=freeze"]) == 0
    assert main(["freeze", "--path", str(site)]) == 0
    assert capsys.readouterr().out.count("demo==1.0") == 2


def test_installing_without_a_python_fails_before_writing(
    no_python: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kpip.cli.main import main

    target = no_python / "target"

    assert main(["install", "--no-index", "--target", str(target), "demo"]) != 0
    assert "No Python interpreter to install for" in capsys.readouterr().err
    assert not target.exists()


def test_a_probe_is_kept_across_runs_until_its_search_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compiled kpip runs a Python for its facts on every command
    otherwise; a new .pth file in site-packages changes them."""
    import subprocess as subprocess_module

    monkeypatch.setenv("KPIP_CACHE_DIR", str(tmp_path / "cache"))
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(site))
    runs: list[object] = []
    real_run = subprocess_module.run

    def counting(*args: object, **kwargs: object) -> object:
        runs.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess_module, "run", counting)

    probe(sys.executable)
    monkeypatch.setattr(interpreter_facts, "_interpreters", {})
    probe(sys.executable)
    assert len(runs) == 1

    (site / "extra.pth").write_text("\n")
    os.utime(site, ns=(1, 1))
    monkeypatch.setattr(interpreter_facts, "_interpreters", {})
    probe(sys.executable)
    assert len(runs) == 2


def test_a_new_environments_facts_are_what_it_would_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remembered for the kpip that installs a build's requirements, which
    would otherwise run the environment's Python to learn them."""
    import json
    import subprocess as subprocess_module

    from kpip.install.build_env import isolated_venv

    monkeypatch.setenv("KPIP_CACHE_DIR", str(tmp_path / "cache"))
    env = tmp_path / "env"
    env.mkdir()
    # Probed as another Python is, not read in this process, whose site
    # module keeps the user site of the home it started in.
    creator = tmp_path / "python"
    creator.symlink_to(sys.executable)

    venv = isolated_venv.create_isolated_venv(
        str(env), python=str(creator), with_pip=False
    )
    remembered, _ = interpreter_facts._cached_facts(venv.python_executable)
    real = json.loads(
        subprocess_module.run(
            [
                venv.python_executable,
                "-c",
                interpreter_facts.SAFE_PATH + interpreter_facts.PROBE,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )

    assert remembered is not None
    for key in real.keys() - {"config", "path"}:
        assert remembered[key] == real[key], key
    for key in ("base", "platbase"):
        assert remembered["config"][key] == real["config"][key]
    sites = [entry for entry in real["path"] if "site-packages" in entry]
    assert [e for e in remembered["path"] if "site-packages" in e] == sites


def test_a_shims_answer_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pyenv shim never changes while the version it picks does."""
    import subprocess as subprocess_module

    monkeypatch.setenv("KPIP_CACHE_DIR", str(tmp_path / "cache"))
    shim = tmp_path / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    runs: list[object] = []
    real_run = subprocess_module.run
    monkeypatch.setattr(
        subprocess_module,
        "run",
        lambda *args, **kwargs: runs.append(args) or real_run(*args, **kwargs),
    )

    probe(str(shim))
    monkeypatch.setattr(interpreter_facts, "_interpreters", {})
    probe(str(shim))

    assert len(runs) == 2


def test_a_link_to_a_python_is_probed_as_itself(tmp_path: Path) -> None:
    """A virtual environment's python links to its base's."""
    link = tmp_path / "python"
    link.symlink_to(sys.executable)

    assert probe(str(link)) is not probe(sys.executable)


def test_the_probe_works_under_safe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONSAFEPATH", "1")

    assert probe(sys.executable).version == tuple(sys.version_info[:3])


def test_a_stale_virtual_env_matters_only_to_an_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    monkeypatch.delenv("KPIP_PYTHON", raising=False)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "gone"))
    monkeypatch.setenv("PATH", str(tmp_path))

    assert target_interpreter(installing=False) is not None
    with pytest.raises(CommandError, match="VIRTUAL_ENV names"):
        target_interpreter()


def test_version_says_when_there_is_no_python_to_install_for(
    no_python: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not the bundled Python's version, which nothing is installed for."""
    from kpip.cli import entrypoint

    entrypoint.print_version()

    assert capsys.readouterr().out.rstrip().endswith("(no Python found to install for)")


def test_version_names_an_active_environment_without_a_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a python on PATH, which an install would not use, nor "none
    found": the environment an install would refuse over."""
    from kpip.cli import entrypoint

    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    monkeypatch.delenv("KPIP_PYTHON", raising=False)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "gone"))
    link = tmp_path / "bin" / "python3"
    link.parent.mkdir()
    link.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(link.parent))

    entrypoint.print_version()

    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith(
            f"(VIRTUAL_ENV names {tmp_path / 'gone'}, which has no working Python)"
        )
    )


def environment(prefix: Path, *, conda: bool = False) -> Path:
    """An environment's directory with an interpreter where kpip looks."""
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").write_text("")
    if conda:
        (prefix / "conda-meta").mkdir()
    else:
        (prefix / "pyvenv.cfg").write_text("")
    return prefix / "bin" / "python"


class Probed(str):
    """A probed interpreter that is only its path, and no environment's."""

    in_virtualenv = False


@pytest.fixture
def found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A compiled kpip whose target is the path it would probe, with a
    python3 on PATH to fall back to."""
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    monkeypatch.setattr(interpreter_facts, "probe", Probed)
    on_path = tmp_path / "path" / "python3"
    on_path.parent.mkdir()
    on_path.write_text("")
    on_path.chmod(0o755)
    monkeypatch.setenv("PATH", str(on_path.parent))

    def target() -> str:
        monkeypatch.setattr(interpreter_facts, "_interpreters", {})
        return str(target_interpreter())

    target.on_path = str(on_path)  # type: ignore[attr-defined]
    return target


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_a_venv_in_a_directory_above_is_found_before_path(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As uv: a project's .venv, with nothing activated."""
    python = environment(tmp_path / "project" / ".venv")
    (tmp_path / "project" / "src" / "pkg").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "project" / "src" / "pkg")
    assert found() == str(python)

    monkeypatch.chdir(tmp_path)
    assert found() == found.on_path


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_the_environment_the_working_directory_is_inside_is_found(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = environment(tmp_path / "env")
    (tmp_path / "env" / "lib").mkdir()
    monkeypatch.chdir(tmp_path / "env" / "lib")

    assert found() == str(python)


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_a_dot_venv_that_is_no_virtual_environment_is_refused(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As uv: not looked past for one further up, nor taken."""
    environment(tmp_path / ".venv")
    environment(tmp_path / "project" / ".venv", conda=True)
    monkeypatch.chdir(tmp_path / "project")

    with pytest.raises(CommandError, match=r"project/\.venv is not a virtual env"):
        found()


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_activated_environments_come_before_a_dot_venv_and_conda_base_after(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dot_venv = environment(tmp_path / ".venv")
    named = environment(tmp_path / "conda" / "envs" / "work", conda=True)
    base = environment(tmp_path / "conda" / "base", conda=True)
    active = environment(tmp_path / "active")

    monkeypatch.setenv("CONDA_PREFIX", str(base.parent.parent))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    monkeypatch.setenv("_CONDA_ROOT", str(base.parent.parent))
    assert found() == str(dot_venv)

    monkeypatch.setenv("CONDA_PREFIX", str(named.parent.parent))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "work")
    assert found() == str(named)

    monkeypatch.setenv("VIRTUAL_ENV", str(active.parent.parent))
    assert found() == str(active)

    monkeypatch.delenv("VIRTUAL_ENV")
    shutil.rmtree(dot_venv.parent.parent)
    monkeypatch.setenv("CONDA_PREFIX", str(base.parent.parent))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    assert found() == str(base)


@pytest.mark.parametrize(
    "prefix,default_env,root,pixi,base",
    [
        ("/opt/conda", "base", None, False, True),
        ("/opt/conda", "base", "/opt/conda", False, True),
        ("/opt/conda/envs/work", "work", "/opt/conda", False, False),
        ("/work/env", "/work/env", None, False, False),
        ("/opt/conda/envs/work", None, None, False, False),
        ("/project/.pixi/envs/default", "default-env", None, True, False),
    ],
)
def test_conda_base_is_told_apart_as_uv_does(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    default_env: str | None,
    root: str | None,
    pixi: bool,
    base: bool,
) -> None:
    if pixi:
        prefix = str(tmp_path / prefix.lstrip("/"))
        (Path(prefix) / "conda-meta").mkdir(parents=True)
        (Path(prefix) / "conda-meta" / "pixi").write_text("")
    if default_env is not None:
        monkeypatch.setenv("CONDA_DEFAULT_ENV", default_env)
    if root is not None:
        monkeypatch.setenv("_CONDA_ROOT", root)

    assert interpreter_facts._is_conda_base(prefix) is base


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_an_install_names_a_dot_venv_without_a_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("")

    with pytest.raises(CommandError, match=r"the environment at .*\.venv, which"):
        target_interpreter()


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_system_passes_over_environments_for_conda_base_and_path(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As uv's --system: a system Python, not an environment."""
    environment(tmp_path / ".venv")
    monkeypatch.setenv(
        "VIRTUAL_ENV", str(environment(tmp_path / "active").parent.parent)
    )
    monkeypatch.setenv("KPIP_SYSTEM_PYTHON", "1")
    assert found() == found.on_path

    base = environment(tmp_path / "conda", conda=True)
    monkeypatch.setenv("CONDA_PREFIX", str(base.parent.parent))
    monkeypatch.setenv("_CONDA_ROOT", str(base.parent.parent))
    assert found() == str(base)


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_a_broken_dot_venv_is_not_looked_at_past_a_chosen_environment(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As uv, lazily: VIRTUAL_ENV wins before any .venv is looked for."""
    (tmp_path / ".venv").mkdir()
    active = environment(tmp_path / "active")
    monkeypatch.setenv("VIRTUAL_ENV", str(active.parent.parent))

    assert found() == str(active)


@pytest.mark.skipif(os.name == "nt", reason="environments keep python in bin")
def test_a_broken_dot_venv_does_not_stop_what_installs_nothing(
    found, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".venv").mkdir()
    monkeypatch.setattr(interpreter_facts, "_interpreters", {})

    assert str(target_interpreter(installing=False)) == found.on_path


@pytest.mark.skipif(os.name == "nt", reason="fake interpreters are files in bin")
def test_system_passes_over_a_virtual_environments_python_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As uv's --system: the activated environment's bin is first on PATH."""
    venv_bin = tmp_path / "venv" / "bin"
    system_bin = tmp_path / "system" / "bin"
    for directory in (venv_bin, system_bin):
        directory.mkdir(parents=True)
        (directory / "python3").write_text("")
        (directory / "python3").chmod(0o755)
    monkeypatch.setattr(interpreter_facts, "is_compiled", lambda: True)
    monkeypatch.setattr(
        interpreter_facts,
        "probe",
        lambda path: types.SimpleNamespace(
            executable=path, in_virtualenv=path.startswith(str(venv_bin))
        ),
    )
    monkeypatch.setenv("PATH", f"{venv_bin}{os.pathsep}{system_bin}")
    monkeypatch.setenv("KPIP_SYSTEM_PYTHON", "1")

    assert target_interpreter().executable == str(system_bin / "python3")
