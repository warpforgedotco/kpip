from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kpip_benchmark.cli import (
    BENCHMARKS,
    build_commands,
    default_benchmarks,
    expand_workloads,
)
from kpip_benchmark.hyperfine import Command, Hyperfine
from kpip_benchmark.runner import run_chain
from kpip_benchmark.workloads import (
    OFFICIAL_WORKLOAD_NAMES,
    OFFICIAL_WORKLOADS,
    fixture_root,
    workload_manifest,
)


def test_builds_all_offline_commands(tmp_path: Path) -> None:
    for benchmark in BENCHMARKS:
        commands = build_commands(
            benchmark,
            workload="offline",
            workspace=tmp_path / benchmark,
            kpip_python=sys.executable,
            kpip_console=None,
            kpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
        assert {command.name.split()[0] for command in commands} == {"kpip", "uv"}
        assert all(command.command for command in commands)
        for command in commands:
            if os.name != "nt":
                assert "kpip_benchmark.runner" not in " ".join(command.command)
                if command.name.startswith("kpip"):
                    assert command.env.get("PYTHONPATH")
                else:
                    assert command.env == {}


def test_generated_fragments_are_not_posix_specific(tmp_path: Path) -> None:
    forbidden = ("rm -rf", "mkdir -p")
    for benchmark in BENCHMARKS:
        commands = build_commands(
            benchmark,
            workload="offline",
            workspace=tmp_path / benchmark,
            kpip_python=sys.executable,
            kpip_console=None,
            kpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
        for command in commands:
            fragments = [command.prepare or "", " ".join(command.command)]
            assert not any(
                token in fragment for token in forbidden for fragment in fragments
            )


def test_direct_launcher_uses_generated_wrapper(tmp_path: Path) -> None:
    commands = build_commands(
        "startup-help",
        workload="offline",
        workspace=tmp_path,
        kpip_python=sys.executable,
        kpip_console=None,
        kpip_launcher="direct",
        uv_path="uv",
        python=sys.executable,
    )

    kpip = commands[0].command
    assert str(tmp_path / "kpip-direct.py") in kpip
    assert (
        (tmp_path / "kpip-direct.py")
        .read_text(encoding="utf-8")
        .startswith(
            "from __future__ import annotations\nfrom kpip.cli.entrypoint import main\n",
        )
    )


def example_run(*commands: Command, setup: str | None = None) -> Hyperfine:
    return Hyperfine(
        name="example",
        commands=list(commands),
        setup=setup,
        warmup=0,
        min_runs=None,
        runs=1,
        verbose=False,
        json=True,
        ignore_failure=False,
    )


def test_hyperfine_dry_run_contains_prepare_and_names() -> None:
    run = example_run(
        Command(
            "kpip (example)",
            "python -m kpip_benchmark.runner cleanup --path target",
            ["python", "-m", "kpip", "--help"],
        ),
    )

    args = run.args()
    assert args[:4] == ["hyperfine", "-N", "--export-json", "example.json"]
    assert "--prepare" in args
    assert "kpip (example)" in args


def test_hyperfine_always_disables_the_shell() -> None:
    run = example_run(Command("kpip (example)", None, ["python", "--version"]))

    assert "-N" in run.args()


def test_hyperfine_omits_prepare_when_no_command_prepares() -> None:
    run = example_run(Command("kpip (example)", None, ["python", "--version"]))

    assert "--prepare" not in run.args()


def test_hyperfine_pads_a_missing_prepare_with_a_no_op() -> None:
    run = example_run(
        Command("kpip (example)", "python -m kpip_benchmark.runner cleanup", ["a"]),
        Command("uv (example)", None, ["b"]),
    )

    args = run.args()
    prepares = [args[index + 1] for index, arg in enumerate(args) if arg == "--prepare"]
    assert len(prepares) == 2
    assert all("kpip_benchmark.runner" in prepare for prepare in prepares)


def test_hyperfine_carries_env_on_its_own_process() -> None:
    run = example_run(
        Command("kpip (example)", None, ["a"], {"PYTHONPATH": "/src"}),
        Command("uv (example)", None, ["b"]),
    )

    assert run.environment() == {"PYTHONPATH": "/src"}
    assert not any("PYTHONPATH" in arg for arg in run.args())


def test_hyperfine_rejects_commands_that_disagree_on_env() -> None:
    run = example_run(
        Command("kpip (example)", None, ["a"], {"PYTHONPATH": "/one"}),
        Command("uv (example)", None, ["b"], {"PYTHONPATH": "/two"}),
    )

    try:
        run.environment()
    except ValueError as error:
        assert "PYTHONPATH" in str(error)
    else:
        raise AssertionError("conflicting env accepted")


def test_generated_preparation_never_needs_a_shell(tmp_path: Path) -> None:
    for benchmark in BENCHMARKS:
        commands = build_commands(
            benchmark,
            workload="offline",
            workspace=tmp_path / benchmark,
            kpip_python=sys.executable,
            kpip_console=None,
            kpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
        for command in commands:
            prepare = command.prepare or ""
            assert "&&" not in prepare
            assert ";" not in prepare
            if prepare:
                assert "kpip_benchmark.runner" in prepare


def test_warm_setup_chain_runs_every_step_in_order(tmp_path: Path) -> None:
    target = tmp_path / "stale"
    target.mkdir()
    marker = tmp_path / "marker"
    steps = [
        {"kind": "cleanup", "path": [str(target)], "mkdir": []},
        {
            "kind": "run",
            "command": [
                sys.executable,
                "-c",
                (
                    "import os,pathlib;"
                    "pathlib.Path(os.environ['MARKER']).write_text('ran')"
                ),
            ],
            "env": {"MARKER": str(marker)},
        },
    ]

    assert run_chain(json.dumps(steps)) == 0
    assert not target.exists()
    assert marker.read_text(encoding="utf-8") == "ran"


def test_chain_stops_at_the_first_failing_step(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    steps = [
        {"kind": "run", "command": [sys.executable, "-c", "raise SystemExit(3)"]},
        {
            "kind": "run",
            "command": [
                sys.executable,
                "-c",
                f"open({str(marker)!r}, 'w').write('ran')",
            ],
        },
    ]

    assert run_chain(json.dumps(steps)) == 3
    assert not marker.exists()


def test_offline_workload_contains_installable_wheels(tmp_path: Path) -> None:
    manifest = workload_manifest(tmp_path, workload="offline")
    wheelhouse = Path(manifest["wheelhouse"])
    requirements = Path(manifest["source_requirements"])

    assert requirements.read_text(encoding="utf-8").strip() == "application"
    assert (wheelhouse / "application-1.0.0-py3-none-any.whl").is_file()
    assert (
        Path(manifest["incremental_wheelhouse"])
        / "incremental_application-2.0.0-py3-none-any.whl"
    ).is_file()
    assert len(list(wheelhouse.glob("*.whl"))) > 20


def test_incremental_install_restores_old_target_before_each_run(
    tmp_path: Path,
) -> None:
    commands = build_commands(
        "install-incremental-warm",
        workload="offline",
        workspace=tmp_path,
        kpip_python=sys.executable,
        kpip_console=None,
        kpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    assert all(command.prepare is not None for command in commands)
    assert all(
        "incremental-base.txt" in (command.prepare or "") for command in commands
    )
    assert all(
        "incremental-update.txt" in " ".join(command.command) for command in commands
    )
    assert all("--upgrade" in command.command for command in commands)


def test_trio_workload_writes_official_files(tmp_path: Path) -> None:
    manifest = workload_manifest(tmp_path, workload="trio")

    assert "sphinx" in Path(manifest["source_requirements"]).read_text(encoding="utf-8")
    assert "sphinx==" in Path(manifest["install_requirements"]).read_text(
        encoding="utf-8"
    )


def test_trio_install_uses_the_prepared_kpip_cache(tmp_path: Path) -> None:
    commands = build_commands(
        "install-warm",
        workload="trio",
        workspace=tmp_path,
        kpip_python=sys.executable,
        kpip_console=None,
        kpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    kpip = commands[0].command
    cache_option = kpip.index("--cache-dir")
    assert kpip[cache_option + 1] == str(tmp_path / "cache" / "kpip")


def test_every_official_workload_builds_lock_commands(tmp_path: Path) -> None:
    for workload in OFFICIAL_WORKLOADS:
        commands = build_commands(
            "lock-cold",
            workload=workload.name,
            workspace=tmp_path / workload.name,
            kpip_python=sys.executable,
            kpip_console=None,
            kpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )

        assert {command.name.split()[0] for command in commands} == {"kpip", "uv"}
        assert all(command.command for command in commands)


def test_every_compiled_official_workload_builds_install_commands(
    tmp_path: Path,
) -> None:
    compiled = [workload for workload in OFFICIAL_WORKLOADS if workload.compiled]
    assert len(compiled) == 9

    for workload in compiled:
        commands = build_commands(
            "install-warm",
            workload=workload.name,
            workspace=tmp_path / workload.name,
            kpip_python=sys.executable,
            kpip_console=None,
            kpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )

        assert all("compiled" in " ".join(command.command) for command in commands)


def test_source_only_workload_rejects_install_benchmark(tmp_path: Path) -> None:
    try:
        build_commands(
            "install-cold",
            workload="home-assistant",
            workspace=tmp_path,
            kpip_python=sys.executable,
            kpip_console=None,
            kpip_launcher="module",
            uv_path="uv",
            python=sys.executable,
        )
    except ValueError as error:
        assert "no official compiled install workload" in str(error)
    else:
        raise AssertionError("source-only workload accepted install benchmark")


def test_airflow2_applies_constraints_to_both_resolvers(tmp_path: Path) -> None:
    commands = build_commands(
        "lock-cold",
        workload="airflow2",
        workspace=tmp_path,
        kpip_python=sys.executable,
        kpip_console=None,
        kpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    for command in commands:
        constraint = command.command.index("--constraint")
        assert command.command[constraint + 1].endswith("airflow2-constraints.txt")


def test_transformers_project_uses_native_project_inputs(tmp_path: Path) -> None:
    commands = build_commands(
        "lock-cold",
        workload="transformers-project",
        workspace=tmp_path,
        kpip_python=sys.executable,
        kpip_console=None,
        kpip_launcher="module",
        uv_path="uv",
        python=sys.executable,
    )

    kpip, uv = commands
    assert any(argument.endswith("/transformers") for argument in kpip.command)
    assert any(
        argument.endswith("/transformers/pyproject.toml") for argument in uv.command
    )


def test_official_defaults_match_available_fixtures() -> None:
    assert default_benchmarks("home-assistant") == ("lock-cold", "lock-warm")
    assert default_benchmarks("jupyter") == (
        "lock-cold",
        "lock-warm",
        "install-cold",
        "install-warm",
    )


def test_live_expands_to_every_official_workload() -> None:
    assert expand_workloads("live") == OFFICIAL_WORKLOAD_NAMES
    assert expand_workloads("trio") == ("trio",)


def test_registry_covers_every_official_fixture() -> None:
    root = fixture_root()
    source_fixtures = {str(path.relative_to(root)) for path in root.rglob("*.in")} | {
        "transformers/pyproject.toml"
    }
    compiled_fixtures = {
        str(path.relative_to(root)) for path in (root / "compiled").glob("*.txt")
    }

    assert {workload.source for workload in OFFICIAL_WORKLOADS} == source_fixtures
    assert {
        workload.compiled for workload in OFFICIAL_WORKLOADS if workload.compiled
    } == compiled_fixtures
    assert {
        workload.constraint for workload in OFFICIAL_WORKLOADS if workload.constraint
    } == {"airflow2-constraints.txt"}


def test_a_failing_logged_step_leaves_its_output_where_hyperfine_discarded_it(
    tmp_path: Path,
) -> None:
    log = tmp_path / "setup-failure.log"
    log.write_text("stale")
    steps = [
        {
            "kind": "run",
            "command": [
                sys.executable,
                "-c",
                "import sys; print('out'); print('boom', file=sys.stderr); sys.exit(2)",
            ],
            "log": str(log),
        },
    ]

    assert run_chain(json.dumps(steps)) == 2
    text = log.read_text(encoding="utf-8")
    assert "exit 2" in text
    assert "out\n" in text
    assert "boom\n" in text


def test_a_passing_logged_step_removes_a_stale_log(tmp_path: Path) -> None:
    log = tmp_path / "setup-failure.log"
    log.write_text("stale")
    steps = [
        {"kind": "run", "command": [sys.executable, "-c", "pass"], "log": str(log)}
    ]

    assert run_chain(json.dumps(steps)) == 0
    assert not log.exists()


def test_hyperfine_prints_the_setup_log_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    log = tmp_path / "setup-failure.log"
    log.write_text("kpip lock: HTTPError 503")
    run = Hyperfine(
        name="x",
        commands=[Command(name="a", prepare=None, command=["true"])],
        setup="setup",
        setup_log=log,
        warmup=None,
        min_runs=None,
        runs=None,
        verbose=False,
        json=False,
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "hyperfine")

    monkeypatch.setattr(subprocess, "check_call", fail)
    with pytest.raises(subprocess.CalledProcessError):
        run.run()

    assert "HTTPError 503" in capsys.readouterr().err


def test_a_failing_step_still_reports_its_exit_code_when_the_log_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full /tmp must not turn a step's failure into the runner's own crash."""
    log = tmp_path / "setup-failure.log"

    def no_space(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", no_space)
    steps = [
        {
            "kind": "run",
            "command": [sys.executable, "-c", "import sys; sys.exit(7)"],
            "log": str(log),
        },
    ]

    assert run_chain(json.dumps(steps)) == 7
    assert "could not write" in capsys.readouterr().err


def test_the_setup_log_records_the_free_space(tmp_path: Path) -> None:
    log = tmp_path / "setup-failure.log"
    steps = [
        {
            "kind": "run",
            "command": [sys.executable, "-c", "raise SystemExit(1)"],
            "log": str(log),
        },
    ]

    assert run_chain(json.dumps(steps)) == 1
    assert "free space on" in log.read_text(encoding="utf-8")
