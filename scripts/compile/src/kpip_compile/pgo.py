"""Profile-guided optimization of the compiled kpip.

Nuitka's ``--pgo-c`` builds an instrumented kpip, runs the training given
as ``--pgo-executable`` in its complete distribution, and then compiles the
final binary with the collected profile. The training is this module, run
as ``python -m kpip_compile.pgo <binary> <result file>``.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kpip_compile.vendor import REPO_ROOT

REQUIREMENTS_DIR = REPO_ROOT / "scripts" / "benchmark" / "requirements"
"""The benchmark's requirement sets, which the training resolves as well."""

# What the training resolves, and for which Python. The widest mix the
# benchmark sets allow: large and small graphs, heavy backtracking, URL and
# VCS requirements, an sdist to build, extras, and two sets with no answer,
# so the conflict report is profiled too. A set that needs another Python
# resolves for it with --python-version, which is a path of its own.
TRAINING_RESOLVES: tuple[tuple[str, str | None], ...] = (
    ("airflow.in", None),
    ("jupyter.in", None),
    ("black.in", None),
    ("boto3.in", None),
    ("dtlssocket.in", None),
    ("trio.in", "3.12"),
    ("scispacy.in", "3.12"),
    ("slow.in", "3.12"),
    ("all-kinds.in", "3.12"),
    ("bio_embeddings.in", "3.12"),
    ("backtracking/sentry.in", "3.12"),
    ("backtracking/starlette-fastapi.in", "3.12"),
    ("backtracking/numpy-numba.in", "3.12"),
    ("backtracking/numpy-sparse.in", "3.12"),
    ("backtracking/apache-beam-dill.in", "3.10"),
    ("pydantic.in", "3.12"),
    ("flyte.in", "3.12"),
)

WEB_REQUIREMENTS = "requests\nrich\nhttpx\nfastapi\n"


class TrainingStep:
    __slots__ = ("arguments", "may_fail")

    def __init__(self, arguments: list[str], *, may_fail: bool = False) -> None:
        self.arguments = arguments
        # Steps that reach the network, git, or a build backend, or that are
        # meant to fail, must not end a training run; what they did run still
        # left a profile behind.
        self.may_fail = may_fail


RESULT_OK = "ok"


class PgoError(RuntimeError):
    pass


def check_supported(platform: str = sys.platform) -> None:
    """Refuse platforms the training script cannot run on."""
    if platform == "win32":
        raise PgoError("--pgo supports macOS and Linux, not Windows")


def training_steps(work: Path, cache: Path) -> list[TrainingStep]:
    """The kpip invocations a profile is collected from, in order."""
    cache_dir = ["--cache-dir", str(cache)]
    steps = [
        TrainingStep(["--version"]),
        TrainingStep(["--help"]),
        *(
            TrainingStep([command, "--help"])
            for command in ("lock", "install", "download", "list")
        ),
    ]

    web = str(work / "web.txt")
    for name, python_version in (
        (str(REQUIREMENTS_DIR / name), version) for name, version in TRAINING_RESOLVES
    ):
        target = ["--python-version", python_version] if python_version else []
        # The first lock of each fills the cache; the replays are removed
        # before every step, so the others resolve from it again.
        steps.extend(
            TrainingStep(
                [
                    "lock",
                    "--quiet",
                    *cache_dir,
                    *target,
                    "-r",
                    name,
                    "--output",
                    str(work / "out.toml"),
                ],
                may_fail=True,
            )
            for _ in range(3)
        )
    steps.extend(
        TrainingStep(
            [
                "lock",
                "--quiet",
                *cache_dir,
                "-r",
                web,
                "--output",
                str(work / "web.toml"),
            ]
        )
        for _ in range(3)
    )

    wheels = str(work / "wheels")
    target_dir = str(work / "target")
    local = ["--no-index", "-f", wheels]
    steps += [
        TrainingStep(["download", *cache_dir, "-d", wheels, "-r", web], may_fail=True),
        TrainingStep(
            [
                "install",
                *cache_dir,
                *local,
                "--dry-run",
                "--target",
                target_dir,
                "-r",
                web,
            ]
        ),
        TrainingStep(
            ["install", *cache_dir, *local, "--target", target_dir, "-r", web]
        ),
        TrainingStep(
            [
                "install",
                *cache_dir,
                *local,
                "--force-reinstall",
                "--compile",
                "--target",
                target_dir,
                "-r",
                web,
            ]
        ),
        TrainingStep(["list", "--path", target_dir]),
        TrainingStep(["list", "--path", target_dir, "--format", "json"]),
        TrainingStep(["freeze", "--path", target_dir]),
        TrainingStep(["inspect", "--path", target_dir]),
        TrainingStep(
            [
                "lock",
                "--quiet",
                *cache_dir,
                *local,
                "-r",
                web,
                "--output",
                str(work / "local.toml"),
            ]
        ),
        TrainingStep(
            [
                "wheel",
                *cache_dir,
                "--no-deps",
                "-w",
                str(work / "built"),
                "DTLSSocket==0.1.16",
            ],
            may_fail=True,
        ),
        TrainingStep(["cache", *cache_dir, "info"]),
        TrainingStep(["cache", *cache_dir, "list"]),
    ]
    return steps


def train(binary: Path) -> None:
    """Run ``binary`` over the training steps.

    Nuitka sets where the instrumented processes write their profiles.
    """
    with tempfile.TemporaryDirectory(prefix="kpip-pgo-") as directory:
        work = Path(directory)
        cache = work / "cache"
        (work / "web.txt").write_text(WEB_REQUIREMENTS, encoding="utf-8")

        steps = training_steps(work, cache)
        failed = 0
        for number, step in enumerate(steps, start=1):
            shutil.rmtree(cache / "v1" / "lock-replay-v1", ignore_errors=True)
            print(
                f"pgo [{number}/{len(steps)}]: kpip {' '.join(step.arguments[:2])}",
                flush=True,
            )
            result = subprocess.run(
                [str(binary), *step.arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode != 0:
                if not step.may_fail:
                    raise PgoError(
                        f"training step failed: kpip {' '.join(step.arguments)}"
                    )
                failed += 1
        print(
            f"pgo: {len(steps)} training steps, {failed} of those allowed to fail did",
            flush=True,
        )


def nuitka_options(script: Path, binary: Path, result: Path) -> list[str]:
    """Write the ``--pgo-executable`` script; returns the Nuitka options.

    Nuitka only warns when the training exits with an error, so its outcome
    goes to ``result`` as well, for :func:`check_training`.
    """
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m kpip_compile.pgo "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    result.unlink(missing_ok=True)
    return [
        "--pgo-c",
        f"--pgo-executable={script}",
        f"--pgo-args={shlex.join([str(binary), str(result)])}",
    ]


def check_training(result: Path) -> None:
    """Raise unless the training run of the build finished."""
    try:
        outcome = result.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PgoError("the PGO training never ran") from None
    if outcome != RESULT_OK:
        raise PgoError(outcome)


def main(argv: list[str] | None = None) -> int:
    binary, result = map(Path, sys.argv[1:] if argv is None else argv)
    try:
        train(binary)
    except PgoError as error:
        result.write_text(str(error), encoding="utf-8")
        print(f"error: {error}", file=sys.stderr)
        return 1
    result.write_text(RESULT_OK, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
