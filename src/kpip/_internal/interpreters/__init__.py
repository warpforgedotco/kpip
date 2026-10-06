"""The Python kpip installs for: the target interpreter.

pip installs for the interpreter running it. The compiled kpip has none of
its own, so it looks for one as uv does, and kpip run from source does the
same when told to with ``--python``:

1. ``--python``: an interpreter, or an environment directory.
2. Run from source, with no ``--python``: the interpreter running kpip.
3. Compiled:

   a. the activated virtual environment (``VIRTUAL_ENV``);
   b. the activated conda environment (``CONDA_PREFIX``), unless it is the
      base one;
   c. a ``.venv`` in the working directory or one above it, or the
      environment the working directory is inside;
   d. the activated conda base environment;
   e. ``python3``, then ``python``, on ``PATH``.

   With none of them, the CPython inside kpip stands in for resolving
   (``lock``, ``download``, ``wheel``), and anything that installs fails.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys

from kpip._internal.exceptions import CommandError
from kpip._internal.interpreters.facts import (
    Interpreter,
    OwnInterpreter,
    ProbedInterpreter,
    probe,
    set_cache_dir,
)
from kpip._internal.utils.compiled import is_compiled
from kpip._vendor.packaging import markers

__all__ = [
    "Interpreter",
    "NoTargetInterpreter",
    "build_interpreter",
    "ProbedInterpreter",
    "configure",
    "identify",
    "installing_interpreter",
    "interpreter_at",
    "search_path",
    "target_interpreter",
]

logger = logging.getLogger(__name__)

_own = OwnInterpreter()

# ``--python``, as given; None when it was not.
_requested: str | None = None

_target: Interpreter | None = None


class NoTargetInterpreter(CommandError):
    """There is no Python to install for."""


def configure(python: str | None, cache_dir: str | None) -> None:
    """Set the command's ``--python`` and where answers are kept."""
    global _requested, _target
    _requested = python or None
    _target = None
    set_cache_dir(cache_dir)


def environment_pythons() -> tuple[str, ...]:
    """Where an environment keeps its interpreter, most likely first."""
    if os.name == "nt":
        return ("Scripts/python.exe", "python.exe", "bin/python")
    return ("bin/python", "Scripts/python.exe")


def identify(python: str) -> str:
    """The interpreter ``python`` names: itself, or an environment's."""
    if os.path.isdir(python):
        for name in environment_pythons():
            candidate = os.path.join(python, name)
            if os.path.exists(candidate):
                return os.path.abspath(candidate)
    elif os.path.exists(python):
        return os.path.abspath(python)
    raise CommandError(f"Could not locate Python interpreter {python}")


def interpreter_at(executable: str) -> Interpreter:
    """The Python at ``executable``: this process's own when it is that one."""
    if not is_compiled() and os.path.abspath(executable) == os.path.abspath(
        sys.executable
    ):
        return _own
    return probe(executable)


def target_interpreter() -> Interpreter:
    """The Python kpip installs for (see the module's documentation)."""
    global _target
    if _target is None:
        _target = _find_target()
    return _target


def _target_environment() -> dict[str, str]:
    return target_interpreter().markers


# Every marker pip evaluates -- requirements, dependencies, build
# requirements -- without an environment of its own is evaluated against
# packaging's default environment: make that the target interpreter's.
markers.default_environment = _target_environment  # type: ignore[assignment]


def search_path() -> list[str]:
    """Where the target interpreter imports from, and so where what is
    installed for it is found: this process's own ``sys.path``, live, when it
    is the Python running kpip."""
    interpreter = target_interpreter()
    if interpreter.is_own:
        return sys.path
    return interpreter.path


def installing_interpreter() -> Interpreter:
    """The target interpreter, which something is to be installed into."""
    interpreter = target_interpreter()
    if interpreter.is_bundled:
        raise NoTargetInterpreter(
            "No Python interpreter to install for: VIRTUAL_ENV and CONDA_PREFIX "
            "are unset, there is no .venv here or above, and there is no python3 "
            "or python on PATH. Activate an environment, or name a Python with "
            "--python."
        )
    return interpreter


def build_interpreter() -> Interpreter:
    """The Python build backends run with: the target interpreter, which
    what is built is for."""
    interpreter = target_interpreter()
    if interpreter.is_bundled:
        raise NoTargetInterpreter(
            "No Python interpreter to build with: building a source "
            "distribution runs its build backend with the Python it is built "
            "for. Activate an environment, or name a Python with --python."
        )
    return interpreter


def _find_target() -> Interpreter:
    if _requested:
        return interpreter_at(identify(_requested))
    if not is_compiled():
        return _own
    for where, prefix in _active_environments():
        try:
            return probe(identify(prefix))
        except CommandError as exc:
            raise CommandError(f"{where}, which has no working Python: {exc}") from exc
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found is None:
            continue
        try:
            return probe(found)
        except CommandError:
            # Windows' python3 is often the Microsoft Store's stub.
            logger.debug("Skipping %s", found, exc_info=True)
    return _own


def _active_environments() -> list[tuple[str, str]]:
    found = []
    conda = os.environ.get("CONDA_PREFIX")
    conda_base = bool(conda) and _is_conda_base(conda)  # type: ignore[arg-type]
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        found.append((f"VIRTUAL_ENV names {virtual_env}", virtual_env))
    if conda and not conda_base:
        found.append((f"CONDA_PREFIX names {conda}", conda))
    discovered = _working_directory_environment()
    if discovered is not None:
        found.append((f"The environment at {discovered}", discovered))
    if conda and conda_base:
        found.append((f"CONDA_PREFIX names {conda}", conda))
    return found


def _working_directory_environment() -> str | None:
    """The environment the working directory is inside, or the nearest
    ``.venv`` in it or a directory above it."""
    directory = os.path.abspath(os.getcwd())
    while True:
        if os.path.isfile(os.path.join(directory, "pyvenv.cfg")):
            return directory
        candidate = os.path.join(directory, ".venv")
        if os.path.isfile(os.path.join(candidate, "pyvenv.cfg")):
            return candidate
        if os.path.isdir(candidate):
            logger.warning("Ignoring %s, which is not a virtual environment", candidate)
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _is_conda_base(prefix: str) -> bool:
    """Whether ``prefix`` is conda's base environment, which uv takes as a
    system Python, after any other environment."""
    if os.path.isdir(os.path.join(prefix, "conda-meta", "pixi")):
        return False
    root = os.environ.get("_CONDA_ROOT")
    if root and os.path.normcase(os.path.abspath(root)) == os.path.normcase(
        os.path.abspath(prefix)
    ):
        return True
    default = os.environ.get("CONDA_DEFAULT_ENV")
    if not default or default == prefix:
        return False
    return os.path.basename(prefix) != default
