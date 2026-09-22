"""Parsing helpers for Simple API HTML and JSON pages."""

from __future__ import annotations

import json
import os
import posixpath
import urllib.parse
from collections.abc import Callable

from kpip.core.errors import InstallationError
from kpip.core.http import raise_for_status, response_text
from kpip.index.artifacts import ArtifactLocator
from kpip.index.catalog_cache import (
    artifact_identity,
    compile_groups,
    identity_for,
    link_record,
    load_links,
    parsed_wheel_for,
    parsed_wheel_from_link,
    record_fields,
    save_catalog,
    save_links,
)
from kpip.index.datetime import parse_iso_datetime
from kpip.index.hashes import SUPPORTED_RECORD_HASHES
from kpip.index.links import Link, split_plain_url
from kpip.core.urls import split_auth_from_netloc
from kpip.index.paths import PathComponent
from kpip.index.source_models import MetadataFile

LinkFactory = Callable[..., Link]

_FROM_URL_FUNCTION = Link.from_url.__func__

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from kpip.core.http import HttpSession


class IndexContent:
    __slots__ = ("body", "content_type", "from_cache")

    def __init__(self, body: str, content_type: str, from_cache: bool = False) -> None:
        self.body = body
        self.content_type = content_type
        self.from_cache = from_cache


