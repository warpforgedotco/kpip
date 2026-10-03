"""A pinned requirement compiles only its release's files from the page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.page_parsing import release_filter
from kpip.index.provider import CandidateProvider

from tests.index.test_summary_revalidation import (
    INDEX_URL,
    FakeIndexSession,
)

FILES = (
    ("demo-1.0-py3-none-any.whl", False),
    ("demo-1.0.tar.gz", False),
    ("demo-2.0-py3-none-any.whl", True),
    ("demo-2.0-1-py3-none-any.whl", True),
    ("demo-2.0.0rc1-py3-none-any.whl", False),
    ("demo-3.0-py3-none-any.whl", False),
    ("demo-3.0.zip", False),
)


def page_body() -> bytes:
    return json.dumps(
        {
            "meta": {"api-version": "1.0"},
            "name": "demo",
            "files": [
                {
                    "url": f"https://files.invalid/{filename}",
                    "filename": filename,
                    "hashes": {"sha256": "a" * 64},
                    "yanked": yanked,
                }
                for filename, yanked in FILES
            ],
        },
    ).encode()


def provider(tmp_path: Path, *, pinned: bool) -> CandidateProvider:
    session = FakeIndexSession(str(tmp_path / "http-cache"))
    session.body = page_body()
    result = CandidateProvider.from_options(index_url=INDEX_URL, session=session)
    if not pinned:
        result.pinned_release = lambda requirement: None
    return result


def found(provider: CandidateProvider, requirement: str) -> list[tuple[str, str]]:
    return sorted(
        (str(candidate.version), candidate.record_internal.link.filename)
        for candidate in provider.find_candidates(parse_requirement(requirement))
    )


@pytest.mark.parametrize(
    "url, kept",
    [
        ("https://files.invalid/demo-1.0-py3-none-any.whl", True),
        ("https://files.invalid/demo-1.0.0-py3-none-any.whl", True),
        ("https://files.invalid/demo-1.0+local-py3-none-any.whl", True),
        ("https://files.invalid/demo-1.0%2Blocal-py3-none-any.whl#sha256=a", True),
        ("https://files.invalid/demo-1.0-1-cp312-cp312-linux_x86_64.whl", True),
        ("https://files.invalid/demo-1.1-py3-none-any.whl", False),
        ("https://files.invalid/demo-1.0.post1-py3-none-any.whl", False),
        ("https://files.invalid/demo-1.0.tar.gz?x=1", True),
        ("https://files.invalid/my-demo-pkg-1.0.tar.gz", True),
        ("https://files.invalid/my-demo-pkg-1.1.tar.gz", False),
        ("https://files.invalid/demo-1.0-weird.whl", True),
        ("https://files.invalid/demo.egg", True),
        ("https://files.invalid/demo-latest.tar.gz", True),
    ],
)
def test_release_filter(url: str, kept: bool) -> None:
    assert release_filter(Version("1.0"))(url) is kept


@pytest.mark.parametrize(
    "requirement",
    ["demo==1.0", "demo==1.0.0", "demo==2.0", "demo==3.0", "demo==4.0", "demo"],
)
def test_pinned_candidates_match_the_full_catalog(
    tmp_path: Path,
    requirement: str,
) -> None:
    full = provider(tmp_path / "full", pinned=False)
    pinned = provider(tmp_path / "pinned", pinned=True)

    assert found(pinned, requirement) == found(full, requirement)
    full.close()
    pinned.close()


def test_a_pinned_catalog_does_not_answer_an_unpinned_requirement(
    tmp_path: Path,
) -> None:
    source = provider(tmp_path, pinned=True)

    assert [version for version, _ in found(source, "demo==1.0")] == ["1.0"]
    assert ("demo", True, True, Version("1.0")) in source.package_catalog_cache
    assert ("demo", True, True) not in source.package_catalog_cache

    versions = {
        str(summary.version)
        for summary in source.available_versions(parse_requirement("demo"))
    }
    assert versions == {"1.0", "2.0", "2.0.0rc1", "3.0"}
    source.close()

    again = provider(tmp_path, pinned=False)
    assert {
        str(summary.version)
        for summary in again.available_versions(parse_requirement("demo"))
    } == versions
    again.close()


def test_a_pinned_requirement_never_parses_the_page_into_links(
    tmp_path: Path,
) -> None:
    source = provider(tmp_path, pinned=True)

    def forbidden(requirement: object) -> None:
        raise AssertionError(f"parsed the page into links for {requirement}")

    source.catalog_links = forbidden
    requirement = parse_requirement("demo==1.0")

    assert [str(record.version) for record in source.newest_accepted(requirement, 2)]
    assert [
        str(summary.version)
        for summary in source.matching_versions(requirement, allow_prereleases=False)
    ] == ["1.0"]
    assert {
        str(record.version) for record in source.evaluate_links(requirement).accepted
    } == {"1.0"}
    source.close()
