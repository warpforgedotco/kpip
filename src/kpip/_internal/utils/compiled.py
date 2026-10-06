"""Whether kpip runs compiled, and how it starts itself again.

kpip ships as a Nuitka-compiled binary. There, ``sys.executable`` names a
``python`` beside the binary that does not exist, and ``sys.prefix``,
``sys.path`` and ``sysconfig`` describe the bundle kpip was built with, not
an environment anyone installs into.
"""

from __future__ import annotations

import sys


def is_compiled() -> bool:
    """Whether this kpip is the compiled binary. Nuitka sets ``__compiled__``
    in every module it compiles."""
    return "__compiled__" in globals()


def own_command() -> list[str]:
    """The command that runs this kpip again: from source, this copy of it,
    whatever else is importable as kpip."""
    compiled = globals().get("__compiled__")
    if compiled is None:
        from kpip._internal.utils.misc import get_runnable_pip

        return [sys.executable, get_runnable_pip()]
    return [compiled.process_exe]
