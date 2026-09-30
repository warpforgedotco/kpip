"""A local wheel winner is materialized from the metadata the resolver
already loaded; the wheel is opened only if a consumer needs its layout."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from kpip.index import candidate_materialization
from kpip.index.provider import CandidateProvider
from kpip.install.output import materialize_candidates
from kpip.resolution.api import ResolutionEngine

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import make_dependency_graph, reset_caches  # noqa: E402


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = candidate_materialization._open_resolver_wheel_archive

    def counting(path: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(
        candidate_materialization, "_open_resolver_wheel_archive", counting
    )
    return calls


def _resolve(wheelhouse: Path, cache_dir: Path):  # type: ignore[no-untyped-def]
    reset_caches()
    return ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
            wheel_cache_dir=str(cache_dir),
        ),
        ignore_installed=True,
    ).resolve(["application"])


def test_winners_materialize_without_reopening_their_wheels(
    tmp_path: Path,
    opened: list[str],
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_dependency_graph(wheelhouse)
    result = _resolve(wheelhouse, tmp_path / "cache")
    opens_during_resolution = len(opened)

    concrete = materialize_candidates(list(result.candidates))

    assert len(concrete) == len(result.candidates) > 10
    assert len(opened) == opens_during_resolution, "materialization reopened a wheel"
    assert all(candidate.wheel_layout_if_loaded is None for candidate in concrete)
    by_name = {candidate.name: candidate for candidate in concrete}
    assert by_name["application"].dependencies
    assert all(candidate.path.endswith(".whl") for candidate in concrete)


def test_the_lazy_layout_matches_the_eager_one(
    tmp_path: Path, opened: list[str]
) -> None:
    from kpip.index.candidate_materialization import CandidateMaterializer

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_dependency_graph(wheelhouse)
    result = _resolve(wheelhouse, tmp_path / "cache")
    (application,) = [
        c
        for c in materialize_candidates(list(result.candidates))
        if c.name == "application"
    ]
    before = len(opened)

    lazy = application.wheel_layout
    assert len(opened) == before + 1, "the layout is read exactly once, on demand"
    eager = CandidateMaterializer.wheel_layout_for(application.path)

    assert isinstance(lazy, tuple)
    assert lazy == eager
    assert application.wheel_layout_if_loaded == eager


def test_an_index_wheel_already_unpacked_is_not_reopened(tmp_path: Path) -> None:
    """An index wheel's metadata may come from the index, not the file, so
    the file is opened to check them against each other -- unless its digest
    already names an archive cache entry, filled only after that check."""
    from kpip.core.versions import Version
    from kpip.index.candidate_materialization import CandidateMaterializer
    from kpip.index.links import Link
    from kpip.index.source_models import CandidateRecord
    from kpip.install.wheel_archive_cache import archive_entry_root

    digest = "ab" * 32
    record = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            f"https://files.example/demo-1.0-py3-none-any.whl#sha256={digest.upper()}",
            source_url=None,
        ),
    )
    unhashed = CandidateRecord(
        name="demo",
        version=Version("1.0"),
        link=Link.from_url(
            "https://files.example/demo-1.0-py3-none-any.whl", source_url=None
        ),
    )
    materializer = CandidateMaterializer(wheel_cache_dir=str(tmp_path))

    assert not materializer.unpacked_before(record)

    Path(archive_entry_root(str(tmp_path), digest)).mkdir(parents=True)

    assert materializer.unpacked_before(record)
    assert not materializer.unpacked_before(unhashed)
    assert not CandidateMaterializer().unpacked_before(record)
