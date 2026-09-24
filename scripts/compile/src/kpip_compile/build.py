"""Compile kpip with the vendored Nuitka."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from kpip_compile.vendor import PACKAGE_ROOT, REPO_ROOT

KPIP_PACKAGE = REPO_ROOT / "src" / "kpip"
DEFAULT_OUTPUT_DIR = PACKAGE_ROOT / "build"


def onefile_tempdir_spec(interpreter: str) -> str:
    """Where a cached onefile binary unpacks: one directory per kpip and Python.

    Static, so runs after the first reuse the unpacked files, and outside
    kpip's own cache directory, which ``kpip cache`` may clear. The payload
    hash in the unpacking manifest tells builds of one version apart, but
    unpacking never removes a file the new payload lacks, so builds for
    another Python -- whose runtime library has another name -- get a
    directory of their own rather than leaving theirs behind in this one.
    """
    return f"{{CACHE_DIR}}/kpip-onefile/{{VERSION}}/{interpreter}"


def interpreter_tag(python: str) -> str:
    """The build interpreter's ``cache_tag``, e.g. ``cpython-314t``."""
    return subprocess.run(
        [python, "-c", "import sys; print(sys.implementation.cache_tag)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass(frozen=True)
class BuildOptions:
    python: str = sys.executable
    output_dir: Path = DEFAULT_OUTPUT_DIR
    mode: str = "onefile"
    cache_mode: str = "cached"
    platform: str = sys.platform
    extra_args: tuple[str, ...] = field(default=())
    pgo: bool = False


def kpip_version(package_dir: Path = KPIP_PACKAGE) -> str:
    """Read ``__version__`` without importing kpip into the build interpreter."""
    text = (package_dir / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ValueError(f"no __version__ in {package_dir / '__init__.py'}")
    return match.group(1)


def executable_name(options: BuildOptions) -> str:
    """The binary's name; a standalone folder already holds a ``kpip`` package."""

    if options.platform == "win32":
        return "kpip.exe"
    return "kpip" if options.mode == "onefile" else "kpip.bin"


def nuitka_command(
    options: BuildOptions,
    version: str,
    interpreter: str = "cpython-314",
) -> list[str]:
    is_windows = options.platform == "win32"
    command = [
        options.python,
        "-m",
        "nuitka",
        f"--mode={options.mode}",
        "--assume-yes-for-downloads",
        f"--output-dir={options.output_dir}",
        f"--output-filename={executable_name(options)}",
        f"--product-version={version}",
        # The command registry imports each command's module by name, which
        # Nuitka cannot follow.
        "--include-package=kpip",
        # certifi's cacert.pem and the vendored license texts.
        "--include-package-data=kpip",
        # Nuitka's automatic choice turns LTO off past 250 compiled modules,
        # even for PGO builds, and kpip is close to that.
        "--lto=yes",
    ]
    if is_windows:
        # Nuitka never treats ``.exe`` files as package data on its own.
        launchers = KPIP_PACKAGE / "_launchers"
        command.append(f"--include-data-files={launchers}/*.exe=kpip/_launchers/")
    if options.mode == "onefile":
        command.append(f"--onefile-cache-mode={options.cache_mode}")
        if options.cache_mode == "cached":
            command.append(
                f"--onefile-tempdir-spec={onefile_tempdir_spec(interpreter)}"
            )
    command.extend(options.extra_args)
    command.append(str(KPIP_PACKAGE))
    return command


def _run_nuitka(
    options: BuildOptions, nuitka_dir: Path, environ: dict[str, str], interpreter: str
) -> int:
    command = nuitka_command(options, kpip_version(), interpreter)
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(nuitka_dir), env.get("PYTHONPATH")))
    )
    options.output_dir.mkdir(parents=True, exist_ok=True)
    print(" ".join(command), flush=True)
    return subprocess.run(command, env=env, check=False).returncode


def build(options: BuildOptions, nuitka_dir: Path) -> int:
    # The binary embeds this interpreter's version, so say which one it is.
    subprocess.run([options.python, "-VV"], check=True)
    interpreter = interpreter_tag(options.python)

    if not options.pgo:
        return _run_nuitka(options, nuitka_dir, dict(os.environ), interpreter)

    from kpip_compile import pgo

    pgo.check_supported(options.platform)

    # Nuitka trains the instrumented build in its standalone distribution,
    # which holds the binary under its standalone name in either mode.
    binary = (
        options.output_dir
        / "kpip.dist"
        / executable_name(replace(options, mode="standalone"))
    )
    result = options.output_dir / "pgo-training.result"
    pgo_options = pgo.nuitka_options(options.output_dir / "pgo-train", binary, result)
    status = _run_nuitka(
        replace(options, extra_args=(*pgo_options, *options.extra_args)),
        nuitka_dir,
        dict(os.environ),
        interpreter,
    )
    if status != 0:
        return status
    pgo.check_training(result)
    return 0
