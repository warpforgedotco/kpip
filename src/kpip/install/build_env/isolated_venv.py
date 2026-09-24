from __future__ import annotations

import os
import sys
import sysconfig

from kpip.core.errors import DiagnosticKpipError

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


class VenvImportError(DiagnosticKpipError):
    reference = "venv-import-error"

    def __init__(self) -> None:
        hint_stmt = None
        if sys.platform == "linux":
            hint_stmt = (
                "If this is an OS-provided Python, it's likely that your OS "
                "package maintainers have split Python's standard library across "
                "multiple OS packages."
            )
        super().__init__(
            message="Cannot import the 'venv' module of the Python standard library",
            context=(
                "This is a symptom of a broken/modified Python, which cannot be used with kpip."
            ),
            note_stmt="This is an issue with the Python installation itself, not kpip.",
            hint_stmt=hint_stmt,
        )


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


def get_venv_path_from_sysconfig(name: str, env_dir: str) -> str:
    vars = {
        "base": env_dir,
        "platbase": env_dir,
    }
    return sysconfig.get_path(name, scheme="venv", vars=vars)


class CreatedVenv:
    """Where a freshly created environment keeps its libraries and interpreter."""

    __slots__ = ("bin_path", "lib_dirs", "python_executable")

    def __init__(
        self, lib_dirs: list[str], bin_path: str, python_executable: str
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


def _create_with_interpreter(
    env_path: str, *, with_pip: bool, python: str
) -> CreatedVenv:
    """Have ``python`` create the environment, and describe it in its own terms.

    The running process may not be that interpreter -- or any interpreter,
    when kpip is compiled -- so both the creation and the layout of the new
    environment come from ``python`` itself rather than from this process's
    ``venv`` and ``sysconfig``.
    """
    import json
    import subprocess

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
        lib_dirs=[purelib], bin_path=scripts, python_executable=venv_python
    )


def create_isolated_venv(
    env_path: str,
    *,
    with_pip: bool = True,
    python: str | None = None,
) -> CreatedVenv:
    """Create a fresh virtualenv (or stdlib ``venv`` fallback) at ``env_path``.

    Used by ``BackendRunner.caller()`` in ``build.build_backend`` (the
    project builder used by ``kpip build``/``kpip wheel`` and metadata-only
    resolution reads) to get "a working isolated venv at this path".
    ``python`` is the interpreter the environment is for; when it is not the
    one running kpip, that interpreter creates it.
    """
    from kpip.core.interpreter import is_own_interpreter

    if python is not None and not is_own_interpreter(python):
        return _create_with_interpreter(env_path, with_pip=with_pip, python=python)

    context: Any = None
    try:
        import virtualenv
    except ImportError:
        try:
            import venv
        except ImportError:
            raise VenvImportError

        import subprocess

        env = venv.EnvBuilder(symlinks=(os.name != "nt"), with_pip=False)
        try:
            context = env.ensure_directories(env_path)
            env.create(env_path)
            bootstrap_environment = _bootstrap_environment()
            if with_pip:
                subprocess.run(
                    [
                        context.env_exec_cmd,
                        "-m",
                        "ensurepip",
                        "--upgrade",
                        "--default-pip",
                    ],
                    check=True,
                    cwd=env_path,
                    env=bootstrap_environment,
                    capture_output=True,
                    text=True,
                )
        except (OSError, subprocess.CalledProcessError) as e:
            detail = str(e)
            if isinstance(e, subprocess.CalledProcessError):
                output = "\n".join(part for part in (e.stdout, e.stderr) if part)
                if output:
                    detail = f"{detail}: {output}"
            raise VenvCreationError(detail)
    else:
        try:
            arguments = [env_path, "--no-download", "--clear"]
            if not with_pip:
                arguments.append("--no-seed")
            virtualenv.cli_run(arguments)
        except (OSError, RuntimeError) as e:
            raise VenvCreationError(str(e))

    if sys.version_info >= (3, 12) and context is not None:
        lib_dirs = [context.lib_path]
        bin_path = context.bin_path
    elif sys.version_info >= (3, 12):
        lib_dirs = [get_venv_path_from_sysconfig("purelib", env_path)]
        bin_path = get_venv_path_from_sysconfig("scripts", env_path)
    elif sys.version_info[:2] == (3, 11):
        lib_dirs = [get_venv_path_from_sysconfig("purelib", env_path)]
        bin_path = get_venv_path_from_sysconfig("scripts", env_path)
    else:
        if sys.platform == "win32":
            libpath = os.path.join(env_path, "Lib", "site-packages")
        else:
            python = "pypy" if sys.implementation.name == "pypy" else "python"
            libpath = os.path.join(
                env_path,
                "lib",
                f"{python}{sys.version_info.major}.{sys.version_info.minor}",
                "site-packages",
            )
        lib_dirs = [libpath]
        try:
            bin_path = context.bin_path
        except AttributeError:
            scripts_dir = "Scripts" if os.name == "nt" else "bin"
            bin_path = os.path.join(env_path, scripts_dir)

    try:
        python_executable = context.env_exec_cmd
    except AttributeError:
        try:
            python_executable = context.env_exe
        except AttributeError:
            executable_name = "python.exe" if os.name == "nt" else "python"
            python_executable = os.path.join(bin_path, executable_name)

    return CreatedVenv(
        lib_dirs=lib_dirs,
        bin_path=bin_path,
        python_executable=python_executable,
    )
