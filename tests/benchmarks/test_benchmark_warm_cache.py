"""Benchmarks for the warm on-disk cache paths.

Every other benchmark measures a cold process: ``reset_caches`` drops the
in-memory state and ``cold_metadata_cache_dir`` hands out an empty cache
directory. These measure the opposite -- the second ``kpip install`` on a
machine, where every persistent store under ``<cache root>/v0`` is already
populated and the work is reading, validating and reusing it. Each iteration
still drops the in-memory state, so what it measures is the disk-backed
warm path, not a memoized one.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmark_support import flush_persistent_caches, make_wheel, reset_caches
from kpip.cli.fast_install import FastInstallMetadataCache, resolve_simple_wheelhouse
from kpip.core.packaging import parse_requirement
from kpip.core.urls import path_to_url
from kpip.core.wheel import parse_wheel_file, wheel_candidate
from kpip.index.catalog_cache import save_links
from kpip.index.links import Link
from kpip.index.provider import CandidateProvider
from kpip.index.source_locations import SimpleIndexSource
from kpip.install.output import materialize_candidates
from kpip.install.target import InstallTarget
from kpip.install.wheel_archive_cache import prepare_cached_wheels
from kpip.install.wheel_archive_installer import install_wheels_from_archive_cache
from kpip.install.wheel_install_plan_cache import (
    exact_install_plan_key,
    load_cached_install_plan,
    save_cached_install_plan,
)
from kpip.network.cache import SafeFileCache
from kpip.resolution.api import ResolutionEngine
from pytest_codspeed import BenchmarkFixture

ROOT = "application"


def resolve_graph(wheelhouse: Path, cache_dir: str):
    reset_caches()
    return ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
            wheel_cache_dir=cache_dir,
        ),
        ignore_installed=True,
    ).resolve([ROOT])


@pytest.fixture(scope="module")
def warm_cache_dir(
    graph_wheelhouse: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> str:
    """A cache directory after one full resolve + archive preparation + plan
    receipt, flushed to disk -- what a second install finds."""
    cache_dir = str(tmp_path_factory.mktemp("warm-cache"))
    result = resolve_graph(graph_wheelhouse, cache_dir)
    candidates = tuple(result.candidates)
    prepare_cached_wheels(candidates, cache_dir)
    key = plan_key()
    assert key is not None
    assert save_cached_install_plan(cache_dir, key, candidates, result.graph)
    flush_persistent_caches(cache_dir)
    reset_caches()
    return cache_dir


def plan_key() -> str | None:
    requirement = SimpleNamespace(
        req=parse_requirement(f"{ROOT}==1.0.0"),
        link=None,
        hash_options={},
        config_settings={},
    )
    return exact_install_plan_key((requirement,), ("benchmark-context",))


def test_warm_resolve_persistent_caches(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
    warm_cache_dir: str,
) -> None:
    """A fresh provider over a populated candidate-metadata / metadata /
    release-facts store: every wheel's metadata is read back from SQLite
    and reparsed instead of being extracted from the wheel."""

    def resolve_warm() -> int:
        return len(resolve_graph(graph_wheelhouse, warm_cache_dir).candidates)

    assert benchmark(resolve_warm) > 10


def test_warm_install_plan_receipt(
    benchmark: BenchmarkFixture,
    warm_cache_dir: str,
) -> None:
    """The exact-pin fast path: load the receipt and revalidate every
    archive entry it references, without resolving."""
    key = plan_key()
    assert key is not None

    def load_receipt() -> int:
        reset_caches()
        plan = load_cached_install_plan(warm_cache_dir, key)
        assert plan is not None
        return len(plan.candidates)

    assert benchmark(load_receipt) > 10


def test_warm_archive_preparation(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
    warm_cache_dir: str,
) -> None:
    """Preparing wheels whose unpacked trees already exist: one manifest
    read and validation per wheel, no extraction."""
    candidates = tuple(
        materialize_candidates(
            resolve_graph(graph_wheelhouse, warm_cache_dir).candidates
        ),
    )

    def prepare_warm() -> int:
        reset_caches()
        for candidate in candidates:
            candidate.wheel_layout = None
        return len(prepare_cached_wheels(candidates, warm_cache_dir))

    assert benchmark(prepare_warm) > 10


def test_warm_archive_install(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
    warm_cache_dir: str,
    tmp_path: Path,
) -> None:
    """Installing a resolved graph into an empty target from the unpacked
    archive trees (clone or copy), the route a warm cached install takes."""
    candidates = tuple(
        materialize_candidates(
            resolve_graph(graph_wheelhouse, warm_cache_dir).candidates
        ),
    )
    requests = tuple((candidate.path, True, None) for candidate in candidates)
    counter = itertools.count()

    def install_warm() -> int:
        reset_caches()
        for candidate in candidates:
            candidate.wheel_layout = None
        target = InstallTarget.from_options(
            ROOT,
            target=str(tmp_path / f"target-{next(counter)}"),
        )
        installed = install_wheels_from_archive_cache(
            requests,
            candidates,
            target=target,
            cache_dir=warm_cache_dir,
            report=False,
        )
        assert installed is not None
        return len(installed)

    assert benchmark(install_warm) > 10


def test_warm_archive_install_compiled(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
    warm_cache_dir: str,
    tmp_path: Path,
) -> None:
    """The same cached install with bytecode compilation on -- what a default
    `kpip install` takes -- compiling each wheel in the staged tree."""
    candidates = tuple(
        materialize_candidates(
            resolve_graph(graph_wheelhouse, warm_cache_dir).candidates
        ),
    )
    requests = tuple((candidate.path, True, None) for candidate in candidates)
    counter = itertools.count()

    def install_compiled() -> int:
        reset_caches()
        for candidate in candidates:
            candidate.wheel_layout = None
        target = InstallTarget.from_options(
            ROOT,
            target=str(tmp_path / f"compiled-{next(counter)}"),
        )
        installed = install_wheels_from_archive_cache(
            requests,
            candidates,
            target=target,
            cache_dir=warm_cache_dir,
            report=False,
            pycompile=True,
        )
        assert installed is not None
        return len(installed)

    assert benchmark(install_compiled) > 10


def test_warm_fast_install_snapshot(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    """The --no-index fast path with its snapshot already on disk: load it,
    then resolve the wheelhouse from cached metadata and plans."""
    cache_dir = str(tmp_path / "fast-cache")
    os.makedirs(cache_dir)
    primed = FastInstallMetadataCache(cache_dir)
    assert resolve_simple_wheelhouse([str(graph_wheelhouse)], [ROOT], primed)
    primed.flush()

    def resolve_snapshot() -> int:
        reset_caches()
        cache = FastInstallMetadataCache(cache_dir)
        resolved = resolve_simple_wheelhouse([str(graph_wheelhouse)], [ROOT], cache)
        assert resolved is not None
        return len(resolved)

    assert benchmark(resolve_snapshot) > 10


INDEX_URL = "https://index.test/simple/"


@pytest.fixture(scope="module")
def warm_index(
    graph_wheelhouse: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[SimpleNamespace, str]:
    """A session whose HTTP cache already holds every project page of the
    graph as a parsed catalog (with its release summary), plus a cache
    directory primed by one resolve against it -- so a second resolve reads
    catalogs, summaries, persisted choices and candidate metadata from disk
    and never fetches a page or opens a wheel."""
    root = tmp_path_factory.mktemp("warm-index")
    cache = SafeFileCache(str(root / "http"))
    by_project: dict[str, list[Path]] = {}
    for wheel in sorted(graph_wheelhouse.glob("*.whl")):
        by_project.setdefault(parse_wheel_file(wheel.name).name, []).append(wheel)
    for name, wheels in by_project.items():
        page_url = SimpleIndexSource.project_page_url(INDEX_URL, name)
        save_links(
            cache,
            page_url,
            [
                Link.from_url(
                    path_to_url(str(wheel)), source_url=page_url, text=wheel.name
                )
                for wheel in wheels
            ],
        )
    session = SimpleNamespace(cache=cache, has_fresh_cached_response=lambda url: True)
    cache_dir = str(root / "cache")
    os.makedirs(cache_dir)
    assert len(resolve_index(session, cache_dir).candidates) > 10
    flush_persistent_caches(cache_dir)
    reset_caches()
    return session, cache_dir


def resolve_index(session: SimpleNamespace, cache_dir: str):
    reset_caches()
    return ResolutionEngine(
        provider=CandidateProvider.from_options(
            index_url=INDEX_URL,
            no_index=False,
            session=session,
            wheel_cache_dir=cache_dir,
        ),
        ignore_installed=True,
    ).resolve([ROOT])


def test_warm_index_catalog_resolve(
    benchmark: BenchmarkFixture,
    warm_index: tuple[SimpleNamespace, str],
) -> None:
    """Resolving against an index whose pages, catalogs, summaries, target
    choices and candidate metadata are all already cached."""
    session, cache_dir = warm_index

    def resolve_warm() -> int:
        return len(resolve_index(session, cache_dir).candidates)

    assert benchmark(resolve_warm) > 10


REAL_WHEELS = Path(__file__).resolve().parents[1] / "cli" / "data" / "common_wheels"


@pytest.fixture(scope="module")
def warm_real_wheel_cache(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A cache directory holding the unpacked trees of the repository's real
    wheels (~56 MB across 13 files) after one preparation, flushed."""
    cache_dir = str(tmp_path_factory.mktemp("warm-real-wheels"))
    candidates = tuple(
        wheel_candidate(str(wheel)) for wheel in sorted(REAL_WHEELS.glob("*.whl"))
    )
    assert len(candidates) > 5
    prepare_cached_wheels(candidates, cache_dir)
    flush_persistent_caches(cache_dir)
    reset_caches()
    return cache_dir


