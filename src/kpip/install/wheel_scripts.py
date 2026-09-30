"""Entry-point script generation for installed wheels.

The scripts are the ones pip writes: pip's template with distlib's shebang,
Windows launcher and file-mode rules, byte for byte.
"""

from __future__ import annotations

lazy import io
lazy import os
lazy import re
lazy import struct
lazy import sys
lazy import sysconfig
lazy import time
lazy import zipfile
lazy from importlib.resources import files

lazy from kpip.core.errors import InstallationError
lazy from kpip.host.clone import replace_contents

# pip's template: distlib's, without the ``re`` import.
SCRIPT_TEMPLATE = (
    "import sys\n"
    "from %(module)s import %(import_name)s\n"
    "if __name__ == '__main__':\n"
    "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
    "    sys.exit(%(func)s())\n"
)

# distlib's pattern for ``name = module:callable [flags]``.
ENTRY_POINT = re.compile(
    r"""(?P<name>([^\[]\S*))
    \s*=\s*(?P<callable>(\w+)([:\.]\w+)*)
    \s*(\[\s*(?P<flags>[\w-]+(=\w+)?(,\s*\w+(=\w+)?)*)\s*\])?
    """,
    re.VERBOSE,
)


def script_python() -> str:
    """The interpreter installed scripts run with, when none is given.

    ``sys.executable``, unless the kpip that started this one named another
    in ``KPIP_SCRIPT_PYTHON``: a build environment's scripts run with that
    environment's Python, not the one installing into it -- which, for a
    compiled kpip, is no Python at all.
    """
    return os.environ.get("KPIP_SCRIPT_PYTHON") or sys.executable


def rewrite_shebang(path: str, executable: str | None) -> None:
    """Point a ``.data/scripts`` pseudo-shebang at the target interpreter.

    The binary distribution format says a first line starting with exactly
    ``#!python`` is rewritten, and separately allows the ``#!pythonw``
    convention for Windows GUI scripts. Matching the whole of ``#!python\n``
    missed both ``#!pythonw`` and the CRLF form a wheel built on Windows
    carries, leaving those scripts unable to find an interpreter.
    """
    with open(path, "rb") as file:
        contents = file.read()

    if not contents.startswith(b"#!python"):
        return

    first_line, separator, rest = contents.partition(b"\n")
    if not separator:
        first_line, rest = contents, b""
    interpreter = executable or script_python()
    if first_line.rstrip(b"\r")[len(b"#!python") :].startswith(b"w"):
        interpreter = _windowed(interpreter)

    replace_contents(path, f"#!{interpreter}\n".encode() + rest)


def _windowed(executable: str) -> str:
    """``pythonw`` beside ``python``, when there is one to point at."""
    directory, _, name = executable.rpartition(os.sep)
    if not name.startswith("python") or name.startswith("pythonw"):
        return executable
    stem, dot, suffix = name.partition(".")
    candidate = f"{stem}w{dot}{suffix}"
    full = os.path.join(directory, candidate) if directory else candidate
    return full if os.path.exists(full) else executable


def entry_point_scripts(path: str) -> dict[str, tuple[str, bool]]:
    try:
        with open(path, encoding="utf-8") as file:
            lines = file.read().splitlines()

    except FileNotFoundError, IsADirectoryError:
        return {}

    active = False

    result: dict[str, tuple[str, bool]] = {}

    gui = False

    for raw_line in lines:
        line = raw_line.strip()

        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()

            active = section in {"console_scripts", "gui_scripts"}

            gui = section == "gui_scripts"

        elif active and "=" in line and not line.startswith("#"):
            name, target = line.split("=", 1)

            result[name.strip()] = (target.strip(), gui)

    return result


def script_callable(name: str, target_ref: str) -> tuple[str, str]:
    """The module and callable an entry point runs, where pip accepts it."""
    specification = f"{name} = {target_ref}"
    match = ENTRY_POINT.search(specification)
    if match is None or match.group("callable").count(":") > 1:
        raise InstallationError(f"Invalid script entry point: {specification}")
    module, colon, callable_ = match.group("callable").partition(":")
    if not colon:
        raise InstallationError(
            f"Invalid script entry point: {specification} - A callable "
            "suffix is required. See https://packaging.python.org/"
            "specifications/entry-points/#use-for-scripts for more "
            "information."
        )
    return module, callable_


