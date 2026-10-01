"""Minimal stdlib-only PEP 517 hook caller.

The frontend only needs the four hooks below. Keeping the bridge here avoids a
runtime dependency on ``pyproject-hooks`` while preserving isolated backend
execution in the interpreter selected by the build environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import json
import os
import subprocess
import tempfile

from kpip.core.interpreter import build_interpreter
from kpip.host.interpreter_facts import SAFE_PATH

if TYPE_CHECKING:
    from typing import Any


class HookMissing(Exception):
    def __init__(self, hook_name: str | None = None) -> None:
        super().__init__(hook_name)
        self.hook_name = hook_name


# Run from the project directory with ``-c``, which would put that directory
# first on ``sys.path``: a project's own ``enum.py`` or ``json.py`` would
# stand in for the standard library, or a ``setuptools`` directory for the
# backend. PEP 517 keeps the source tree off the backend's path; only
# ``backend-path``, which the caller puts there itself, goes on it.
_CALLER = (
    SAFE_PATH
    + r"""
import importlib
import json
import os
import sys
import traceback

hook_name, control_dir = sys.argv[1:]
module_name, _, object_path = os.environ["KPIP_BUILD_BACKEND"].partition(":")
backend_path = os.environ.get("KPIP_BUILD_BACKEND_PATH")
if backend_path:
    sys.path[:0] = backend_path.split(os.pathsep)
backend = importlib.import_module(module_name)
for part in object_path.split(".") if object_path else ():
    backend = getattr(backend, part)
with open(os.path.join(control_dir, "input.json"), encoding="utf-8") as stream:
    kwargs = json.load(stream)
try:
    hook = getattr(backend, hook_name)
except AttributeError:
    with open(os.path.join(control_dir, "output.json"), "w", encoding="utf-8") as stream:
        json.dump({"missing": True}, stream)
else:
    try:
        result = hook(**kwargs)
    except Exception:
        with open(os.path.join(control_dir, "output.json"), "w", encoding="utf-8") as stream:
            json.dump({"error": traceback.format_exc()}, stream)
    else:
        with open(os.path.join(control_dir, "output.json"), "w", encoding="utf-8") as stream:
            json.dump({"return_val": result}, stream)
"""
)


class BuildBackendHookCaller:
    def __init__(
        self,
        source_dir: str,
        backend: str,
        *,
        backend_path: list[str] | None = None,
        python_executable: str | None = None,
        scripts_dir: str | None = None,
    ) -> None:
        self.source_dir = os.path.abspath(source_dir)
        # An isolated build environment's own scripts, first on the hook's
        # PATH: backends such as maturin run the tool their build
        # requirements installed there, as pip's build environments allow.
        self.scripts_dir = scripts_dir
        self.backend = backend
        self.backend_path = tuple(
            os.path.abspath(os.path.join(self.source_dir, path))
            for path in (backend_path or ())
        )
        if python_executable is None:
            python_executable = build_interpreter()

        self.python_executable = python_executable

    def _call(self, hook: str, **kwargs: Any) -> Any:
        with tempfile.TemporaryDirectory(prefix="kpip-pep517-") as directory:
            input_path = os.path.join(directory, "input.json")
            output_path = os.path.join(directory, "output.json")
            with open(input_path, "w", encoding="utf-8") as stream:
                json.dump(kwargs, stream)
            environment = os.environ.copy()
            environment["KPIP_BUILD_BACKEND"] = self.backend
            if self.scripts_dir:
                inherited_path = environment.get("PATH")
                environment["PATH"] = (
                    self.scripts_dir
                    if not inherited_path
                    else os.pathsep.join((self.scripts_dir, inherited_path))
                )
            if self.backend_path:
                environment["KPIP_BUILD_BACKEND_PATH"] = os.pathsep.join(
                    self.backend_path,
                )
            try:
                subprocess.run(
                    [self.python_executable, "-c", _CALLER, hook, directory],
                    check=True,
                    cwd=self.source_dir,
                    env=environment,
                    capture_output=True,
                    # What a backend prints is in no one encoding:
                    # its Python's code page, a compiler's, or UTF-8.
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                if detail:
                    raise RuntimeError(
                        f"backend hook {hook!r} failed: {detail}",
                    ) from exc
                raise
            with open(output_path, encoding="utf-8") as stream:
                result = json.load(stream)
        if result.get("missing"):
            raise HookMissing(hook)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("return_val")

    def build_wheel(
        self,
        wheel_directory: str,
        *,
        config_settings: dict[str, Any] | None = None,
        metadata_directory: str | None = None,
    ) -> str | None:
        return self._call(
            "build_wheel",
            wheel_directory=wheel_directory,
            config_settings=config_settings,
            metadata_directory=metadata_directory,
        )

    def build_editable(
        self,
        wheel_directory: str,
        *,
        config_settings: dict[str, Any] | None = None,
        metadata_directory: str | None = None,
    ) -> str | None:
        return self._call(
            "build_editable",
            wheel_directory=wheel_directory,
            config_settings=config_settings,
            metadata_directory=metadata_directory,
        )

    def prepare_metadata_for_build_wheel(
        self,
        metadata_directory: str,
        *,
        config_settings: dict[str, Any] | None = None,
    ) -> str | None:
        return self._call(
            "prepare_metadata_for_build_wheel",
            metadata_directory=metadata_directory,
            config_settings=config_settings,
        )

    def prepare_metadata_for_build_editable(
        self,
        metadata_directory: str,
        *,
        config_settings: dict[str, Any] | None = None,
    ) -> str | None:
        return self._call(
            "prepare_metadata_for_build_editable",
            metadata_directory=metadata_directory,
            config_settings=config_settings,
        )
