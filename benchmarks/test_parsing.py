"""Parsing requirements files and index pages.

The requirements files are uv's compiled benchmark workloads
(``scripts/benchmark/requirements/compiled``): pinned, real-world sets. The
index pages are the largest project pages the airflow graph reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import uv_graphs
from kpip._internal.index.collector import IndexContent, parse_links
from kpip._internal.network.session import PipSession
from kpip._internal.req.constructors import install_req_from_parsed_requirement
from kpip._internal.req.req_file import parse_requirements
from pytest_codspeed import BenchmarkFixture

COMPILED = (
    Path(__file__).parents[1] / "scripts" / "benchmark" / "requirements" / "compiled"
)


@pytest.mark.parametrize(
    "workload", sorted(path.stem for path in COMPILED.glob("*.txt"))
)
def test_parse_requirements_file(benchmark: BenchmarkFixture, workload: str) -> None:
    session = PipSession()
    path = str(COMPILED / f"{workload}.txt")

    def parse() -> int:
        return len(
            [
                install_req_from_parsed_requirement(parsed)
                for parsed in parse_requirements(path, session=session)
            ]
        )

    assert benchmark(parse) > 0


def _largest_pages(count: int) -> list[tuple[str, bytes]]:
    pages = [
        (key.split(" ", 1)[1], data)
        for key, (_status, _reason, _headers, data) in uv_graphs.load("airflow").items()
        if "/simple/" in key
    ]
    return sorted(pages, key=lambda page: len(page[1]), reverse=True)[:count]


@pytest.mark.parametrize(
    "url, content",
    [
        pytest.param(url, content, id=url.rstrip("/").rsplit("/", 1)[-1])
        for url, content in _largest_pages(3)
    ],
)
def test_parse_index_page(
    benchmark: BenchmarkFixture, url: str, content: bytes
) -> None:
    page = IndexContent(
        content,
        "application/vnd.pypi.simple.v1+json",
        encoding=None,
        url=url,
        cache_link_parsing=False,
    )

    def parse() -> int:
        return sum(1 for _ in parse_links(page))

    assert benchmark(parse) > 0
