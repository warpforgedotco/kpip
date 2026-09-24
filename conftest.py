from __future__ import annotations

import compileall
import contextlib
import errno
import fnmatch
import http.server
import importlib.metadata
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from textwrap import dedent
from typing import Any, AnyStr, ClassVar
from unittest.mock import patch
from zipfile import ZipFile

if sys.version_info < (3, 11):
    from kpip._vendor import tomli

    sys.modules.setdefault("tomllib", tomli)

import pytest
from filelock import FileLock

sys.path.insert(0, str(Path(__file__).parent / "tests/cli"))

from _pytest.config import Config

from _pytest.config.argparsing import Parser
from kpip_test_support import (
    DATA_DIR,
    SRC_DIR,
    CertFactory,
    KpipTestEnvironment,
    InMemoryKpip,
    ScriptFactory,
    TestData,
)
from kpip_test_support.server import MockServer, make_mock_server, patch_getfqdn
from kpip_test_support.venv import VirtualEnvironment, VirtualEnvironmentType
from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile

from kpip.core.temp_dir import global_tempdir_manager

ZIPAPP_PYC_BLOCKLIST = [
    "kpip/__kpip-runner__.py",
]


def pytest_addoption(parser: Parser) -> None:
    parser.addoption(
        "--keep-tmpdir",
        action="store_true",
        default=False,
        help="keep temporary test directories",
    )

    parser.addoption(
        "--resolver",
        action="store",
        default="resolvelib",
        choices=["resolvelib", "legacy"],
        help="use given resolver in tests",
    )
    parser.addoption(
        "--use-venv",
        action="store_true",
        default=False,
        help="use venv for virtual environment creation",
    )
    parser.addoption(
        "--run-search",
        action="store_true",
        default=False,
        help="run 'kpip search' tests",
    )
    parser.addoption(
        "--proxy",
        action="store",
        default=None,
        help="use given proxy in session network tests",
    )
    parser.addoption(
        "--use-zipapp",
        action="store_true",
        default=False,
        help="use a zipapp when running kpip in tests",
    )
    parser.addoption(
        "--num-test-groups",
        action="store",
        type=int,
        default=None,
        help="split collected tests into this many groups, for parallel CI shards",
    )
    parser.addoption(
        "--test-group",
        action="store",
        type=int,
        default=None,
        help="run only the given 1-based group (requires --num-test-groups)",
    )


def pytest_configure(config: Config) -> None:
    """Make pytest's numbered-directory cleanup tolerate transient races."""
    from _pytest import pathlib

    original = pathlib.on_rm_rf_error
    if getattr(original, "_kpip_retry_enotempty", False):
        return

    def on_rm_rf_error(
        func: Callable[..., Any] | None,
        path: str,
        excinfo: BaseException | tuple[type[BaseException], BaseException, Any],
        *,
        start_path: Path,
    ) -> bool:
        exc = excinfo if isinstance(excinfo, BaseException) else excinfo[1]
        if getattr(exc, "errno", None) == errno.ENOTEMPTY and func is not None:
            for attempt in range(5):
                if attempt:
                    time.sleep(0.01 * 2 ** (attempt - 1))
                try:
                    func(path)
                    return True
                except OSError:
                    pass
            return True
        return original(func, path, excinfo, start_path=start_path)

    on_rm_rf_error._kpip_retry_enotempty = True  # type: ignore[attr-defined]
    pathlib.on_rm_rf_error = on_rm_rf_error


