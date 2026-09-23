from __future__ import annotations

import json
import sys
from pathlib import Path

from import_harness import ROOT, baseline_modules, imported_modules, run_kpip

if sys.version_info >= (3, 11):
    from tomllib import loads
else:
    from kpip._vendor.tomli import loads


PACKAGES = ROOT / "tests" / "cli" / "data" / "packages"
SIMPLEWHEEL = PACKAGES / "simplewheel-2.0-py2.py3-none-any.whl"


def test_literal_version_matches_project_metadata() -> None:
    import kpip

    project = loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert kpip.__version__ == project["project"]["version"]


def test_top_level_help_exits_zero_and_prints_usage() -> None:
    result = run_kpip(["--help"])

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "kpip" in result.stdout


def test_unknown_command_errors() -> None:
    result = run_kpip(["definitely-not-a-command"])

    assert result.returncode == 1
    assert "Unknown command" in result.stderr


def test_version_prints_package_location() -> None:
    result = run_kpip(["--version"])

    assert result.returncode == 0
    assert result.stdout.startswith("kpip ")


def test_fast_list_empty_json_output(tmp_path: Path) -> None:
    result = run_kpip(["list", "--format=json", "--path", str(tmp_path)], cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout == "[]\n"


def test_fast_list_reads_simple_dist_info(tmp_path: Path) -> None:
    dist_info = tmp_path / "demo_pkg-1.2.dist-info"
    dist_info.mkdir()
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )

    result = run_kpip(["list", "--format=json", "--path", str(tmp_path)], cwd=tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout) == [{"name": "demo-pkg", "version": "1.2"}]