def script_body(module: str, callable_: str) -> bytes:
    return (
        SCRIPT_TEMPLATE
        % {
            "module": module,
            "import_name": callable_.split(".")[0],
            "func": callable_,
        }
    ).encode("utf-8")


def shebang(executable: str | None, *, gui: bool) -> bytes:
    """distlib's shebang for ``executable``, or for the default interpreter.

    An interpreter named for the scripts is used as given; the default one
    is quoted when its path has a space. On POSIX, a path with a space, or
    one too long for the kernel to read, is run through ``/bin/sh``.
    """
    named = executable or os.environ.get("KPIP_SCRIPT_PYTHON")
    interpreter = named or sys.executable
    if gui and os.name == "nt":
        directory, name = os.path.split(interpreter)
        interpreter = os.path.join(directory, name.replace("python", "pythonw"))
    if not named and " " in interpreter and not interpreter.startswith('"'):
        interpreter = f'"{interpreter}"'
    encoded = interpreter.encode("utf-8")

    if os.name != "posix":
        simple = True
    elif getattr(sys, "cross_compiling", False):
        simple = False
    else:
        # "#!" and the newline count towards the kernel's limit.
        limit = 512 if sys.platform == "darwin" else 127
        simple = b" " not in encoded and len(encoded) + 3 <= limit

    if simple:
        return b"#!" + encoded + b"\n"
    return b"#!/bin/sh\n'''exec' " + encoded + b' "$0" "$@"\n' + b"' '''\n"


def windows_launcher(body: bytes, head: bytes, *, gui: bool) -> bytes:
    """A launcher ``.exe`` that runs ``body`` with the interpreter ``head`` names.

    The launcher reads the shebang between itself and the zip archive it
    runs as ``__main__.py``.
    """
    bits = "64" if struct.calcsize("P") == 8 else "32"
    arm = "-arm" if sysconfig.get_platform() == "win-arm64" else ""
    name = f"{'w' if gui else 't'}{bits}{arm}.exe"
    launcher = (files("kpip._launchers") / name).read_bytes()

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if source_date_epoch:
            member = zipfile.ZipInfo(
                "__main__.py",
                date_time=time.gmtime(int(source_date_epoch))[:6],
            )
            package.writestr(member, body)
        else:
            package.writestr("__main__.py", body)

    return launcher + head + archive.getvalue()


def generate_entry_point_files(
    scripts: dict[str, tuple[str, bool]],
    destination: str,
    executable: str | None = None,
) -> tuple[tuple[str, int], ...]:
    """Write the entry points' scripts; the paths written, with their modes.

    Only what it wrote: ``destination`` may be a scripts directory shared
    with other wheels' scripts, which are not this call's to report.
    """
    if not scripts:
        return ()

    os.makedirs(destination, exist_ok=True)

    written: list[tuple[str, int]] = []

    for name, (target_ref, gui) in scripts.items():
        if os.path.basename(name) != name or name in {"", ".", ".."}:
            raise InstallationError(
                f"Invalid script entry point name {name!r}: the script would "
                f"be installed outside the scripts directory ({destination}).",
            )

        body = script_body(*script_callable(name, target_ref))
        head = shebang(executable, gui=gui)
        path = os.path.join(destination, name)

        if os.name == "nt":
            path += ".exe"
            with open(path, "wb") as file:
                file.write(windows_launcher(body, head, gui=gui))
            written.append((path, os.stat(path).st_mode))
        else:
            with open(path, "wb") as file:
                file.write(head + body)
            os.chmod(path, (os.stat(path).st_mode | 0o555) & 0o7777)
            written.append((path, os.stat(path).st_mode))

    return tuple(written)


def script_matches(
    path: str,
    scripts: dict[str, tuple[str, bool]],
) -> bool:
    """Whether ``path`` is the script one of ``scripts`` would write."""
    basename = os.path.basename(os.fspath(path))

    is_executable = basename.lower().endswith(".exe")

    name = os.path.splitext(basename)[0] if is_executable else basename

    script = scripts.get(name)

    if script is None:
        return False

    try:
        module, callable_ = script_callable(name, script[0])
    except InstallationError:
        return False

    try:
        if is_executable:
            try:
                with open(path, "rb") as file:
                    contents = file.read()

                with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                    text = archive.read("__main__.py").decode("utf-8")

            except zipfile.BadZipFile:
                return False

        else:
            with open(path, encoding="utf-8") as file:
                text = file.read()

    except OSError, KeyError, UnicodeDecodeError:
        return False

    return f"from {module} import {callable_.split('.')[0]}" in text
