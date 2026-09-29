"""Simple API page parsing: the link URL join fast path and the JSON loop."""

from __future__ import annotations

import random
import urllib.parse
from pathlib import Path

import pytest
from kpip.index.page_parsing import IndexPageParser, join_index_url

BASES = (
    "https://pypi.org/simple/pkg/",
    "http://example.invalid/simple/",
    "file:///srv/index/",
    "https://u:p@h:8443/a/b",
    "https://pypi.org/simple/pkg",
    "https://h",
    "https://h/",
    "http://h:8080/a/b/c/",
    "https://h/a//b/",
    "https://h/a/./b/../c/",
    "https://h/x?q",
    "https://h/x#f",
    "https://h/x;p/",
    "https:///x/",
    "https://[::1]/a/",
    "HTTPS://X/",
    "ftp://h/",
    "",
)

HREFS = (
    "https://files.pythonhosted.org/packages/a/b/c-1.0-py3-none-any.whl",
    "https://files.pythonhosted.org/packages/c-1.0.tar.gz#sha256=abc",
    "https://h/x#egg=foo&sha256=1",
    "https://h/x?a=1&b=2",
    "https://h/x%20y",
    "https://user@h/x",
    "https://h:80/x",
    "https://h",
    "https://h/",
    "https://h?",
    "https://h#",
    "https://h/;",
    "https://h/a;b",
    "https://h/a;b/c",
    "https://h/a;b;c",
    "https://h/a;;",
    "https://h/a;?q",
    "https://h/a;#f",
    "https://h/a?#f",
    "https://h/a#?",
    "https://h/a#f?x",
    "https://h/a?q#",
    "https://h/a?q;",
    "https://h/a?;",
    "https://h/a#;",
    "https://h/a?b?c",
    "https://h/a##b",
    "https://h/a;b?c#d",
    "https://h/a?c;d#e;f",
    "https://h/a#b;c?d",
    "https://h;x/y",
    "https://h;",
    "https://h/a/../b",
    "https://h/./a",
    "https://h//a",
    "https://h/a//b",
    "https://h/x\\y",
    "https:///x",
    "https://?x",
    "https://#x",
    "https://",
    "http://",
    "https:/x",
    "https:x",
    "https:",
    "https",
    "HTTPS://h/x",
    "Https://h/x",
    "https://h/x y",
    "https://h/x\ty",
    "https://h/x\ny",
    "https://h/x\ry",
    " https://h/x",
    "https://h/x ",
    "https://h/\x7f",
    "https://h/\x00",
    "https://h/é",
    "https://[::1]/x",
    "https://[::1/x",
    "https://]/x",
    "https://[::1]:8080/x",
    "https://[zz]/x",
    "http://h/a;?",
    "http://h/a;#",
    "http://h/a?#",
    "http://h/a;",
    "http://h/a?",
    "http://h/a#",
    "http://h",
    "../a.whl",
    "../../packages/x-1.0-py3-none-any.whl",
    "../../packages/x.whl#sha256=abc",
    "a.whl",
    "./a.whl",
    "../",
    "..",
    ".",
    "./",
    "../..",
    "../../../../../../x",
    "/abs/x",
    "/",
    "//cdn/a.whl",
    "///x",
    "////x",
    "a//b",
    "a///b",
    "a/./b/../c",
    "a/b/",
    "a/..",
    "a/.",
    "a/../..",
    "...",
    "..a",
    "a..",
    ".hidden",
    "x#",
    "x#a#b",
    "x#?q",
    "x?q",
    "x;p",
    "x:y",
    "mailto:x",
    " x",
    "x ",
    "x\ty",
    "x\ny",
    "é",
    "a\\b",
    "%20x",
    "a%2F..%2Fb",
    "?q",
    "?",
    "#f",
    "#",
    "",
)


def _outcome(join, base: str, href: str) -> tuple[str, object]:
    try:
        return ("ok", join(base, href))
    except ValueError as exc:
        return ("error", type(exc))


def _reference_urljoin(base: str, href: str) -> str:
    """Normalize Python 3.14's preservation of an explicitly empty fragment."""
    joined = urllib.parse.urljoin(base, href)
    if base and href.count("#") == 1 and href.endswith("#") and joined.endswith("#"):
        return joined[:-1]
    return joined