def test_fast_lock_produces_output_on_cache_hit(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    output = tmp_path / "pylock.toml"
    args = [
        "lock",
        "--quiet",
        "--no-index",
        "--find-links",
        str(SIMPLEWHEEL),
        "--output",
        str(output),
        "simplewheel==2.0",
    ]
    env = {"KPIP_CACHE_DIR": str(cache_dir)}

    first = run_kpip(args, cwd=tmp_path, env=env)
    second = run_kpip(args, cwd=tmp_path, env=env)

    assert first.returncode == 0
    assert second.returncode == 0
    assert output.is_file()


FAST_INSTALL_FORBIDDEN = frozenset(
    {
        "dataclasses",
        "email.parser",
        "hashlib",
        "importlib.resources",
        "inspect",
        # ``urllib.parse`` reaches it, and an install from a wheelhouse has
        # no URL to read.
        "ipaddress",
        "logging",
        "tempfile",
        "json",
        "sqlite3",
        "urllib.parse",
        "zipfile",
        "kpip.resolution.models",
        "kpip.resolution.api",
        "kpip.index.provider",
        "kpip.cli.install",
        "kpip.cli.requirements",
    },
)


def test_fast_local_install_stays_import_light(tmp_path: Path) -> None:
    import shutil

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    target = tmp_path / "target"
    args = [
        "install",
        "--no-index",
        "--ignore-installed",
        "--no-compile",
        "--target",
        str(target),
        "--find-links",
        str(wheelhouse),
        "simplewheel==2.0",
    ]
    env = {"KPIP_CACHE_DIR": str(tmp_path / "cache")}

    modules = imported_modules(args, cwd=tmp_path, env=env)

    assert next(target.glob("simplewheel-2.0.dist-info"), None) is not None
    assert "kpip.cli.fast_install" in modules
    assert not (modules & FAST_INSTALL_FORBIDDEN), sorted(
        modules & FAST_INSTALL_FORBIDDEN
    )


def test_default_install_scans_installed_state_without_importlib_metadata(
    tmp_path: Path,
) -> None:
    """Without --ignore-installed the install lists the target's (and the
    interpreter's) dist-info directories itself; importlib.metadata is only
    for roots and finders a directory listing cannot answer."""
    import shutil

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    target = tmp_path / "target"
    args = [
        "install",
        "--no-index",
        "--target",
        str(target),
        "--find-links",
        str(wheelhouse),
        "simplewheel==2.0",
    ]
    env = {"KPIP_CACHE_DIR": str(tmp_path / "cache")}

    first = imported_modules(args, cwd=tmp_path, env=env) - baseline_modules()
    assert next(target.glob("simplewheel-2.0.dist-info"), None) is not None
    second = imported_modules(args, cwd=tmp_path, env=env) - baseline_modules()

    for modules in (first, second):
        assert "kpip.cli.install" in modules
        assert "importlib.metadata" not in modules


SATISFIED_INSTALL_FORBIDDEN = frozenset(
    {
        "kpip.cli.install",
        "kpip.cli.fast_install",
        "kpip.cli.requirements",
        "kpip.index.provider",
        "kpip.resolution.api",
        "logging",
        "argparse",
        "sqlite3",
        "json",
    },
)


def test_already_satisfied_install_answers_before_startup(tmp_path: Path) -> None:
    """``kpip install <name>`` for names already installed in a release
    version is answered by the pre-startup recognizer: the same lines the
    normal path prints, without loading it."""
    import os
    import shutil

    from import_harness import SRC, import_snapshot

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    site = tmp_path / "site"
    imported_modules(
        [
            "install",
            "-q",
            "--no-index",
            "--target",
            str(site),
            "--find-links",
            str(wheelhouse),
            "simplewheel==2.0",
        ],
        cwd=tmp_path,
        env={"KPIP_CACHE_DIR": str(tmp_path / "cache")},
    )
    assert next(site.glob("simplewheel-2.0.dist-info"), None) is not None

    env = {
        "KPIP_CACHE_DIR": str(tmp_path / "cache"),
        "PYTHONPATH": f"{site}{os.pathsep}{SRC}",
    }
    snapshot = import_snapshot(
        ["install", "--find-links", str(wheelhouse), "simplewheel", "simplewheel>=1"],
        cwd=tmp_path,
        env=env,
    )
    assert snapshot.returncode == 0, snapshot.describe()
    assert snapshot.stdout == (
        f"Looking in links: {wheelhouse}\n"
        "Requirement already satisfied: simplewheel\n"
        "Requirement already satisfied: simplewheel>=1\n"
    ), snapshot.describe()
    assert not (set(snapshot.modules) & SATISFIED_INSTALL_FORBIDDEN), sorted(
        set(snapshot.modules) & SATISFIED_INSTALL_FORBIDDEN
    )

    snapshot = import_snapshot(
        ["install", "--no-index", "--find-links", str(wheelhouse), "simplewheel>=3"],
        cwd=tmp_path,
        env=env,
    )
    assert "Could not find a version that satisfies" in snapshot.stderr, (
        snapshot.describe()
    )
    assert "kpip.cli.install" in snapshot.modules


NORMAL_INSTALL_FORBIDDEN = frozenset(
    {
        "dataclasses",
        "importlib.metadata",
        "kpip.resolution.files.parser",
        "kpip.vcs.versioncontrol",
        "kpip.core.subprocesses",
        "html.parser",
        "tomllib",
        "kpip._vendor.tomli",
        "kpip.build.build_backend",
        "email.message",
        "configparser",
    },
)


def test_normal_local_install_stays_import_light(tmp_path: Path) -> None:
    import shutil

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    target = tmp_path / "target"
    args = [
        "install",
        "--no-index",
        "--ignore-installed",
        "--target",
        str(target),
        "--find-links",
        str(wheelhouse),
        "simplewheel==2.0",
    ]
    env = {"KPIP_CACHE_DIR": str(tmp_path / "cache")}

    modules = imported_modules(args, cwd=tmp_path, env=env) - baseline_modules()

    assert next(target.glob("simplewheel-2.0.dist-info"), None) is not None
    assert "kpip.cli.install" in modules
    forbidden = NORMAL_INSTALL_FORBIDDEN
    if sys.version_info >= (3, 14):
        forbidden = forbidden - {"dataclasses"}
    assert not (modules & forbidden), sorted(modules & forbidden)


FAST_LIST_FORBIDDEN = frozenset(
    {
        "typing",
        "kpip.cli.fast_install",
        "kpip.core.packaging",
        "kpip.cli.list",
        "kpip.build.query",
        "kpip.core.metadata",
        "argparse",
    }
)


def test_plain_freeze_stays_import_light(tmp_path: Path) -> None:
    """``kpip freeze`` with no options is the fast path over sys.path."""
    import os

    from import_harness import SRC, import_snapshot

    site = tmp_path / "site"
    dist_info = site / "demo_pkg-1.2.dist-info"
    dist_info.mkdir(parents=True)
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": f"{site}{os.pathsep}{SRC}"}

    snapshot = import_snapshot(["freeze", "--exclude-editable"], cwd=tmp_path, env=env)
    assert "demo-pkg==1.2\n" in snapshot.stdout, snapshot.describe()
    forbidden = FAST_LIST_FORBIDDEN | {
        "kpip.cli.freeze",
        "kpip.core.light_metadata",
        "logging",
    }
    assert not (set(snapshot.modules) & forbidden), sorted(
        set(snapshot.modules) & forbidden
    )


def test_plain_list_stays_import_light(tmp_path: Path) -> None:
    """``kpip list`` with no options is the fast path over sys.path."""
    import os

    from import_harness import SRC, import_snapshot

    site = tmp_path / "site"
    dist_info = site / "demo_pkg-1.2.dist-info"
    dist_info.mkdir(parents=True)
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )
    env = {"PYTHONPATH": f"{site}{os.pathsep}{SRC}"}

    snapshot = import_snapshot(["list"], cwd=tmp_path, env=env)
    import re

    assert re.search(r"^demo-pkg +1\.2$", snapshot.stdout, re.M), snapshot.describe()
    assert not (set(snapshot.modules) & FAST_LIST_FORBIDDEN), sorted(
        set(snapshot.modules) & FAST_LIST_FORBIDDEN
    )


