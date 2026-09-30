from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kpip_compile.workers import (
    compiled_path,
    pure_modules,
    stage_worker_modules,
)


def test_a_module_and_a_package_land_where_a_path_finder_looks() -> None:
    assert compiled_path("kpip.install.archive_workers", False) == (
        "kpip/install/archive_workers.pyc"
    )
    assert compiled_path("encodings", True) == "encodings/__init__.pyc"


def test_only_modules_with_python_source_are_shipped() -> None:
    modules = [
        ("zlib", "built-in", False),
        ("_kpip_worker_probe", "/tmp/_kpip_worker_probe.py", False),
        ("__main__", "/tmp/run.py", False),
        ("kpip._vendor", None, True),
        ("_page_catalog", "/x/_page_catalog.cpython-314-darwin.so", False),
        ("os", "/lib/python3.14/os.py", False),
    ]

    assert pure_modules(modules) == [("os", "/lib/python3.14/os.py", False)]


@pytest.mark.skipif(
    sys.version_info < (3, 14) or sys.implementation.name != "cpython",
    reason="subinterpreter pools arrived in CPython 3.14",
)
def test_a_real_worker_needs_encodings_and_the_unpacking_modules(
    tmp_path: Path,
) -> None:
    paths = stage_worker_modules(sys.executable, tmp_path)

    for needed in (
        "encodings/__init__.pyc",
        "kpip/__init__.pyc",
        "kpip/install/archive_workers.pyc",
        "kpip/install/wheel_archive_cache.pyc",
        "concurrent/futures/interpreter.pyc",
    ):
        assert needed in paths
        assert (tmp_path / needed).is_file()
    assert not any(
        path.startswith(("_virtualenv", "_distutils_hack")) for path in paths
    )
    assert not list(tmp_path.rglob("*.py"))
