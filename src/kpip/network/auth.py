"""Network Authentication Helpers

Contains interface (MultiDomainBasicAuth) and associated glue code for
providing credentials in the context of network requests.
"""

from __future__ import annotations

lazy import getpass
lazy import importlib.util
lazy import json
lazy import logging
lazy import netrc
lazy import os
lazy import shutil
lazy import subprocess
lazy import sys
lazy import sysconfig
lazy import typing
lazy import urllib.parse
lazy from abc import ABC, abstractmethod
lazy from functools import cache
lazy from os.path import commonpath
lazy from typing import NamedTuple

lazy from kpip.core.urls import remove_auth_from_url, split_auth_netloc_from_url
lazy from kpip.core.utils import AuthInfo

logger = logging.getLogger(__name__)

KEYRING_DISABLED = False


@cache
def load_netrc(netrc_path: str | None) -> netrc.netrc | None:
    try:
        return netrc.netrc(netrc_path)
    except FileNotFoundError, OSError, netrc.NetrcParseError:
        return None


def get_netrc_auth(url: str) -> tuple[str, str] | None:
    """Return credentials from the user's netrc file for ``url``."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        return None
    netrc_file = load_netrc(os.environ.get("NETRC"))
    if netrc_file is None:
        return None
    auth = netrc_file.authenticators(parsed.hostname)
    if auth is None:
        return None
    login, _, password = auth
    if login is None or password is None:
        return None
    return login, password


def ask(message: str, options) -> str:
    while True:
        response = input(message)
        if response in options:
            return response


def ask_input(message: str) -> str:
    return input(message)


def ask_password(message: str) -> str:
    return getpass.getpass(message)


class Credentials(NamedTuple):
    url: str
    username: str
    password: str


class KeyRingBaseProvider(ABC):
    """Keyring base provider interface"""

    has_keyring: bool

    @abstractmethod
    def get_auth_info(self, url: str, username: str | None) -> AuthInfo | None: ...

    @abstractmethod
    def save_auth_info(self, url: str, username: str, password: str) -> None: ...


class KeyRingNullProvider(KeyRingBaseProvider):
    """Keyring null provider"""

    has_keyring = False

    def get_auth_info(self, url: str, username: str | None) -> AuthInfo | None:
        return None

    def save_auth_info(self, url: str, username: str, password: str) -> None:
        return None


class KeyRingPythonProvider(KeyRingBaseProvider):
    """Keyring interface which uses locally imported `keyring`"""

    has_keyring = True

    def __init__(self) -> None:
        # Imported only when this provider is chosen: keyring is optional, a
        # plugin host whose import can fail in any way, and not wanted at all
        # with --keyring-provider disabled or subprocess.
        import keyring

        self.keyring = keyring

    def get_auth_info(self, url: str, username: str | None) -> AuthInfo | None:
        if hasattr(self.keyring, "get_credential"):
            logger.debug("Getting credentials from keyring for %s", url)
            cred = self.keyring.get_credential(url, username)
            if cred is not None:
                return cred.username, cred.password
            return None

        if username is not None:
            logger.debug("Getting password from keyring for %s", url)
            password = self.keyring.get_password(url, username)
            if password:
                return username, password
        return None

    def save_auth_info(self, url: str, username: str, password: str) -> None:
        self.keyring.set_password(url, username, password)


class KeyRingCliProvider(KeyRingBaseProvider):
    """Provider which uses `keyring` cli

    Instead of calling the keyring package installed alongside kpip
    we call keyring on the command line which will enable kpip to
    use which ever installation of keyring is available first in
    PATH.
    """

    has_keyring = True

    def __init__(self, cmd: str) -> None:
        self.keyring = cmd

    def get_auth_info(self, url: str, username: str | None) -> AuthInfo | None:
        return self.get_creds(url, username)

    def save_auth_info(self, url: str, username: str, password: str) -> None:
        return self.set_password_internal(url, username, password)

    def get_creds(self, service_name: str, username: str | None) -> AuthInfo | None:
        """Mirror the implementation of keyring.get_credential using cli"""
        if self.keyring is None:
            return None

        cmd = [self.keyring, "--mode=creds", "--output=json", "get", service_name]
        if username is not None:
            cmd.append(username)

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        res = subprocess.run(  # noqa: UP022
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        errs = res.stderr.decode("utf-8")
        if (
            res.returncode == 2
            and "unrecognized arguments" in errs
            and "--mode=creds" in errs
        ):
            raise RuntimeError(
                "Keyring util is outdated; must be at least version 25.2.1, please upgrade it",
            )

        if res.returncode:
            return None

        data = json.loads(res.stdout.decode("utf-8"))
        return (data["username"], data["password"])

    def set_password_internal(
        self,
        service_name: str,
        username: str,
        password: str,
    ) -> None:
        """Mirror the implementation of keyring.set_password using cli"""
        if self.keyring is None:
            return

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        subprocess.run(
            [self.keyring, "set", service_name, username],
            input=f"{password}{os.linesep}".encode(),
            env=env,
            check=True,
        )
        return


@cache
def get_keyring_provider(provider: str) -> KeyRingBaseProvider:
    logger.debug("Keyring provider requested: %s", provider)

    if KEYRING_DISABLED:
        provider = "disabled"
    cli = shutil.which("keyring")
    scripts = sysconfig.get_path("scripts")
    external_cli = (
        cli is not None and scripts is not None and not cli.startswith(scripts)
    )
    if provider == "import" or (provider == "auto" and not external_cli):
        try:
            if provider == "auto":
                spec = importlib.util.find_spec("keyring")
                if spec is None or spec.origin is None:
                    raise ImportError("keyring is not installed")
                try:
                    if os.path.commonpath(
                        (os.path.realpath(spec.origin), os.path.realpath(sys.prefix)),
                    ) != os.path.realpath(sys.prefix):
                        raise ValueError
                except (OSError, ValueError) as exc:
                    raise ImportError(
                        "keyring is not installed in this environment",
                    ) from exc
            impl = KeyRingPythonProvider()
            logger.debug("Keyring provider set: import")
            return impl
        except ImportError:
            pass
        except Exception as exc:
            msg = "Installed copy of keyring fails with exception %s"
            if provider == "auto":
                msg = msg + ", trying to find a keyring executable as a fallback"
            logger.warning(msg, exc, exc_info=logger.isEnabledFor(logging.DEBUG))
    if provider in ["subprocess", "auto"]:
        if cli and cli.startswith(sysconfig.get_path("scripts")):

            @typing.no_type_check
            def PATH_as_shutil_which_determines_it() -> str:
                path = os.environ.get("PATH", None)
                if path is None:
                    try:
                        path = os.confstr("CS_PATH")
                    except AttributeError, ValueError:
                        path = os.defpath

                return path

            scripts = sysconfig.get_path("scripts")

            paths = []
            for path in PATH_as_shutil_which_determines_it().split(os.pathsep):
                try:
                    if not os.path.samefile(path, scripts):
                        paths.append(path)
                except FileNotFoundError:
                    pass

            path = os.pathsep.join(paths)

            cli = shutil.which("keyring", path=path)

        if cli:
            logger.debug("Keyring provider set: subprocess with executable %s", cli)
            return KeyRingCliProvider(cli)

    logger.debug("Keyring provider set: disabled")
    return KeyRingNullProvider()


class MultiDomainBasicAuth:
    def __init__(
        self,
        prompting: bool = True,
        index_urls: list[str] | None = None,
        keyring_provider: str = "auto",
    ) -> None:
        self.prompting = prompting
        self.index_urls = index_urls
        self.keyring_provider = keyring_provider
        self.passwords: dict[str, AuthInfo] = {}
        self.credential_cache: dict[str, tuple[str, str | None, str | None]] = {}
        self.prepared_index_urls_internal: (
            list[tuple[str, urllib.parse.SplitResult, urllib.parse.SplitResult]] | None
        ) = None
        self.index_urls_snapshot_internal: tuple[str, ...] = ()

    @property
    def keyring_provider(self) -> KeyRingBaseProvider:
        return get_keyring_provider(self.keyring_provider_internal)

    @keyring_provider.setter
    def keyring_provider(self, provider: str) -> None:
        self.keyring_provider_internal = provider

    @property
    def use_keyring(self) -> bool:
        return self.prompting or self.keyring_provider_internal not in [
            "auto",
            "disabled",
        ]

    def get_keyring_auth(
        self,
        url: str | None,
        username: str | None,
    ) -> AuthInfo | None:
        """Return the tuple auth for a given url from keyring."""
        if not url:
            return None
        try:
            return self.keyring_provider.get_auth_info(url, username)
        except Exception as exc:
            logger.debug("Keyring is skipped due to an exception", exc_info=True)
            logger.warning(
                "Keyring is skipped due to an exception: %s",
                str(exc),
            )
            global KEYRING_DISABLED
            KEYRING_DISABLED = True
            get_keyring_provider.cache_clear()
            return None

    def prepared_index_urls(
        self,
    ) -> list[tuple[str, urllib.parse.SplitResult, urllib.parse.SplitResult]]:
        """Split the configured index URLs once, refreshing when they change."""
        snapshot = tuple(self.index_urls) if self.index_urls else ()
        prepared = self.prepared_index_urls_internal
        if prepared is None or snapshot != self.index_urls_snapshot_internal:
            self.credential_cache.clear()
            prepared = []
            for index in snapshot:
                index = index.rstrip("/") + "/"
                prepared.append(
                    (
                        index,
                        urllib.parse.urlsplit(remove_auth_from_url(index)),
                        urllib.parse.urlsplit(index),
                    ),
                )
            self.prepared_index_urls_internal = prepared
            self.index_urls_snapshot_internal = snapshot
        return prepared

    def get_index_url(self, url: str) -> str | None:
        """Return the original index URL matching the requested URL.

        Cached or dynamically generated credentials may work against
        the original index URL rather than just the netloc.

        The provided url should have had its username and password
        removed already. If the original index url had credentials then
        they will be included in the return value.

        Returns None if no matching index was found, or if --no-index
        was specified by the user.
        """
        if not url or not self.index_urls:
            return None

        prepared = self.prepared_index_urls()

        url = remove_auth_from_url(url).rstrip("/") + "/"
        parsed_url = urllib.parse.urlsplit(url)

        candidates = []

        for index, parsed_index, parsed_with_auth in prepared:
            if parsed_url == parsed_index:
                return index

            if parsed_url.netloc != parsed_index.netloc:
                continue

            candidates.append(parsed_with_auth)

        if not candidates:
            return None

        candidates.sort(
            reverse=True,
            key=lambda candidate: len(
                commonpath(
                    [
                        parsed_url.path,
                        candidate.path,
                    ],
                ),
            ),
        )

        return urllib.parse.urlunsplit(candidates[0])

    def get_new_credentials(
        self,
        original_url: str,
        *,
        allow_netrc: bool = True,
        allow_keyring: bool = False,
    ) -> AuthInfo:
        """Find and return credentials for the specified URL."""
        url, netloc, url_user_password = split_auth_netloc_from_url(
            original_url,
        )

        username, password = url_user_password
        if username is not None and password is not None:
            logger.debug("Found credentials in url for %s", netloc)
            return url_user_password

        index_url = self.get_index_url(url)
        if index_url:
            index_info = split_auth_netloc_from_url(index_url)
            if index_info:
                index_url, _, index_url_user_password = index_info
                logger.debug("Found index url %s", index_url)

        if index_url and index_url_user_password[0] is not None:
            username, password = index_url_user_password
            if username is not None and password is not None:
                logger.debug("Found credentials in index url for %s", netloc)
                return index_url_user_password

        if allow_netrc:
            netrc_auth = get_netrc_auth(original_url)
            if netrc_auth:
                logger.debug("Found credentials in netrc for %s", netloc)
                return netrc_auth

        if allow_keyring:
            # fmt: off
            kr_auth = (
                self.get_keyring_auth(index_url, username) or
                self.get_keyring_auth(netloc, username)
            )
            # fmt: on
            if kr_auth:
                logger.debug("Found credentials in keyring for %s", netloc)
                return kr_auth

        return username, password

    def get_url_and_credentials(
        self,
        original_url: str,
    ) -> tuple[str, str | None, str | None]:
        """Return the credentials to use for the provided URL.

        If allowed, netrc and keyring may be used to obtain the
        correct credentials.

        Returns (url_without_credentials, username, password). Note
        that even if the original URL contains credentials, this
        function may return a different username and password.
        """
        self.prepared_index_urls()
        cached = self.credential_cache.get(original_url)
        if cached is not None:
            return cached

        url, netloc, _ = split_auth_netloc_from_url(original_url)

        username, password = self.get_new_credentials(original_url)

        if (username is None or password is None) and netloc in self.passwords:
            un, pw = self.passwords[netloc]
            if username is None or username == un:
                username, password = un, pw

        if username is not None or password is not None:
            username = username or ""
            password = password or ""

            if self.passwords.get(netloc) != (username, password):
                self.passwords[netloc] = (username, password)
                self.credential_cache.clear()

        assert (username is not None and password is not None) or (
            username is None and password is None
        ), f"Could not load credentials from url: {original_url}"

        result = url, username, password
        self.credential_cache[original_url] = result
        return result

    def prompt_for_password(self, netloc: str) -> tuple[str | None, str | None, bool]:
        username = ask_input(f"User for {netloc}: ") if self.prompting else None
        if not username:
            return None, None, False
        if self.use_keyring:
            auth = self.get_keyring_auth(netloc, username)
            if auth and auth[0] is not None and auth[1] is not None:
                return auth[0], auth[1], False
        password = ask_password("Password: ")
        return username, password, True

    def should_save_password_to_keyring_internal(self) -> bool:
        if (
            not self.prompting
            or not self.use_keyring
            or not self.keyring_provider.has_keyring
        ):
            return False
        return ask("Save credentials to keyring [y/N]: ", ["y", "n"]) == "y"

    def credentials_after_401(
        self,
        url: str,
    ) -> tuple[str | None, str | None, Credentials | None]:
        """Return credentials for a transport-managed 401 retry."""
        username, password = self.get_new_credentials(
            url,
            allow_netrc=True,
            allow_keyring=self.use_keyring,
        )
        save = False
        if not username and not password and self.prompting:
            username, password, save = self.prompt_for_password(
                urllib.parse.urlsplit(url).netloc,
            )
        if username is None or password is None:
            return username, password, None
        netloc = urllib.parse.urlsplit(url).netloc
        self.passwords[netloc] = (username, password)
        self.credential_cache.clear()
        credentials = None
        if save and self.should_save_password_to_keyring_internal():
            credentials = Credentials(url=netloc, username=username, password=password)
        return username, password, credentials