def test_warm_archive_preparation_real_wheels(
    benchmark: BenchmarkFixture,
    warm_real_wheel_cache: str,
) -> None:
    """Preparing real-sized local wheels whose trees already exist. A local
    wheel carries no index-supplied hash, so finding its archive entry is
    where a warm install pays for the wheel's size."""
    candidates = tuple(
        wheel_candidate(str(wheel)) for wheel in sorted(REAL_WHEELS.glob("*.whl"))
    )

    def prepare_warm() -> int:
        reset_caches()
        for candidate in candidates:
            candidate.wheel_layout = None
        return len(prepare_cached_wheels(candidates, warm_real_wheel_cache))

    assert benchmark(prepare_warm) == len(candidates)


@pytest.fixture(scope="module")
def installed_environment(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A site-packages with 200 installed distributions."""
    root = tmp_path_factory.mktemp("site-packages")
    for index in range(200):
        dist_info = root / f"installed_pkg{index}-1.{index}.dist-info"
        dist_info.mkdir()
        dist_info.joinpath("METADATA").write_text(
            "Metadata-Version: 2.1\n"
            f"Name: installed-pkg{index}\n"
            f"Version: 1.{index}\n"
            "Requires-Dist: installed-pkg0>=1.0\n"
            'Requires-Dist: installed-pkg1; extra == "extra"\n'
            "Provides-Extra: extra\n"
            "Summary: benchmark fixture\n"
            "\n"
            "A description body the header parser must skip.\n",
            encoding="utf-8",
        )
    return str(root)


def test_warm_installed_state_scan(
    benchmark: BenchmarkFixture,
    installed_environment: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The default install's scan of an unchanged environment: every
    distribution's headers come from the persistent store after one
    priming run, so the scan is a listing plus one stat per entry."""
    from kpip.core.metadata import (
        clear_installed_index,
        installed_index,
        use_header_cache,
    )
    from kpip.index.metadata_cache import get_wheel_metadata_cache

    cache_dir = str(tmp_path_factory.mktemp("installed-cache"))
    use_header_cache(get_wheel_metadata_cache(cache_dir))
    try:
        clear_installed_index()
        assert len(installed_index([installed_environment])) == 200
        flush_persistent_caches(cache_dir)
        reset_caches()
        use_header_cache(get_wheel_metadata_cache(cache_dir))

        def scan_warm() -> int:
            clear_installed_index()
            return len(installed_index([installed_environment]))

        assert benchmark(scan_warm) == 200
    finally:
        use_header_cache(None)
        clear_installed_index()


@pytest.fixture(scope="module")
def stale_index(
    graph_wheelhouse: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[object, str]:
    """warm_index, but every page's HTTP entry is stale with a matching ETag:
    each resolve must revalidate (304 answered offline) before the summary
    world opens. This is the everyday case -- PyPI pages outlive their
    max-age between two kpip runs while their content stays unchanged."""
    from kpip.core.http_contracts import HttpResponse
    from kpip.network.session import NetworkSession
    from kpip_test_support.transport_mocks import make_response

    class Stale304Session(NetworkSession):
        def open_internal(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            request_body: bytes | None,
            timeout,
            *,
            stream: bool = False,
        ) -> HttpResponse:
            if headers.get("if-none-match") == '"stale-page"':
                return make_response(
                    status=304,
                    reason="Not Modified",
                    url=url,
                    headers={"ETag": '"stale-page"', "Cache-Control": "max-age=0"},
                    body=b"",
                )
            body = b"{}"
            return make_response(
                status=200,
                reason="OK",
                url=url,
                headers={
                    "Content-Type": "application/vnd.pypi.simple.v1+json",
                    "Cache-Control": "max-age=0",
                    "ETag": '"stale-page"',
                    "Content-Length": str(len(body)),
                },
                body=body,
            )

    root = tmp_path_factory.mktemp("stale-index")
    session = Stale304Session(cache=str(root / "http"))
    cache = session.cache
    by_project: dict[str, list[Path]] = {}
    for wheel in sorted(graph_wheelhouse.glob("*.whl")):
        by_project.setdefault(parse_wheel_file(wheel.name).name, []).append(wheel)
    for name, wheels in by_project.items():
        page_url = SimpleIndexSource.project_page_url(INDEX_URL, name)
        save_links(
            cache,
            page_url,
            [
                Link.from_url(
                    path_to_url(str(wheel)), source_url=page_url, text=wheel.name
                )
                for wheel in wheels
            ],
        )
        session.get(page_url).close()

    bulk_wheel = make_wheel(root, "bulk-dependency", "2.0.0")
    bulk_page_url = SimpleIndexSource.project_page_url(INDEX_URL, "bulk-dependency")
    bulk_links = [
        Link.from_url(
            f"https://files.invalid/bulk_dependency-1.{index}.0-py3-none-any.whl",
            source_url=bulk_page_url,
            text=f"bulk_dependency-1.{index}.0-py3-none-any.whl",
        )
        for index in range(400)
    ]
    bulk_links.append(
        Link.from_url(
            path_to_url(str(bulk_wheel)),
            source_url=bulk_page_url,
            text=bulk_wheel.name,
        ),
    )
    save_links(cache, bulk_page_url, bulk_links)
    session.get(bulk_page_url).close()

    cache_dir = str(root / "cache")
    os.makedirs(cache_dir)
    assert len(resolve_stale_index(session, cache_dir).candidates) > 10
    flush_persistent_caches(cache_dir)
    reset_caches()
    return session, cache_dir


def resolve_stale_index(session: object, cache_dir: str):
    """resolve_index plus one PyPI-scale project (400 releases, one winner)."""
    reset_caches()
    return ResolutionEngine(
        provider=CandidateProvider.from_options(
            index_url=INDEX_URL,
            no_index=False,
            session=session,
            wheel_cache_dir=cache_dir,
        ),
        ignore_installed=True,
    ).resolve([ROOT, "bulk-dependency"])


def test_stale_index_revalidated_resolve(
    benchmark: BenchmarkFixture,
    stale_index: tuple[object, str],
) -> None:
    """Resolving when every cached page is past its max-age but unchanged:
    one conditional request per project, then the summary world."""
    session, cache_dir = stale_index

    def resolve_stale() -> int:
        return len(resolve_stale_index(session, cache_dir).candidates)

    assert benchmark(resolve_stale) > 10
