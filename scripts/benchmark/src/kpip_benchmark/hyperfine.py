from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

NO_SHELL = "-N"


def command_line(command: list[str]) -> str:
    """Render ``command`` as the single string hyperfine takes per command.

    Under --shell=none hyperfine splits that string back into an argv with
    POSIX word-splitting rules on every platform, so this is ``shlex.join``
    even on Windows -- there is no cmd.exe in the loop to quote for.
    """
    return shlex.join(command)


def env_prefix(env: dict[str, str] | None) -> str:
    """A POSIX shell env-assignment prefix, e.g. ``FOO=bar BAZ=qux ``.

    Only used to render a copy-pasteable ``--dry-run`` line now that the real
    run passes the environment to hyperfine directly. Quotes only the values,
    so the leading tokens still parse as shell assignments (a fully-quoted
    ``'FOO=bar'`` token would not).
    """
    if not env:
        return ""
    return "".join(f"{name}={shlex.quote(value)} " for name, value in env.items())


def noop_prepare() -> str:
    """A preparation command that does nothing and succeeds.

    hyperfine wants --prepare either once or once per command, and rejects an
    empty one under --shell=none. ``runner cleanup`` with nothing to clean is
    the no-op for commands that need no preparation.
    """
    return command_line([sys.executable, "-m", "kpip_benchmark.runner", "cleanup"])


@dataclass(frozen=True)
class Command:
    name: str
    prepare: str | None
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Hyperfine:
    name: str
    commands: list[Command]
    setup: str | None
    warmup: int | None
    min_runs: int | None
    runs: int | None
    verbose: bool
    json: bool
    ignore_failure: bool = False
    setup_log: Path | None = None
    """Where a failed ``setup`` chain wrote its output; shown when hyperfine fails."""

    def args(self) -> list[str]:
        args = ["hyperfine", NO_SHELL]
        if self.json:
            args.extend(["--export-json", f"{self.name}.json"])
        if self.verbose:
            args.append("--show-output")
        if self.ignore_failure:
            args.append("--ignore-failure")
        if self.setup is not None:
            args.extend(["--setup", self.setup])
        if self.warmup is not None:
            args.extend(["--warmup", str(self.warmup)])
        if self.min_runs is not None:
            args.extend(["--min-runs", str(self.min_runs)])
        if self.runs is not None:
            args.extend(["--runs", str(self.runs)])
        for command in self.commands:
            args.extend(["--command-name", command.name])
        if any(command.prepare for command in self.commands):
            noop = noop_prepare()
            for command in self.commands:
                args.extend(["--prepare", command.prepare or noop])
        args.extend(command_line(command.command) for command in self.commands)
        return args

    def environment(self) -> dict[str, str]:
        """The env vars every command in this benchmark runs with.

        --shell=none leaves no shell to carry a per-command assignment, and
        wrapping each command in a helper interpreter would put a whole Python
        startup inside the timed region, so the variables ride on hyperfine
        itself and are inherited. Only kpip asks for any (PYTHONPATH,
        KPIP_CACHE_DIR): uv is a static binary that ignores both, and the
        PYTHONPATH entry holds the single ``kpip`` package, so it cannot shadow
        an import in a build subprocess uv spawns. Commands that disagree on a
        value would need a real per-command mechanism, so that is an error
        rather than a silent last-one-wins.
        """
        merged: dict[str, str] = {}
        for command in self.commands:
            for name, value in command.env.items():
                previous = merged.setdefault(name, value)
                if previous != value:
                    raise ValueError(
                        f"{self.name}: commands disagree on {name}: "
                        f"{previous!r} != {value!r}",
                    )
        return merged

    def run(self) -> None:
        environment = os.environ.copy()
        environment.update(self.environment())
        try:
            subprocess.check_call(self.args(), env=environment)
        except subprocess.CalledProcessError:
            if self.setup_log is not None and self.setup_log.exists():
                sys.stderr.write(
                    f"\n== {self.name}: setup failed; {self.setup_log} ==\n"
                    f"{self.setup_log.read_text()}\n"
                )
            raise
