"""Stale index pages answered from the cache while they revalidate."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from kpip.index import source_locations
from kpip.index.source_locations import SimpleIndexSource

PAGE = "https://index.invalid/simple/demo/"


class Session:
    cache = object()

    def __init__(self, revalidates: bool = True) -> None:
        self.revalidates = revalidates

    def can_revalidate(self, url: str) -> bool:
        return self.revalidates


class Source(SimpleIndexSource):
    """Without slots, so a test can replace a method on one instance."""


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> SimpleIndexSource:
    monkeypatch.setattr(
        source_locations, "load_summary", lambda cache, url: ("summary", url)
    )
    source = Source("https://index.invalid/simple", session=Session())  # type: ignore[arg-type]
    source.serve_stale = True
    return source


def outcome(
    monkeypatch: pytest.MonkeyPatch, source: SimpleIndexSource, result: Any
) -> list[str]:
    refreshed: list[str] = []

    def refresh_page(url: str) -> bool:
        refreshed.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(source, "refresh_page", refresh_page)
    return refreshed


def test_a_stale_page_is_answered_now_and_revalidated_once(
    monkeypatch: pytest.MonkeyPatch, source: SimpleIndexSource
) -> None:
    refreshed = outcome(monkeypatch, source, True)

    assert source.stale_summary(PAGE) == ("summary", PAGE)
    assert source.stale_summary(PAGE) == ("summary", PAGE)
    assert source.stale_pages_unchanged()
    assert refreshed == [PAGE]


@pytest.mark.parametrize("result", [False, OSError("unreachable")])
def test_a_changed_or_unreachable_page_is_not_unchanged(
    monkeypatch: pytest.MonkeyPatch, source: SimpleIndexSource, result: Any
) -> None:
    outcome(monkeypatch, source, result)

    source.stale_summary(PAGE)

    assert not source.stale_pages_unchanged()


def test_nothing_served_stale_is_unchanged(source: SimpleIndexSource) -> None:
    assert source.stale_pages_unchanged()


def test_a_page_without_a_validator_is_not_served_stale(
    monkeypatch: pytest.MonkeyPatch, source: SimpleIndexSource
) -> None:
    source.session = Session(revalidates=False)  # type: ignore[assignment]
    refreshed = outcome(monkeypatch, source, True)

    assert source.stale_summary(PAGE) is None
    assert refreshed == []


def test_only_a_resolve_that_asks_is_answered_stale(
    monkeypatch: pytest.MonkeyPatch, source: SimpleIndexSource
) -> None:
    """Without ``serve_stale`` a stale page is fetched, as it always was."""
    source.serve_stale = False
    monkeypatch.setattr(source, "has_fresh_cached_page", lambda requirement: False)
    monkeypatch.setattr(
        source,
        "stale_summary",
        lambda url: pytest.fail("served a stale page unasked"),
    )

    class Fetched(Exception):
        pass

    def read(self: Any, url: str) -> Any:
        raise Fetched

    monkeypatch.setattr(source_locations.IndexPageParser, "read", read)

    from kpip.core.packaging import parse_requirement

    with pytest.raises(Fetched):
        source.collect_cached_catalog_summary(
            parse_requirement("demo"), allow_fetch=True
        )


def test_closing_drops_revalidations_not_yet_sent(
    monkeypatch: pytest.MonkeyPatch, source: SimpleIndexSource
) -> None:
    release = threading.Event()
    monkeypatch.setattr(source_locations, "REFRESH_WORKERS", 1)
    refreshed: list[str] = []

    def refresh_page(url: str) -> bool:
        refreshed.append(url)
        release.wait(5)
        return True

    monkeypatch.setattr(source, "refresh_page", refresh_page)

    for name in ("a", "b", "c"):
        source.stale_summary(f"https://index.invalid/simple/{name}/")

    source.stop_revalidating()
    release.set()

    assert len(refreshed) <= 1
    assert source.stale_pages_unchanged()
