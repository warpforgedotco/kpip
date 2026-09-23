"""Package source locations and their link collection behavior."""

from __future__ import annotations

import ntpath
import os
import urllib.parse
from functools import lru_cache

from kpip.core.packaging import Requirement, canonicalize_name
from kpip.core.urls import WINDOWS, path_to_url, url_to_path
from kpip.index.catalog_cache import load_summary
from kpip.index.directory_index import (
    LocalSourceSnapshot,
    local_source_snapshot,
)
from kpip.index.links import SUPPORTED_EXTENSIONS, Link
from kpip.index.page_parsing import IndexPageParser
from kpip.index.source_models import ArtifactKind

TYPE_CHECKING = False

if TYPE_CHECKING:
    from kpip.core.http_contracts import HttpSession
    from kpip.index.catalog_cache import CatalogSummary


SUPPORTED_SCHEMES = frozenset(("http", "https", "file", "ftp"))

VCS_SCHEMES = frozenset(("git", "hg", "svn", "bzr"))

HTML_SUFFIXES = frozenset((".html", ".htm"))


def is_supported_location(value: str) -> bool:
    scheme = urllib.parse.urlsplit(value).scheme

    vcs_scheme = scheme.partition("+")[0]

    return scheme in SUPPORTED_SCHEMES or vcs_scheme in VCS_SCHEMES


def is_remote_source_location(value: str) -> bool:
    """Return whether a find-links value can require network prefetching."""

    scheme = urllib.parse.urlsplit(value).scheme

    return bool(scheme and scheme != "file")


def resolve_source_location(location: str) -> tuple[str | None, str | None]:
    """Return the normalized URL and local path represented by a source option."""

    if location.startswith("file:"):
        return location, url_to_path(location)

    if is_supported_location(location):
        return location, None

    if os.path.exists(location):
        absolute_location = os.path.abspath(location)

        return path_to_url(absolute_location), absolute_location

    return None, None


class FindLinksSource:
    __slots__ = (
        "local_file_links",
        "local_snapshots",
        "links",
        "session",
        "trusted_hosts",
    )

    def __init__(
        self,
        links: tuple[str, ...],
        trusted_hosts: tuple[str, ...] = (),
        session: HttpSession | None = None,
    ) -> None:
        self.links = links

        self.trusted_hosts = trusted_hosts

        self.session = session

        self.local_snapshots: dict[str, LocalSourceSnapshot | None] = {}

        self.local_file_links: dict[str, tuple[Link, ...]] = {}

    def collect_links(self, requirement: Requirement) -> list[Link]:
        links: list[Link] = []

        for link in self.links:
            links.extend(self.links_from_find_link(link))

        return links

    def refresh_local_sources(self, path: str | None = None) -> None:
        """Explicitly invalidate local discovery state."""

        if path is None:
            self.local_snapshots.clear()

            self.local_file_links.clear()

        else:
            path_text = os.fspath(path)

            self.local_snapshots.pop(path_text, None)

            self.local_file_links.pop(path_text, None)

    def links_from_find_link(self, link: str) -> list[Link]:
        parsed_link = urllib.parse.urlsplit(link)

        if not parsed_link.scheme and "://" not in link:
            return self.links_from_local_path(link)

        normalized, local = resolve_source_location(link)

        if local is not None:
            return self.links_from_local_path(local)

        if normalized is None:
            return []

        candidate = Link.from_url(normalized, source_url=None)

        if urllib.parse.urlparse(normalized).fragment.startswith("egg="):
            return [candidate]

        if candidate.kind is not ArtifactKind.UNKNOWN:
            return [candidate]

        return IndexPageParser(
            trusted_hosts=self.trusted_hosts,
            session=self.session,
        ).links_from_url(normalized)

    def links_from_local_path(self, path: str | os.PathLike[str]) -> list[Link]:
        path_text = os.fspath(path)

        cached_file_links = self.local_file_links.get(path_text)

        if cached_file_links is not None or path_text in self.local_file_links:
            return list(cached_file_links or ())

        if path_text in self.local_snapshots:
            snapshot = self.local_snapshots[path_text]

        else:
            snapshot = local_source_snapshot(
                path_text,
                suffixes=(
                    ".html",
                    ".htm",
                    ".html.gz",
                    ".htm.gz",
                    *SUPPORTED_EXTENSIONS,
                ),
            )

            self.local_snapshots[path_text] = snapshot

        if snapshot is not None and snapshot.is_directory:
            directory_url: str | None = None
            if not WINDOWS:
                directory_path = os.path.abspath(path_text)
                try:
                    directory_url = path_to_url(path_text)
                except UnicodeEncodeError:
                    directory_url = None
            if directory_url is not None:
                return [
                    Link.from_local_file(
                        os.path.basename(item.path),
                        directory_path=directory_path,
                        directory_url=directory_url,
                        path_text=item.path,
                        source_url=path_text,
                        local_identity=item.identity,
                    )
                    for item in snapshot.entries
                ]
            return [
                Link.from_path(
                    item.path,
                    source_url=path_text,
                    is_dir=False,
                    local_identity=item.identity,
                )
                for item in snapshot.entries
            ]

        if snapshot is not None or os.path.isfile(path_text):
            if os.path.splitext(path_text)[1].lower() in HTML_SUFFIXES:
                links = IndexPageParser(
                    trusted_hosts=self.trusted_hosts,
                    session=self.session,
                ).links_from_url(path_to_url(os.path.abspath(path_text)))

            else:
                links = [
                    Link.from_path(path_text, source_url=None, is_dir=False),
                ]

            self.local_file_links[path_text] = tuple(links)

            return links

        self.local_file_links[path_text] = ()

        return []


