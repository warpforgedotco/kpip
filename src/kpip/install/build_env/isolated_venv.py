from __future__ import annotations

import json
import os
import subprocess

from kpip.core.errors import CommandError, DiagnosticKpipError
from kpip.host.interpreter_facts import interpreter_at


class VenvCreationError(DiagnosticKpipError):
    reference = "venv-creation-error"

    def __init__(self, context: str) -> None:
        hint_stmt = (
            "This may be caused by running antivirus software."
            if os.name == "nt"
            else None
        )
        super().__init__(
            message="Cannot create a virtual environment",
            context=context,
            hint_stmt=hint_stmt,
        )


class CreatedVenv:
    """Where a freshly created environment keeps its libraries and interpreter."""

    __slots__ = ("bin_path", "lib_dirs", "python_executable")

    def __init__(
        self,
        lib_dirs: list[str],
        bin_path: str,
        python_executable: str,
    ) -> None:
        self.lib_dirs = lib_dirs
        self.bin_path = bin_path
        self.python_executable = python_executable

    def __eq__(self, other: object) -> bool:
        return type(other) is CreatedVenv and (
            self.lib_dirs,
            self.bin_path,
            self.python_executable,
        ) == (other.lib_dirs, other.bin_path, other.python_executable)

    __hash__ = None  # type: ignore[assignment]  # mutable, so unhashable

    def __repr__(self) -> str:
        return (
            f"CreatedVenv(lib_dirs={self.lib_dirs!r}, bin_path={self.bin_path!r}, "
            f"python_executable={self.python_executable!r})"
        )


def _bootstrap_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KPIP_") and key != "PYTHONPATH"
    }


_VENV_PATHS = (
    "import json, sys, sysconfig; "
    "print(json.dumps([sysconfig.get_path('purelib'), "
    "sysconfig.get_path('scripts'), sys.executable]))"
)


def _layout_from_facts(env_path: str, python: str) -> CreatedVenv | None:
    """The new environment's layout, from what ``python`` already said of
    itself: its ``venv`` scheme, there from 3.11, laid out at ``env_path``.
    ``None`` -- asking the environment instead -- for an older Python."""
    try:
        interpreter = interpreter_at(python)
    except CommandError:
        return None
    if "venv" not in interpreter.schemes:
        return None
    try:
        paths = interpreter.paths("venv", {"base": env_path, "platbase": env_path})
    except AttributeError:
        return None
    executable = os.path.join(
        paths["scripts"], "python.exe" if os.name == "nt" else "python"
    )
    if not os.path.exists(executable):
        return None
    return CreatedVenv(
        lib_dirs=[paths["purelib"]],
        bin_path=paths["scripts"],
        python_executable=executable,
    )


def _create_with_interpreter(
    env_path: str, *, with_pip: bool, python: str
) -> CreatedVenv:
    """Have ``python`` create the environment, and describe it in its own terms.

    The running process may not be that interpreter -- or any interpreter,
    when kpip is compiled -- so both the creation and the layout of the new
    environment come from ``python`` itself rather than from this process's
    ``venv`` and ``sysconfig``.
    """

    command = [
        python,
        "-m",
        "venv",
        *(() if with_pip else ("--without-pip",)),
        env_path,
    ]

    try:
        subprocess.run(
            command,
            check=True,
            cwd=env_path,
            env=_bootstrap_environment(),
            capture_output=True,
            text=True,
        )

        known = _layout_from_facts(env_path, python)
        if known is not None:
            return known

        executable = (
            os.path.join(env_path, "Scripts", "python.exe")
            if os.name == "nt"
            else os.path.join(env_path, "bin", "python")
        )
        described = subprocess.run(
            [executable, "-c", _VENV_PATHS],
            check=True,
            cwd=env_path,
            env=_bootstrap_environment(),
            capture_output=True,
            text=True,
        )
        purelib, scripts, venv_python = json.loads(described.stdout)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        detail = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            output = "\n".join(part for part in (e.stdout, e.stderr) if part)
            if output:
                detail = f"{detail}: {output}"
        raise VenvCreationError(detail)

    return CreatedVenv(
        lib_dirs=[purelib],
        bin_path=scripts,
        python_executable=venv_python,
    )


def create_isolated_venv(
    env_path: str,
    *,
    python: str,
    with_pip: bool = True,
) -> CreatedVenv:
    """A fresh environment at ``env_path`` for the interpreter ``python``,
    which creates it: compiled, kpip has neither ``venv`` of its own to
    create one with nor ``sysconfig`` that would describe it."""

    return _create_with_interpreter(env_path, with_pip=with_pip, python=python)
