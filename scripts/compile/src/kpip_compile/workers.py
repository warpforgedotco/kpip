"""The modules to embed as bytecode for a compiled program's subinterpreters.

A new interpreter imports every module it needs through the standard import
system, ``encodings`` first of all, and cannot see the modules compiled into
the binary. Nuitka embeds bytecode of the modules named with
``--subinterpreter-bytecode`` in the binary's frozen table, where they can
(patch 0014); the binary itself still runs the compiled ones. kpip itself
runs no subinterpreters; ``tests/test_nuitka_subinterpreters.py`` checks the
patches with programs of its own.
"""

from __future__ import annotations

_PROBE_MODULE = "_kpip_worker_probe"


def bytecode_modules(modules: list) -> list[str]:
    """The names to embed, from ``(name, origin, is_alias)`` of each module
    a worker loaded: modules with Python source, less aliases and the
    probe's own."""
    return sorted(
        name
        for name, origin, alias in modules
        if isinstance(origin, str)
        and origin.endswith(".py")
        and not alias
        and name not in {_PROBE_MODULE, "__main__"}
    )