@pytest.mark.parametrize("href", HREFS)
@pytest.mark.parametrize("base", BASES)
def test_join_index_url_matches_urljoin(base: str, href: str) -> None:
    assert _outcome(join_index_url, base, href) == _outcome(
        _reference_urljoin,
        base,
        href,
    )


def test_join_index_url_matches_urljoin_on_random_inputs() -> None:
    alphabet = "hs:/?#;.&=%@[] \t\n\x00\x7fAaZz09-_~éHTTPSftp\\"
    prefixes = ("https://", "http://", "HTTPS://", "https:/", "https:", "", "//")
    rng = random.Random(20260820)
    for _ in range(20_000):
        base = rng.choice(BASES)
        href = rng.choice(prefixes) + "".join(
            rng.choice(alphabet) for _ in range(rng.randint(0, 12))
        )
        assert _outcome(join_index_url, base, href) == _outcome(
            _reference_urljoin,
            base,
            href,
        ), (base, href)


def test_join_index_url_matches_urljoin_on_random_relative_references() -> None:
    alphabet = "ab./#?:;% -_~\\é\tA"
    pieces = ("..", ".", "", "a", "b-1.0.whl")
    rng = random.Random(20260820)
    for _ in range(20_000):
        base = rng.choice(BASES)
        segments = [
            rng.choice(pieces)
            if rng.random() < 0.7
            else "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5)))
            for _ in range(rng.randint(0, 6))
        ]
        href = "/".join(segments)
        if rng.random() < 0.2:
            href = "/" + href
        if rng.random() < 0.3:
            href += "#" + "".join(
                rng.choice(alphabet) for _ in range(rng.randint(0, 4))
            )
        assert _outcome(join_index_url, base, href) == _outcome(
            _reference_urljoin,
            base,
            href,
        ), (base, href)


def test_join_index_url_returns_absolute_href_unchanged() -> None:
    href = "https://files.pythonhosted.org/packages/c-1.0-py3-none-any.whl"
    assert join_index_url("https://pypi.org/simple/c/", href) is href


@pytest.mark.parametrize(
    "base, href, expected",
    [
        (
            "https://pypi.org/simple/c/",
            "../../packages/c-1.0.tar.gz",
            "https://pypi.org/packages/c-1.0.tar.gz",
        ),
        (
            "https://pypi.org/simple/c/",
            "../../packages/c-1.0.whl#sha256=ab",
            "https://pypi.org/packages/c-1.0.whl#sha256=ab",
        ),
        ("https://pypi.org/simple/c", "c-1.0.whl", "https://pypi.org/simple/c-1.0.whl"),
        ("https://h", "x.whl", "https://h/x.whl"),
        ("https://h/a/", "../../../x", "https://h/x"),
        ("https://h/a/", "/abs/x", "https://h/abs/x"),
        ("https://h/a/b/", "..", "https://h/a/"),
        ("https://h/a/b/", "x#", "https://h/a/b/x"),
    ],
)
def test_join_index_url_resolves_relative_href(
    base: str,
    href: str,
    expected: str,
) -> None:
    assert join_index_url(base, href) == expected
    assert _reference_urljoin(base, href) == expected


PAGE_URL = "https://example.invalid/simple/pkg/"


def test_links_from_json_reads_every_field() -> None:
    body = """{
      "meta": {"api-version": "1.1"},
      "files": [
        {
          "filename": "pkg-1.0-py3-none-any.whl",
          "url": "https://files.invalid/pkg-1.0-py3-none-any.whl",
          "hashes": {"sha256": "ab"},
          "requires-python": ">=3.9",
          "yanked": "broken",
          "core-metadata": {"sha256": "cd"},
          "upload-time": "2024-01-02T03:04:05.000000Z"
        },
        {
          "filename": "pkg-1.1.tar.gz",
          "url": "../../packages/pkg-1.1.tar.gz",
          "hashes": "not-a-dict",
          "requires-python": 3,
          "yanked": true,
          "dist-info-metadata": true
        },
        {"filename": "pkg-1.2.tar.gz", "url": "pkg-1.2.tar.gz", "yanked": false},
        {"filename": "no-url.tar.gz"},
        {"filename": "bad-url.tar.gz", "url": 7},
        "not-a-file-entry"
      ]
    }"""
    links = IndexPageParser().links_from_json(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://files.invalid/pkg-1.0-py3-none-any.whl",
        "https://example.invalid/packages/pkg-1.1.tar.gz",
        "https://example.invalid/simple/pkg/pkg-1.2.tar.gz",
    ]
    first, second, third = links
    assert first.comes_from == PAGE_URL
    assert first.text == "pkg-1.0-py3-none-any.whl"
    assert first.hashes_internal == {"sha256": "ab"}
    assert first.requires_python == ">=3.9"
    assert first.yanked_reason == "broken"
    assert first.metadata_file_data is not None
    assert first.metadata_file_data.hashes == {"sha256": "cd"}
    assert first.upload_time is not None
    assert first.upload_time.year == 2024
    assert second.hashes_internal == {}
    assert second.requires_python is None
    assert second.yanked_reason == ""
    assert second.metadata_file_data is not None
    assert second.metadata_file_data.hashes is None
    assert second.upload_time is None
    assert third.yanked_reason is None
    assert third.metadata_file_data is None


