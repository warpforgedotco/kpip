from __future__ import annotations

import shlex
from pathlib import Path

from kpip_benchmark.codspeed import Workspace, benchmarks, render_config


def test_every_benchmark_runs_the_workspace_binary(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "with space")
    config = render_config(workspace)

    assert len(config["benchmarks"]) == len(benchmarks(workspace))
    for benchmark in config["benchmarks"]:
        argv = shlex.split(benchmark["exec"])
        assert argv[0] == str(workspace.kpip)


def test_names_are_unique(tmp_path: Path) -> None:
    names = [name for name, _args in benchmarks(Workspace(tmp_path))]

    assert len(names) == len(set(names))


def test_nothing_reaches_an_index(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    for name, args in benchmarks(workspace):
        if ("lock" in args or "install" in args) and "--help" not in args:
            assert "--no-index" in args, name
            assert str(workspace.wheelhouse) in args, name


def test_cold_cases_use_no_cache_and_warm_cases_the_workspace_cache(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    for name, args in benchmarks(workspace):
        if "(cold)" in name:
            assert "--no-cache-dir" in args, name
            assert "--cache-dir" not in args, name
        if "(warm)" in name:
            assert args[args.index("--cache-dir") + 1] == str(workspace.cache), name
