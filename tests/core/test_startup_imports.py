from __future__ import annotations

import json
from pathlib import Path


from import_harness import ROOT, imported_modules, run_kpip


from tomllib import loads


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


def test_list_empty_json_output(tmp_path: Path) -> None:
    result = run_kpip(["list", "--format=json", "--path", str(tmp_path)], cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout == "[]\n"


def test_list_reads_simple_dist_info(tmp_path: Path) -> None:
    dist_info = tmp_path / "demo_pkg-1.2.dist-info"
    dist_info.mkdir()
    dist_info.joinpath("METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2\n",
        encoding="utf-8",
    )

    result = run_kpip(["list", "--format=json", "--path", str(tmp_path)], cwd=tmp_path)

    assert result.returncode == 0
    assert json.loads(result.stdout) == [{"name": "demo-pkg", "version": "1.2"}]


def test_a_repeated_wheelhouse_lock_writes_its_output(tmp_path: Path) -> None:
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


def test_already_satisfied_install_reports_each_requirement(tmp_path: Path) -> None:
    """``kpip install <name>`` for names already installed says so, and an
    unmet specifier still goes to the index."""
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
        # The Python running kpip, which no environment names.
        "KPIP_SYSTEM_PYTHON": "1",
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

    snapshot = import_snapshot(
        ["install", "--no-index", "--find-links", str(wheelhouse), "simplewheel>=3"],
        cwd=tmp_path,
        env=env,
    )
    assert "Could not find a version that satisfies" in snapshot.stderr, (
        snapshot.describe()
    )