def test_links_from_html_resolves_hrefs() -> None:
    body = (
        "<html><body>"
        '<a href="https://files.invalid/pkg-1.0-py3-none-any.whl#sha256=ab">'
        "pkg-1.0-py3-none-any.whl</a>"
        '<a href="../../packages/pkg-1.1.tar.gz" data-requires-python="&gt;=3.9">'
        "pkg-1.1.tar.gz</a>"
        "</body></html>"
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://files.invalid/pkg-1.0-py3-none-any.whl#sha256=ab",
        "https://example.invalid/packages/pkg-1.1.tar.gz",
    ]
    assert links[0].hashes_internal == {"sha256": "ab"}
    assert links[1].requires_python == ">=3.9"


def test_links_from_html_honours_base_href() -> None:
    """A repository may serve its index and its artifacts from different hosts.

    Resolving anchors against the page URL instead of the declared base sent
    every download to a path on the index host that does not exist.
    """
    body = (
        "<html><head>"
        '<base href="https://cdn.invalid/files/">'
        "</head><body>"
        '<a href="pkg-1.0-py3-none-any.whl">pkg-1.0-py3-none-any.whl</a>'
        '<a href="sub/pkg-1.1.tar.gz">pkg-1.1.tar.gz</a>'
        "</body></html>"
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://cdn.invalid/files/pkg-1.0-py3-none-any.whl",
        "https://cdn.invalid/files/sub/pkg-1.1.tar.gz",
    ]


def test_base_href_may_itself_be_relative() -> None:
    body = (
        '<html><head><base href="../packages/"></head>'
        '<body><a href="pkg-1.0.tar.gz">pkg</a></body></html>'
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://example.invalid/simple/packages/pkg-1.0.tar.gz",
    ]


def test_only_the_first_base_href_counts() -> None:
    body = (
        "<html><head>"
        '<base href="https://first.invalid/">'
        '<base href="https://second.invalid/">'
        "</head><body>"
        '<a href="pkg-1.0.tar.gz">pkg</a>'
        "</body></html>"
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == ["https://first.invalid/pkg-1.0.tar.gz"]


def test_base_without_href_is_ignored() -> None:
    body = '<html><head><base></head><body><a href="pkg-1.0.tar.gz">p</a></body></html>'
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://example.invalid/simple/pkg/pkg-1.0.tar.gz",
    ]


