"""kpip's Nuitka patches keep compiled code correct when threads share it.

Compiles ``nuitka_programs/threads.py`` with the vendored Nuitka and runs it:
minutes of C compilation, so it runs only with ``KPIP_NUITKA_TESTS=1`` (the
compile workflow sets it after building kpip, which vendors Nuitka).

Run by a free-threaded Python, the program compiles without the GIL and its
threaded cases check patch 0015. With the GIL they still check what applies
to both builds: references released, exact results, and assignment
expressions bound in their function (patch 0016).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROGRAM = Path(__file__).parent / "nuitka_programs" / "threads.py"

pytestmark = pytest.mark.skipif(
    os.environ.get("KPIP_NUITKA_TESTS") != "1",
    reason="compiles a program with Nuitka; set KPIP_NUITKA_TESTS=1",
)


@pytest.fixture(scope="module")
def binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The program compiled standalone with the vendored Nuitka."""
    explicit = os.environ.get("KPIP_NUITKA_DIR")
    if explicit:
        nuitka_dir = Path(explicit)
    else:
        from kpip_compile.cli import _vendor

        nuitka_dir = _vendor(force=False)

    workdir = tmp_path_factory.mktemp("threads")
    shutil.copy(PROGRAM, workdir / PROGRAM.name)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "nuitka",
            "--mode=standalone",
            "--assume-yes-for-downloads",
            "--quiet",
            f"--output-dir={workdir / 'build'}",
            str(workdir / PROGRAM.name),
        ],
        cwd=workdir,
        env=dict(os.environ, PYTHONPATH=str(nuitka_dir)),
        check=True,
    )
    name = f"{PROGRAM.stem}.exe" if os.name == "nt" else f"{PROGRAM.stem}.bin"
    return workdir / "build" / f"{PROGRAM.stem}.dist" / name


def test_compiled_code_shared_by_threads_stays_correct(binary: Path) -> None:
    """Unpatched without the GIL, this crashed, hung, or computed wrong
    results within a run; unpatched with it, assignment expressions in
    generator expressions leaked into the module (patch 0016)."""
    # Races show some of the time: a few runs, each on fresh threads.
    for _ in range(3):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=600, check=False
        )

        assert result.returncode == 0, result.stdout + result.stderr[-2000:]
        assert result.stdout.startswith("compiled=True"), result.stdout
        assert "FAIL" not in result.stdout, result.stdout
