"""Reference definitions ``Range``'s fast paths must agree with.

Shared by the seeded tests in ``tests/resolution/test_ranges.py`` and the
property tests in ``tests/properties/test_range_algebra.py``; the pure-Python
``Range`` is itself the reference for the compiled one in
``tests/resolution/test_compiled_ranges.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from kpip._vendor import nab_resolver
from kpip._vendor.nab_resolver import ranges as ranges_module
from kpip._vendor.nab_resolver.ranges import (
    NEGATIVE_INFINITY,
    POSITIVE_INFINITY,
    Range,
)

RANGE_IS_COMPILED = Range.__module__ != ranges_module.__name__
"""Whether ``Range`` is the compiled class from ``_cranges``."""

requires_patchable_range = pytest.mark.skipif(
    RANGE_IS_COMPILED,
    reason="spies on Range by replacing its methods, which a compiled "
    "extension type forbids; the pure-Python test runs cover it",
)


def load_pure_ranges() -> ModuleType:
    """A separate copy of ``ranges`` that keeps its pure-Python ``Range``.

    Once the compiled class replaces the installed module's ``Range``, the
    pure class's own methods, which look ``Range`` up in that module, stop
    working; a copy imported with ``_cranges`` hidden falls back to its own
    class and stays whole. ``from . import _cranges`` reads the package
    attribute before ``sys.modules``, so both are hidden for the import.
    """
    compiled_name = f"{nab_resolver.__name__}._cranges"
    saved_module = sys.modules.get(compiled_name)
    saved_attribute = nab_resolver.__dict__.pop("_cranges", None)
    sys.modules[compiled_name] = None  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location(
            f"{nab_resolver.__name__}._pure_ranges", ranges_module.__file__
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved_module is None:
            del sys.modules[compiled_name]
        else:
            sys.modules[compiled_name] = saved_module
        if saved_attribute is not None:
            nab_resolver._cranges = saved_attribute  # type: ignore[attr-defined]
    assert module.Range.__module__ == module.__name__
    return module


def subset_by_set_algebra(left: Range, right: Range) -> bool:
    """The definition the walk replaced."""

    return (left - right).is_empty


def disjoint_by_set_algebra(left: Range, right: Range) -> bool:
    """The definition the walk replaced."""

    return (left & right).is_empty


def contains_by_linear_scan(candidate: Range, version: object) -> bool:
    """The scan the binary search in ``__contains__`` replaced."""

    for lower, lower_inclusive, upper, upper_inclusive in candidate._intervals:
        if lower is not NEGATIVE_INFINITY and (
            version < lower or (version == lower and not lower_inclusive)
        ):
            continue
        if upper is not POSITIVE_INFINITY and (
            version > upper or (version == upper and not upper_inclusive)
        ):
            continue
        return True
    return False


def build(intervals: list[tuple]) -> Range:
    """Normalize intervals the way the resolver does, through union."""

    result: Range = Range.empty()
    for interval in intervals:
        result = result | Range((interval,))
    return result
