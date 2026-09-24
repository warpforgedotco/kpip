"""Revalidate-then-summary: a 304 on a stale page keeps the link-free path."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from kpip.core.http_contracts import HttpResponse
from kpip.core.packaging import parse_requirement
from kpip.core.expiry import expiry_is_fresh
from kpip.index.catalog_cache import (
    cache_key,
    catalog_generation,
    decode_summary,
    embed_summary_choices,
    save_summary_value,
    summary_freshness,
    summary_is_fresh,
    summary_key,
)
from kpip.index.source_locations import SimpleIndexSource
from kpip.network.exceptions import ConnectionFailedError
from kpip.network.session import NetworkSession
from kpip_test_support.transport_mocks import make_response

INDEX_URL = "https://index.invalid/simple"
PROJECT_URL = "https://index.invalid/simple/demo/"
JSON_TYPE = "application/vnd.pypi.simple.v1+json"


def page_body(versions: tuple[str, ...]) -> bytes:
    return json.dumps(
        {
            "meta": {"api-version": "1.0"},
            "name": "demo",
            "files": [
                {
                    "url": f"https://files.invalid/demo-{version}-py3-none-any.whl",
                    "filename": f"demo-{version}-py3-none-any.whl",
                    "hashes": {"sha256": "a" * 64},
                }
                for version in versions
            ],
        },
    ).encode()


class FakeIndexSession(NetworkSession):
    def __init__(self, cache_dir: str) -> None:
        super().__init__(cache=cache_dir)
        self.etag = '"v1"'
        self.body = page_body(("1.0", "2.0"))
        self.transport_calls = 0
        self.fail_with: type[BaseException] | None = None
        self.status_override: int | None = None

    def open_internal(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout,
        *,
        stream: bool = False,
    ) -> HttpResponse:
        self.transport_calls += 1
        if self.fail_with is not None:
            raise self.fail_with()
        if self.status_override is not None:
            return make_response(
                status=self.status_override,
                reason="Not Found",
                url=url,
                headers={"Content-Type": "text/plain"},
                body=b"",
            )
        if headers.get("if-none-match") == self.etag:
            return make_response(
                status=304,
                reason="Not Modified",
                url=url,
                headers={"ETag": self.etag, "Cache-Control": "max-age=600"},
                body=b"",
            )
        return make_response(
            status=200,
            reason="OK",
            url=url,
            headers={
                "Content-Type": JSON_TYPE,
                "Cache-Control": "max-age=0",
                "ETag": self.etag,
            },
            body=self.body,
        )


def primed_source(tmp_path: Path) -> tuple[SimpleIndexSource, FakeIndexSession]:
    session = FakeIndexSession(str(tmp_path / "http-cache"))
    source = SimpleIndexSource(INDEX_URL, session=session)
    links = source.collect_links(parse_requirement("demo"))
    assert len(links) == 2
    assert session.transport_calls == 1
    assert not source.has_fresh_cached_page(parse_requirement("demo"))
    return source, session


def stored_generation(session: FakeIndexSession) -> str:
    raw = session.cache.get_atomic(cache_key(PROJECT_URL))
    assert raw is not None
    return catalog_generation(raw)


def test_stale_page_revalidates_into_summary(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)
    generation = stored_generation(session)

    summary = source.collect_cached_catalog_summary(
        parse_requirement("demo"),
        allow_fetch=True,
    )

    assert summary is not None
    assert summary[0] == generation
    assert session.transport_calls == 2
    assert source.has_fresh_cached_page(parse_requirement("demo"))
    assert not source.page_fetch_outcomes


def test_changed_page_compiles_its_summary_without_building_links(
    tmp_path: Path,
) -> None:
    """A changed JSON page is compiled straight into the catalog.

    It used to be parsed into one link per file and reported as a miss, so
    the provider reasoned over those links; an airflow lock built 313,925 of
    them and discarded them. The summary now comes back directly, and the
    body is kept so a caller that still wants links pays no second fetch.
    """
    source, session = primed_source(tmp_path)
    old_generation = stored_generation(session)
    session.etag = '"v2"'
    session.body = page_body(("1.0", "2.0", "3.0"))

    requirement = parse_requirement("demo")
    summary = source.collect_cached_catalog_summary(requirement, allow_fetch=True)

    assert summary is not None
    assert summary[0] == stored_generation(session)
    assert summary[0] != old_generation
    assert [group[1] for group in summary[1]] == ["1.0", "2.0", "3.0"]
    assert session.transport_calls == 2
    assert not source.page_fetch_outcomes

    # Nothing on the cold path asks for links any more -- a cold airflow lock
    # makes zero such calls -- but a caller that does still gets them, by
    # re-reading the page rather than from a body held in memory for the whole
    # run.
    links = source.collect_links(requirement)
    assert len(links) == 3


def test_missing_page_memoizes_empty(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)
    session.cache.delete(PROJECT_URL)
    session.fresh_cached_response_cache.clear()
    session.status_override = 404

    requirement = parse_requirement("demo")
    summary = source.collect_cached_catalog_summary(requirement, allow_fetch=True)

    assert summary is None
    assert session.transport_calls == 2
    assert source.collect_links(requirement) == []
    assert session.transport_calls == 2


def test_network_error_propagates(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)
    session.fail_with = ConnectionError

    with pytest.raises(ConnectionFailedError):
        source.collect_cached_catalog_summary(
            parse_requirement("demo"),
            allow_fetch=True,
        )


def test_no_fetch_by_default(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)

    summary = source.collect_cached_catalog_summary(parse_requirement("demo"))

    assert summary is None
    assert session.transport_calls == 1


def test_provider_stays_in_record_world_after_revalidation(tmp_path: Path) -> None:
    _source, session = primed_source(tmp_path)
    from kpip.index.provider import CandidateProvider

    provider = CandidateProvider.from_options(
        index_url=INDEX_URL,
        session=session,
    )
    link_from_record = provider.link_from_catalog_record
    constructed: list[str] = []

    def counting_link_from_record(record, source_url):
        constructed.append(str(record[0]))
        return link_from_record(record, source_url)

    provider.link_from_catalog_record = counting_link_from_record

    versions = {
        summary.version
        for summary in provider.available_versions(
            parse_requirement("demo"),
        )
    }
    assert {str(version) for version in versions} == {"1.0", "2.0"}
    assert session.transport_calls == 2

    catalog = provider.package_catalog_cache[("demo", True, True)]
    assert catalog.records_by_version is not None
    assert catalog.links == ()

    candidates = provider.find_candidates(parse_requirement("demo==2.0"))
    assert [str(candidate.version) for candidate in candidates] == ["2.0"]
    assert constructed == ["https://files.invalid/demo-2.0-py3-none-any.whl"]
    assert session.transport_calls == 2
    provider.close()


def _counting_reads(session: FakeIndexSession) -> list[str]:
    """Record every atomic entry the session's cache reads."""
    reads: list[str] = []
    get_atomic = session.cache.get_atomic

    def counted(key: str) -> bytes | None:
        reads.append(key)
        return get_atomic(key)

    session.cache.get_atomic = counted
    return reads