def pytest_collection_modifyitems(config: Config, items: list[pytest.Function]) -> None:
    for item in items:
        if not hasattr(item, "module"):
            continue

        if item.get_closest_marker("search") and not config.getoption("--run-search"):
            item.add_marker(pytest.mark.skip("kpip search test skipped"))

        if item.get_closest_marker("network") is not None:
            item.add_marker(pytest.mark.enable_socket)

        if "CI" in os.environ and item.get_closest_marker("network") is not None:
            item.add_marker(pytest.mark.flaky(reruns=3, reruns_delay=2))

        if (
            item.get_closest_marker("incompatible_with_venv")
            and sys.prefix != sys.base_prefix
        ):
            item.add_marker(pytest.mark.skip("Incompatible with venv"))

        module_file = item.module.__file__
        module_path = Path(
            os.path.relpath(module_file, os.path.commonpath([__file__, module_file])),
        ).as_posix()

        module_root_dir = Path(module_path).parts[0]
        if module_path.startswith(("tests/cli/functional", "tests/cli/integration")):
            item.add_marker(pytest.mark.integration)
        elif module_path.startswith("tests/cli/migrated"):
            item.add_marker(pytest.mark.unit)
            if "script" in item.fixturenames:
                raise RuntimeError(
                    "Cannot use the ``script`` funcarg in a migrated unit test: "
                    f"(filename = {module_path}, item = {item})",
                )
        elif (
            len(Path(module_path).parts) >= 2
            and Path(module_path).parts[0] == "tests"
            and Path(module_path).parts[1]
            in {
                "cli",
                "core",
                "build",
                "index",
                "resolution",
                "install",
                "network",
                "platform",
                "vcs",
            }
        ):
            item.add_marker(pytest.mark.unit)
            if "script" in item.fixturenames:
                raise RuntimeError(
                    "Cannot use the ``script`` funcarg in a package unit test: "
                    f"(filename = {module_path}, item = {item})",
                )
        elif module_root_dir.startswith(("functional", "integration", "lib")):
            item.add_marker(pytest.mark.integration)
        elif module_root_dir.startswith("unit"):
            item.add_marker(pytest.mark.unit)

            if "script" in item.fixturenames:
                raise RuntimeError(
                    "Cannot use the ``script`` funcarg in a unit test: "
                    f"(filename = {module_path}, item = {item})",
                )
        elif module_root_dir in {
            "core",
            "build",
            "index",
            "resolution",
            "install",
            "network",
            "platform",
            "vcs",
            "cli",
        }:
            item.add_marker(pytest.mark.unit)
            if "script" in item.fixturenames:
                raise RuntimeError(
                    "Cannot use the ``script`` funcarg in a package unit test: "
                    f"(filename = {module_path}, item = {item})",
                )
        elif (
            module_path.startswith("tests/benchmarks/")
            or module_path.startswith("scripts/benchmark/tests/")
            or module_path == "tests/test_workspace_boundaries.py"
        ):
            item.add_marker(pytest.mark.unit)
        else:
            raise RuntimeError(f"Unknown test type (filename = {module_path})")

    shard_collected_items(config, items)


def shard_collected_items(config: Config, items: list[pytest.Function]) -> None:
    """Keep only the tests belonging to the configured CI shard.

    Tests are assigned to a group by a stable hash of their node id, which keeps
    the groups balanced by count and deterministic across xdist workers (so each
    worker collects an identical subset). This lets CI run the suite across
    several runners in parallel without overlapping work.
    """
    num_groups = config.getoption("--num-test-groups")
    group = config.getoption("--test-group")
    if num_groups is None and group is None:
        return
    if num_groups is None or group is None:
        raise pytest.UsageError(
            "--num-test-groups and --test-group must be supplied together",
        )
    if num_groups < 1 or not 1 <= group <= num_groups:
        raise pytest.UsageError(
            f"--test-group must be between 1 and --num-test-groups ({num_groups})",
        )

    selected: list[pytest.Function] = []
    deselected: list[pytest.Function] = []
    for item in items:
        shard = zlib.crc32(item.nodeid.encode("utf-8")) % num_groups
        if shard == group - 1:
            selected.append(item)
        else:
            deselected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


@pytest.fixture(scope="session", autouse=True)
def resolver_variant(request: pytest.FixtureRequest) -> Iterator[str]:
    """Set environment variable to make kpip default to the correct resolver."""
    resolver = request.config.getoption("--resolver")

    features = set(os.environ.get("KPIP_USE_FEATURE", "").split())
    deprecated_features = set(os.environ.get("KPIP_USE_DEPRECATED", "").split())

    if resolver == "legacy":
        deprecated_features.add("legacy-resolver")
    else:
        deprecated_features.discard("legacy-resolver")

    env = {
        "KPIP_USE_FEATURE": " ".join(features),
        "KPIP_USE_DEPRECATED": " ".join(deprecated_features),
    }
    with patch.dict(os.environ, env):
        yield resolver


