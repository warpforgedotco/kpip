"""PEP 700 artifact sizes: parsed, cached, and used to skip range reads.

The JSON Simple API publishes each file's ``size``. Carrying it on ``Link``
lets ``ranged_wheel_metadata`` skip the range-request tier for small wheels,
where 2-3 range round-trips cost more than downloading the wheel outright
(pip's fast-deps lesson, pypa/pip#8670) -- and the full download is reusable
at install time.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from kpip.index.candidate_materialization import (
    _RANGED_METADATA_MIN_WHEEL_BYTES,
    CandidateMaterializer,
)
from kpip.index.catalog_cache import link_from_record, link_record
from kpip.index.links import Link
from kpip.index.page_parsing import IndexPageParser


def parse_links(files: list[dict[str, Any]]) -> list[Link]:
    body = json.dumps({"meta": {"api-version": "1.1"}, "files": files})
    return IndexPageParser().links_from_json(body, "https://index.invalid/demo/")


def test_json_size_lands_on_the_link() -> None:
    [link] = parse_links(
        [{"url": "demo-1.0-py3-none-any.whl", "filename": "x", "size": 123}],
    )

    assert link.size == 123


def test_invalid_sizes_are_ignored() -> None:
    links = parse_links(
        [
            {"url": "a-1.0-py3-none-any.whl", "filename": "a", "size": True},
            {"url": "b-1.0-py3-none-any.whl", "filename": "b", "size": -1},
            {"url": "c-1.0-py3-none-any.whl", "filename": "c", "size": "9"},
            {"url": "d-1.0-py3-none-any.whl", "filename": "d"},
        ],
    )

    assert [link.size for link in links] == [None, None, None, None]


def test_size_survives_the_catalog_record_round_trip() -> None:
    [link] = parse_links(
        [{"url": "demo-1.0-py3-none-any.whl", "filename": "x", "size": 4096}],
    )

    restored = link_from_record(link_record(link))

    assert restored.size == 4096
    assert restored.url == link.url


def test_the_record_layout_is_pinned_to_the_bucket_version() -> None:
    """The stored shape changed, and the cache bucket version changed with it.

    An entry written by an older kpip is not read and mis-parsed; it is not
    found at all, because the catalog, summary and choice buckets are
    versioned and were bumped alongside the record layout. That is what a
    record's shape rests on now: reading a catalog no longer re-proves the
    type of every field it contains, because the digest over the blob says
    the bytes are the ones this kpip wrote.
    """
    from kpip.index.catalog_cache import PREFIX

    [link] = parse_links(
        [{"url": "demo-1.0-py3-none-any.whl", "filename": "x", "size": 4096}],
    )
    record = link_record(link)

    assert len(record) == 9
    assert "3" in PREFIX, "the bucket version carries the layout change"

    restored = link_from_record(record)
    assert restored.size == 4096
    assert restored.url == link.url


def ranged_materializer(reader: Any) -> CandidateMaterializer:
    return CandidateMaterializer(
        session=SimpleNamespace(wheel_metadata_text=reader),  # type: ignore[arg-type]
    )


def candidate_with_size(size: int | None) -> SimpleNamespace:
    link = Link.from_url(
        "https://files.invalid/demo-1.0-py3-none-any.whl",
        source_url="https://index.invalid/demo/",
    )
    link.size = size
    return SimpleNamespace(link=link, name="demo")


def test_small_wheels_skip_the_range_tier() -> None:
    def reader(url: str, name: str) -> str:
        raise AssertionError("a small wheel must not be probed with ranges")

    materializer = ranged_materializer(reader)
    candidate = candidate_with_size(_RANGED_METADATA_MIN_WHEEL_BYTES - 1)

    assert materializer.ranged_wheel_metadata(candidate, frozenset()) is None  # type: ignore[arg-type]


def test_large_and_unsized_wheels_still_use_ranges() -> None:
    seen: list[str] = []

    def reader(url: str, name: str) -> str:
        seen.append(url)
        return ""

    materializer = ranged_materializer(reader)

    for size in (_RANGED_METADATA_MIN_WHEEL_BYTES, None):
        materializer.ranged_wheel_metadata(candidate_with_size(size), frozenset())  # type: ignore[arg-type]

    assert len(seen) == 2
