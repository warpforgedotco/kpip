"""The fast URL split for index-page artifact links never disagrees with ``urlsplit``.

A page lists thousands of ``https://host/path`` URLs and ``urlsplit`` was the
single largest cost of building a link. One regex match admits exactly the
plain shape; everything else is left to ``urlsplit``.
"""

from __future__ import annotations

import urllib.parse

import pytest
from kpip.index.links import Link, _split_plain_url

PLAIN = "https://files.pythonhosted.org/packages/ab/cd/0123abcd/grpcio-1.60.0-cp310-cp310-manylinux_2_17_x86_64.whl"


@pytest.mark.parametrize(
    "url",
    [
        PLAIN,
        "http://index.invalid/simple/demo/demo-1.0.tar.gz",
        "https://host/a%20b/x.whl",
        "https://host",
        "https://host/",
        "https://host/x.whl#sha256=abc",
        "https://host/x.whl?x=1",
        "https://[::1]/x.whl",
        "https://host/x y.whl",
        "HTTPS://host/x.whl",
        "https://user:pw@host/x.whl",
        "file:///tmp/x.whl",
        "https://host/x\\y.whl",
    ],
)
def test_it_agrees_with_urlsplit_or_declines(url: str) -> None:
    fast = _split_plain_url(url)

    assert fast is None or fast == urllib.parse.urlsplit(url)


def test_the_common_shape_is_handled_and_a_fragment_is_not() -> None:
    assert _split_plain_url(PLAIN) == urllib.parse.urlsplit(PLAIN)
    assert _split_plain_url("https://host/x.whl#sha256=abc") is None


def test_an_index_page_link_is_the_same_either_way() -> None:
    link = Link.from_index_page(
        PLAIN, source_url="https://index.invalid/simple/grpcio/"
    )

    assert link.parsed_url_internal == urllib.parse.urlsplit(PLAIN)
    assert str(link.filename) == PLAIN.rsplit("/", 1)[1]
