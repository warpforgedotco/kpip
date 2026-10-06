"""Fixtures for kpip's CodSpeed benchmarks.

``src/kpip`` and ``tests`` are generated from upstream pip, so the
benchmarks live here instead; they call kpip's own code paths, never a
benchmark-only reimplementation of them.
"""

from __future__ import annotations

import functools
import io
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: too slow to simulate under CodSpeed; run locally"
    )


def reset_caches() -> None:
    """Drop kpip's in-memory caches, so an iteration starts as a new process
    does: with only what is on disk."""
    for name, module in list(sys.modules.items()):
        if not name.startswith("kpip") or module is None:
            continue
        for value in list(vars(module).values()):
            if isinstance(value, functools._lru_cache_wrapper):
                value.cache_clear()


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    reset_caches()
    yield


# uv-bench's ``MANY_FILES_*_FILE_COUNT`` (crates/uv-bench/benches/uv.rs).
MANY_FILES = 10_000


def _payload(index: int) -> bytes:
    return f"VALUE = {index}\n".encode()


@pytest.fixture(scope="session")
def many_files_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A wheel of 10,000 modules."""
    path = tmp_path_factory.mktemp("wheels") / "many_files_pkg-1.0.0-py3-none-any.whl"
    dist_info = "many_files_pkg-1.0.0.dist-info"
    records = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as wheel:
        for index in range(MANY_FILES):
            member = f"many_files_pkg/module_{index}.py"
            wheel.writestr(member, _payload(index))
            records.append(f"{member},,")
        wheel.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: many-files-pkg\nVersion: 1.0.0\n",
        )
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: kpip-benchmarks\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        records.extend(
            [f"{dist_info}/METADATA,,", f"{dist_info}/WHEEL,,", f"{dist_info}/RECORD,,"]
        )
        wheel.writestr(f"{dist_info}/RECORD", "\n".join(records) + "\n")
    return path


@pytest.fixture(scope="session")
def many_files_sdist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An sdist of 10,000 modules."""
    path = tmp_path_factory.mktemp("sdists") / "many_files_pkg-1.0.0.tar.gz"
    with tarfile.open(path, "w:gz") as sdist:
        for index in range(MANY_FILES):
            data = _payload(index)
            member = tarfile.TarInfo(f"many_files_pkg-1.0.0/src/module_{index}.py")
            member.size = len(data)
            sdist.addfile(member, io.BytesIO(data))
    return path