class IndexPageParser:
    """Read and parse one Simple API page into canonical links."""

    def __init__(
        self,
        link_factory: LinkFactory = Link.from_url,
        trusted_hosts: tuple[str, ...] = (),
        session: HttpSession | None = None,
    ) -> None:
        self.link_factory = link_factory
        self.trusted_hosts = {host.lower() for host in trusted_hosts}
        self.session = session
        self.artifacts = ArtifactLocator(session)

    def links_from_url(self, url: str) -> list[Link]:
        try:
            content = self.read(url)
        except OSError:
            return []
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status", None) == 404:
                return []
            raise
        return self.links_from_content(content, url)

    def links_from_content(self, content: IndexContent, url: str) -> list[Link]:
        if content.from_cache:
            cached = load_links(getattr(self.session, "cache", None), url)
            if cached is not None:
                return cached
        if content.content_type.endswith("+json") or "json" in content.content_type:
            links = self.links_from_json(content.body, url)
        else:
            links = self.links_from_html(content.body, url)
        if self.session is not None:
            save_links(getattr(self.session, "cache", None), url, links)
        return links

    def read(self, url: str) -> IndexContent:
        local = self.artifacts.local_path(url)
        if local is not None:
            local_text = os.fspath(local)
            if os.path.isdir(local_text):
                json_path = os.path.join(local_text, "index.json")
                try:
                    with open(json_path, encoding="utf-8") as file:
                        return IndexContent(
                            file.read(),
                            "application/vnd.pypi.simple.v1+json",
                        )
                except FileNotFoundError:
                    pass
                local_text = os.path.join(local_text, "index.html")
            with open(local_text, encoding="utf-8") as file:
                return IndexContent(file.read(), "text/html")

        headers = {
            "Accept": (
                "application/vnd.pypi.simple.v1+json, "
                "text/html;q=0.2, application/vnd.pypi.simple.v1+html;q=0.2"
            ),
        }
        if self.session is None:
            raise InstallationError(
                f"A configured HTTP session is required to read index page {url}",
            )
        response = self.session.get(url, headers=headers)
        raise_for_status(response)
        return IndexContent(
            response_text(response),
            response.headers.get("Content-Type", "text/html").split(";", 1)[0],
            getattr(response, "from_cache", False),
        )

    def links_from_html(self, body: str, url: str) -> list[Link]:
        link_factory = self.link_factory
        if getattr(link_factory, "__func__", None) is _FROM_URL_FUNCTION:
            link_factory = Link.from_index_page
        parser = link_parser_class()(url, link_factory)
        parser.feed(body)
        return parser.links

    def summary_from_content(self, content: IndexContent, url: str) -> Any:
        """Compile a freshly fetched JSON page into its persisted summary.

        The cold path used to parse a page into links, hand them to the
        catalog, and report nothing, so the provider fell back to reasoning
        over those links.  The catalog and its summary are built here
        instead and handed straight back, which is the same view a warm run
        loads from disk.  ``None`` means this page is not the JSON shape, so
        the caller keeps the link route.
        """
        content_type = content.content_type
        if not (content_type.endswith("+json") or "json" in content_type):
            return None
        cache = getattr(self.session, "cache", None)
        if cache is None:
            return None
        groups, unparsed = self.catalog_from_json(content.body, url)
        return save_catalog(cache, url, (groups, unparsed))

    def catalog_from_json(
        self,
        body: str,
        url: str,
    ) -> tuple[list[Any], list[Any]]:
        """Compile a Simple API JSON page straight into catalog records.

        The cold path used to build a ``Link`` for every file a page lists --
        313,925 of them on an airflow lock -- only to read their fields back
        out into the record tuples the catalog stores, and then discard them.
        Every record field comes from the JSON entry, so the links were
        scaffolding.

        A file whose URL is not the plain ``https://host/path`` shape still
        goes through ``Link``, which owns the fragment, escaping and fallback
        rules, so anything unusual stays identical by construction rather
        than by re-derivation here.
        """
        data = json.loads(body)
        base_url = ensure_trailing_slash(url)
        grouped: dict[tuple[str, str], list[Any]] = {}
        unparsed: list[Any] = []
        for file_data in data.get("files", []):
            if not isinstance(file_data, dict):
                continue
            file_url = file_data.get("url")
            if not isinstance(file_url, str):
                continue
            record, identity = self.record_from_json(base_url, url, file_data, file_url)
            if identity is None:
                unparsed.append(record)
                continue
            kind, name, version = identity
            grouped.setdefault((name, version), []).append((kind, record))
        return compile_groups(grouped), unparsed

    def record_from_json(
        self,
        base_url: str,
        source_url: str,
        file_data: Any,
        file_url: str,
    ) -> tuple[Any, tuple[int, str, str] | None]:
        """One catalog record and its release identity, from one JSON entry."""
        url = join_index_url(base_url, file_url)
        filename = file_data.get("filename")
        hashes = file_data.get("hashes")
        yanked = file_data.get("yanked")
        requires_python = file_data.get("requires-python")
        upload_time = file_data.get("upload-time")
        size = file_data.get("size")
        text = str(filename or "")
        yanked_reason = (
            None
            if yanked is False or yanked is None
            else ""
            if yanked is True
            else str(yanked)
        )
        metadata_file = metadata_file_from_json(file_data)
        parsed = split_plain_url(url)

        if parsed is None or "&" in url:
            link = Link.from_index_page(
                url,
                source_url=source_url,
                text=text,
                hashes=hashes if isinstance(hashes, dict) else None,
                requires_python=(
                    requires_python if isinstance(requires_python, str) else None
                ),
                yanked_reason=yanked_reason,
                metadata_file=metadata_file,
                upload_time=parse_iso_datetime(upload_time) if upload_time else None,
            )
            if type(size) is int and size >= 0:
                link.size = size
            parsed_wheel = parsed_wheel_from_link(link)
            return (
                link_record(link, parsed_wheel=parsed_wheel),
                artifact_identity(link, parsed_wheel=parsed_wheel),
            )

        path = urllib.parse.unquote(parsed.path)
        stripped = path.rstrip("/")
        kind = Link.artifact_kind_from_filename(posixpath.basename(stripped))
        name = PathComponent.from_name(stripped[stripped.rfind("/") + 1 :])
        if not name:
            name = PathComponent.from_name(split_auth_from_netloc(parsed.netloc)[0])
        parsed_wheel = parsed_wheel_for(kind, str(name))
        return (
            record_fields(
                url=url,
                text=text,
                hashes=(
                    {str(key): str(value) for key, value in hashes.items()}
                    if isinstance(hashes, dict)
                    else {}
                ),
                requires_python=(
                    requires_python if isinstance(requires_python, str) else None
                ),
                yanked_reason=yanked_reason,
                metadata_file=metadata_file,
                upload_time=parse_iso_datetime(upload_time) if upload_time else None,
                parsed_wheel=parsed_wheel,
                parts=tuple(parsed),
                size=size if type(size) is int and size >= 0 else None,
            ),
            identity_for(kind, str(name), parsed_wheel=parsed_wheel),
        )

    def links_from_json(self, body: str, url: str) -> list[Link]:
        data = json.loads(body)
        links: list[Link] = []
        append = links.append
        link_factory = self.link_factory
        if getattr(link_factory, "__func__", None) is _FROM_URL_FUNCTION:
            link_factory = Link.from_index_page
        base_url = ensure_trailing_slash(url)
        for file_data in data.get("files", []):
            if not isinstance(file_data, dict):
                continue
            file_url = file_data.get("url")
            if not isinstance(file_url, str):
                continue
            filename = file_data.get("filename")
            hashes = file_data.get("hashes")
            yanked = file_data.get("yanked")
            requires_python = file_data.get("requires-python")
            upload_time = file_data.get("upload-time")
            size = file_data.get("size")
            link = link_factory(
                join_index_url(base_url, file_url),
                source_url=url,
                text=str(filename or ""),
                hashes=hashes if isinstance(hashes, dict) else None,
                requires_python=requires_python
                if isinstance(requires_python, str)
                else None,
                yanked_reason=(
                    None
                    if yanked is False or yanked is None
                    else ""
                    if yanked is True
                    else str(yanked)
                ),
                metadata_file=metadata_file_from_json(file_data),
                upload_time=(parse_iso_datetime(upload_time) if upload_time else None),
            )
            # PEP 700: assigned after construction so custom link factories
            # keep their signature. bool is an int, hence the exact type check.
            if type(size) is int and size >= 0:
                link.size = size
            append(link)
        return links


