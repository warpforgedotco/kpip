"""kpip's Nuitka patches let compiled programs run subinterpreters, safely.

Each test compiles a small program with the vendored Nuitka and runs it:
minutes of C compilation, so they run only with ``KPIP_NUITKA_TESTS=1`` (the
compile workflow sets it after building kpip, which vendors Nuitka).

The programs' subinterpreters import from the binary alone: the bytecode
``--subinterpreter-bytecode`` embeds (patch 0014) of the modules a worker is
found to load, run first on a pool of plain Python.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROGRAMS = Path(__file__).parent / "nuitka_programs"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KPIP_NUITKA_TESTS") != "1",
        reason="compiles programs with Nuitka; set KPIP_NUITKA_TESTS=1",
    ),
    pytest.mark.skipif(
        sys.version_info < (3, 14) or sys.implementation.name != "cpython",
        reason="subinterpreter pools arrived in CPython 3.14",
    ),
]

_PROBE = """\
import json
import sys
from concurrent.futures import InterpreterPoolExecutor

import workers

with InterpreterPoolExecutor(max_workers=1) as pool:
    pool.submit(workers.raise_and_catch, 1).result()
    pool.submit(workers.hash_and_compare, 0.0).result()
    json.dump(pool.submit(workers.loaded).result(), sys.stdout)
"""


def worker_modules() -> list[str]:
    """The modules the programs' workers load, run on plain Python."""
    import json

    from kpip_compile.workers import bytecode_modules

    result = subprocess.run(
        [sys.executable, "-S", "-c", _PROBE],
        cwd=PROGRAMS,
        env=dict(os.environ, PYTHONPATH=str(PROGRAMS)),
        capture_output=True,
        text=True,
        check=True,
    )
    return bytecode_modules(json.loads(result.stdout))


@pytest.fixture(scope="module")
def nuitka_dir() -> Path:
    """The vendored Nuitka, or the checkout ``KPIP_NUITKA_DIR`` names."""
    explicit = os.environ.get("KPIP_NUITKA_DIR")
    if explicit:
        return Path(explicit)

    from kpip_compile.cli import _vendor

    return _vendor(force=False)


def compile_program(
    nuitka_dir: Path, source: Path, workdir: Path, extra: tuple[str, ...] = ()
) -> Path:
    """``source`` compiled standalone, with bytecode for its workers."""
    for program in PROGRAMS.iterdir():
        if program.suffix == ".py":
            shutil.copy(program, workdir / program.name)
    env = dict(os.environ, PYTHONPATH=str(nuitka_dir))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "nuitka",
            "--mode=standalone",
            "--assume-yes-for-downloads",
            "--quiet",
            f"--output-dir={workdir / 'build'}",
            "--subinterpreter-bytecode=" + ",".join(worker_modules()),
            *extra,
            str(workdir / source.name),
        ],
        cwd=workdir,
        env=env,
        check=True,
    )
    dist = workdir / "build" / f"{source.stem}.dist"
    # Nothing beside the binary for the workers to import from.
    assert not list(dist.glob("*.py*")), sorted(dist.iterdir())
    return dist / (f"{source.stem}.exe" if os.name == "nt" else f"{source.stem}.bin")


def run(binary: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=300, check=False
    )


def test_tracebacks_freed_by_subinterpreters_leave_the_heap_whole(
    nuitka_dir: Path, tmp_path: Path
) -> None:
    """Unpatched, every run aborted: subinterpreters freed their tracebacks
    onto the main interpreter's free list (patch 0012)."""
    binary = compile_program(nuitka_dir, PROGRAMS / "tracebacks.py", tmp_path)

    for _ in range(3):
        result = run(binary)
        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip().endswith("ok")


def test_loading_constants_leaves_subinterpreters_hashing_their_own_way(
    nuitka_dir: Path, tmp_path: Path
) -> None:
    """Unpatched, a subinterpreter's tuples hashed by address and its floats
    compared as equal while the main interpreter loaded constants (patch
    0013)."""
    lines = ["def load_all():"]
    for index in range(300):
        values = ", ".join(
            f"({index}, {offset}, {index * offset}.5), {offset}.25, "
            f"frozenset({{{index}, {offset}}}), [{index}, {offset}]"
            for offset in range(25)
        )
        (tmp_path / f"constant_module_{index}.py").write_text(
            f"VALUES = ({values},)\n", encoding="utf-8"
        )
        lines.append(f"    import constant_module_{index}  # noqa: F401")
    (tmp_path / "constant_modules.py").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    # Imported by name as well, so each must be compiled in.
    binary = compile_program(
        nuitka_dir,
        PROGRAMS / "constants.py",
        tmp_path,
        tuple(f"--include-module=constant_module_{index}" for index in range(300)),
    )

    result = run(binary)

    assert result.returncode == 0, result.stdout + result.stderr[-2000:]
    assert "failures=0 " in result.stdout