@pytest.fixture(scope="session")
def tmpdir_factory(tmp_path_factory: pytest.TempPathFactory) -> pytest.TempPathFactory:
    """Override Pytest's ``tmpdir_factory`` with our pathlib implementation.

    This prevents misuse of this fixture.
    """
    return tmp_path_factory


@pytest.fixture
def tmpdir(tmp_path: Path) -> Path:
    """Override Pytest's ``tmpdir`` with our pathlib implementation.

    This prevents misuse of this fixture.
    """
    return tmp_path


@pytest.fixture(autouse=True)
def isolate(tmpdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate our tests so that things like global configuration files and the
    like do not affect our test results.

    We use an autouse function scoped fixture because we want to ensure that
    every test has it's own isolated home directory.
    """

    # --refresh sets a process-wide cut-off; one command's must not outlive it.
    monkeypatch.setattr("kpip.core.expiry._refreshed_since", float("-inf"))

    home_dir = os.path.join(str(tmpdir), "home")
    os.makedirs(home_dir)

    fake_root = os.path.join(str(tmpdir), "fake-root")
    os.makedirs(fake_root)

    if sys.platform == "win32":
        home_drive, home_path = os.path.splitdrive(home_dir)
        monkeypatch.setenv("USERPROFILE", home_dir)
        monkeypatch.setenv("HOMEDRIVE", home_drive)
        monkeypatch.setenv("HOMEPATH", home_path)
        for env_var, sub_path in (
            ("APPDATA", "AppData/Roaming"),
            ("LOCALAPPDATA", "AppData/Local"),
        ):
            path = os.path.join(home_dir, *sub_path.split("/"))
            monkeypatch.setenv(env_var, path)
            os.makedirs(path)
    else:
        monkeypatch.setenv("HOME", home_dir)
        monkeypatch.setenv(
            "XDG_DATA_HOME",
            os.path.join(
                home_dir,
                ".local",
                "share",
            ),
        )
        monkeypatch.setenv(
            "XDG_CONFIG_HOME",
            os.path.join(
                home_dir,
                ".config",
            ),
        )
        monkeypatch.setenv("XDG_CACHE_HOME", os.path.join(home_dir, ".cache"))
        monkeypatch.setenv(
            "XDG_RUNTIME_DIR",
            os.path.join(
                home_dir,
                ".runtime",
            ),
        )
        monkeypatch.setenv(
            "XDG_DATA_DIRS",
            os.pathsep.join(
                [
                    os.path.join(fake_root, "usr", "local", "share"),
                    os.path.join(fake_root, "usr", "share"),
                ],
            ),
        )
        monkeypatch.setenv(
            "XDG_CONFIG_DIRS",
            os.path.join(
                fake_root,
                "etc",
                "xdg",
            ),
        )

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kpip")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "distutils-sig@python.org")
    monkeypatch.delenv("KPIP_NO_PARTIAL_CLONE_FOR_BROKEN_GIT_SERVER", False)

    monkeypatch.setenv("KPIP_DISABLE_KPIP_VERSION_CHECK", "true")

    monkeypatch.delenv("KPIP_BUILD_TRACKER", False)

    monkeypatch.delenv("FORCE_COLOR", False)
    monkeypatch.delenv("NO_COLOR", False)

    os.makedirs(os.path.join(home_dir, ".config", "git"))
    with open(os.path.join(home_dir, ".config", "git", "config"), "wb") as fp:
        fp.write(b"[user]\n\tname = kpip\n\temail = distutils-sig@python.org\n")


@pytest.fixture(autouse=True)
def scoped_global_tempdir_manager(request: pytest.FixtureRequest) -> Iterator[None]:
    """Make unit tests with globally-managed tempdirs easier

    Each test function gets its own individual scope for globally-managed
    temporary directories in the application.
    """
    if "no_auto_tempdir_manager" in request.keywords:
        ctx: Callable[[], AbstractContextManager[None]] = contextlib.nullcontext
    else:
        ctx = global_tempdir_manager

    with ctx():
        yield


@pytest.fixture(scope="session")
def kpip_src(tmpdir_factory: pytest.TempPathFactory) -> Path:
    def not_code_files_and_folders(path: str, names: list[str]) -> Iterable[str]:
        if os.path.samefile(path, SRC_DIR):
            folders = {
                name for name in names if os.path.isdir(os.path.join(path, name))
            }
            to_ignore = folders - {"src"}
            if ".git" in names:
                to_ignore.add(".git")
            return to_ignore

        ignored = set()
        for pattern in ("__pycache__", "*.pyc", "kpip.egg-info"):
            ignored.update(fnmatch.filter(names, pattern))
        return ignored

    kpip_src = tmpdir_factory.mktemp("kpip_src").joinpath("kpip_src")
    shutil.copytree(
        SRC_DIR,
        kpip_src.resolve(),
        ignore=not_code_files_and_folders,
    )
    return kpip_src


@pytest.fixture(scope="session")
def kpip_editable_parts(
    kpip_src: Path,
    tmpdir_factory: pytest.TempPathFactory,
) -> tuple[Path, ...]:
    kpip_editable = tmpdir_factory.mktemp("kpip") / "kpip"
    shutil.copytree(kpip_src, kpip_editable, symlinks=True)
    assert compileall.compile_dir(
        kpip_editable,
        quiet=1,
    )
    kpip_self_install_path = tmpdir_factory.mktemp("kpip_self_install")
    shutil.copytree(SRC_DIR / "src" / "kpip", kpip_self_install_path / "kpip")
    lock_path = Path(tempfile.gettempdir()) / "kpip-tests-editable-install.lock"
    with FileLock(lock_path):
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "kpip",
                "install",
                "--target",
                kpip_self_install_path,
                "--no-deps",
                "-e",
                kpip_editable,
            ],
        )
    pth = next(kpip_self_install_path.glob("*kpip*.pth"))
    pth.write_text(
        pth.read_text(encoding="utf-8")
        + os.linesep
        + str(kpip_self_install_path.resolve()),
        encoding="utf-8",
    )
    dist_info = next(kpip_self_install_path.glob("*.dist-info"))
    return (pth, dist_info)


def common_wheel_editable_install(
    tmpdir_factory: pytest.TempPathFactory,
    common_wheels: Path,
    package: str,
) -> Path:
    wheel_candidates = list(common_wheels.glob(f"{package}-*.whl"))
    assert len(wheel_candidates) == 1, (
        f"Missing wheels in {common_wheels}, expected 1 got '{wheel_candidates}'."
        " Are the common test wheels available?"
    )
    install_dir = tmpdir_factory.mktemp(package) / "install"
    lib_install_dir = install_dir / "lib"
    bin_install_dir = install_dir / "bin"
    with WheelFile.open(wheel_candidates[0]) as source:
        install(
            source,
            SchemeDictionaryDestination(
                {
                    "purelib": os.fspath(lib_install_dir),
                    "platlib": os.fspath(lib_install_dir),
                    "scripts": os.fspath(bin_install_dir),
                },
                interpreter=sys.executable,
                script_kind="posix",
                bytecode_optimization_levels=[0],
            ),
            additional_metadata={},
        )
    if bin_install_dir.exists():
        shutil.rmtree(bin_install_dir)
    return lib_install_dir


@pytest.fixture(scope="session")
def setuptools_install(
    tmpdir_factory: pytest.TempPathFactory,
    common_wheels: Path,
) -> Path:
    return common_wheel_editable_install(tmpdir_factory, common_wheels, "setuptools")


@pytest.fixture(scope="session")
def coverage_install(tmpdir_factory: pytest.TempPathFactory) -> Path:
    """Expose the test runner's platform-compatible coverage installation."""
    distribution = importlib.metadata.distribution("coverage")
    install_dir = tmpdir_factory.mktemp("coverage") / "install"
    for file in distribution.files or ():
        source = Path(str(distribution.locate_file(file)))
        destination = install_dir / str(file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for wheel_metadata in install_dir.glob("*.dist-info/WHEEL"):
        contents = wheel_metadata.read_text(encoding="utf-8")
        contents = "\n".join(
            "Tag: py3-none-any" if line.startswith("Tag:") else line
            for line in contents.splitlines()
        )
        wheel_metadata.write_text(contents + "\n", encoding="utf-8")
    return install_dir


@pytest.fixture(scope="session")
def socket_install(tmpdir_factory: pytest.TempPathFactory, common_wheels: Path) -> Path:
    lib_dir = common_wheel_editable_install(
        tmpdir_factory,
        common_wheels,
        "pytest_subket",
    )
    dist_info = next(lib_dir.glob("*.dist-info"))
    shutil.rmtree(dist_info)
    return lib_dir


def install_pth_link(
    venv: VirtualEnvironment,
    project_name: str,
    lib_dir: Path,
) -> None:
    venv.site.joinpath(f"_kpip_testsuite_{project_name}.pth").write_text(
        str(lib_dir.resolve()),
        encoding="utf-8",
    )


@pytest.fixture(scope="session")
def virtualenv_template(
    request: pytest.FixtureRequest,
    tmpdir_factory: pytest.TempPathFactory,
    kpip_src: Path,
    kpip_editable_parts: tuple[Path, ...],
    setuptools_install: Path,
    coverage_install: Path,
    socket_install: Path,
) -> VirtualEnvironment:
    venv_type: VirtualEnvironmentType
    if request.config.getoption("--use-venv"):
        venv_type = "venv"
    else:
        venv_type = "virtualenv"

    tmpdir = tmpdir_factory.mktemp("virtualenv")
    venv = VirtualEnvironment(tmpdir.joinpath("venv_orig"), venv_type=venv_type)

    install_pth_link(venv, "setuptools", setuptools_install)
    install_pth_link(venv, "pytest_subket", socket_install)
    dependency_root = tmpdir_factory.mktemp("kpip_test_dependencies")
    for name, package_names in {
        "msgpack": ("msgpack",),
        "packaging": ("packaging",),
        "pyproject-hooks": ("pyproject_hooks",),
    }.items():
        distribution = importlib.metadata.distribution(name)
        for package_name in package_names:
            package_path = Path(distribution.locate_file(package_name)).resolve()
            if package_path.exists():
                (dependency_root / package_name).symlink_to(
                    package_path,
                    target_is_directory=package_path.is_dir(),
                )
    venv.site.joinpath("_kpip_testsuite_dependencies.pth").write_text(
        str(dependency_root),
        encoding="utf-8",
    )
    with open(venv.site / "pytest_socket.pth", "w") as f:
        f.write(socket_install.joinpath("pytest_socket.pth").read_text())

    pth, dist_info = kpip_editable_parts

    shutil.copy(pth, venv.site)
    shutil.copytree(
        dist_info,
        venv.site / dist_info.name,
        dirs_exist_ok=True,
        symlinks=True,
    )
    venv.site.joinpath("easy-install.pth").touch()

    if request.config.getoption("--cov"):
        install_pth_link(venv, "coverage", coverage_install)
        with open(venv.site / "zz-coverage-helper.pth", "a") as f:
            f.write("import coverage; coverage.process_startup()")

    for exe in os.listdir(venv.bin):
        if not exe.startswith(("python", "libpy")):
            (venv.bin / exe).unlink()

    venv_template = tmpdir / "venv_template"
    venv.move(venv_template)
    return venv


@pytest.fixture(scope="session")
def virtualenv_factory(
    virtualenv_template: VirtualEnvironment,
) -> Callable[[Path], VirtualEnvironment]:
    def factory(tmpdir: Path) -> VirtualEnvironment:
        return VirtualEnvironment(tmpdir, virtualenv_template)

    return factory


@pytest.fixture
def virtualenv(
    virtualenv_factory: Callable[[Path], VirtualEnvironment],
    tmpdir: Path,
) -> VirtualEnvironment:
    """Return a virtual environment which is unique to each test function
    invocation created inside of a sub directory of the test function's
    temporary directory. The returned object is a
    ``kpip_test_support.venv.VirtualEnvironment`` object.
    """
    return virtualenv_factory(tmpdir.joinpath("workspace", "venv"))


@pytest.fixture(scope="session")
def script_factory(
    virtualenv_factory: Callable[[Path], VirtualEnvironment],
    deprecated_python: bool,
    zipapp: str | None,
) -> ScriptFactory:
    def factory(
        tmpdir: Path,
        virtualenv: VirtualEnvironment | None = None,
        environ: dict[AnyStr, AnyStr] | None = None,
    ) -> KpipTestEnvironment:
        kwargs = {}
        if environ:
            kwargs["environ"] = environ
        if virtualenv is None:
            virtualenv = virtualenv_factory(tmpdir.joinpath("venv"))
        return KpipTestEnvironment(
            tmpdir,
            virtualenv=virtualenv,
            ignore_hidden=False,
            start_clear=False,
            capture_temp=True,
            assert_no_temp=True,
            kpip_expect_warning=deprecated_python,
            zipapp=zipapp,
            **kwargs,
        )

    return factory


ZIPAPP_MAIN = """\
#!/usr/bin/env python

import os
import runpy
import sys

lib = os.path.join(os.path.dirname(__file__), "lib")
sys.path.insert(0, lib)

runpy.run_module("kpip", run_name="__main__")
"""


def make_zipapp_from_pip(kpip_src: Path, zipapp_path: Path) -> None:
    src_dir = kpip_src / "src"
    with zipapp_path.open("wb") as zipapp_file:
        zipapp_file.write(b"#!/usr/bin/env python\n")
        with ZipFile(zipapp_file, "w") as zipapp:
            for kpip_file in src_dir.rglob("*"):
                rel_name = kpip_file.relative_to(src_dir)
                if (
                    sys.implementation.name == "cpython"
                    and kpip_file.suffix == ".py"
                    and str(rel_name) not in ZIPAPP_PYC_BLOCKLIST
                ):
                    pyc_path = kpip_file.with_suffix(".pyc")
                    py_compile.compile(str(kpip_file), str(pyc_path), doraise=True)
                    kpip_file = pyc_path
                    rel_name = rel_name.with_suffix(".pyc")
                zipapp.write(kpip_file, arcname=f"lib/{rel_name}")
            zipapp.writestr("__main__.py", ZIPAPP_MAIN)


@pytest.fixture(scope="session")
def zipapp(
    request: pytest.FixtureRequest,
    kpip_src: Path,
    tmpdir_factory: pytest.TempPathFactory,
) -> str | None:
    """If the user requested for kpip to be run from a zipapp, build that zipapp
    and return its location. If the user didn't request a zipapp, return None.

    This fixture is session scoped, so the zipapp will only be created once.
    """
    if not request.config.getoption("--use-zipapp"):
        return None

    temp_location = tmpdir_factory.mktemp("zipapp")
    kpip_src_copy = temp_location / "kpip-src"
    shutil.copytree(kpip_src, kpip_src_copy)
    pyz_file = temp_location / "kpip.pyz"
    make_zipapp_from_pip(kpip_src_copy, pyz_file)
    return str(pyz_file)


@pytest.fixture
def script(
    request: pytest.FixtureRequest,
    tmpdir: Path,
    virtualenv: VirtualEnvironment,
    script_factory: ScriptFactory,
) -> KpipTestEnvironment:
    """Return a KpipTestEnvironment which is unique to each test function and
    will execute all commands inside of the unique virtual environment for this
    test function. The returned object is a
    ``kpip_test_support.KpipTestEnvironment``.
    """
    return script_factory(tmpdir.joinpath("workspace"), virtualenv)


@pytest.fixture(scope="session")
def common_wheels() -> Path:
    """Provide a directory with latest setuptools and wheel wheels"""
    return DATA_DIR.joinpath("common_wheels")


@pytest.fixture(scope="session")
def shared_data(tmpdir_factory: pytest.TempPathFactory) -> TestData:
    return TestData.copy(tmpdir_factory.mktemp("data"))


@pytest.fixture
def data(tmpdir: Path) -> TestData:
    return TestData.copy(tmpdir.joinpath("data"))


@pytest.fixture
def in_memory_pip() -> InMemoryKpip:
    return InMemoryKpip()


@pytest.fixture(scope="session")
def deprecated_python() -> bool:
    """Used to indicate whether kpip deprecated this Python version"""
    return sys.version_info[:2] in []


@pytest.fixture(scope="session")
def cert_factory(tmpdir_factory: pytest.TempPathFactory) -> CertFactory:
    from kpip_test_support.certs import make_tls_cert, serialize_cert, serialize_key

    def factory() -> str:
        """Returns path to cert/key file."""
        output_path = tmpdir_factory.mktemp("certs") / "cert.pem"
        cert, key = make_tls_cert("localhost")
        with open(str(output_path), "wb") as f:
            f.write(serialize_cert(cert))
            f.write(serialize_key(key))

        return str(output_path)

    return factory


@pytest.fixture
def mock_server() -> Iterator[MockServer]:
    server = make_mock_server()
    test_server = MockServer(server)
    with test_server.context:
        yield test_server


@pytest.fixture
def proxy(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("proxy")


@pytest.fixture
def enable_user_site(virtualenv: VirtualEnvironment) -> None:
    virtualenv.user_site_packages = True


class MetadataKind(Enum):
    """All the types of values we might be provided for the data-dist-info-metadata
    attribute from PEP 658.
    """

    No = "none"
    Unhashed = "unhashed"
    Sha256 = "sha256"
    WrongHash = "wrong-hash"
    NoFile = "no-file"


@dataclass(frozen=True)
class FakePackage:
    """Mock package structure used to generate a PyPI repository.

    FakePackage name and version should correspond to sdists (.tar.gz files) in our test
    data.
    """

    name: str
    version: str
    filename: str
    metadata: MetadataKind
    requires_dist: tuple[str, ...] = ()
    provides_extra: tuple[str, ...] = ()
    metadata_name: str | None = None

    def metadata_filename(self) -> str:
        """This is specified by PEP 658."""
        return f"{self.filename}.metadata"

    def generate_additional_tag(self) -> str:
        """This gets injected into the <a> tag in the generated PyPI index page for this
        package.
        """
        if self.metadata == MetadataKind.No:
            return ""
        if self.metadata in [MetadataKind.Unhashed, MetadataKind.NoFile]:
            return 'data-dist-info-metadata="true"'
        if self.metadata == MetadataKind.WrongHash:
            return 'data-dist-info-metadata="sha256=WRONG-HASH"'
        assert self.metadata == MetadataKind.Sha256
        checksum = sha256(self.generate_metadata()).hexdigest()
        return f'data-dist-info-metadata="sha256={checksum}"'

    def generate_metadata(self) -> bytes:
        """This is written to `self.metadata_filename()` and will override the actual
        dist's METADATA, unless `self.metadata == MetadataKind.NoFile`.
        """
        lines = [
            "Metadata-Version: 2.1",
            f"Name: {self.metadata_name or self.name}",
            f"Version: {self.version}",
        ]
        lines.extend(f"Requires-Dist: {entry}" for entry in self.requires_dist)
        lines.extend(f"Provides-Extra: {extra}" for extra in self.provides_extra)
        return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture(scope="session")
def fake_packages() -> dict[str, list[FakePackage]]:
    """The package database we generate for testing PEP 658 support."""
    return {
        "simple": [
            FakePackage("simple", "1.0", "simple-1.0.tar.gz", MetadataKind.Sha256),
            FakePackage("simple", "2.0", "simple-2.0.tar.gz", MetadataKind.No),
            FakePackage("simple", "3.0", "simple-3.0.tar.gz", MetadataKind.WrongHash),
        ],
        "simple2": [
            FakePackage(
                "simple2",
                "1.0",
                "simple2-1.0.tar.gz",
                MetadataKind.Unhashed,
                ("simple==1.0",),
            ),
            FakePackage("simple2", "2.0", "simple2-2.0.tar.gz", MetadataKind.NoFile),
            FakePackage(
                "simple2",
                "3.0",
                "simple2-3.0.tar.gz",
                MetadataKind.Sha256,
                metadata_name="not-simple2",
            ),
        ],
        "colander": [
            FakePackage(
                "colander",
                "0.9.9",
                "colander-0.9.9-py2.py3-none-any.whl",
                MetadataKind.No,
            ),
        ],
        "compilewheel": [
            FakePackage(
                "compilewheel",
                "1.0",
                "compilewheel-1.0-py2.py3-none-any.whl",
                MetadataKind.Unhashed,
                ("simple==1.0",),
            ),
        ],
        "has-script": [
            FakePackage(
                "has-script",
                "1.0",
                "has.script-1.0-py2.py3-none-any.whl",
                MetadataKind.WrongHash,
            ),
        ],
        "translationstring": [
            FakePackage(
                "translationstring",
                "1.1",
                "translationstring-1.1.tar.gz",
                MetadataKind.No,
            ),
        ],
        "priority": [
            FakePackage(
                "priority",
                "1.0",
                "priority-1.0-py2.py3-none-any.whl",
                MetadataKind.NoFile,
            ),
        ],
        "requires-simple-extra": [
            FakePackage(
                "requires-simple-extra",
                "0.1",
                "requires_simple_extra-0.1-py2.py3-none-any.whl",
                MetadataKind.Sha256,
                requires_dist=("simple==1.0; extra == 'extra'",),
                provides_extra=("extra",),
                metadata_name="Requires_Simple.Extra",
            ),
        ],
    }


@pytest.fixture(scope="session")
def html_index_for_packages(
    shared_data: TestData,
    fake_packages: dict[str, list[FakePackage]],
    tmpdir_factory: pytest.TempPathFactory,
) -> Path:
    """Generate a PyPI HTML package index within a local directory pointing to
    synthetic test data.
    """
    html_dir = tmpdir_factory.mktemp("fake_index_html_content")

    pkg_links = "\n".join(
        f'    <a href="{pkg}/index.html">{pkg}</a>' for pkg in fake_packages
    )
    index_html = dedent(f"""\
        <!DOCTYPE html>
        <html>
          <head>
            <meta name="pypi:repository-version" content="1.0">
            <title>Simple index</title>
          </head>
          <body>
          {pkg_links}
          </body>
        </html>""")
    (html_dir / "index.html").write_text(index_html)

    for pkg, links in fake_packages.items():
        pkg_subdir = html_dir / pkg
        pkg_subdir.mkdir()

        download_links: list[str] = []
        for package_link in links:
            download_links.append(
                f'    <a href="{package_link.filename}" {package_link.generate_additional_tag()}>{package_link.filename}</a><br/>',  # noqa: E501
            )
            shutil.copy(
                shared_data.packages / package_link.filename,
                pkg_subdir / package_link.filename,
            )
            if package_link.metadata != MetadataKind.NoFile:
                with open(pkg_subdir / package_link.metadata_filename(), "wb") as f:
                    f.write(package_link.generate_metadata())

        download_links_str = "\n".join(download_links)
        pkg_index_content = dedent(f"""\
            <!DOCTYPE html>
            <html>
              <head>
                <meta name="pypi:repository-version" content="1.0">
                <title>Links for {pkg}</title>
              </head>
              <body>
                <h1>Links for {pkg}</h1>
                {download_links_str}
              </body>
            </html>""")
        with open(pkg_subdir / "index.html", "w") as f:
            f.write(pkg_index_content)

    return html_dir


class OneTimeDownloadHandler(http.server.SimpleHTTPRequestHandler):
    """Serve files from the current directory, but error if a file is downloaded more
    than once.
    """

    seen_paths: ClassVar[set[str]] = set()

    def do_GET(self) -> None:
        if self.path in self.seen_paths:
            self.send_error(
                http.HTTPStatus.NOT_FOUND,
                f"File {self.path} not available more than once!",
            )
            return
        super().do_GET()
        if not (self.path.endswith("/") or self.path.endswith(".metadata")):
            self.seen_paths.add(self.path)


@pytest.fixture
def html_index_with_onetime_server(
    html_index_for_packages: Path,
) -> Iterator[http.server.ThreadingHTTPServer]:
    """Serve files from a generated pypi index, erroring if a file is downloaded more
    than once.

    Provide `-i http://localhost:8000` to kpip invocations to point them at this server.
    """

    class InDirectoryServer(http.server.ThreadingHTTPServer):
        def finish_request(
            self: http.server.ThreadingHTTPServer,
            request: Any,
            client_address: Any,
        ) -> None:
            self.RequestHandlerClass(
                request,
                client_address,
                self,
                directory=str(html_index_for_packages),  # type: ignore[call-arg]
            )

    class Handler(OneTimeDownloadHandler):
        seen_paths: ClassVar[set[str]] = set()

    with patch_getfqdn(), InDirectoryServer(("", 8000), Handler) as httpd:
        server_thread = threading.Thread(target=httpd.serve_forever)
        server_thread.start()

        try:
            yield httpd
        finally:
            httpd.shutdown()
            server_thread.join()
