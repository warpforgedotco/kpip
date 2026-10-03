"""kpip's C built-ins, built as extensions against the test interpreter.

The binary compiles them in; built here, the same code runs under the suite.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import types
from pathlib import Path

import pytest


def compiler() -> list[str] | None:
    if os.name == "nt":
        found = shutil.which("cl")
        return None if found is None else [found]
    configured = shlex.split(sysconfig.get_config_var("CC") or "cc")
    found = shutil.which(configured[0]) or shutil.which("cc")
    return None if found is None else [found]


def build_extension(
    source: Path,
    name: str,
    directory: Path,
    *,
    defines: tuple[str, ...] = (),
    link: tuple[str, ...] = (),
) -> types.ModuleType | None:
    """``source`` built and loaded as the module ``name``, or None when it
    does not build or load with these defines and libraries. Without a
    compiler the test skips, except in CI, which must build it."""
    command = compiler()
    if command is None:
        if os.environ.get("CI"):
            pytest.fail(f"no C compiler to build {source.name} with")
        pytest.skip("no C compiler")
    output = directory / (name + sysconfig.get_config_var("EXT_SUFFIX"))
    include = sysconfig.get_path("include")
    if os.name == "nt":
        arguments = [
            "/nologo",
            "/O2",
            "/W3",
            "/WX",
            "/LD",
            f"/I{include}",
            *(f"/D{define}" for define in defines),
            str(source),
            f"/Fo{directory}\\",
            f"/Fe{output}",
            "/link",
            f"/LIBPATH:{Path(sys.base_prefix) / 'libs'}",
            *link,
        ]
    else:
        arguments = [
            "-O2",
            "-Wall",
            "-Werror",
            "-fPIC",
            "-I",
            include,
            *(f"-D{define}" for define in defines),
            str(source),
            "-o",
            str(output),
            *(
                ["-bundle", "-undefined", "dynamic_lookup"]
                if sys.platform == "darwin"
                else ["-shared"]
            ),
            *link,
        ]
    built = subprocess.run(
        [*command, *arguments], capture_output=True, text=True, check=False
    )
    if built.returncode != 0:
        return None
    spec = importlib.util.spec_from_file_location(name, output)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return None
    return module
