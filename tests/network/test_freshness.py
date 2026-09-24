"""How long a cached response may be served without asking the server again."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kpip.network.freshness import (
    cached_response_is_fresh,
    freshness_deadline,
    metadata_is_fresh,
)
from kpip.network.session import NetworkSession
from kpip_test_support.transport_mocks import make_response

NOW = 1_000_000.0
DATE = "Mon, 12 Jan 1970 13:46:40 GMT"  # NOW as an HTTP date


@pytest.mark.parametrize(
    "headers, deadline",
    [
        ({"Cache-Control": "max-age=600"}, NOW + 600),
        ({"Cache-Control": "public, max-age=600", "Age": "100"}, NOW + 500),
        ({"Cache-Control": "max-age=600", "Age": "900"}, NOW),
        ({"Cache-Control": 'max-age="600"'}, NOW + 600),
        ({"Cache-Control": "max-age=0"}, NOW),
        ({"Cache-Control": "max-age=soon"}, NOW),
        ({"Cache-Control": "no-cache, max-age=600"}, NOW),
        # Qualified no-cache allows reuse only without the fields it names,
        # and a cached response is served with all of them.
        ({"Cache-Control": 'no-cache="Set-Cookie", max-age=600'}, NOW),
        # Of a repeated directive the first counts: a later one cannot
        # lengthen what an earlier one allowed.
        ({"Cache-Control": "max-age=0, max-age=600"}, NOW),
        ({"Cache-Control": "max-age=60, max-age=600"}, NOW + 60),
        # A response the origin dated a while ago arrives that old.
        (
            {"Cache-Control": "max-age=600", "Date": "Mon, 12 Jan 1970 13:41:40 GMT"},
            NOW + 300,
        ),
        (
            {"Cache-Control": "max-age=600", "Date": "Mon, 12 Jan 1970 12:46:40 GMT"},
            NOW,
        ),
        # Age wins when it says the response is older than its Date does.
        ({"Cache-Control": "max-age=600", "Date": DATE, "Age": "120"}, NOW + 480),
        ({"Cache-Control": "no-store, max-age=600"}, None),
        # Expires is measured against the response's own Date, so a server
        # clock an hour ahead still yields the lifetime it meant.
        (
            {
                "Date": "Mon, 12 Jan 1970 14:46:40 GMT",
                "Expires": "Mon, 12 Jan 1970 14:56:40 GMT",
            },
            NOW + 600,
        ),
        (
            {"Date": DATE, "Expires": "Mon, 12 Jan 1970 13:56:40 GMT", "Age": "60"},
            NOW + 540,
        ),
        ({"Expires": "0"}, NOW),
        (
            {"Cache-Control": "max-age=60", "Expires": "Mon, 12 Jan 1970 13:56:40 GMT"},
            NOW + 60,
        ),
        # Nothing to go on: stored, but asked about again next time.
        ({}, NOW),
        ({"ETag": '"tag"'}, NOW),
    ],
)
def test_freshness_deadline(headers: dict[str, str], deadline: float | None) -> None:
    assert freshness_deadline(headers, NOW) == deadline


def test_a_directive_repeated_across_header_lines_keeps_its_first_value() -> None:
    from kpip._vendor.urllib3._collections import HTTPHeaderDict

    headers = HTTPHeaderDict()
    headers.add("Cache-Control", "max-age=0")
    headers.add("Cache-Control", "max-age=600")

    assert freshness_deadline(headers, NOW) == NOW


def test_a_remembered_answer_does_not_survive_the_clock_going_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NetworkSession(cache=str(tmp_path / "http-cache"))
    url = "https://example.invalid/simple/demo/"
    session.cache_response(
        make_response(
            status=200,
            reason="OK",
            url=url,
            headers={"Cache-Control": "max-age=86400", "ETag": '"tag"'},
            body=b"page",
        ),
    )
    remembered: dict[str, tuple[float, float]] = {}
    assert cached_response_is_fresh(session.cache, remembered, url)
    assert url in remembered

    stored = time.time()
    monkeypatch.setattr(time, "time", lambda: stored - 3600)

    assert not cached_response_is_fresh(session.cache, remembered, url)


@pytest.mark.parametrize(
    "values, fresh",
    [
        ({"expires_at": NOW + 1}, True),
        ({"expires_at": NOW}, False),
        ({"expires_at": NOW + 1, "stored_at": NOW - 10}, True),
        # Written before every response got a deadline: once fresh forever.
        ({"expires_at": None}, False),
        ({}, False),
        ({"expires_at": "later"}, False),
        # The clock went back past when the entry was written.
        ({"expires_at": NOW + 600, "stored_at": NOW + 3600}, False),
    ],
)
def test_metadata_is_fresh(values: dict[str, object], fresh: bool) -> None:
    assert metadata_is_fresh(values, NOW) is fresh


@pytest.mark.parametrize(
    "headers",
    [
        {"Cache-Control": "max-age=600", "ETag": '"tag"'},
        {"Cache-Control": "max-age=0", "ETag": '"tag"'},
        {"ETag": '"tag"'},
        {"Cache-Control": "no-cache", "ETag": '"tag"'},
    ],
)
def test_session_and_cache_only_check_agree(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    """The metadata-only check a warm resolve asks must match what a request would do."""

    session = NetworkSession(cache=str(tmp_path / "http-cache"))
    url = "https://example.invalid/simple/demo/"
    session.cache_response(
        make_response(status=200, reason="OK", url=url, headers=headers, body=b"page"),
    )

    served, stale_metadata, _ = session.cache_lookup(url)

    assert cached_response_is_fresh(session.cache, {}, url) is (served is not None)
    assert (stale_metadata is None) is (served is not None)


def test_legacy_entry_without_expiry_is_revalidated(tmp_path: Path) -> None:
    session = NetworkSession(cache=str(tmp_path / "http-cache"))
    assert session.cache is not None
    url = "https://example.invalid/simple/demo/"
    session.cache_response(
        make_response(
            status=200,
            reason="OK",
            url=url,
            headers={"Cache-Control": "max-age=600", "ETag": '"tag"'},
            body=b"page",
        ),
    )
    raw_metadata = session.cache.get(url)
    assert raw_metadata is not None
    metadata = json.loads(raw_metadata)
    metadata["expires_at"] = None
    del metadata["stored_at"]
    session.cache.set(url, json.dumps(metadata).encode("utf-8"))

    served, stale_metadata, _ = session.cache_lookup(url)

    assert served is None
    assert stale_metadata is not None
    assert stale_metadata["etag"] == '"tag"'
    assert not cached_response_is_fresh(session.cache, {}, url)