_LINK_PARSER: type | None = None


def link_parser_class() -> type:
    """The HTML link parser, built on first use.

    html.parser is imported only here: a JSON Simple API response or a
    find-links directory never needs it, and importing it costs more than
    parsing a small page.
    """
    global _LINK_PARSER
    if _LINK_PARSER is not None:
        return _LINK_PARSER

    from html.parser import HTMLParser

    class LinkParser(HTMLParser):
        def __init__(self, page_url: str, link_factory: LinkFactory) -> None:
            super().__init__(convert_charrefs=True)
            self.page_url = page_url
            self.base_url_internal = ensure_trailing_slash(page_url)
            self.saw_base_internal = False
            self.link_factory = link_factory
            self.links: list[Link] = []
            self.current_internal: dict[str, str | None] | None = None
            self.text_internal: list[str] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag == "base":
                # The first <base> that carries an href wins, even an empty
                # one -- which selects the page URL and still rules out a
                # later <base>, as the reference parser does. The href is
                # used as given: appending a slash would turn
                # <base href="https://cdn/files"> into a directory it does
                # not name.
                if not self.saw_base_internal:
                    href = dict(attrs).get("href")
                    if href is not None:
                        self.saw_base_internal = True
                        if href:
                            self.base_url_internal = join_index_url(
                                self.page_url,
                                href,
                            )
                return
            if tag != "a":
                return
            self.current_internal = dict(attrs)
            self.text_internal = []

        def handle_data(self, data: str) -> None:
            if self.current_internal is not None:
                self.text_internal.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag != "a" or self.current_internal is None:
                return
            href = self.current_internal.get("href")
            if href:
                self.links.append(
                    self.link_factory(
                        join_index_url(self.base_url_internal, href),
                        source_url=self.page_url,
                        text="".join(self.text_internal).strip(),
                        requires_python=self.current_internal.get(
                            "data-requires-python"
                        ),
                        yanked_reason=self.current_internal.get("data-yanked"),
                        metadata_file=metadata_file_from_attrs(self.current_internal),
                    ),
                )
            self.current_internal = None
            self.text_internal = []

    _LINK_PARSER = LinkParser
    return LinkParser


def __getattr__(name: str) -> object:
    if name == "LinkParser":
        return link_parser_class()
    raise AttributeError(name)


def metadata_file_from_attrs(attrs: dict[str, str | None]) -> MetadataFile | None:
    if "data-core-metadata" in attrs:
        return metadata_file_from_value(attrs.get("data-core-metadata"))
    if "data-dist-info-metadata" in attrs:
        return metadata_file_from_value(attrs.get("data-dist-info-metadata"))
    return None


def metadata_file_from_json(file_data: dict[str, object]) -> MetadataFile | None:
    if "core-metadata" in file_data:
        return metadata_file_from_json_value(file_data["core-metadata"])
    if "dist-info-metadata" in file_data:
        return metadata_file_from_json_value(file_data["dist-info-metadata"])
    return None


def metadata_file_from_json_value(value: object) -> MetadataFile | None:
    if isinstance(value, dict):
        return MetadataFile({str(name): str(hash_) for name, hash_ in value.items()})
    if value is True:
        return MetadataFile(None)
    return None


def metadata_file_from_value(value: str | None) -> MetadataFile | None:
    if value is None:
        return None
    if value in {"", "true"}:
        return MetadataFile(None)
    name, sep, digest = value.partition("=")
    return MetadataFile(
        {name: digest} if sep and name in SUPPORTED_RECORD_HASHES else None,
    )


