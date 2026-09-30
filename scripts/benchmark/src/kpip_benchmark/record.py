"""Record a kpip-bench baseline, refusing to measure a busy machine.

A baseline taken under load is worse than no baseline: it looks authoritative
and quietly poisons every comparison made against it. So the preflight is a
refusal, not a warning.

This is a Python entry point rather than a shell script on purpose. A script
with a ``#!`` line pointing at a system shell is itself an exec of a
SIP-protected binary, which strips ``DYLD_*`` from the environment of
everything it goes on to spawn -- including the hyperfine run underneath. That
is the very thing ``--shell=none`` exists to avoid here, so there is no shell
anywhere in this path either.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from kpip_benchmark.cli import repo_root

DEFAULT_WORKLOADS = ("offline", "live")


def is_macos() -> bool:
    return platform.system() == "Darwin"


def pmset(argument: str) -> str:
    """Read a pmset report, or an empty string where pmset doesn't exist."""
    try:
        return subprocess.check_output(
            ["pmset", "-g", argument],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except OSError, subprocess.CalledProcessError:
        return ""


def on_battery() -> bool:
    return "Battery Power" in pmset("batt")


THERMAL_LIMIT_KEYS = ("CPU_Speed_Limit", "CPU_Scheduler_Limit")


def parse_thermal_limits(report: str) -> list[int]:
    """The CPU limit percentages ``pmset -g therm`` reports, if any."""
    limits = []
    for line in report.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key.strip() not in THERMAL_LIMIT_KEYS:
            continue
        try:
            limits.append(int(value.strip()))
        except ValueError:
            continue
    return limits


def is_throttled() -> bool:
    """Whether the CPU is actually being held below full speed.

    ``pmset`` prints these keys whenever it has a CPU power status to report
    at all, and the unthrottled value is 100 -- so reading the key's presence
    as throttling refuses to record on a perfectly idle machine.
    """
    return any(limit < 100 for limit in parse_thermal_limits(pmset("therm")))


def load_average() -> float | None:
    """The 1-minute load average, or ``None`` where there is no such thing.

    ``os.getloadavg`` does not exist on Windows, and nothing restricts this
    command to Unix.
    """
    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    try:
        return getloadavg()[0]
    except OSError:
        return None


def busiest_processes(count: int = 5) -> list[str]:
    """The top CPU consumers, to name what has to be closed.

    ``top -l 1`` reports 0.0% for everything -- CPU share needs a second
    sample to diff against -- so this always takes two.
    """
    if not is_macos():
        return []
    try:
        output = subprocess.check_output(
            ["top", "-l", "2", "-n", str(count), "-o", "cpu", "-stats", "command,cpu"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except OSError, subprocess.CalledProcessError:
        return []
    return [line.rstrip() for line in output.splitlines()[-count - 1 :] if line.strip()]


def preflight_failures(
    *,
    load: float | None,
    max_load: float,
    battery: bool,
    throttled: bool,
) -> list[str]:
    """Every reason this machine is unfit to record, in report order.

    Split out from the probing so the policy is testable without a busy
    machine to test it on.
    """
    failures = []
    if load is None:
        failures.append(
            "this platform reports no load average, "
            "so a busy machine cannot be ruled out",
        )
    elif load > max_load:
        failures.append(
            f"load average {load:.2f} is above the {max_load:.2f} ceiling",
        )
    if battery:
        failures.append(
            "running on battery; macOS throttles sustained P-core boost unplugged",
        )
    if throttled:
        failures.append("the CPU is thermally limited right now")
    return failures


def default_output(root: Path) -> Path:
    branch = "detached"
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except OSError, subprocess.CalledProcessError:
        pass
    stamp = time.strftime("%Y%m%d-%H%M")
    return root / "benchmark-runs" / f"{branch.replace('/', '-')}-{stamp}"


def record(output: Path, workloads: list[str], extra: list[str]) -> None:
    """Run one kpip-bench export per workload into ``output``.

    ``--json`` writes its exports to the working directory, so each sweep runs
    with ``output`` as its cwd. Offline exports are named for the benchmark and
    live ones for the workload too, so they share a directory without
    colliding, and a later sweep can be added to an earlier one.
    """
    output.mkdir(parents=True, exist_ok=True)
    for workload in workloads:
        print(f"== {workload} workload ==", flush=True)
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "kpip_benchmark.cli",
                "--json",
                "--workload",
                workload,
                *extra,
            ],
            cwd=output,
        )


def main() -> int:
    cores = os.cpu_count() or 1
    parser = argparse.ArgumentParser(
        description="Record a kpip-bench baseline on a quiet machine.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Directory for the JSON exports "
        "(default: benchmark-runs/<branch>-<timestamp>)",
    )
    parser.add_argument(
        "--workload",
        action="append",
        dest="workloads",
        help=f"Workload to sweep, repeatable (default: {', '.join(DEFAULT_WORKLOADS)})",
    )
    parser.add_argument(
        "--max-load",
        type=float,
        default=cores / 4,
        help="Refuse to record above this 1-minute load average "
        f"(default: cores/4 = {cores / 4:.2f})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Record anyway. The numbers will not be a usable baseline.",
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to kpip-bench, after --",
    )
    args = parser.parse_args()

    failures = preflight_failures(
        load=load_average(),
        max_load=args.max_load,
        battery=on_battery(),
        throttled=is_throttled(),
    )
    if failures and not args.force:
        print("Refusing to record -- this machine is not quiet:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        for line in busiest_processes():
            print(f"    {line}", file=sys.stderr)
        print("\nNothing recorded. Re-run when idle, or --force.", file=sys.stderr)
        return 1
    if failures:
        print("Preflight failed but --force was given; these are not a baseline.")

    root = repo_root()
    output = args.output or default_output(root)
    workloads = args.workloads or list(DEFAULT_WORKLOADS)
    extra = [argument for argument in args.extra if argument != "--"]

    record(output, workloads, extra)

    exports = sorted(path.name for path in output.glob("*.json"))
    print(f"\nRecorded {len(exports)} export(s) in {output}")
    print(f"Compare a later run with:  kpip-bench-compare {output} <later-run>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
