"""Entry-point script generation for installed wheels."""

from __future__ import annotations

import io
import os
import stat
import sys

from kpip.core.errors import InstallationError
from kpip.host.clone import replace_contents


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

    except (FileNotFoundError, IsADirectoryError):
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


def script_text(target_ref: str, executable: str | None) -> str:
    module, _, attribute = target_ref.partition(":")

    entry = attribute or "main"

    return (
        f"#!{executable or script_python()}\n"
        "import re\nimport sys\n"
        f"from {module} import {entry}\n\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        f"    sys.exit({entry}())\n"
    )


def write_windows_script(path: str, script: str, *, gui: bool) -> None:
    """Create a distlib-compatible Windows launcher without importing distlib."""

    if sys.maxsize <= 2**32:
        # Only the 64-bit launchers ship with kpip.
        raise InstallationError(
            "kpip installs console and GUI scripts only for 64-bit Python on Windows"
        )

    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "").lower()

    suffix = "-arm" if "arm" in machine else ""

    launcher_name = f"{'w' if gui else 't'}64{suffix}.exe"

    from importlib.resources import files

    launcher = (files("kpip._launchers") / launcher_name).read_bytes()

    import zipfile

    archive = io.BytesIO()

    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("__main__.py", script.encode("utf-8"))

    with open(path, "wb") as file:
        file.write(launcher + archive.getvalue())


def generate_entry_point_files(
    scripts: dict[str, tuple[str, bool]],
    destination: str,
    executable: str | None = None,
) -> tuple[tuple[str, int], ...]:
    """Generate console entry points; the paths written, with their modes.

    Only what it wrote: ``destination`` may be a scripts directory shared
    with other wheels' scripts, which are not this call's to report.
    """

    if not scripts:
        return ()

    os.makedirs(destination, exist_ok=True)

    script_maker_type = None

    try:
        from distlib.scripts import ScriptMaker

    except ImportError:
        pass

    else:
        script_maker_type = ScriptMaker

    explicit_modes: dict[str, int] = {}

    written: list[str] = []

    for name, (target_ref, gui) in scripts.items():
        if os.path.basename(name) != name or name in {".", ".."}:
            raise InstallationError(
                f"console script {name!r} is outside the scripts directory",
            )

        if script_maker_type is None:
            if os.name == "nt":
                path = os.path.join(destination, f"{name}.exe")

                write_windows_script(
                    path,
                    script_text(target_ref, executable),
                    gui=gui,
                )

                written.append(path)

            else:
                path = os.path.join(destination, name)

                with open(path, "w", encoding="utf-8") as file:
                    file.write(script_text(target_ref, executable))

                    file.flush()

                    mode = (
                        os.fstat(file.fileno()).st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )

                os.chmod(path, mode)

                explicit_modes[path] = mode

                written.append(path)

        else:
            maker = script_maker_type(None, destination)

            maker.clobber = True

            maker.variants = {""}

            if executable is not None:
                maker.executable = executable

            written.extend(
                maker.make(f"{name} = {target_ref}", options={"gui": gui}) or ()
            )

            if os.name == "nt":
                path = os.path.join(destination, name)

                with open(path, "w", encoding="utf-8") as file:
                    file.write(script_text(target_ref, executable))

                    file.flush()

                    mode = (
                        os.fstat(file.fileno()).st_mode
                        | stat.S_IXUSR
                        | stat.S_IXGRP
                        | stat.S_IXOTH
                    )

                os.chmod(path, mode)

                explicit_modes[path] = mode

                written.append(path)

    return tuple(
        (path, explicit_modes.get(path) or os.stat(path).st_mode)
        for path in dict.fromkeys(written)
    )


def script_matches(
    path: str,
    scripts: dict[str, tuple[str, bool]],
) -> bool:
    import zipfile

    path_text = os.fspath(path)

    basename = os.path.basename(path_text)

    is_executable = basename.lower().endswith(".exe")

    name = os.path.splitext(basename)[0] if is_executable else basename

    script = scripts.get(name)

    if script is None:
        return False

    target_ref, _ = script

    module, _, attribute = target_ref.partition(":")

    entry = attribute or "main"

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

    except (OSError, KeyError, UnicodeDecodeError):
        return False

    return f"from {module} import {entry}" in text
