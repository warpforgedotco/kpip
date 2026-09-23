"""Local materialization and URL path helpers for distribution artifacts."""

from __future__ import annotations

import atexit
import hashlib
import os
import posixpath
import urllib.parse

from kpip.core.logger import get_logger
from kpip.core.errors import InstallationError
from kpip.core.urls import url_to_path
from kpip.index.artifact_cache import ArtifactCache, materialize_cached_artifact
from kpip.index.vcs_urls import vcs_scheme

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from kpip.core.http_contracts import HttpSession

logger = get_logger(__name__)


DOWNLOAD_DIR: str | None = None


def download_dir_internal() -> str:
    global DOWNLOAD_DIR

    if DOWNLOAD_DIR is None:
        import shutil
        import tempfile

        DOWNLOAD_DIR = tempfile.mkdtemp(prefix="kpip-index-downloads-")

        atexit.register(shutil.rmtree, DOWNLOAD_DIR, ignore_errors=True)

    return DOWNLOAD_DIR


class ArtifactLocator:
    """Locate local artifacts and materialize remote distribution URLs."""

    __slots__ = (
        "artifact_cache",
        "download_cache",
        "download_hashes",
        "local_path_cache",
        "session",
    )

    def __init__(
        self,
        session: HttpSession | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.session = session

        self.download_cache: dict[str, str] = {}

        self.download_hashes: dict[str, dict[str, str]] = {}

        self.local_path_cache: dict[str, str | None] = {}

        if cache_dir is None:
            self.artifact_cache = None

        else:
            self.artifact_cache = ArtifactCache(cache_dir)

    def ensure_local(
        self,
        url_or_path: str,
        *,
        is_vcs: bool | None = None,
        local_path: str | None = None,
    ) -> str:
        return self.ensure_local_text(
            url_or_path,
            is_vcs=is_vcs,
            local_path=local_path,
        )

    def ensure_local_text(
        self,
        url_or_path: str,
        *,
        is_vcs: bool | None = None,
        local_path: str | None = None,
        hashes: dict[str, str] | None = None,
    ) -> str:
        if is_vcs is None:
            is_vcs = vcs_scheme(url_or_path) is not None

        if is_vcs:
            # Only a URL that turned out to name a repository pays for the
            # machinery that clones one.
            from kpip.index.vcs import materialize_vcs

            prompting = True

            if self.session is not None:
                prompting = getattr(self.session.auth, "prompting", True)

            return os.fspath(materialize_vcs(url_or_path, prompting=prompting))

        local = (
            os.fspath(local_path)
            if local_path is not None
            else self.local_path(url_or_path)
        )

        if local is not None:
            return local

        filename = self.filename(url_or_path)

        target = os.path.join(
            download_dir_internal(),
            hashlib.sha256(url_or_path.encode()).hexdigest(),
            filename,
        )

        cached = self.download_cache.get(url_or_path)

        if cached is not None:
            return cached

        parsed = urllib.parse.urlparse(url_or_path)

        url = parsed._replace(fragment="").geturl()

        artifact_cache = self.artifact_cache

        if artifact_cache is not None:
            cached_artifact = artifact_cache.get(url, hashes)

            if cached_artifact is not None:
                materialize_cached_artifact(cached_artifact.path, target)

                self.download_hashes[url_or_path] = {
                    "sha256": cached_artifact.digest,
                }

                self.download_cache[url_or_path] = target

                return target

        cache = getattr(self.session, "cache", None)

        cache_key = f"artifact:{url}"

        if cache is not None:
            cached_path = getattr(cache, "get_body_path", lambda _key: None)(
                cache_key,
            )

            if cached_path is not None:
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)

                    os.link(cached_path, target)

                except FileExistsError:
                    pass

                except OSError:
                    cached_path = None

                if cached_path is not None:
                    if artifact_cache is not None:
                        try:
                            cached_artifact = artifact_cache.store_path(
                                url,
                                filename,
                                cached_path,
                                hashes,
                            )

                        except OSError:
                            pass

                        else:
                            self.download_hashes[url_or_path] = {
                                "sha256": cached_artifact.digest,
                            }

                    self.download_cache[url_or_path] = target

                    return target

            cached_body = cache.get_body(cache_key)

            if cached_body is not None:
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)

                    import shutil

                    with open(target, "wb") as file:
                        shutil.copyfileobj(cached_body, file)

                finally:
                    cached_body.close()

                if artifact_cache is not None:
                    try:
                        cached_artifact = artifact_cache.store_path(
                            url,
                            filename,
                            target,
                            hashes,
                        )

                    except OSError:
                        pass

                    else:
                        self.download_hashes[url_or_path] = {
                            "sha256": cached_artifact.digest,
                        }

                self.download_cache[url_or_path] = target

                return target

        os.makedirs(os.path.dirname(target), exist_ok=True)

        if self.session is None:
            raise InstallationError(
                f"A configured HTTP session is required to download {url}",
            )
        session = self.session

        def request() -> Any:
            current = session.get(url, stream=True)

            if current.status < 400:
                return current

            logger.critical(
                "HTTP error %s while getting %s",
                current.status,
                url,
            )

            try:
                raise InstallationError(
                    f"{current.status} Client Error: {current.reason} for url: {url}",
                )

            finally:
                current.close()

        response = request()

        try:
            chunks = response.stream(1024 * 1024)

            if artifact_cache is not None:
                try:
                    cached_artifact = artifact_cache.store_chunks(
                        url,
                        filename,
                        chunks,
                        hashes,
                    )

                except OSError:
                    response.close()

                    response = request()

                    with open(target, "wb") as file:
                        file.writelines(
                            response.stream(1024 * 1024),
                        )

                else:
                    materialize_cached_artifact(cached_artifact.path, target)

                    self.download_hashes[url_or_path] = {
                        "sha256": cached_artifact.digest,
                    }

            else:
                with open(target, "wb") as file:
                    file.writelines(chunks)

        finally:
            response.close()

        if cache is not None and artifact_cache is None:
            with open(target, "rb") as file:
                cache.set_body_from_io(cache_key, file)

            cache.set(cache_key, b"{}")

        self.download_cache[url_or_path] = target

        return target

    def hashes_for(self, url_or_path: str) -> dict[str, str] | None:
        hashes = self.download_hashes.get(url_or_path)

        return None if hashes is None else dict(hashes)

    def local_path(self, link: str) -> str | None:
        cached = self.local_path_cache.get(link)

        if cached is not None or link in self.local_path_cache:
            return cached

        parsed = urllib.parse.urlparse(link)

        if parsed.scheme == "file":
            path = url_to_path(link)

        elif parsed.scheme:
            path = None

        else:
            path = link

        self.local_path_cache[link] = path

        return path

    def filename(self, link: str) -> str:
        parsed = urllib.parse.urlparse(link)

        path = urllib.parse.unquote(parsed.path)

        filename = posixpath.basename(path.rstrip("/"))

        if filename not in {"", ".", ".."}:
            return filename

        fallback = parsed.netloc or path

        return posixpath.basename(fallback.replace("\\", "/").rstrip("/")) or fallback
