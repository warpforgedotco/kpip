from __future__ import annotations

from kpip_compile.workers import bytecode_modules


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