_ABSOLUTE_HTTP_PREFIXES = ("https://", "http://")


def join_index_url(base_url: str, href: str) -> str:
    """``urllib.parse.urljoin(base_url, href)`` for one link on an index page.

    Nearly every file URL a real index serves is already absolute, and for
    those ``urljoin`` still parses both URLs and rebuilds the result from the
    parts -- two ``urlparse`` calls and an ``urlunparse`` per link, more than
    a third of the cost of parsing a PyPI JSON page. For an absolute
    ``http(s)`` URL that round-trip is the identity, except in the few shapes
    where ``urlsplit`` would normalize or reject: an upper-case scheme,
    whitespace or control characters (stripped), an empty netloc (resolved
    against the base), a delimiter introducing an empty component -- a
    trailing ``?``, ``#`` or ``;``, or ``?#``, ``;?``, ``;#`` (``urlunparse``
    drops the empty part) -- or a bracketed IPv6 host (validated, and raised
    on when malformed). Those, and every relative reference, still take the
    real ``urljoin``; this only returns early when the answer is known to be
    ``href`` itself.
    """
    if (
        href.startswith(_ABSOLUTE_HTTP_PREFIXES)
        and href.isascii()
        and href.isprintable()
        and href[-1] not in ";?#"
        and "?#" not in href
        and ";?" not in href
        and ";#" not in href
        and "[" not in href
        and "]" not in href
    ):
        start = 8 if href[4] == "s" else 7
        if href[start : start + 1] not in "/?#":
            return href
    elif (
        href
        and href[0] != " "
        and href[:2] != "//"
        and ":" not in href
        and "?" not in href
        and ";" not in href
        and href.isascii()
        and href.isprintable()
    ):
        joined = _join_relative_reference(base_url, href)
        if joined is not None:
            return joined
    return _urljoin_compat(base_url, href)


def _urljoin_compat(base_url: str, href: str) -> str:
    """Keep the pre-3.14 treatment of an explicitly empty fragment."""
    joined = urllib.parse.urljoin(base_url, href)
    if (
        base_url
        and href.count("#") == 1
        and href.endswith("#")
        and joined.endswith("#")
    ):
        return joined[:-1]
    return joined


def _join_relative_reference(base_url: str, href: str) -> str | None:
    """``urljoin`` for a plain relative reference against a clean http(s) base.

    Mirrors of the same index page serve hrefs such as
    ``../../packages/x.whl#sha256=...``; ``urljoin`` resolves those by
    parsing both URLs into six parts, merging the paths segment by segment
    and rebuilding. This is that merge -- the same segment walk the stdlib
    performs -- on the shapes where nothing else in ``urljoin`` can apply:
    the href carries no scheme, netloc, query or params (the caller has
    checked for ``:``, ``//``, ``?`` and ``;``), and the base is an absolute
    ``http(s)`` URL with a non-empty netloc and no query, fragment or params
    of its own, so its path is exactly the text after the netloc. Returns
    ``None`` for anything else so the caller falls through to ``urljoin``.
    """
    if not (
        base_url.startswith(_ABSOLUTE_HTTP_PREFIXES)
        and base_url.isascii()
        and base_url.isprintable()
        and "?" not in base_url
        and "#" not in base_url
        and ";" not in base_url
        and "[" not in base_url
        and "]" not in base_url
    ):
        return None
    start = 8 if base_url[4] == "s" else 7
    if base_url[start : start + 1] in "/":
        return None
    path, _, fragment = href.partition("#")
    if not path:
        return None
    slash = base_url.find("/", start)
    if slash < 0:
        origin = base_url
        base_path = ""
    else:
        origin = base_url[:slash]
        base_path = base_url[slash:]
    if path[:1] == "/":
        segments = path.split("/")
    else:
        base_parts = base_path.split("/")
        if base_parts[-1] != "":
            del base_parts[-1]
        segments = base_parts + path.split("/")
        segments[1:-1] = filter(None, segments[1:-1])
    resolved: list[str] = []
    for segment in segments:
        if segment == "..":
            if resolved:
                resolved.pop()
        elif segment != ".":
            resolved.append(segment)
    if segments[-1] in (".", ".."):
        resolved.append("")
    joined = "/".join(resolved) or "/"
    if joined[0] != "/":
        joined = "/" + joined
    if fragment:
        return f"{origin}{joined}#{fragment}"
    return origin + joined


def ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"