def test_base_href_is_used_as_given() -> None:
    """A base without a trailing slash names a file, not a directory.

    RFC 3986 resolves "x.whl" against "https://cdn/files" to "https://cdn/x.whl";
    appending a slash first would invent a directory the page never named.
    """
    body = (
        '<html><head><base href="https://cdn.invalid/files"></head>'
        '<body><a href="pkg-1.0.tar.gz">pkg</a></body></html>'
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == ["https://cdn.invalid/pkg-1.0.tar.gz"]


def test_empty_base_href_selects_the_page_and_consumes_the_slot() -> None:
    """`<base href="">` is a base: it resolves to the page, and the next one
    is ignored, as the reference parser does."""
    body = (
        '<html><head><base href=""><base href="https://second.invalid/"></head>'
        '<body><a href="pkg-1.0.tar.gz">pkg</a></body></html>'
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == [
        "https://example.invalid/simple/pkg/pkg-1.0.tar.gz",
    ]


def test_base_without_an_href_attribute_does_not_consume_the_slot() -> None:
    body = (
        '<html><head><base><base href="https://second.invalid/"></head>'
        '<body><a href="pkg-1.0.tar.gz">pkg</a></body></html>'
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.url for link in links] == ["https://second.invalid/pkg-1.0.tar.gz"]


def test_find_links_html_file_resolves_against_the_file(tmp_path: Path) -> None:
    """``--find-links /dir/links.html``: "x.whl" is /dir/x.whl, not a file
    inside a directory named links.html."""
    page = tmp_path / "links.html"
    page.write_text('<a href="pkg-1.0-py3-none-any.whl">pkg</a>', encoding="utf-8")

    links = IndexPageParser().links_from_url(page.as_uri())

    assert [link.url for link in links] == [
        (tmp_path / "pkg-1.0-py3-none-any.whl").as_uri(),
    ]


def test_local_directory_index_resolves_against_the_directory(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(
        '<a href="pkg-1.0-py3-none-any.whl">pkg</a>', encoding="utf-8"
    )

    links = IndexPageParser().links_from_url(tmp_path.as_uri())

    assert [link.url for link in links] == [
        (tmp_path / "pkg-1.0-py3-none-any.whl").as_uri(),
    ]


def test_links_resolve_against_the_url_after_redirects() -> None:
    """A redirect to ``/simple/pkg/`` moves the base with it, as pip's
    ``urljoin(response.url, href)`` does; the requested URL is not it."""

    class Response:
        status = 200
        url = "https://mirror.invalid/simple/pkg/"
        headers = {"Content-Type": "text/html"}
        data = b'<a href="../../files/pkg-1.0.tar.gz">pkg</a>'

    class Session:
        def get(self, url: str, **kwargs: object) -> Response:
            return Response()

    parser = IndexPageParser(session=Session())  # type: ignore[arg-type]
    links = parser.links_from_url("https://example.invalid/simple/pkg")

    assert [link.url for link in links] == [
        "https://mirror.invalid/files/pkg-1.0.tar.gz",
    ]


def test_bare_data_yanked_yanks() -> None:
    """PEP 592: the attribute may have no value; its presence yanks."""
    body = (
        '<a href="pkg-1.0.tar.gz" data-yanked>pkg</a>'
        '<a href="pkg-1.1.tar.gz" data-yanked="broken">pkg</a>'
        '<a href="pkg-1.2.tar.gz">pkg</a>'
    )
    links = IndexPageParser().links_from_html(body, PAGE_URL)
    assert [link.yanked_reason for link in links] == ["", "broken", None]


@pytest.mark.parametrize("version", ["1.0", "1.4", " 1.99 "])
def test_simple_api_version_1_is_read(version: str) -> None:
    html = (
        f'<meta name="pypi:repository-version" content="{version}">'
        '<a href="pkg-1.0.tar.gz">pkg</a>'
    )
    json_page = (
        f'{{"meta": {{"api-version": "{version}"}}, "files": '
        '[{"filename": "pkg-1.0.tar.gz", "url": "pkg-1.0.tar.gz", "hashes": {}}]}'
    )
    parser = IndexPageParser()

    assert len(parser.links_from_html(html, PAGE_URL)) == 1
    assert len(parser.links_from_json(json_page, PAGE_URL)) == 1


def test_simple_api_major_version_2_is_refused() -> None:
    """The spec: a client MUST fail on a major version it does not support."""
    from kpip.core.errors import InstallationError

    parser = IndexPageParser()
    with pytest.raises(InstallationError, match="API version 2.0"):
        parser.links_from_html(
            '<meta name="pypi:repository-version" content="2.0">', PAGE_URL
        )
    with pytest.raises(InstallationError, match="API version 2.1"):
        parser.links_from_json(
            '{"meta": {"api-version": "2.1"}, "files": []}', PAGE_URL
        )


def test_relative_base_href_resolves_against_the_url_after_redirects() -> None:
    body = (
        '<html><head><base href="../packages/"></head>'
        '<body><a href="pkg-1.0.tar.gz">pkg</a></body></html>'
    )
    links = IndexPageParser().links_from_html(
        body,
        "https://example.invalid/simple/pkg",
        "https://mirror.invalid/simple/pkg/",
    )
    assert [link.url for link in links] == [
        "https://mirror.invalid/simple/packages/pkg-1.0.tar.gz",
    ]