def test_fast_list_stays_import_light(tmp_path: Path) -> None:
    dist_info = tmp_path / "demo_pkg-1.2.dist-info"
    dist_info.mkdir()
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )

    modules = (
        imported_modules(
            ["list", "--format=json", "--path", str(tmp_path)],
            cwd=tmp_path,
        )
        - baseline_modules()
    )

    assert "kpip.cli.fast" in modules
    assert not (modules & FAST_LIST_FORBIDDEN), sorted(modules & FAST_LIST_FORBIDDEN)


INDEX_LOCK_FORBIDDEN = frozenset(
    {
        "http.client",
        "logging",
        "ssl",
        "traceback",
        "kpip._vendor.urllib3",
        "kpip.network.session",
    },
)


def test_a_lock_does_not_build_the_client_it_may_never_use(tmp_path: Path) -> None:
    """``kpip lock`` reaches the full command without the HTTP stack.

    ``--python-version`` is what puts this past the wheelhouse fast path and
    into ``kpip.cli.lock``, which is the one that used to build a
    ``NetworkSession`` before it knew whether anything would be sent. That
    import is the largest on the lock path -- the vendored urllib3 client,
    ``ssl``, ``http.client`` and ``logging`` behind it -- and a resolve
    whose answers are all local, or all still fresh in the cache, never
    opens a socket at all.
    """
    import shutil

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(SIMPLEWHEEL, wheelhouse / SIMPLEWHEEL.name)
    requirements = tmp_path / "requirements.in"
    requirements.write_text("simplewheel==2.0\n", encoding="utf-8")
    output = tmp_path / "kpip.lock"
    args = [
        "lock",
        "--quiet",
        "--no-index",
        "--find-links",
        str(wheelhouse),
        "--python-version",
        "3.12",
        "--output",
        str(output),
        "-r",
        str(requirements),
    ]
    env = {"KPIP_CACHE_DIR": str(tmp_path / "cache")}

    modules = imported_modules(args, cwd=tmp_path, env=env)

    assert "simplewheel" in output.read_text(encoding="utf-8")
    assert "kpip.cli.lock" in modules
    assert not (modules & INDEX_LOCK_FORBIDDEN), sorted(modules & INDEX_LOCK_FORBIDDEN)
