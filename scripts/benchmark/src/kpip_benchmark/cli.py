from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kpip_benchmark.hyperfine import Command, Hyperfine, command_line, env_prefix
from kpip_benchmark.workloads import (
    OFFICIAL_WORKLOAD_NAMES,
    OFFICIAL_WORKLOADS,
    WORKLOAD_NAMES,
    official_workload,
    workload_manifest,
)

BENCHMARKS = (
    "startup-help",
    "startup-version",
    "startup-install-help",
    "startup-lock-help",
    "startup-list-help",
    "startup-invalid-command",
    "startup-list-empty",
    "startup-fast-lock",
    "startup-fast-install",
    "lock-cold",
    "lock-warm",
    "install-cold",
    "install-warm",
    "install-incremental-warm",
)

OFFICIAL_LOCK_BENCHMARKS = ("lock-cold", "lock-warm")
OFFICIAL_INSTALL_BENCHMARKS = ("install-cold", "install-warm")


def expand_workloads(selector: str) -> tuple[str, ...]:
    if selector == "live":
        return OFFICIAL_WORKLOAD_NAMES
    return (selector,)


def default_benchmarks(workload: str) -> tuple[str, ...]:
    if workload == "offline":
        return BENCHMARKS
    definition = official_workload(workload)
    if definition is None:
        raise ValueError(f"Unknown workload: {workload}")
    if definition.compiled is None:
        return OFFICIAL_LOCK_BENCHMARKS
    return (*OFFICIAL_LOCK_BENCHMARKS, *OFFICIAL_INSTALL_BENCHMARKS)


def supports_benchmark(workload: str, benchmark: str) -> bool:
    if workload == "offline":
        return True
    if benchmark.startswith("startup-fast-"):
        return False
    if not benchmark.startswith("install-"):
        return True
    if benchmark == "install-incremental-warm":
        return True
    definition = official_workload(workload)
    return definition is not None and definition.compiled is not None


