from __future__ import annotations

import sys

import pytest
from kpip_compile.workers import bytecode_modules, subinterpreter_modules


def test_only_modules_with_python_source_are_embedded() -> None:
    modules = [
        ("zlib", "built-in", False),
        ("_kpip_worker_probe", "/tmp/_kpip_worker_probe.py", False),
        ("__main__", "/tmp/run.py", False),
        ("kpip._vendor", None, False),
        ("_json", "/x/_json.cpython-314-darwin.so", False),
        ("collections.abc", "/lib/python3.14/_collections_abc.py", True),
        ("os", "/lib/python3.14/os.py", False),
    ]

    assert bytecode_modules(modules) == ["os"]


@pytest.mark.skipif(
    sys.version_info < (3, 14) or sys.implementation.name != "cpython",
    reason="subinterpreter pools arrived in CPython 3.14",
)
def test_a_real_worker_needs_encodings_and_the_unpacking_modules() -> None:
    modules = subinterpreter_modules(sys.executable)

    for needed in (
        "encodings",
        "kpip",
        "kpip.install.archive_workers",
        "kpip.install.wheel_archive_cache",
        "concurrent.futures.interpreter",
    ):
        assert needed in modules
    assert not any(
        name.startswith(("_virtualenv", "_distutils_hack")) for name in modules
    )
