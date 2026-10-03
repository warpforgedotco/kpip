"""Index pages decoded by msgspec compile to exactly what ``json`` gives."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from kpip.index import typed_pages
from kpip.index.page_parsing import IndexPageParser

pytest.importorskip("msgspec")

CORPUS = Path(__file__).parents[1] / "benchmarks" / "corpus" / "uv_graphs"

SOURCE = "https://x.org/simple/a/"

EDGE_PAGES = [
    # A null core-metadata hides the older spelling beside it; a negative
    # size, a string yanked reason and a second hash.
    {
        "meta": {"api-version": "1.1"},
        "files": [
            {
                "url": "https://x.org/p/a-1.0.tar.gz",
                "filename": "a-1.0.tar.gz",
                "core-metadata": None,
                "dist-info-metadata": {"sha256": "ab"},
                "size": -3,
                "yanked": "bad",
                "hashes": {"sha256": "cd", "md5": "ef"},
            }
        ],
    },
    # Fields of the wrong type are judged by the record builder, not the
    # decoder.
    {
        "files": [
            {
                "url": "https://x.org/p/a-1.0.tar.gz",
                "requires-python": 5,
                "upload-time": 7,
                "dist-info-metadata": True,
                "hashes": [],
            }
        ]
    },
    # An entry that is not an object: the whole page goes to ``json``.
    {"files": [3, {"url": "https://x.org/p/a-1.0.tar.gz"}]},
    # A URL that is not plain goes through ``Link``.
    {
        "files": [
            {
                "url": "https://x.org/p/a-1.0.tar.gz?x=1&y=2#sha256=00",
                "filename": "a-1.0.tar.gz",
                "core-metadata": {"sha256": "aa"},
            }
        ]
    },
    # URLs at every edge of what the typed path takes as plain.
    {
        "files": [
            {"url": url, "filename": "a-1.0-py3-none-any.whl"}
            for url in (
                "https://x.org/p/a-1.0-py3-none-any.whl",
                "HTTPS://x.org/p/a-1.0-py3-none-any.whl",
                "https://x.org:8080/p/a-1.0-py3-none-any.whl",
                "https://x.org",
                "http://x",
                "https://x.org/p/a%2D1.0-py3-none-any.whl",
                "https://x.org/p/a-1.0-py3-none-any.whl&x",
                "https://x.org/p/a-1.0-py3-none-any.whl;",
                "https://x.org/p/a 1.0-py3-none-any.whl",
                "https://x.org/p\\a-1.0-py3-none-any.whl",
                "https://[::1]/p/a-1.0-py3-none-any.whl",
                "https://x.org/p/a-1.0-py3-none-any.whl#sha256=00",
                "https://x.org/p/a-1.0-py3-none-any.whl?x=1",
                "https://x.org/p/a-1.0-py3-none-any.whl\x7f",
                "https://x.org/p/\u00e9-1.0-py3-none-any.whl",
                "https://x.org/p/",
                "https://x.org/p/..",
                "https://x.org/p/a__b-1.0-py3-none-any.whl",
                "https://x.org/p/a-1.0-x-py3-none-any.whl",
                "https://x.org/p/a-bad!-py3-none-any.whl",
                "p/a-1.0-py3-none-any.whl",
            )
        ]
    },
    # Relative URLs, a yanked wheel and a file with no URL.
    {
        "files": [
            {
                "url": "../../a/a-2.0-py3-none-any.whl",
                "filename": "a-2.0-py3-none-any.whl",
                "yanked": True,
            },
            {"filename": "a-3.0.tar.gz"},
        ]
    },
]


def _compile(body: bytes | str, url: str, *, typed: bool) -> object:
    parser = IndexPageParser.__new__(IndexPageParser)
    if typed:
        return parser.catalog_from_json(body, url)
    decode_page = typed_pages.decode_page
    typed_pages.decode_page = lambda body: None  # type: ignore[assignment]
    try:
        return parser.catalog_from_json(body, url)
    finally:
        typed_pages.decode_page = decode_page


@pytest.mark.parametrize("page", EDGE_PAGES)
def test_edge_pages_compile_the_same_either_way(page: dict[str, object]) -> None:
    body = json.dumps(page).encode()

    assert _compile(body, SOURCE, typed=True) == _compile(body, SOURCE, typed=False)


def test_a_page_msgspec_will_not_take_is_read_with_json() -> None:
    assert typed_pages.decode_page(b'{"files": [3]}') is None
    assert typed_pages.decode_page(b'{"files": {}}') is None
    assert typed_pages.decode_page(b"not json") is None


def test_an_unsupported_api_version_is_refused_either_way() -> None:
    from kpip.core.errors import InstallationError

    body = b'{"meta": {"api-version": "2.0"}, "files": []}'
    for typed in (True, False):
        with pytest.raises(InstallationError, match="version 2.0"):
            _compile(body, SOURCE, typed=typed)


@pytest.mark.parametrize("workload", ["jupyter", "airflow"])
def test_recorded_pypi_pages_compile_the_same_either_way(workload: str) -> None:
    archive_path = CORPUS / workload / "pages.zip"
    if not archive_path.exists():
        pytest.skip(f"no recorded {workload} pages")
    with zipfile.ZipFile(archive_path) as archive:
        index = json.loads(archive.read("index.json"))
        pages = [
            (key, archive.read(member))
            for key, status, _, _, member in index
            if status == 200
        ]
    pages = [(key, body) for key, body in pages if body[:1] == b"{"]
    assert pages

    for key, body in pages:
        assert _compile(body, key, typed=True) == _compile(body, key, typed=False), key
