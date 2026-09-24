from __future__ import annotations

import marshal
from pathlib import Path

from kpip.core.versions import Version
from kpip.index.catalog_cache import (
    WHEEL_RECORD,
    CatalogChoices,
    cache_key,
    load_catalog,
    load_choices,
    load_links,
    load_summary,
    save_choices,
    save_links,
    save_summary_value,
    summary_key,
)
from kpip.index.links import Link
from kpip.index.source_models import MetadataFile
from kpip.network.cache import SafeFileCache


def test_catalog_cache_roundtrip(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    original = Link.from_url(
        "https://files.example.test/demo-1.2.3-py3-none-any.whl#sha256=abc",
        source_url="https://example.test/simple/demo/",
        text="demo-1.2.3-py3-none-any.whl",
        requires_python=">=3.9",
        yanked_reason="broken release",
        metadata_file=MetadataFile({"sha256": "def"}),
    )

    save_links(cache, "https://example.test/simple/demo/", [original])
    loaded = load_links(cache, "https://example.test/simple/demo/")
    catalog = load_catalog(cache, "https://example.test/simple/demo/")
    summary = load_summary(cache, "https://example.test/simple/demo/")

    assert loaded is not None
    assert catalog is not None
    assert summary is not None
    assert [
        (name, version, [kind for kind, _record in artifacts], facts)
        for name, version, artifacts, facts in catalog[0]
    ] == [
        (
            "demo",
            "1.2.3",
            [WHEEL_RECORD],
            [(WHEEL_RECORD, ">=3.9", "broken release")],
        ),
    ]
    assert catalog[1] == []
    assert [
        (name, version, str(Version.from_wire(version_state)), facts)
        for name, version, version_state, facts in summary[1]
    ] == [
        (
            "demo",
            "1.2.3",
            "1.2.3",
            [(WHEEL_RECORD, ">=3.9", "broken release")],
        ),
    ]
    assert loaded[0].url == original.url
    assert loaded[0].comes_from == original.comes_from
    assert loaded[0].hashes == original.hashes
    assert loaded[0].requires_python == original.requires_python
    assert loaded[0].yanked_reason == original.yanked_reason
    assert loaded[0].metadata_file == original.metadata_file


def test_catalog_summary_hands_off_decoded_catalog(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0-py3-none-any.whl",
        source_url=page_url,
    )
    save_links(cache, page_url, [link])
    cache.delete(summary_key(page_url))

    assert load_summary(cache, page_url) is not None
    pending = getattr(cache, "_kpip_pending_catalogs")
    assert page_url in pending
    assert load_catalog(cache, page_url) is not None
    assert page_url not in pending


def test_catalog_choices_are_scoped_to_generation(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0-py3-none-any.whl",
        source_url=page_url,
    )
    save_links(cache, page_url, [link])
    catalog = load_catalog(cache, page_url)
    summary = load_summary(cache, page_url)

    assert catalog is not None
    assert summary is not None
    record = catalog[0][0][2][0][1]
    choices: CatalogChoices = {"1.0": (record, WHEEL_RECORD, 0)}
    save_choices(cache, page_url, summary[0], "target", True, True, choices)

    embedded = load_summary(cache, page_url)
    assert embedded is not None
    assert embedded[3][("target", True, True)] == choices
    assert (
        load_choices(
            cache,
            page_url,
            summary[0],
            "target",
            True,
            True,
        )
        == choices
    )
    assert (
        load_choices(
            cache,
            page_url,
            "different-generation",
            "target",
            True,
            True,
        )
        == {}
    )


def test_a_tampered_catalog_is_a_miss(tmp_path: Path) -> None:
    """The digest is what a cached catalog's contents now rest on.

    Reading one no longer re-proves the type of every group and record it
    holds. In exchange the blob is sealed: a byte changed in it is not read
    at all, rather than read and then vetted field by field.
    """
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    link = Link.from_url(
        "https://files.example.test/demo-1.0-py3-none-any.whl",
        source_url=page_url,
    )
    save_links(cache, page_url, [link])

    assert load_catalog(cache, page_url) is not None

    stored = cache.get_atomic(cache_key(page_url))
    assert stored is not None
    # Flip a byte of the body, leaving the header and digest in place.
    tampered = bytearray(stored)
    tampered[-1] ^= 0xFF
    cache.set_atomic(cache_key(page_url), bytes(tampered))

    assert load_catalog(cache, page_url) is None


def test_a_catalog_from_another_format_is_not_read(tmp_path: Path) -> None:
    """A blob under a header this kpip does not write is not its own.

    The bucket version is the other half of what replaced per-record
    validation: an older kpip's catalog lives under a different key, and a
    body written under a different header fails the digest check for it.
    """
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    body = marshal.dumps(("kpip-index-catalog", [], []))

    cache.set_atomic(cache_key(page_url), b"kpip-index-catalog-2\0" + body)

    assert load_catalog(cache, page_url) is None


def test_catalog_cache_ignores_corrupt_entries(tmp_path: Path) -> None:
    cache = SafeFileCache(str(tmp_path))
    key = cache_key("https://example.test/simple/demo/")
    cache.set_atomic(key, b"not marshal")

    assert load_links(cache, "https://example.test/simple/demo/") is None


def test_catalog_cache_reuses_validation_until_blob_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    save_links(
        cache,
        page_url,
        [
            Link.from_url(
                "https://files.example.test/demo-1.0-py3-none-any.whl",
                source_url=page_url,
            ),
        ],
    )

    def unexpected_loads(_raw: bytes) -> object:
        raise AssertionError("an unchanged catalog should not be decoded again")

    monkeypatch.setattr(marshal, "loads", unexpected_loads)
    assert load_catalog(cache, page_url) is not None

    monkeypatch.undo()
    cache.set_atomic(cache_key(page_url), b"not marshal")
    assert load_catalog(cache, page_url) is None


def test_catalog_with_an_unparseable_version_is_a_miss(tmp_path: Path) -> None:
    """A semantically corrupt catalog must not raise out of load_summary."""
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    record = (None,) * 8
    groups = [
        ("demo", "1.0", [(WHEEL_RECORD, record)], []),
        ("demo", "not a version", [(WHEEL_RECORD, record)], []),
    ]
    cache.set_atomic(
        cache_key(page_url),
        marshal.dumps(("kpip-index-catalog", groups, [])),
    )

    assert load_catalog(cache, page_url) is None
    assert load_summary(cache, page_url) is None


def test_catalog_with_an_unparseable_upload_time_is_a_miss(tmp_path: Path) -> None:
    """A shape-valid record with a corrupt date must not raise out of
    load_links; validation treats it as a cache miss."""
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    url = "https://files.example.test/demo-1.0.0-py3-none-any.whl"
    record = (
        url,
        "demo-1.0.0-py3-none-any.whl",
        {},
        None,
        None,
        None,
        "not-a-date",
        None,
        ("https", "files.example.test", "/demo-1.0.0-py3-none-any.whl", "", ""),
    )
    groups = [("demo", "1.0.0", [(WHEEL_RECORD, record)], [])]
    cache.set_atomic(
        cache_key(page_url),
        marshal.dumps(("kpip-index-catalog", groups, [])),
    )

    assert load_catalog(cache, page_url) is None
    assert load_links(cache, page_url) is None


def test_a_stored_summary_shares_what_releases_have_in_common(tmp_path: Path) -> None:
    """Equal facts and version parts are stored, and loaded, as one object."""
    cache = SafeFileCache(str(tmp_path))
    page_url = "https://example.test/simple/demo/"
    facts = [(WHEEL_RECORD, ">=3.9", None)]
    summary = (
        "generation",
        [
            ("demo", text, Version(text).to_wire(), list(facts))
            for text in ("1.0", "1.1", "2.0")
        ],
        False,
        {},
    )

    save_summary_value(cache, page_url, summary)
    loaded = load_summary(cache, page_url)

    assert loaded == summary
    assert loaded is not None
    first, second, third = loaded[1]
    assert first[3] is second[3] is third[3]
    suffix = first[2][1][2]
    assert second[2][1][2] is suffix
    assert third[2][1][2] is suffix
