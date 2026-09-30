"""Jobs for the subinterpreters of the programs beside this file.

Imported by those interpreters from a plain copy, not compiled.
"""

import os
import time


def raise_and_catch(rounds: int) -> int:
    """Free tracebacks in this interpreter, as unpacking and imports do."""
    caught = 0
    for index in range(rounds):
        try:
            os.stat(f"/nonexistent/{index}")
        except FileNotFoundError:
            caught += 1
        try:
            {}[index]
        except KeyError:
            caught += 1
    return caught


def hash_and_compare(seconds: float) -> tuple[int, int]:
    """Count wrong answers from tuple hashing and float comparison.

    Both are this interpreter's own objects; nothing the main interpreter
    does may change what they hash to or how they compare.
    """
    failures = 0
    rounds = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        table = {}
        for index in range(200):
            table[(index, index + 1)] = index
        for index in range(200):
            if table.get((index, index + 1)) != index:
                failures += 1
        low = 1.5 + rounds % 3
        high = low + 1.0
        if not low < high or low == high:
            failures += 1
        rounds += 1
    return failures, rounds


def loaded() -> list[tuple[str, str | None, bool]]:
    """``(name, origin, is_alias)`` of this interpreter's modules, as
    ``kpip_compile.workers`` reads a worker's."""
    import sys

    modules = []
    for name, module in list(sys.modules.items()):
        spec = getattr(module, "__spec__", None)
        origin = getattr(module, "__file__", None) or getattr(spec, "origin", None)
        modules.append((name, origin, getattr(module, "__name__", name) != name))
    return modules
