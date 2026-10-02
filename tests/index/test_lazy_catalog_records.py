"""Characterization tests for CandidateProvider.lazy_catalog_records.

These pin the behavior of a dense, closure-heavy method. Persisted choices
are read when present; a release missing from the profile is filled on demand
from the generation-verified catalog blob, and the stream declines only when
that blob is missing or no longer matches the recorded generation. Most tests
still warm the choice cache first via
``candidate_records_from_catalog(primary_only=True)``, mirroring the
production two-phase usage.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.catalog_cache import (
    cache_key,
    catalog_generation,
    load_choices,
    save_links,
)
from kpip.index.links import Link
from kpip.index.provider import CandidateProvider
from kpip.network.cache import SafeFileCache


def _generation(cache: SafeFileCache, source_url: str) -> str:
    raw = cache.get_atomic(cache_key(source_url))
    assert raw is not None
    return catalog_generation(raw)


def _provider(cache: SafeFileCache) -> CandidateProvider:
    return CandidateProvider.from_options(
        no_index=True,
        session=SimpleNamespace(cache=cache),
    )


def _warm_and_seed(
    provider: CandidateProvider,
    catalog_key: tuple[str, bool, bool],
    records_by_version: dict[Version, tuple[tuple[str, str], ...]],
    versions: tuple[Version, ...],
) -> None:
    """Populate the persisted choice cache and the package-catalog cache the

    way production does before ``lazy_catalog_records`` can read from them.
    """
    catalog = SimpleNamespace(records_by_version=records_by_version)
    provider.candidate_records_from_catalog(
        catalog_key,
        catalog,
        versions,
        primary_only=True,
    )
    provider.package_catalog_cache[catalog_key] = catalog


def test_lazy_catalog_records_yields_pinned_wheel(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=aaa",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    _warm_and_seed(provider, ("demo", True, True), records_by_version, (version,))

    requirement = parse_requirement("demo==1.0.0")
    records = provider.lazy_catalog_records(requirement)
    assert records is not None
    materialized = list(records)

    assert len(materialized) == 1
    assert materialized[0].version == version
    assert materialized[0].wheel is not None


def test_lazy_catalog_records_returns_none_for_url_requirement(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    provider = _provider(cache)
    requirement = parse_requirement(
        "demo @ https://files.example.test/demo-1.0.0-py3-none-any.whl",
    )

    assert provider.lazy_catalog_records(requirement) is None


def test_lazy_catalog_records_returns_none_when_find_links_configured(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    provider = CandidateProvider.from_options(
        no_index=True,
        find_links=[str(tmp_path)],
        session=SimpleNamespace(cache=cache),
    )
    requirement = parse_requirement("demo==1.0.0")

    assert provider.lazy_catalog_records(requirement) is None


def test_lazy_catalog_records_fills_missing_choice(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=bbb",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    provider.package_catalog_cache[("demo", True, True)] = SimpleNamespace(
        records_by_version=records_by_version,
    )

    requirement = parse_requirement("demo==1.0.0")
    stream = provider.lazy_catalog_records(requirement)
    assert stream is not None
    records = list(stream)
    assert len(records) == 1
    assert records[0].wheel is not None

    target_key = provider.catalog_target_internal()[1]
    # Choices are written when the provider is closed or the process exits.
    provider.flush_catalog_choices()
    persisted = load_choices(cache, source_url, generation, target_key, True, True)
    assert "1.0.0" in persisted
    assert persisted["1.0.0"] is not None


def test_lazy_catalog_records_declines_when_catalog_blob_missing(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=bbb",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    generation = _generation(cache, source_url)
    cache.delete(cache_key(source_url))
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    provider.package_catalog_cache[("demo", True, True)] = SimpleNamespace(
        records_by_version=records_by_version,
    )

    requirement = parse_requirement("demo==1.0.0")
    assert provider.lazy_catalog_records(requirement) is None

    target_key = provider.catalog_target_internal()[1]
    assert not load_choices(cache, source_url, generation, target_key, True, True)


def test_lazy_catalog_records_declines_on_generation_mismatch(
    tmp_path: Path,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=bbb",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [link])
    stale_generation = _generation(cache, source_url)
    replacement = Link.from_url(
        "https://files.example.test/demo-2.0.0-py3-none-any.whl#sha256=ccc",
        source_url=source_url,
        text="demo-2.0.0-py3-none-any.whl",
    )
    save_links(cache, source_url, [replacement])
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, stale_generation),)}

    provider = _provider(cache)
    provider.package_catalog_cache[("demo", True, True)] = SimpleNamespace(
        records_by_version=records_by_version,
    )

    requirement = parse_requirement("demo==1.0.0")
    assert provider.lazy_catalog_records(requirement) is None

    target_key = provider.catalog_target_internal()[1]
    assert not load_choices(
        cache,
        source_url,
        stale_generation,
        target_key,
        True,
        True,
    )


def test_filled_choice_matches_full_path_choice(tmp_path: Path) -> None:
    """Parity oracle: the lazy fill persists the same payload the full
    candidate_records_from_catalog evaluation would have."""

    def build(root: Path) -> tuple[SafeFileCache, str, str, Version]:
        cache = SafeFileCache(str(root))
        source_url = "https://example.test/simple/demo/"
        links = [
            Link.from_url(
                "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=bbb",
                source_url=source_url,
                text="demo-1.0.0-py3-none-any.whl",
            ),
            Link.from_url(
                "https://files.example.test/demo-1.0.0.tar.gz#sha256=ddd",
                source_url=source_url,
                text="demo-1.0.0.tar.gz",
            ),
        ]
        save_links(cache, source_url, links)
        return cache, source_url, _generation(cache, source_url), Version("1.0.0")

    lazy_root = tmp_path / "lazy"
    full_root = tmp_path / "full"
    lazy_root.mkdir()
    full_root.mkdir()

    cache, source_url, generation, version = build(lazy_root)
    provider = _provider(cache)
    provider.package_catalog_cache[("demo", True, True)] = SimpleNamespace(
        records_by_version={version: ((source_url, generation),)},
    )
    assert provider.lazy_catalog_records(parse_requirement("demo==1.0.0"))
    target_key = provider.catalog_target_internal()[1]
    # Choices are written when the provider is closed or the process exits.
    provider.flush_catalog_choices()
    lazy_choices = load_choices(cache, source_url, generation, target_key, True, True)

    full_cache, full_url, full_generation, _ = build(full_root)
    full_provider = _provider(full_cache)
    full_provider.candidate_records_from_catalog(
        ("demo", True, True),
        SimpleNamespace(
            records_by_version={version: ((full_url, full_generation),)},
        ),
        (version,),
        primary_only=True,
    )
    full_target_key = full_provider.catalog_target_internal()[1]
    full_provider.flush_catalog_choices()
    full_choices = load_choices(
        full_cache,
        full_url,
        full_generation,
        full_target_key,
        True,
        True,
    )

    assert generation == full_generation
    assert lazy_choices == full_choices
    assert lazy_choices


def test_lazy_catalog_records_choice_prefers_wheel_over_sdist(
    tmp_path: Path,
) -> None:
    """The persisted per-version choice already picked a single winner

    (``_select_catalog_choice`` ranks WHEEL_RECORD above SDIST_RECORD), so when both
    artifact kinds exist for the same version, only the wheel is yielded.
    """
    cache = SafeFileCache(str(tmp_path))
    source_url = "https://example.test/simple/demo/"
    wheel_link = Link.from_url(
        "https://files.example.test/demo-1.0.0-py3-none-any.whl#sha256=ccc",
        source_url=source_url,
        text="demo-1.0.0-py3-none-any.whl",
    )
    sdist_link = Link.from_url(
        "https://files.example.test/demo-1.0.0.tar.gz#sha256=ddd",
        source_url=source_url,
        text="demo-1.0.0.tar.gz",
    )
    save_links(cache, source_url, [wheel_link, sdist_link])
    generation = _generation(cache, source_url)
    version = Version("1.0.0")
    records_by_version = {version: ((source_url, generation),)}

    provider = _provider(cache)
    _warm_and_seed(provider, ("demo", True, True), records_by_version, (version,))

    requirement = parse_requirement("demo==1.0.0")
    records = provider.lazy_catalog_records(requirement)
    assert records is not None
    materialized = list(records)

    assert len(materialized) == 1
    assert materialized[0].wheel is not None


def test_summary_path_fills_missing_choice_without_demotion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A release missing from the summary-embedded choice profile is filled
    per release instead of demoting the package to full link materialization."""
    cache = SafeFileCache(str(tmp_path / "cache"))
    page_url = "https://index.invalid/simple/demo/"
    links = [
        Link.from_url(
            f"https://files.invalid/demo-{version}-py3-none-any.whl",
            source_url=page_url,
            text=f"demo-{version}-py3-none-any.whl",
        )
        for version in ("1.0", "2.0")
    ]
    save_links(cache, page_url, links)
    generation = _generation(cache, page_url)

    class Session:
        def __init__(self) -> None:
            self.cache = cache

        @staticmethod
        def has_fresh_cached_response(url: str) -> bool:
            del url
            return True

    warm_provider = CandidateProvider.from_options(
        index_url="https://index.invalid/simple",
        session=Session(),
    )
    assert warm_provider.find_candidates(parse_requirement("demo==2.0"))
    target_key = warm_provider.catalog_target_internal()[1]
    # Choices are written when the provider is closed or the process exits.
    warm_provider.flush_catalog_choices()
    persisted = load_choices(cache, page_url, generation, target_key, True, True)
    assert set(persisted) == {"2.0"}
    warm_provider.close()

    provider = CandidateProvider.from_options(
        index_url="https://index.invalid/simple",
        session=Session(),
    )

    def no_demotion(requirement):
        raise AssertionError("full link materialization must not run")

    monkeypatch.setattr(provider, "catalog_links", no_demotion)
    link_from_record = provider.link_from_catalog_record
    constructed: list[str] = []

    def counting_link_from_record(record, source_url):
        constructed.append(str(record[0]))
        return link_from_record(record, source_url)

    monkeypatch.setattr(provider, "link_from_catalog_record", counting_link_from_record)

    records = provider.lazy_catalog_records(parse_requirement("demo"))
    assert records is not None
    assert next(records).version == Version("2.0")
    assert constructed == ["https://files.invalid/demo-2.0-py3-none-any.whl"]

    provider.flush_catalog_choices()
    persisted = load_choices(cache, page_url, generation, target_key, True, True)
    assert set(persisted) == {"1.0", "2.0"}
    provider.close()
