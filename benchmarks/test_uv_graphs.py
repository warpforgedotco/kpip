"""The warm resolves uv's own CodSpeed suite measures, on the same graphs.

uv-bench's ``resolve_warm_jupyter`` and ``resolve_warm_airflow``: real PyPI
graphs pinned to 2024-09-01, resolved for CPython 3.11 on an arm64 Mac from
a cache an earlier resolve filled. See ``uv_graphs`` for how the graphs are
recorded and replayed without the network.

Each iteration drops kpip's in-memory caches, so what is measured is a new
``kpip install --dry-run`` finding the cache on disk.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import uv_graphs
from conftest import reset_caches
from pytest_codspeed import BenchmarkFixture


@pytest.fixture(scope="module", autouse=True)
def uv_environment() -> Iterator[None]:
    with uv_graphs.uv_environment(), uv_graphs.static_sdist_metadata():
        yield


def warm(name: str, root: Path, benchmark: BenchmarkFixture) -> int:
    """Resolve ``name`` once to fill the cache, then benchmark resolving it again."""
    replay = uv_graphs.Replay(
        uv_graphs.served(uv_graphs.load(name), uv_graphs.load_digests(name))
    )
    cache_dir = str(root / "cache")
    report = str(root / "report.json")
    with uv_graphs.replaying(replay):
        uv_graphs.resolve(name, cache_dir, report)

        def resolve_warm() -> int:
            reset_caches()
            return uv_graphs.resolve(name, cache_dir, report)

        return benchmark(resolve_warm)


def test_resolve_warm_jupyter(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    assert warm("jupyter", tmp_path, benchmark) == 99


@pytest.mark.slow
def test_resolve_warm_airflow(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    assert warm("airflow", tmp_path, benchmark) == 584
