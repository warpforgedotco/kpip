"""Entry-point script generation for installed wheels.

The scripts are the ones pip writes: pip's template with distlib's shebang,
Windows launcher and file-mode rules, byte for byte.
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import time
import zipfile
from pathlib import Path

from kpip.core.errors import InstallationError
from kpip.host.clone import replace_contents
from kpip.host.interpreter_facts import target_interpreter

logger = logging.getLogger(__name__)

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

    The one a kpip that started this one named in ``KPIP_SCRIPT_PYTHON``: a
    build environment's scripts run with that environment's Python.
    Otherwise :func:`environment_python`.
    """
    return os.environ.get("KPIP_SCRIPT_PYTHON") or environment_python()


def environment_python() -> str:
    """The Python of the environment kpip installs into.

    pip runs under that interpreter, and its scripts run with
    ``sys.executable``. kpip may not run under it: ``--python`` names
    another, and a compiled kpip is no Python at all.
    """
    return target_interpreter().executable


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
    """The scripts an ``entry_points.txt`` asks for, by name, as pip writes
    them: each target, and whether it is a GUI script."""
    try:
        with open(path, encoding="utf-8") as file:
            text = file.read()

    except FileNotFoundError, IsADirectoryError:
        return {}

    return parse_entry_point_scripts(text)


def parse_entry_point_scripts(text: str) -> dict[str, tuple[str, bool]]:
    """:func:`entry_point_scripts`, from the file's text."""
    console: dict[str, str] = {}

    gui: dict[str, str] = {}

    section: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line.startswith("[") and line.endswith("]"):
            section = {"console_scripts": console, "gui_scripts": gui}.get(
                line[1:-1].strip()
            )

        elif section is not None and "=" in line and not line.startswith("#"):
            name, target = line.split("=", 1)

            section[name.strip()] = target.strip()

    result = {name: (target, False) for name, target in versioned(console).items()}
    result.update((name, (target, True)) for name, target in gui.items())
    return result


def versioned(console: dict[str, str]) -> dict[str, str]:
    """pip's console scripts for ``console``: pip's and easy_install's own
    under this Python's version, as pip writes them.

    Their wheels are universal, so a version baked into a script name at
    build time can be the wrong one. ``ENSUREPIP_OPTIONS`` picks which
    names ensurepip installs.
    """
    console = dict(console)
    result: dict[str, str] = {}
    ensurepip = os.environ.get("ENSUREPIP_OPTIONS")
    major, minor = target_interpreter().version[:2]

    pip_script = console.pop("pip", None)
    if pip_script:
        if ensurepip is None:
            result["pip"] = pip_script
        if ensurepip != "altinstall":
            result[f"pip{major}"] = pip_script
        result[f"pip{major}.{minor}"] = pip_script
        for name in [name for name in console if re.match(r"pip(\d+(\.\d+)?)?$", name)]:
            del console[name]

    easy_install_script = console.pop("easy_install", None)
    if easy_install_script:
        if ensurepip is None:
            result["easy_install"] = easy_install_script
        result[f"easy_install-{major}.{minor}"] = easy_install_script
        for name in [
            name for name in console if re.match(r"easy_install(-\d+\.\d+)?$", name)
        ]:
            del console[name]

    result.update(console)
    return result


def quiet_directories(interpreter: str) -> list[Path]:
    """Where a script draws no warning: ``PATH``, and beside ``interpreter``,
    which is where an environment used without activating it keeps them."""
    quiet = [
        Path(entry).resolve() for entry in os.environ.get("PATH", "").split(os.pathsep)
    ]
    quiet.append(Path(interpreter).parent.resolve())
    return quiet


def scripts_not_on_path_message(scripts: list[str], interpreter: str) -> str | None:
    """pip's warning about console scripts installed outside ``PATH``.

    None when every script's directory is on ``PATH``, or beside
    ``interpreter``: the scripts of an environment used without being
    activated sit with its Python.
    """
    if not scripts:
        return None

    grouped: dict[Path, set[str]] = {}
    for script in scripts:
        path = Path(script)
        grouped.setdefault(path.parent.resolve(), set()).add(path.name)

    quiet = quiet_directories(interpreter)
    warn_for = {
        directory: names
        for directory, names in grouped.items()
        if directory not in quiet
    }
    if not warn_for:
        return None

    lines = []
    for directory, names in warn_for.items():
        ordered = sorted(names)
        if len(ordered) == 1:
            start = f"script {ordered[0]} is"
        else:
            start = f"scripts {', '.join(ordered[:-1])} and {ordered[-1]} are"
        lines.append(f"The {start} installed in '{directory}' which is not on PATH.")

    lines.append(
        f"Consider adding {'this directory' if len(lines) == 1 else 'these directories'}"
        " to PATH or, if you prefer to suppress this warning, use "
        "--no-warn-script-location."
    )
    if any(
        entry[0] == "~"
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry
    ):
        lines.append(
            "NOTE: The current PATH contains path(s) starting with `~`, "
            "which may not be expanded by all applications."
        )
    return "\n".join(lines)


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

    An interpreter the caller names is used as given, as distlib uses one;
    the default one, and the build environment's kpip names with
    ``KPIP_SCRIPT_PYTHON`` -- under ``%TEMP%``, which a profile name with a
    space puts one in -- are quoted when their path has a space. On POSIX, a
    path with a space, or one too long for the kernel to read, is run
    through ``/bin/sh``.
    """
    interpreter = (
        executable or os.environ.get("KPIP_SCRIPT_PYTHON") or environment_python()
    )
    if gui and os.name == "nt":
        directory, name = os.path.split(interpreter)
        interpreter = os.path.join(directory, name.replace("python", "pythonw"))
    if not executable and " " in interpreter and not interpreter.startswith('"'):
        interpreter = f'"{interpreter}"'
    encoded = interpreter.encode("utf-8")

    if os.name != "posix":
        simple = True
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
    # The target's: a 32-bit Python on 64-bit Windows takes 32-bit launchers.
    interpreter = target_interpreter()
    bits = "64" if interpreter.pointer_bits == 64 else "32"
    arm = "-arm" if interpreter.platform == "win-arm64" else ""
    name = f"{'w' if gui else 't'}{bits}{arm}.exe"
    from importlib.resources import files

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


def console_scripts_in_wheel(path: str) -> list[str]:
    """The names of the console scripts the wheel at ``path`` installs."""
    try:
        with zipfile.ZipFile(path) as archive:
            member = next(
                (
                    name
                    for name in archive.namelist()
                    if name.count("/") == 1
                    and name.endswith(".dist-info/entry_points.txt")
                ),
                None,
            )
            if member is None:
                return []
            text = archive.read(member).decode("utf-8")
    except OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile:
        return []
    return [
        name for name, (_, gui) in parse_entry_point_scripts(text).items() if not gui
    ]


def warn_about_scripts_not_on_path(
    wheels: list[str], scripts_directory: str, executable: str | None
) -> None:
    """pip's warning, per wheel, for console scripts installed off ``PATH``."""
    interpreter = executable or script_python()
    if Path(scripts_directory).resolve() in quiet_directories(interpreter):
        # Every script lands there, so no wheel needs opening.
        return
    suffix = ".exe" if os.name == "nt" else ""
    for wheel in wheels:
        message = scripts_not_on_path_message(
            [
                os.path.join(scripts_directory, name + suffix)
                for name in console_scripts_in_wheel(wheel)
            ],
            interpreter,
        )
        if message is not None:
            logger.warning(message)
