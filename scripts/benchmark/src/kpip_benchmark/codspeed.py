"""Walltime benchmarks of the compiled kpip, for CodSpeed's exec harness.

kpip ships only as the binary ``scripts/compile`` builds, so this suite runs
that binary and nothing else. ``prepare`` lays out a workspace the commands
run against -- the offline workload's wheelhouse, a warm cache, a virtual
environment the workload is already installed in -- and writes the
``codspeed.yml`` that names them. ``codspeed run -m walltime --config
<workspace>/codspeed.yml`` then times each one.

The exec harness has no per-round setup: it splits each ``exec`` string into
argv (no shell) and runs the same command round after round. So every
command here is one that repeats unchanged. A cold case is the same command
with ``--no-cache-dir`` rather than a cache emptied between rounds, and an
install either goes into ``--target`` with ``--ignore-installed`` (it lays
every file down again each round) or into an environment that already has
everything (the "nothing to do" path users hit most).

Everything is offline. Live PyPI belongs to ``kpip-bench``: on a shared CI
runner the network would dominate whatever kpip did.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from kpip_benchmark.workloads import write_offline_workload

DEFAULT_WORKSPACE = Path(".codspeed")


class Workspace:
    """Where ``prepare`` puts each thing the benchmarks read."""

    __slots__ = ("root",)

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def kpip(self) -> Path:
        return self.root / "kpip"

    @property
    def workload(self) -> Path:
        return self.root / "workload"

    @property
    def wheelhouse(self) -> Path:
        return self.workload / "wheelhouse"

    @property
    def requirements(self) -> Path:
        return self.workload / "requirements.in"

    @property
    def trivial(self) -> Path:
        return self.workload / "trivial.in"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @property
    def target(self) -> Path:
        return self.root / "target"

    @property
    def output(self) -> Path:
        return self.root / "lock.toml"

    @property
    def config(self) -> Path:
        return self.root / "codspeed.yml"


def benchmarks(workspace: Workspace) -> list[tuple[str, list[str]]]:
    """Each benchmark's name and the kpip arguments it runs."""
    find_links = ["--no-index", "--find-links", str(workspace.wheelhouse)]
    cache = ["--cache-dir", str(workspace.cache)]
    lock = ["lock", "--quiet", *find_links, "--output", str(workspace.output)]
    to_target = [
        "install",
        "--quiet",
        "--ignore-installed",
        "--no-compile",
        *find_links,
        "--target",
        str(workspace.target),
        "-r",
        str(workspace.requirements),
    ]
    in_venv = ["--python", str(workspace.venv)]
    return [
        ("startup --version", ["--version"]),
        ("startup --help", ["--help"]),
        ("startup install --help", ["install", "--help"]),
        ("list (venv)", [*in_venv, "list", "--format=json"]),
        ("lock trivial (warm)", [*lock, *cache, "-r", str(workspace.trivial)]),
        ("lock offline (warm)", [*lock, *cache, "-r", str(workspace.requirements)]),
        (
            "lock offline (cold)",
            [*lock, "--no-cache-dir", "-r", str(workspace.requirements)],
        ),
        ("install offline --target (warm)", [*to_target, *cache]),
        ("install offline --target (cold)", [*to_target, "--no-cache-dir"]),
        (
            "install offline (venv, satisfied)",
            [
                *in_venv,
                "install",
                "--quiet",
                *find_links,
                *cache,
                "-r",
                str(workspace.requirements),
            ],
        ),
    ]


def exec_string(argv: list[str]) -> str:
    """One argv as the exec harness reads it back (``shell_words::split``)."""
    return shlex.join(argv)


def render_config(workspace: Workspace) -> dict:
    return {
        "options": {"warmup-time": "1s", "max-time": "10s"},
        "benchmarks": [
            {
                "name": f"kpip {name}",
                "exec": exec_string([str(workspace.kpip), *args]),
            }
            for name, args in benchmarks(workspace)
        ],
    }


def run(argv: list[str]) -> None:
    print("+ " + " ".join(argv), file=sys.stderr, flush=True)
    subprocess.run(argv, check=True)


def prepare(workspace: Workspace, *, kpip: Path, python: str) -> None:
    """Lay out ``workspace`` and run every benchmark once.

    The single run warms the cache the warm cases read, installs the workload
    into the environment the satisfied case finds it in, unpacks a onefile
    binary, and fails here, with kpip's own output, a command that would
    otherwise fail inside the harness.
    """
    if workspace.root.exists():
        shutil.rmtree(workspace.root)
    workspace.root.mkdir(parents=True)
    # A copy, so a rebuild under the source path cannot change the binary
    # mid-run, and the ``exec`` lines stay the same wherever it was built.
    shutil.copy2(kpip, workspace.kpip)
    write_offline_workload(workspace.workload)
    workspace.trivial.write_text("leaf-0\n", encoding="utf-8")
    run([python, "-m", "venv", "--without-pip", str(workspace.venv)])
    # The satisfied install needs the workload in place before its first run.
    run(
        [
            str(workspace.kpip),
            "--python",
            str(workspace.venv),
            "install",
            "--quiet",
            "--no-index",
            "--find-links",
            str(workspace.wheelhouse),
            "--cache-dir",
            str(workspace.cache),
            "-r",
            str(workspace.requirements),
        ]
    )
    for _name, args in benchmarks(workspace):
        run([str(workspace.kpip), *args])
    # JSON is YAML, and needs no YAML library to write.
    workspace.config.write_text(
        json.dumps(render_config(workspace), indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {workspace.config}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare CodSpeed walltime benchmarks of a compiled kpip."
    )
    parser.add_argument(
        "kpip", type=Path, help="The compiled kpip (scripts/compile's build/kpip)"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="Directory to lay the benchmarks out in (replaced if present)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Interpreter the benchmarks' virtual environment is made from",
    )
    args = parser.parse_args()
    if not args.kpip.is_file():
        parser.error(f"no such file: {args.kpip}")
    prepare(
        Workspace(args.workspace.resolve()),
        kpip=args.kpip.resolve(),
        python=args.python,
    )
