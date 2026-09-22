"""Minimal stdlib-only PEP 517 hook caller.

The frontend only needs the four hooks below. Keeping the bridge here avoids a
runtime dependency on ``pyproject-hooks`` while preserving isolated backend
execution in the interpreter selected by the build environment.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any


class HookMissing(Exception):
    def __init__(self, hook_name: str | None = None) -> None:
        super().__init__(hook_name)
        self.hook_name = hook_name


_CALLER = r"""
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


class BuildBackendHookCaller:
    def __init__(
        self,
        source_dir: str,
        backend: str,
        *,
        backend_path: list[str] | None = None,
        python_executable: str | None = None,
    ) -> None:
        self.source_dir = os.path.abspath(source_dir)
        self.backend = backend
        self.backend_path = tuple(
            os.path.abspath(os.path.join(self.source_dir, path))
            for path in (backend_path or ())
        )
        self.python_executable = python_executable or sys.executable

    @contextmanager
    def subprocess_runner(self, runner: Any) -> Iterator[None]:
        del runner
        yield

    def _call(self, hook: str, **kwargs: Any) -> Any:
        import subprocess

        with tempfile.TemporaryDirectory(prefix="kpip-pep517-") as directory:
            input_path = os.path.join(directory, "input.json")
            output_path = os.path.join(directory, "output.json")
            with open(input_path, "w", encoding="utf-8") as stream:
                json.dump(kwargs, stream)
            environment = os.environ.copy()
            environment["KPIP_BUILD_BACKEND"] = self.backend
            # The backend runs with the project on its path, as it would if
            # it had been started from inside the directory. Passing it to
            # the hook's own environment rather than setting it on this
            # process is what lets two projects have their metadata prepared
            # at once: a chdir and an os.environ write are process-wide, and
            # concurrent builds would hand each other the wrong project.
            search_path = [self.source_dir]
            inherited = environment.get("PYTHONPATH")
            if inherited:
                search_path.append(inherited)
            environment["PYTHONPATH"] = os.pathsep.join(search_path)
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
                    text=True,
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
