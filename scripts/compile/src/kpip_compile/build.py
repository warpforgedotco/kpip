"""Compile kpip with the vendored Nuitka."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from kpip_compile.extensions import EXTENSIONS, build_extensions
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
    extensions: bool = True


class ExtensionMismatchError(RuntimeError):
    """The compiled modules cannot be built for the interpreter Nuitka uses."""


def kpip_version(package_dir: Path = KPIP_PACKAGE) -> str:
    """Read ``__version__`` without importing kpip into the build interpreter."""
    text = (package_dir / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ValueError(f"no __version__ in {package_dir / '__init__.py'}")
    return match.group(1)


def build_id(interpreter: str, package_dir: Path = KPIP_PACKAGE) -> str:
    """What the binary's code is: a digest of kpip's modules, as built, and
    the Python they are built into.

    The binary ships it as ``kpip/BUILD_ID`` for the caches that replay what
    an earlier kpip rendered (``kpip.core.code_identity``): two copies of one
    build share them, and any change to a module retires them.
    """
    digest = hashlib.sha256(interpreter.encode())
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(b"\0" + path.relative_to(package_dir).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def executable_name(options: BuildOptions) -> str:
    """The binary's name; a standalone folder already holds a ``kpip`` package."""

    if options.platform == "win32":
        return "kpip.exe"
    return "kpip" if options.mode == "onefile" else "kpip.bin"


def nuitka_command(
    options: BuildOptions,
    version: str,
    interpreter: str = "cpython-314",
    subinterpreter_modules: tuple[str, ...] = (),
    build_id_file: Path | None = None,
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
        # Imported only when an index page is read, and optional to kpip;
        # the binary always has it (kpip.index.typed_pages).
        "--include-package=msgspec",
        # Nuitka's automatic choice turns LTO off past 250 compiled modules,
        # even for PGO builds, and kpip is close to that.
        "--lto=yes",
    ]
    if options.extensions:
        # A compiled module sits beside the pure-Python-mode source it was
        # built from; ship the extension, which Nuitka's own compilation of
        # that source (an ImportError, by design) would only replace with the
        # pure fallback.
        command.append("--no-prefer-source-code")
    else:
        # Leave out any extension a previous build left in the tree, so the
        # binary runs the pure-Python fallback it was asked for.
        command.extend(f"--nofollow-import-to={name}" for name in EXTENSIONS)
    if subinterpreter_modules:
        # Bytecode for the subinterpreters kpip unpacks wheels on, which
        # cannot import compiled modules (kpip_compile.workers).
        command.append("--subinterpreter-bytecode=" + ",".join(subinterpreter_modules))
    if build_id_file is not None:
        command.append(f"--include-data-files={build_id_file}=kpip/BUILD_ID")
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
    from kpip_compile.workers import subinterpreter_modules

    modules = tuple(subinterpreter_modules(options.python))
    print(f"{len(modules)} modules for subinterpreters", flush=True)
    options.output_dir.mkdir(parents=True, exist_ok=True)
    build_id_file = options.output_dir / "BUILD_ID"
    build_id_file.write_text(build_id(interpreter), encoding="ascii")
    command = nuitka_command(
        options, kpip_version(), interpreter, modules, build_id_file
    )
    env = dict(environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(nuitka_dir), env.get("PYTHONPATH")))
    )
    options.output_dir.mkdir(parents=True, exist_ok=True)
    print(" ".join(command), flush=True)
    return subprocess.run(command, env=env, check=False).returncode


def prepare_extensions(options: BuildOptions, interpreter: str) -> None:
    """Build the compiled modules the binary ships, for its interpreter.

    They are built here, by this interpreter, which has Cython; an extension
    only loads into a Python with the same ``cache_tag``, so a binary for
    another Python needs kpip-compile run under that one.
    """
    if not options.extensions:
        return
    if interpreter != sys.implementation.cache_tag:
        raise ExtensionMismatchError(
            f"the binary's Python ({interpreter}) is not the one kpip-compile "
            f"runs under ({sys.implementation.cache_tag}), so its compiled "
            f"modules cannot be built here; run kpip-compile with that Python "
            f"(uv run --python {options.python} kpip-compile build), or pass "
            f"--no-extensions for a binary without them"
        )
    for path in build_extensions():
        print(f"built {path}", flush=True)


def build(options: BuildOptions, nuitka_dir: Path) -> int:
    # The binary embeds this interpreter's version, so say which one it is.
    subprocess.run([options.python, "-VV"], check=True)
    interpreter = interpreter_tag(options.python)
    prepare_extensions(options, interpreter)

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