class SimpleIndexSource:
    __slots__ = ("index_url", "page_fetch_outcomes", "session", "trusted_hosts")

    def __init__(
        self,
        index_url: str,
        trusted_hosts: tuple[str, ...] = (),
        session: HttpSession | None = None,
    ) -> None:
        self.index_url = index_url

        self.trusted_hosts = trusted_hosts

        self.session = session

        self.page_fetch_outcomes: dict[str, tuple[list[Link]]] = {}

    def collect_links(self, requirement: Requirement) -> list[Link]:
        project_url = self.project_page_url(self.index_url, requirement.canonical_name)

        outcome = self.page_fetch_outcomes.pop(project_url, None)

        if outcome is not None:
            return outcome[0]

        # A page compiled straight into the catalog left its body in the HTTP
        # cache a moment earlier, so a caller that still wants links re-reads
        # it from there.  Holding every body in memory instead cost more in
        # page faults than the parse it saved: a cold airflow lock would
        # retain some 700 of them for the whole run.
        return IndexPageParser(
            trusted_hosts=self.trusted_hosts,
            session=self.session,
        ).links_from_url(project_url)

    def collect_cached_catalog_summary(
        self,
        requirement: Requirement,
        *,
        allow_fetch: bool = False,
    ) -> CatalogSummary | None:
        """Return the compact release view when the page is fresh, or -- with
        ``allow_fetch`` -- after one revalidation proves it unchanged."""

        if self.session is None:
            return None

        project_url = self.project_page_url(self.index_url, requirement.canonical_name)

        cache = getattr(self.session, "cache", None)

        if self.has_fresh_cached_page(requirement):
            return load_summary(cache, project_url)

        if not allow_fetch or not project_url.startswith(("http://", "https://")):
            return None

        parser = IndexPageParser(
            trusted_hosts=self.trusted_hosts,
            session=self.session,
        )

        try:
            content = parser.read(project_url)

        except OSError:
            self.page_fetch_outcomes[project_url] = ([],)

            return None

        except Exception as exc:
            response = getattr(exc, "response", None)

            if getattr(response, "status", None) == 404:
                self.page_fetch_outcomes[project_url] = ([],)

                return None

            raise

        if content.from_cache:
            summary = load_summary(cache, project_url)

            if summary is not None:
                return summary

        # A JSON page compiles straight into the catalog and its summary, so
        # the provider reasons over records rather than over a link per file:
        # an airflow lock listed 313,925 of them only to discard them here.
        summary = parser.summary_from_content(content, project_url)

        if summary is not None:
            return summary

        links = parser.links_from_content(content, project_url)

        self.page_fetch_outcomes[project_url] = (links,)

        return None

    def has_fresh_cached_page(self, requirement: Requirement) -> bool:
        """Return whether catalog discovery can avoid remote I/O."""

        if self.session is None:
            return False

        project_url = self.project_page_url(self.index_url, requirement.canonical_name)

        return bool(
            getattr(self.session, "has_fresh_cached_response", lambda _: False)(
                project_url,
            ),
        )

    @staticmethod
    @lru_cache(maxsize=16384)
    def project_page_url(index_url: str, canonical_name: str) -> str:
        return urllib.parse.urljoin(
            index_url if index_url.endswith("/") else index_url + "/",
            canonicalize_name(canonical_name) + "/",
        )


@lru_cache(maxsize=4096)
def looks_like_path_requirement(value: str) -> bool:
    return (
        value.startswith((".", "/", "~"))
        or os.sep in value
        or (os.altsep is not None and os.altsep in value)
        or bool(ntpath.splitdrive(value)[0])
    )