def print_workloads() -> None:
    print("offline\tlock + install\tGenerated local wheelhouse (no network)")
    print(
        f"live\tsuite\tAll {len(OFFICIAL_WORKLOADS)} official uv workloads "
        "(install where compiled)",
    )
    for workload in OFFICIAL_WORKLOADS:
        capabilities = "lock + install" if workload.compiled else "lock"
        python = f"; Python {workload.python}" if workload.python else ""
        print(
            f"{workload.name}\t{capabilities}\t{workload.description}{python}",
        )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _version_of(executable: str) -> str:
    try:
        return subprocess.check_output(
            [executable, "--version"], text=True, stderr=subprocess.STDOUT
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def collect_run_metadata(*, kpip_python: str, uv_path: str) -> dict[str, str]:
    """Record what actually ran, so ``kpip-bench-compare`` can catch a
    mismatched interpreter between two runs instead of silently comparing
    apples to oranges (a fresh ``uv sync`` with no pin can resolve a
    different Python version than an existing checkout)."""
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root(),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_commit = "unknown"
    return {
        "kpip_python_version": _version_of(kpip_python),
        "uv_version": _version_of(uv_path),
        "git_commit": git_commit,
    }


def kpip_direct_launcher(workspace: Path) -> Path:
    launcher = workspace / "kpip-direct.py"
    if not launcher.exists():
        launcher.write_text(
            "from __future__ import annotations\n"
            "from kpip.cli.entrypoint import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
    return launcher


def kpip_command(
    args: list[str],
    *,
    workspace: Path,
    kpip_python: str,
    kpip_console: str | None,
    kpip_launcher: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    if kpip_console is not None:
        command = [kpip_console, *args]
    elif kpip_launcher == "direct":
        command = [kpip_python, str(kpip_direct_launcher(workspace)), *args]
    else:
        command = [kpip_python, "-m", "kpip", *args]
    env = {"PYTHONPATH": str(repo_root() / "src")}
    env.update(extra_env or {})
    return command, env


def uv_command(uv_path: str, args: list[str]) -> list[str]:
    return [uv_path, *args]


def runner_command(*args: str) -> str:
    return command_line([sys.executable, "-m", "kpip_benchmark.runner", *args])


def cleanup_command(paths: list[Path], *, mkdir: list[Path] | None = None) -> str:
    args = ["cleanup"]
    for path in paths:
        args.extend(["--path", str(path)])
    for path in mkdir or []:
        args.extend(["--mkdir", str(path)])
    return runner_command(*args)


def cleanup_step(paths: list[Path], *, mkdir: list[Path] | None = None) -> dict:
    return {
        "kind": "cleanup",
        "path": [str(path) for path in paths],
        "mkdir": [str(path) for path in mkdir or []],
    }


def run_step(
    command: list[str], env: dict[str, str] | None = None, *, log: Path | None = None
) -> dict:
    step: dict = {"kind": "run", "command": command}
    if env:
        step["env"] = env
    if log is not None:
        step["log"] = str(log)
    return step


def chain_command(steps: list[dict]) -> str:
    """Render a multi-step ``--setup``/``--prepare`` for a shell-less hyperfine.

    ``--shell=none`` means no ``&&``, so the steps travel as one JSON argument
    and ``kpip_benchmark.runner chain`` sequences them. Preparation is untimed,
    so the extra interpreter is free.
    """
    return runner_command("chain", "--spec", json.dumps(steps, separators=(",", ":")))


def prepare_with_cache(
    cwd: Path, *, cache: Path, outputs: list[Path], cold: bool
) -> str:
    paths = []
    if cold:
        paths.append(cache)
    paths.extend(outputs)
    return cleanup_command(paths, mkdir=[cwd])


def setup_log(workspace: Path) -> Path:
    """Where a failed warm setup leaves the output hyperfine discarded."""
    return workspace / "setup-failure.log"


def warm_setup(commands: list[Command], stale: list[Path], *, log: Path) -> str:
    steps = [cleanup_step(stale)]
    steps.extend(
        run_step(command.command, command.env, log=log) for command in commands
    )
    return chain_command(steps)


def build_commands(
    benchmark: str,
    *,
    workload: str,
    workspace: Path,
    kpip_python: str,
    kpip_console: str | None,
    kpip_launcher: str,
    uv_path: str,
    python: str,
) -> list[Command]:
    manifest = workload_manifest(workspace / "workload", workload=workload)
    workload_name = manifest["workload"]
    kpip_cache = workspace / "cache" / "kpip"
    uv_cache = workspace / "cache" / "uv"
    kpip_output = workspace / "kpip.out"
    uv_output = workspace / "uv.out"
    kpip_target = workspace / "target" / "kpip"
    uv_target = workspace / "target" / "uv"
    wheelhouse = manifest.get("wheelhouse")
    source_requirements = manifest["source_requirements"]
    kpip_source = manifest.get("kpip_source", source_requirements)
    source_kind = manifest.get("source_kind", "requirements")
    constraint_requirements = manifest.get("constraint_requirements")
    install_requirements = manifest.get("install_requirements")
    incremental_wheelhouse = manifest["incremental_wheelhouse"]
    incremental_base = manifest["incremental_base_requirements"]
    incremental_update = manifest["incremental_update_requirements"]

    def kpip(
        args: list[str], *, extra_env: dict[str, str] | None = None
    ) -> tuple[list[str], dict[str, str]]:
        return kpip_command(
            args,
            workspace=workspace,
            kpip_python=kpip_python,
            kpip_console=kpip_console,
            kpip_launcher=kpip_launcher,
            extra_env=extra_env,
        )

    def label(tool: str) -> str:
        return f"{tool} ({workload_name}/{benchmark})"

    def kpip_step(
        prepare: str | None, args: list[str], *, extra_env: dict[str, str] | None = None
    ) -> Command:
        command, env = kpip(args, extra_env=extra_env)
        return Command(label("kpip"), prepare, command, env)

    def uv_step(prepare: str | None, args: list[str]) -> Command:
        return Command(label("uv"), prepare, uv_command(uv_path, args))

    if benchmark == "startup-help":
        return [
            kpip_step(None, ["--help"]),
            uv_step(None, ["--help"]),
        ]
    if benchmark == "startup-version":
        return [
            kpip_step(None, ["--version"]),
            uv_step(None, ["--version"]),
        ]
    if benchmark == "startup-install-help":
        return [
            kpip_step(None, ["install", "--help"]),
            uv_step(None, ["pip", "install", "--help"]),
        ]
    if benchmark == "startup-lock-help":
        return [
            kpip_step(None, ["lock", "--help"]),
            uv_step(None, ["pip", "compile", "--help"]),
        ]
    if benchmark == "startup-list-help":
        return [
            kpip_step(None, ["list", "--help"]),
            uv_step(None, ["pip", "list", "--help"]),
        ]
    if benchmark == "startup-invalid-command":
        return [
            kpip_step(None, ["definitely-not-a-command"]),
            uv_step(None, ["definitely-not-a-command"]),
        ]
    if benchmark == "startup-list-empty":
        return [
            kpip_step(
                cleanup_command([kpip_target], mkdir=[kpip_target]),
                ["list", "--format=json", "--path", str(kpip_target)],
            ),
            uv_step(
                cleanup_command([uv_target], mkdir=[uv_target]),
                ["pip", "list", "--format=json", "--target", str(uv_target)],
            ),
        ]
    if benchmark == "startup-fast-lock":
        if wheelhouse is None:
            raise ValueError("startup-fast-lock requires the offline workload")
        trivial_requirements = workspace / "trivial.in"
        trivial_requirements.write_text("leaf-0\n", encoding="utf-8")
        kpip_args = [
            "lock",
            "--quiet",
            "--no-index",
            "--find-links",
            wheelhouse or "",
            "--output",
            str(kpip_output),
            "-r",
            str(trivial_requirements),
        ]
        uv_args = [
            "pip",
            "compile",
            str(trivial_requirements),
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--output-file",
            str(uv_output),
            "--python",
            python,
        ]
        uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            kpip_step(
                cleanup_command([kpip_output]),
                kpip_args,
                extra_env={"KPIP_CACHE_DIR": str(kpip_cache)},
            ),
            uv_step(cleanup_command([uv_output]), uv_args),
        ]
    if benchmark == "startup-fast-install":
        if wheelhouse is None:
            raise ValueError("startup-fast-install requires the offline workload")
        trivial_install = workspace / "trivial-install.txt"
        trivial_install.write_text("leaf-0==1.1.0\n", encoding="utf-8")
        kpip_args = [
            "install",
            "--quiet",
            "--ignore-installed",
            "--no-compile",
            "--cache-dir",
            str(kpip_cache),
            "--target",
            str(kpip_target),
            "-r",
            str(trivial_install),
        ]
        uv_args = [
            "pip",
            "install",
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--target",
            str(uv_target),
            "--python",
            python,
            "-r",
            str(trivial_install),
        ]
        kpip_args.extend(["--no-index", "--find-links", wheelhouse])
        uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            kpip_step(cleanup_command([kpip_target]), kpip_args),
            uv_step(cleanup_command([uv_target]), uv_args),
        ]

    if benchmark.startswith("lock-"):
        cold = benchmark.endswith("cold")
        kpip_prepare = prepare_with_cache(
            workspace,
            cache=kpip_cache,
            outputs=[kpip_output],
            cold=cold,
        )
        uv_prepare = prepare_with_cache(
            workspace,
            cache=uv_cache,
            outputs=[uv_output],
            cold=cold,
        )
        kpip_args = ["lock", "--quiet", "--output", str(kpip_output)]
        uv_args = [
            "pip",
            "compile",
            source_requirements,
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--output-file",
            str(uv_output),
            "--python",
            python,
        ]
        if source_kind == "project":
            if wheelhouse is not None:
                raise ValueError("project workloads cannot use the offline wheelhouse")
            kpip_args.append(kpip_source)
        elif wheelhouse is None:
            kpip_args.extend(["-r", kpip_source])
        else:
            kpip_args.extend(
                ["--no-index", "--find-links", wheelhouse, "-r", kpip_source]
            )
            uv_args.extend(["--no-index", "--find-links", wheelhouse])
        if constraint_requirements is not None:
            kpip_args.extend(["--constraint", constraint_requirements])
            uv_args.extend(["--constraint", constraint_requirements])
        return [
            kpip_step(
                kpip_prepare,
                kpip_args,
                extra_env={"KPIP_CACHE_DIR": str(kpip_cache)},
            ),
            uv_step(uv_prepare, uv_args),
        ]

    if benchmark == "install-incremental-warm":
        kpip_common = [
            "--quiet",
            "--no-compile",
            "--cache-dir",
            str(kpip_cache),
            "--target",
            str(kpip_target),
            "--no-index",
            "--find-links",
            incremental_wheelhouse,
        ]
        uv_common = [
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--target",
            str(uv_target),
            "--python",
            python,
            "--no-index",
            "--find-links",
            incremental_wheelhouse,
        ]
        kpip_base, kpip_base_env = kpip(
            [
                "install",
                "--ignore-installed",
                *kpip_common,
                "-r",
                incremental_base,
            ],
        )
        uv_base = uv_command(
            uv_path,
            ["pip", "install", *uv_common, "-r", incremental_base],
        )
        kpip_update, kpip_update_env = kpip(
            ["install", "--upgrade", *kpip_common, "-r", incremental_update],
        )
        uv_update = uv_command(
            uv_path,
            [
                "pip",
                "install",
                "--upgrade",
                *uv_common,
                "-r",
                incremental_update,
            ],
        )
        kpip_prepare = chain_command(
            [cleanup_step([kpip_target]), run_step(kpip_base, kpip_base_env)],
        )
        uv_prepare = chain_command(
            [cleanup_step([uv_target]), run_step(uv_base)],
        )
        return [
            Command(label("kpip"), kpip_prepare, kpip_update, kpip_update_env),
            Command(label("uv"), uv_prepare, uv_update),
        ]

    if benchmark.startswith("install-"):
        if install_requirements is None:
            raise ValueError(
                f"{workload_name} has no official compiled install workload; "
                "run a lock benchmark instead",
            )
        cold = benchmark.endswith("cold")
        kpip_prepare = prepare_with_cache(
            workspace,
            cache=kpip_cache,
            outputs=[kpip_target],
            cold=cold,
        )
        uv_prepare = prepare_with_cache(
            workspace,
            cache=uv_cache,
            outputs=[uv_target],
            cold=cold,
        )
        kpip_args = [
            "install",
            "--quiet",
            "--ignore-installed",
            "--no-compile",
            "--cache-dir",
            str(kpip_cache),
            "--target",
            str(kpip_target),
            "-r",
            install_requirements,
        ]
        uv_args = [
            "pip",
            "install",
            "--quiet",
            "--cache-dir",
            str(uv_cache),
            "--target",
            str(uv_target),
            "--python",
            python,
            "-r",
            install_requirements,
        ]
        if wheelhouse is not None:
            kpip_args.extend(
                [
                    "--no-index",
                    "--find-links",
                    wheelhouse,
                ]
            )
            uv_args.extend(["--no-index", "--find-links", wheelhouse])
        return [
            kpip_step(kpip_prepare, kpip_args),
            uv_step(uv_prepare, uv_args),
        ]

    raise ValueError(f"Unknown benchmark: {benchmark}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark kpip against uv with hyperfine."
    )
    parser.add_argument("--benchmark", "-b", choices=BENCHMARKS, action="append")
    parser.add_argument(
        "--workload",
        choices=WORKLOAD_NAMES,
        default="offline",
        help="Workload to run; 'live' expands to every official uv workload",
    )
    parser.add_argument(
        "--list-workloads",
        action="store_true",
        help="List workload capabilities and exit",
    )
    parser.add_argument("--kpip-python", default=sys.executable)
    parser.add_argument("--kpip-console")
    parser.add_argument(
        "--kpip-launcher", choices=("module", "direct"), default="module"
    )
    parser.add_argument("--uv-path", default=shutil.which("uv") or "uv")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--min-runs", type=int)
    parser.add_argument("--runs", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args()

    if args.list_workloads:
        print_workloads()
        return

    min_runs = args.min_runs
    if args.runs is None and min_runs is None:
        min_runs = 10

    workloads = expand_workloads(args.workload)
    runs: list[tuple[str, str]] = []
    for workload in workloads:
        benchmarks = tuple(args.benchmark or default_benchmarks(workload))
        for benchmark in benchmarks:
            if supports_benchmark(workload, benchmark):
                runs.append((workload, benchmark))
            elif args.workload == "live":
                print(
                    f"Skipping {workload}/{benchmark}: unsupported by workload",
                    file=sys.stderr,
                )
            else:
                parser.error(f"{workload} does not support {benchmark}")
    if not runs:
        parser.error("No supported workload and benchmark combinations selected")

    if args.json and not args.dry_run:
        metadata = collect_run_metadata(
            kpip_python=args.kpip_python,
            uv_path=args.uv_path,
        )
        Path("meta.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="kpip-bench-") as temporary:
        workspace_root = Path(temporary)
        for workload, benchmark in runs:
            workspace = workspace_root / workload / benchmark
            workspace.mkdir(parents=True)
            commands = build_commands(
                benchmark,
                workload=workload,
                workspace=workspace,
                kpip_python=args.kpip_python,
                kpip_console=args.kpip_console,
                kpip_launcher=args.kpip_launcher,
                uv_path=args.uv_path,
                python=args.python,
            )
            setup = None
            if benchmark.endswith("warm") or benchmark.startswith("startup-fast-"):
                setup = warm_setup(
                    commands,
                    [
                        workspace / "cache",
                        workspace / "target",
                        workspace / "kpip.out",
                        workspace / "uv.out",
                    ],
                    log=setup_log(workspace),
                )
            run = Hyperfine(
                name=(
                    benchmark if workload == "offline" else f"{workload}-{benchmark}"
                ),
                commands=commands,
                setup=setup,
                setup_log=setup_log(workspace) if setup is not None else None,
                warmup=args.warmup,
                min_runs=min_runs,
                runs=args.runs,
                verbose=args.verbose,
                json=args.json,
                ignore_failure=benchmark == "startup-invalid-command",
            )
            if args.dry_run:
                print(env_prefix(run.environment()) + command_line(run.args()))
            else:
                run.run()
                if not args.keep_workspace:
                    # A finished benchmark's caches (hundreds of MB for a
                    # live workload, for kpip and uv each) would otherwise
                    # sit in the temporary directory until the whole sweep
                    # ends, and a small or tmpfs /tmp fills up mid-sweep.
                    shutil.rmtree(workspace, ignore_errors=True)
        if args.keep_workspace:
            kept = Path(os.getcwd()) / "kpip-benchmark-workspace"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(workspace_root, kept)
            print(f"Kept workspace at {kept}")


if __name__ == "__main__":
    main()
