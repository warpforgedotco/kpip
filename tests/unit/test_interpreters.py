"""The target interpreter: the Python kpip installs for."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from collections.abc import Iterator
from pathlib import Path

import pytest

from kpip._internal import interpreters
from kpip._internal.exceptions import CommandError
from kpip._internal.interpreters import facts
from kpip._internal.interpreters.facts import OwnInterpreter, probe


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No interpreter known yet, no environment activated, and a working
    directory with nothing above it that looks like one."""
    for name in ("VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "_CONDA_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(facts, "_probed", {})
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    interpreters.configure(None, str(tmp_path / "cache"))
    yield
    interpreters.configure(None, None)


def compiled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Act as the compiled kpip, which has no Python of its own."""
    monkeypatch.setattr(interpreters, "is_compiled", lambda: True)
    monkeypatch.setattr(facts, "is_compiled", lambda: True)


@pytest.fixture(scope="module")
def environment(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A virtual environment of the Python running the tests."""
    path = tmp_path_factory.mktemp("env") / "env"
    venv.create(path, with_pip=False, symlinks=os.name != "nt")
    return path


def environment_python(path: Path) -> str:
    return interpreters.identify(str(path))


def test_probing_this_python_finds_what_it_knows_of_itself() -> None:
    own = OwnInterpreter()
    probed = probe(sys.executable)

    assert probed.version == own.version
    assert probed.prefix == own.prefix
    assert probed.markers == own.markers
    assert [str(tag) for tag in probed.tags] == [str(tag) for tag in own.tags]
    assert probed.platforms == own.platforms
    assert probed.abis == own.abis
    assert probed.interpreter_name == own.interpreter_name
    assert probed.interpreter_version == own.interpreter_version
    assert probed.cache_tag == own.cache_tag
    assert probed.magic == own.magic
    # This process read its user base before the tests isolated HOME; the
    # probe reads the isolated one.
    for scheme in [name for name in own.scheme_names() if "user" not in name]:
        assert probed.get_paths(scheme) == own.get_paths(scheme), scheme
        variables = {"base": "/somewhere", "platbase": "/somewhere"}
        assert probed.get_paths(scheme, variables) == own.get_paths(
            scheme, variables
        ), scheme


def test_an_environment_is_probed_as_itself(environment: Path) -> None:
    probed = probe(environment_python(environment))

    assert probed.in_virtualenv
    assert os.path.samefile(probed.prefix, environment)
    purelib = probed.get_paths(probed.preferred_scheme("prefix"))["purelib"]
    assert Path(purelib).is_relative_to(probed.prefix)
    assert purelib in probed.path


def test_answers_are_kept_until_the_search_path_changes(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = environment_python(environment)
    first = probe(python)

    def no_probe(executable: str) -> dict[str, object]:
        raise AssertionError("probed again")

    monkeypatch.setattr(facts, "_run_probe", no_probe)
    monkeypatch.setattr(facts, "_probed", {})
    assert probe(python).facts == first.facts

    purelib = first.get_paths(first.preferred_scheme("prefix"))["purelib"]
    (Path(purelib) / "added.pth").write_text("")
    monkeypatch.setattr(facts, "_probed", {})
    with pytest.raises(AssertionError, match="probed again"):
        probe(python)


def test_a_shims_answer_is_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("a shell script shim")
    shim = tmp_path / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    probe(str(shim))

    calls = []
    run_probe = facts._run_probe

    def counting(executable: str) -> dict[str, object]:
        calls.append(executable)
        return run_probe(executable)

    monkeypatch.setattr(facts, "_run_probe", counting)
    monkeypatch.setattr(facts, "_probed", {})
    probe(str(shim))
    assert calls == [str(shim)]


def test_something_that_is_not_python_is_refused(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("a shell script")
    impostor = tmp_path / "python"
    impostor.write_text("#!/bin/sh\necho hello\n")
    impostor.chmod(0o755)
    with pytest.raises(CommandError, match="did not answer as one"):
        probe(str(impostor))


def test_identify_takes_an_interpreter_or_an_environment(
    environment: Path, tmp_path: Path
) -> None:
    python = environment_python(environment)
    assert interpreters.identify(python) == python
    assert Path(python).is_relative_to(environment)
    with pytest.raises(CommandError, match="Could not locate"):
        interpreters.identify(str(tmp_path / "missing"))


def test_from_source_kpip_installs_for_itself() -> None:
    target = interpreters.target_interpreter()
    assert target.is_own
    assert not target.is_bundled


def test_python_option_names_the_target(environment: Path) -> None:
    interpreters.configure(str(environment), None)
    target = interpreters.target_interpreter()
    assert not target.is_own
    assert os.path.samefile(target.prefix, environment)


def test_python_option_naming_this_python_reads_it_live() -> None:
    interpreters.configure(sys.executable, None)
    assert interpreters.target_interpreter().is_own


def test_compiled_kpip_installs_into_the_activated_environment(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled(monkeypatch)
    monkeypatch.setenv("VIRTUAL_ENV", str(environment))
    target = interpreters.target_interpreter()
    assert os.path.samefile(target.prefix, environment)


def test_compiled_kpip_finds_a_dot_venv_above_the_working_directory(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled(monkeypatch)
    project = tmp_path / "project"
    (project / "src" / "deep").mkdir(parents=True)
    (project / ".venv").symlink_to(environment, target_is_directory=True)
    monkeypatch.chdir(project / "src" / "deep")
    target = interpreters.target_interpreter()
    assert os.path.samefile(target.prefix, environment)


def test_an_activated_environment_without_python_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled(monkeypatch)
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "gone"))
    with pytest.raises(CommandError, match="VIRTUAL_ENV names"):
        interpreters.target_interpreter()


def test_compiled_kpip_falls_back_to_python_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled(monkeypatch)
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))
    found = interpreters.target_interpreter()
    assert not found.is_own
    assert found.version == tuple(sys.version_info[:3])


def test_with_no_python_at_all_only_resolving_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    target = interpreters.target_interpreter()
    assert target.is_bundled
    assert target.tags
    with pytest.raises(interpreters.NoTargetInterpreter):
        interpreters.installing_interpreter()


def test_with_no_python_nothing_is_installed_into_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip._internal.locations import get_scheme

    compiled(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(interpreters.NoTargetInterpreter):
        get_scheme("demo")
    with pytest.raises(interpreters.NoTargetInterpreter):
        get_scheme("demo", home=str(tmp_path / "target"))


def test_with_no_python_install_fails_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip._internal.commands import create_command

    compiled(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    command = create_command("install")

    worked = []
    monkeypatch.setattr(
        command, "get_requirements", lambda *args, **kwargs: worked.append(1)
    )
    assert command.main(["--target", str(tmp_path / "t"), "demo"]) != 0
    assert not worked


def test_conda_base_comes_after_a_dot_venv(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".venv").symlink_to(environment, target_is_directory=True)
    monkeypatch.chdir(project)
    base = tmp_path / "miniconda3"
    base.mkdir()
    monkeypatch.setenv("CONDA_PREFIX", str(base))
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")
    target = interpreters.target_interpreter()
    assert os.path.samefile(target.prefix, environment)


def test_the_probe_is_not_shadowed_by_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "work" / "json.py").write_text("raise SystemExit('shadowed')\n")
    assert probe(sys.executable).version == tuple(sys.version_info[:3])


def test_the_probe_runs_under_safe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    assert probe(sys.executable).path == [
        entry
        for entry in subprocess.run(
            [sys.executable, "-c", "import sys; print('\\n'.join(sys.path))"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if entry
    ]


def test_installs_go_where_the_target_installs(environment: Path) -> None:
    from kpip._internal.locations import get_scheme, get_site_packages
    from kpip._internal.utils.virtualenv import running_under_virtualenv

    interpreters.configure(str(environment), None)
    target = interpreters.target_interpreter()
    expected = target.get_paths(target.preferred_scheme("prefix"))

    scheme = get_scheme("demo")
    assert scheme.purelib == expected["purelib"]
    assert scheme.scripts == expected["scripts"]
    assert Path(scheme.headers).is_relative_to(environment)
    assert get_site_packages() == expected["purelib"]
    assert running_under_virtualenv()

    elsewhere = get_scheme("demo", prefix="/opt/demo")
    assert Path(elsewhere.purelib).is_relative_to("/opt/demo")
    assert f"python{target.major_minor}" in elsewhere.purelib


def test_externally_managed_is_the_targets(
    environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip._internal.exceptions import ExternallyManagedEnvironment
    from kpip._internal.utils.misc import check_externally_managed

    interpreters.configure(str(environment), None)
    target = interpreters.target_interpreter()
    # A virtual environment never is; its base can be.
    check_externally_managed()
    monkeypatch.setattr(target, "base_prefix", target.prefix)
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()
    (stdlib / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")
    monkeypatch.setattr(target, "stdlib", str(stdlib))
    with pytest.raises(ExternallyManagedEnvironment):
        check_externally_managed()


def test_local_means_inside_the_target_environment(environment: Path) -> None:
    from kpip._internal.utils.misc import is_local

    interpreters.configure(str(environment), None)
    prefix = interpreters.target_interpreter().prefix
    assert is_local(os.path.join(os.path.normcase(os.path.realpath(prefix)), "lib"))
    assert not is_local(os.path.realpath(os.path.dirname(sys.base_prefix)))


def test_what_is_installed_is_read_from_the_target(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from kpip._internal.metadata import get_default_environment, get_environment

    path = tmp_path_factory.mktemp("installed") / "env"
    venv.create(path, with_pip=False, symlinks=os.name != "nt")
    interpreters.configure(str(path), None)
    target = interpreters.target_interpreter()
    purelib = Path(target.get_paths(target.preferred_scheme("prefix"))["purelib"])
    dist_info = purelib / "only_here-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: only-here\nVersion: 1.0\n"
    )

    for environment in (get_default_environment(), get_environment(None)):
        names = {dist.canonical_name for dist in environment.iter_all_distributions()}
        assert names == {"only-here"}


@pytest.fixture
def other_python(monkeypatch: pytest.MonkeyPatch) -> facts.ProbedInterpreter:
    """A target that is not this Python: 3.11, on another platform."""
    probed = probe(sys.executable).facts
    other = facts.ProbedInterpreter(
        {
            **probed,
            "version": [3, 11, 9],
            "interpreter_version": "311",
            "abis": ["cp311"],
            "platforms": ["emscripten_3_1_58_wasm32"],
            "tags": [
                "cp311-cp311-emscripten_3_1_58_wasm32",
                "cp311-abi3-emscripten_3_1_58_wasm32",
                "py3-none-any",
            ],
            "markers": {
                **probed["markers"],
                "python_version": "3.11",
                "python_full_version": "3.11.9",
                "sys_platform": "emscripten",
            },
        }
    )
    monkeypatch.setattr(interpreters, "_target", other)
    return other


def test_wheel_tags_are_the_targets(other_python: facts.ProbedInterpreter) -> None:
    from kpip._internal.models.target_python import TargetPython
    from kpip._internal.utils.compatibility_tags import get_supported

    assert [str(tag) for tag in get_supported()] == [
        "cp311-cp311-emscripten_3_1_58_wasm32",
        "cp311-abi3-emscripten_3_1_58_wasm32",
        "py3-none-any",
    ]
    # What is given is used; the rest is the target's.
    linux = {str(tag) for tag in get_supported(platforms=["linux_x86_64"])}
    assert "cp311-cp311-linux_x86_64" in linux
    assert not any("emscripten" in tag for tag in linux)

    target_python = TargetPython()
    assert target_python.py_version_info == (3, 11, 9)
    assert str(target_python.get_sorted_tags()[0]) == (
        "cp311-cp311-emscripten_3_1_58_wasm32"
    )


def test_markers_and_requires_python_are_the_targets(
    other_python: facts.ProbedInterpreter,
) -> None:
    from kpip._internal.resolution.resolvelib.candidates import (
        RequiresPythonCandidate,
    )
    from kpip._vendor.packaging.markers import Marker

    assert Marker("python_version < '3.12'").evaluate()
    assert Marker("sys_platform == 'emscripten'").evaluate()
    assert str(RequiresPythonCandidate(None).version) == "3.11.9"


def test_scripts_run_with_the_target(environment: Path, tmp_path: Path) -> None:
    import zipfile

    from kpip._internal.locations import get_scheme
    from kpip._internal.operations.install.wheel import install_wheel

    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo.py", "def main():\n    pass\n")
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "demo-1.0.dist-info/entry_points.txt",
            "[console_scripts]\ndemo = demo:main\n",
        )
        archive.writestr("demo-1.0.data/scripts/raw", "#!python\nprint('raw')\n")
        archive.writestr("demo-1.0.dist-info/RECORD", "")

    interpreters.configure(str(environment), None)
    target = interpreters.target_interpreter()
    scheme = get_scheme("demo", home=str(tmp_path / "home"))
    install_wheel("demo", str(wheel), scheme, "demo==1.0", pycompile=False)

    scripts = Path(scheme.scripts)
    for name in ("demo", "raw"):
        first = (scripts / name).read_bytes().splitlines()[0].decode()
        assert target.executable in first, name
        assert sys.executable not in first or sys.executable in target.executable


def test_bytecode_is_written_by_the_target(
    other_python: facts.ProbedInterpreter, tmp_path: Path
) -> None:
    from kpip._internal.interpreters.bytecode import compile_with

    good = tmp_path / "good.py"
    good.write_text("VALUE = 1\n")
    bad = tmp_path / "bad.py"
    bad.write_text("def\n")
    compiled = compile_with(other_python, [str(good), str(bad)])
    assert [source for source, _ in compiled] == [str(good)]
    pyc = Path(compiled[0][1])
    assert pyc.is_file()
    assert pyc.read_bytes()[:4] == other_python.magic


def test_building_needs_a_python_but_requirements_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip._internal.build_env import NoOpBuildEnvironment

    compiled(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path))
    build_env = NoOpBuildEnvironment()
    with pytest.raises(interpreters.NoTargetInterpreter, match="to build with"):
        build_env.python_executable


def test_version_names_the_python_option_target(
    environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from kpip._internal.cli.main_parser import parse_command

    with pytest.raises(SystemExit):
        parse_command(["--python", str(environment), "--version"])
    target = interpreters.target_interpreter()
    assert f"(python {target.major_minor})" in capsys.readouterr().out
