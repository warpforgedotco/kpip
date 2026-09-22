"""The forward check reads the artifact the resolver would materialize.

A freshly parsed catalog offers every release's wheel *and* sdist, and the
forward check declined on any release with more than one artifact, so on a
cold run it never rejected anything: boto3 with ``urllib3<1.25.4`` walked
1,438 conflicts cold and none warm.  The per-release read orders a release's
artifacts preferred first, the order a decision materializes them in and
records ``candidates[0]`` from, so the check reads that same artifact.
"""

from __future__ import annotations

from pathlib import Path

from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version

from .test_provider_memos import make_provider


def _wheelhouse_with_both_artifacts(tmp_path: Path) -> Path:
    from benchmark_support import make_sdist, make_wheel, reset_caches

    reset_caches()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "demo", "1.0.0", requires=["dep>=2"])
    make_sdist(wheelhouse, "demo", "1.0.0")
    return wheelhouse


def test_a_release_with_a_wheel_and_an_sdist_is_read_from_its_wheel(
    tmp_path: Path,
) -> None:
    provider = make_provider(_wheelhouse_with_both_artifacts(tmp_path))
    provider.requirements["demo"] = parse_requirement("demo")
    version = Version("1.0.0")

    records = provider.provider.release_candidates(parse_requirement("demo"), version)
    assert records is not None
    assert len(records) == 2, "the read still offers both artifacts"

    candidate = provider._catalog_candidate("demo", version)

    assert candidate is not None
    assert getattr(candidate, "source_kind", None) == "wheel"
    assert [str(d) for d in candidate.dependencies] == ["dep>=2"]


def test_prefetching_a_release_with_both_artifacts_caches_its_wheel(
    tmp_path: Path,
) -> None:
    provider = make_provider(_wheelhouse_with_both_artifacts(tmp_path))
    provider.requirements["demo"] = parse_requirement("demo")
    version = Version("1.0.0")

    provider._prefetch_catalog_candidates("demo", [version])

    cached = provider._catalog_candidate_cache.get(("demo", version))
    assert cached is not None
    assert getattr(cached, "source_kind", None) == "wheel"
