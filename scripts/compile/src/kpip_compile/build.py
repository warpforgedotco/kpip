"""Compile kpip with the vendored Nuitka."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kpip_compile.vendor import PACKAGE_ROOT, REPO_ROOT

KPIP_PACKAGE = REPO_ROOT / "src" / "kpip"
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "build"
# Static, so onefile runs after the first reuse the unpacked files. The
# payload hash in the unpacking manifest tells builds of one version apart.
# Not under kpip's own cache directory, which ``kpip cache`` may clear.
ONEFILE_TEMPDIR_SPEC = "{CACHE_DIR}/kpip-onefile/{VERSION}"


@dataclass(frozen=True)
class BuildOptions:
    python: str = sys.executable
    output_dir: Path = DEFAULT_OUTPUT_DIR
    mode: str = "onefile"
    cache_mode: str = "cached"
    platform: str = sys.platform
    extra_args: tuple[str, ...] = field(default=())


def kpip_version(package_dir: Path = KPIP_PACKAGE) -> str:
    """Read ``__version__`` without importing kpip into the build interpreter."""
    text = (package_dir / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ValueError(f"no __version__ in {package_dir / '__init__.py'}")
    return match.group(1)


def nuitka_command(options: BuildOptions, version: str) -> list[str]:
    is_windows = options.platform == "win32"
    command = [
        options.python,
        "-m",
        "nuitka",
        f"--mode={options.mode}",
        "--assume-yes-for-downloads",
        f"--output-dir={options.output_dir}",
        f"--output-filename={'kpip.exe' if is_windows else 'kpip'}",
        f"--product-version={version}",
        # The command registry imports each command's module by name, which
        # Nuitka cannot follow.
        "--include-package=kpip",
        # certifi's cacert.pem and the vendored license texts.
        "--include-package-data=kpip",
    ]
    if is_windows:
        # Nuitka never treats ``.exe`` files as package data on its own.
        launchers = KPIP_PACKAGE / "_launchers"
        command.append(f"--include-data-files={launchers}/*.exe=kpip/_launchers/")
    if options.mode == "onefile":
        command.append(f"--onefile-cache-mode={options.cache_mode}")
        if options.cache_mode == "cached":
            command.append(f"--onefile-tempdir-spec={ONEFILE_TEMPDIR_SPEC}")
    command.extend(options.extra_args)
    command.append(str(KPIP_PACKAGE))
    return command


def build(options: BuildOptions, nuitka_dir: Path) -> int:
    command = nuitka_command(options, kpip_version())
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(nuitka_dir), env.get("PYTHONPATH")))
    )
    options.output_dir.mkdir(parents=True, exist_ok=True)
    # The binary embeds this interpreter's version, so say which one it is.
    subprocess.run([options.python, "-VV"], check=True)
    print(" ".join(command), flush=True)
    return subprocess.run(command, env=env, check=False).returncode
