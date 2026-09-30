"""Whether kpip is a compiled binary, and how to run it again.

kpip ships compiled with Nuitka. There, ``sys.executable`` is a ``python``
beside the binary that does not exist, and ``sys.prefix``, ``sys.path`` and
``sysconfig`` describe the binary's bundle. This module imports nothing of
kpip's, so any module can ask.
"""

from __future__ import annotations

import sys


def is_compiled() -> bool:
    """Whether this kpip is a compiled binary rather than Python source."""

    return "__compiled__" in globals()


def own_command() -> list[str]:
    """This kpip, as a command to run another copy of it.

    ``-m kpip`` under this interpreter; a compiled kpip is its own
    executable, which is not ``sys.executable`` -- Nuitka points that at a
    ``python`` beside the binary that does not exist -- but the one Nuitka
    names for starting the program again.
    """
    compiled = globals().get("__compiled__")

    if compiled is None:
        return [sys.executable, "-m", "kpip"]

    return [compiled.process_exe]


def own_binary() -> str | None:
    """The file that was run to start this compiled kpip, or ``None``.

    A onefile kpip's ``process_exe`` is the copy unpacked in its cache;
    Nuitka leaves the binary that was run, made absolute, in ``sys.argv[0]``.
    """
    compiled = globals().get("__compiled__")

    if compiled is None:
        return None

    return sys.argv[0] if compiled.onefile else compiled.process_exe


def is_own_interpreter(executable: str) -> bool:
    """Whether ``executable`` is the interpreter running this process."""

    return not is_compiled() and executable == sys.executable