def _page_metadata_forbidden(session: FakeIndexSession) -> None:
    def forbidden(url: str) -> bool:
        raise AssertionError(f"read the page metadata of {url}")

    session.has_fresh_cached_response = forbidden


def test_a_revalidated_summary_answers_alone_in_the_next_process(
    tmp_path: Path,
) -> None:
    """One file per fresh page: the summary vouches for it and is the catalog."""
    source, _session = primed_source(tmp_path)
    assert source.collect_cached_catalog_summary(
        parse_requirement("demo"), allow_fetch=True
    )

    later = FakeIndexSession(str(tmp_path / "http-cache"))
    reads = _counting_reads(later)
    _page_metadata_forbidden(later)
    fresh_source = SimpleIndexSource(INDEX_URL, session=later)
    requirement = parse_requirement("demo")

    assert fresh_source.has_fresh_cached_page(requirement)
    summary = fresh_source.collect_cached_catalog_summary(requirement)

    assert summary is not None
    assert [group[1] for group in summary[1]] == ["1.0", "2.0"]
    assert reads == [summary_key(PROJECT_URL)]
    assert later.transport_calls == 0


def test_a_summary_learns_its_freshness_from_the_page(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)
    requirement = parse_requirement("demo")
    summary = source.collect_cached_catalog_summary(requirement, allow_fetch=True)
    assert summary is not None
    # A summary written without it, as one compiled from its catalog is.
    save_summary_value(session.cache, PROJECT_URL, summary)
    raw = session.cache.get_atomic(summary_key(PROJECT_URL))
    assert raw is not None
    assert summary_freshness(raw) is None

    second = SimpleIndexSource(
        INDEX_URL, session=FakeIndexSession(str(tmp_path / "http-cache"))
    )
    assert second.has_fresh_cached_page(requirement)
    assert second.collect_cached_catalog_summary(requirement) == summary

    third_session = FakeIndexSession(str(tmp_path / "http-cache"))
    _page_metadata_forbidden(third_session)
    third = SimpleIndexSource(INDEX_URL, session=third_session)
    assert third.has_fresh_cached_page(requirement)
    assert third.collect_cached_catalog_summary(requirement) == summary


def test_a_torn_freshness_block_reads_as_unknown(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)
    assert source.collect_cached_catalog_summary(
        parse_requirement("demo"), allow_fetch=True
    )
    raw = session.cache.get_atomic(summary_key(PROJECT_URL))
    assert raw is not None
    assert summary_freshness(raw) is not None

    offset = raw.index(b"\0") + 1
    torn = raw[:offset] + bytes([raw[offset] ^ 0xFF]) + raw[offset + 1 :]

    assert summary_freshness(torn) is None
    assert not summary_is_fresh(torn, time.time())
    assert decode_summary(torn) == decode_summary(raw)


def test_embedding_choices_keeps_the_recorded_freshness(tmp_path: Path) -> None:
    source, session = primed_source(tmp_path)
    summary = source.collect_cached_catalog_summary(
        parse_requirement("demo"), allow_fetch=True
    )
    assert summary is not None
    before = session.cache.get_atomic(summary_key(PROJECT_URL))
    assert before is not None

    embed_summary_choices(
        session.cache, PROJECT_URL, summary[0], "cp314", True, True, {}
    )

    after = session.cache.get_atomic(summary_key(PROJECT_URL))
    assert after is not None
    assert after != before
    assert summary_freshness(after) == summary_freshness(before)


@pytest.mark.parametrize(
    "expires_in,stored_in,fresh",
    [(600, 0, True), (-1, 0, False), (600, 30, True), (600, 3600, False)],
)
def test_freshness_distrusts_an_entry_from_the_future(
    expires_in: float, stored_in: float, fresh: bool
) -> None:
    now = 1_000_000.0

    assert expiry_is_fresh(now + expires_in, now + stored_in, now) is fresh
