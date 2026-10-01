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

from kpip.core.compiled import is_compiled
from kpip.core.errors import CommandError, DiagnosticKpipError
from kpip.core.packaging import target_python_version
from kpip.host import interpreter_facts


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


def _candidates(target: str) -> list[str]:
    candidates = []

    for _, prefix in interpreter_facts.active_environments(strict=False):
        candidates.extend(
            os.path.join(prefix, *name.split("/"))
            for name in interpreter_facts.environment_pythons()
        )

    for name in (f"python{target}", "python3", "python"):
        found = shutil.which(name)

        if found is not None:
            candidates.append(found)

    candidates.extend(interpreter_facts.registered_pythons())

    return candidates


def _discover() -> str:
    # The version the build is for: the lock's, or the target's -- never the
    # Python the binary bundles, which nothing is built for.
    requested = target_python_version()
    target = (
        ".".join(requested.split(".")[:2])
        if requested
        else interpreter_facts.target_interpreter(installing=False).major_minor
    )
    tried: list[str] = []
    fallback: str | None = None

    for candidate in _candidates(target):
        if candidate in tried or not os.path.exists(candidate):
            continue

        tried.append(candidate)

        try:
            interpreter = interpreter_facts.probe(candidate)

        except CommandError:
            continue

        # It must be able to create the environment a build runs in.
        if not interpreter.venv:
            continue

        version = interpreter.major_minor

        # The interpreter that answered, not the file found: a pyenv-win
        # .bat shim cannot be handed code with -c.
        executable = interpreter.executable or candidate

        # The target's own version first: an sdist's metadata may depend on
        # the interpreter that prepares it.
        if version == target:
            return executable

        if fallback is None:
            fallback = executable

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
