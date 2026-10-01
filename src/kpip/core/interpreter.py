"""The Python interpreter that runs build backends.

Interpreted, kpip builds with the interpreter running it, as it always has.
Compiled into a single binary, there is no such interpreter: ``sys.executable``
is kpip itself, and asking it to ``-m venv`` or to run a PEP 517 hook asks the
binary to be a Python it is not. A build then needs a real one, found the way
an installer finds the environment it serves: an explicit choice, the active
environment, then ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from kpip.core.compiled import is_compiled
from kpip.core.errors import DiagnosticKpipError
from kpip.core.packaging import target_python_version
from kpip.host import interpreter_facts

_PROBE = "import sys, venv; print('%d.%d' % sys.version_info[:2])"

_build_interpreters: dict[tuple[str | None, str | None, str | None], str] = {}


class NoBuildInterpreterError(DiagnosticKpipError):
    reference = "no-build-interpreter"

    def __init__(self, tried: list[str]) -> None:
        super().__init__(
            message="Cannot find a Python interpreter to build this source distribution",
            context=(
                "kpip is compiled into a single binary, so it has no interpreter of "
                "its own to run build backends with. Tried: "
                + (", ".join(tried) if tried else "nothing on PATH")
            ),
            hint_stmt=(
                "Activate a virtual environment, put python3 on PATH, or set "
                "KPIP_BUILD_PYTHON to the interpreter to build with."
            ),
        )


def _environment_python(prefix: str) -> str:
    if os.name == "nt":
        return os.path.join(prefix, "Scripts", "python.exe")
    return os.path.join(prefix, "bin", "python")


def _probe(executable: str) -> str | None:
    """The interpreter's ``major.minor`` if it runs and can create environments."""

    try:
        result = subprocess.run(
            [executable, "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None

    version = result.stdout.strip()

    return version if result.returncode == 0 and version else None


def _candidates(target: str) -> list[str]:
    candidates = []

    for variable in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        prefix = os.environ.get(variable)

        if prefix:
            candidates.append(_environment_python(prefix))

    for name in (f"python{target}", "python3", "python"):
        found = shutil.which(name)

        if found is not None:
            candidates.append(found)

    return candidates


def _discover() -> str:
    target = target_python_version() or "%d.%d" % sys.version_info[:2]
    tried: list[str] = []
    fallback: str | None = None

    for candidate in _candidates(target):
        if candidate in tried:
            continue

        tried.append(candidate)
        version = _probe(candidate)

        if version is None:
            continue

        # The target's own version first: an sdist's metadata may depend on
        # the interpreter that prepares it.
        if version == target:
            return candidate

        if fallback is None:
            fallback = candidate

    if fallback is not None:
        return fallback

    raise NoBuildInterpreterError(tried)


def build_interpreter() -> str:
    """The Python to create build environments and run build backends with.

    ``KPIP_BUILD_PYTHON`` wins when set. Otherwise it is the Python kpip
    installs for, as pip, run under it, builds with it: the one ``--python``
    names, or the one running kpip. A compiled kpip has none of its own, and
    discovers one -- the active virtual or conda environment's, then
    ``python<target>``, ``python3`` and ``python`` on ``PATH`` -- preferring
    the lock's target version when ``--python-version`` names one. The
    answer is kept for the process.
    """

    explicit = os.environ.get("KPIP_BUILD_PYTHON") or None
    key = (explicit, target_python_version(), os.environ.get("KPIP_PYTHON"))
    found = _build_interpreters.get(key)

    if found is None:
        if explicit is not None:
            found = explicit
        else:
            target = interpreter_facts.target_interpreter(installing=False)
            if is_compiled() and (target.is_own or target_python_version()):
                found = _discover()
            else:
                found = target.executable

        _build_interpreters[key] = found

    return found
