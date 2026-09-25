"""The warm resolves uv's own CodSpeed suite measures, on the same graphs.

uv-bench's ``resolve_warm_jupyter`` and ``resolve_warm_airflow``: real PyPI
graphs pinned to 2024-09-01, resolved for CPython 3.11 on an arm64 Mac from
a cache a previous resolve filled. See ``uv_graphs`` for how the graphs are
recorded and replayed without the network.

Each iteration drops the in-memory state, as the other warm-cache
benchmarks do, so what is measured is a new process finding the cache on
disk: catalogs, summaries, persisted choices and candidate metadata.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import uv_graphs
from benchmark_support import flush_persistent_caches, reset_caches
from pytest_codspeed import BenchmarkFixture


@pytest.fixture(scope="module", autouse=True)
def uv_environment() -> Iterator[None]:
    with uv_graphs.uv_environment():
        yield


def warm(name: str, root: Path) -> tuple[uv_graphs.ReplaySession, str]:
    """A replay session and a cache directory two resolves have filled.

    Two, as uv-bench primes: the first reads every page and metadata file,
    the second finds what the first stored and stores what only a warm run
    writes.
    """
    session = uv_graphs.ReplaySession(
        uv_graphs.CORPUS / f"{name}.zip",
        cache=str(root / "http"),
    )
    cache_dir = str(root / "cache")
    for _ in range(2):
        reset_caches()
        uv_graphs.resolve(name, session, cache_dir)
        flush_persistent_caches(cache_dir)
    reset_caches()
    return session, cache_dir


@pytest.fixture(scope="module")
def warm_jupyter(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[uv_graphs.ReplaySession, str]:
    return warm("jupyter", tmp_path_factory.mktemp("uv-jupyter"))


@pytest.fixture(scope="module")
def warm_airflow(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[uv_graphs.ReplaySession, str]:
    return warm("airflow", tmp_path_factory.mktemp("uv-airflow"))


def test_resolve_warm_jupyter(
    benchmark: BenchmarkFixture,
    warm_jupyter: tuple[uv_graphs.ReplaySession, str],
) -> None:
    session, cache_dir = warm_jupyter

    def resolve_warm() -> int:
        reset_caches()
        return len(uv_graphs.resolve("jupyter", session, cache_dir).candidates)

    assert benchmark(resolve_warm) == 99


def test_resolve_warm_airflow(
    benchmark: BenchmarkFixture,
    warm_airflow: tuple[uv_graphs.ReplaySession, str],
) -> None:
    session, cache_dir = warm_airflow

    def resolve_warm() -> int:
        reset_caches()
        return len(uv_graphs.resolve("airflow", session, cache_dir).candidates)

    assert benchmark(resolve_warm) == 585
