"""A session that costs nothing until something asks it to speak."""

from __future__ import annotations

import threading

from kpip.core.appdirs import http_cache_path
from kpip.network.freshness import cached_response_is_fresh

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


class DeferredNetworkSession:
    """Delay transport policy and cache setup until a session attribute is used.

    Building a ``NetworkSession`` means importing ``kpip.network.session``, and
    with it the vendored HTTP stack, ``ssl``, ``http.client`` and
    ``logging`` -- the largest single import on a command that resolves.
    A resolve whose answers are all in the cache never opens a socket, so
    on that path every millisecond of it buys nothing.

    Every argument but the cache directory has the value that
    ``NetworkSession`` and ``MultiDomainBasicAuth`` would have chosen for
    themselves, so a caller with no authentication policy of its own --
    ``kpip lock`` -- names one argument and gets the session it used to
    build by hand.
    """

    __slots__ = (
        "cache_dir",
        "cert",
        "client_cert",
        "index_urls",
        "keyring_provider",
        "lock",
        "no_input",
        "page_cache_internal",
        "page_expiry_internal",
        "proxy",
        "session",
    )

    def __init__(
        self,
        *,
        cache_dir: str | None,
        index_urls: list[str] | None = None,
        cert: str | None = None,
        client_cert: str | None = None,
        no_input: bool = False,
        keyring_provider: str = "auto",
        proxy: str | None = None,
    ) -> None:
        self.index_urls = index_urls

        self.cache_dir = cache_dir

        self.cert = cert

        self.client_cert = client_cert

        self.no_input = no_input

        self.keyring_provider = keyring_provider

        self.proxy = proxy

        self.session: Any = None

        self.page_cache_internal: Any = None

        self.page_expiry_internal: dict[str, float | None] = {}

        self.lock = threading.Lock()

    def materialize(self) -> Any:
        if self.session is not None:
            return self.session

        with self.lock:
            if self.session is not None:
                return self.session

            from kpip.network.session import DEFAULT_RETRIES, NetworkSession

            session = NetworkSession(
                index_urls=self.index_urls,
                cache=self.page_cache(),
                retries=DEFAULT_RETRIES,
            )

            assert session.auth is not None

            session.auth.prompting = not self.no_input

            session.auth.keyring_provider = self.keyring_provider

            if self.cert:
                session.verify = self.cert

            if self.client_cert:
                session.cert = self.client_cert

            if self.proxy is not None:
                session.proxies = (
                    {"http": self.proxy, "https": self.proxy} if self.proxy else {}
                )

            self.session = session

            return session

    def __getattr__(self, name: str) -> Any:
        return getattr(self.materialize(), name)

    # Spelled out rather than left to ``__getattr__`` so this reads as the
    # session contract it stands in for: these four are what ``HttpSession``
    # and the requirement-file parser ask any session for.
    @property
    def auth(self) -> Any:
        return self.materialize().auth

    @auth.setter
    def auth(self, value: Any) -> None:
        self.materialize().auth = value

    @property
    def cache(self) -> Any:
        # Answered without materializing: this is the same object the
        # session is handed when it is finally built, and reading a page
        # out of it is how a resolve avoids needing the session at all.
        return self.page_cache()

    @cache.setter
    def cache(self, value: Any) -> None:
        self.page_cache_internal = value

        if self.session is not None:
            self.session.cache = value

    @property
    def trusted_hosts(self) -> Any:
        return self.materialize().trusted_hosts

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self.materialize().get(*args, **kwargs)

    def head(self, *args: Any, **kwargs: Any) -> Any:
        return self.materialize().head(*args, **kwargs)

    def page_cache(self) -> Any:
        """The HTTP page cache, built without the client that fills it."""

        if self.page_cache_internal is None and self.cache_dir:
            from kpip.network.cache import SafeFileCache

            self.page_cache_internal = SafeFileCache(http_cache_path(self.cache_dir))

        return self.page_cache_internal

    def has_fresh_cached_response(self, url: str) -> bool:
        """Answer from the cache directory, without building a client.

        This is the question a resolve asks first and, when every page is
        still fresh, the only one it asks at all. Forwarding it would build
        a session to be told that nothing needs sending.
        """

        return cached_response_is_fresh(
            self.page_cache(),
            self.page_expiry_internal,
            url,
        )
