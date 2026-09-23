from __future__ import annotations

import contextlib
import functools
import json
import os
import subprocess
import sys
from collections.abc import Generator, Iterable
from typing import Any
from unittest.mock import Mock

import kpip.network.auth
import pytest
from kpip.network.auth import MultiDomainBasicAuth
from kpip.network.session import NetworkSession
from kpip_test_support.transport_mocks import MockResponse


@pytest.fixture(autouse=True)
def reset_keyring() -> Iterable[None]:
    kpip.network.auth.KEYRING_DISABLED = False
    kpip.network.auth.get_keyring_provider.cache_clear()
    yield None
    kpip.network.auth.KEYRING_DISABLED = False
    kpip.network.auth.get_keyring_provider.cache_clear()


@pytest.mark.parametrize(
    "input_url, url, username, password",
    [
        (
            "http://user%40email.com:password@example.com/path",
            "http://example.com/path",
            "user@email.com",
            "password",
        ),
        (
            "http://username:password@example.com/path",
            "http://example.com/path",
            "username",
            "password",
        ),
        (
            "http://token@example.com/path",
            "http://example.com/path",
            "token",
            "",
        ),
        (
            "http://example.com/path",
            "http://example.com/path",
            None,
            None,
        ),
    ],
)
def test_get_credentials_parses_correctly(
    input_url: str,
    url: str,
    username: str | None,
    password: str | None,
) -> None:
    auth = MultiDomainBasicAuth()
    get = auth.get_url_and_credentials

    assert get(input_url) == (url, username, password)
    assert (username is None and password is None) or auth.passwords["example.com"] == (
        username,
        password,
    )


def test_get_credentials_not_to_uses_cached_credentials() -> None:
    auth = MultiDomainBasicAuth()
    auth.passwords["example.com"] = ("user", "pass")

    got = auth.get_url_and_credentials("http://foo:bar@example.com/path")
    expected = ("http://example.com/path", "foo", "bar")
    assert got == expected


def test_get_credentials_not_to_uses_cached_credentials_only_username() -> None:
    auth = MultiDomainBasicAuth()
    auth.passwords["example.com"] = ("user", "pass")

    got = auth.get_url_and_credentials("http://foo@example.com/path")
    expected = ("http://example.com/path", "foo", "")
    assert got == expected


def test_get_credentials_uses_cached_credentials() -> None:
    auth = MultiDomainBasicAuth()
    auth.passwords["example.com"] = ("user", "pass")

    got = auth.get_url_and_credentials("http://example.com/path")
    expected = ("http://example.com/path", "user", "pass")
    assert got == expected


def test_get_credentials_uses_cached_credentials_only_username() -> None:
    auth = MultiDomainBasicAuth()
    auth.passwords["example.com"] = ("user", "pass")

    got = auth.get_url_and_credentials("http://user@example.com/path")
    expected = ("http://example.com/path", "user", "pass")
    assert got == expected


def test_get_index_url_credentials() -> None:
    auth = MultiDomainBasicAuth(
        index_urls=[
            "http://example.com/",
            "http://foo:bar@example.com/path",
        ],
    )
    get = functools.partial(
        auth.get_new_credentials,
        allow_netrc=False,
        allow_keyring=False,
    )

    assert get("http://example.com/path/path2") == ("foo", "bar")
    assert get("http://example.com/path3/path2") == (None, None)


def test_prioritize_longest_path_prefix_match_organization() -> None:
    auth = MultiDomainBasicAuth(
        index_urls=[
            "http://foo:bar@example.com/org-name-alpha/repo-alias/simple",
            "http://bar:foo@example.com/org-name-beta/repo-alias/simple",
        ],
    )
    get = functools.partial(
        auth.get_new_credentials,
        allow_netrc=False,
        allow_keyring=False,
    )

    assert get("http://example.com/org-name-alpha/repo-guid/dowbload/") == (
        "foo",
        "bar",
    )
    assert get("http://example.com/org-name-beta/repo-guid/dowbload/") == ("bar", "foo")


def test_prioritize_longest_path_prefix_match_project() -> None:
    auth = MultiDomainBasicAuth(
        index_urls=[
            "http://foo:bar@example.com/org-alpha/project-name-alpha/repo-alias/simple",
            "http://bar:foo@example.com/org-alpha/project-name-beta/repo-alias/simple",
        ],
    )
    get = functools.partial(
        auth.get_new_credentials,
        allow_netrc=False,
        allow_keyring=False,
    )

    assert get(
        "http://example.com/org-alpha/project-name-alpha/repo-guid/dowbload/",
    ) == ("foo", "bar")
    assert get(
        "http://example.com/org-alpha/project-name-beta/repo-guid/dowbload/",
    ) == ("bar", "foo")


class KeyringModuleV1:
    """Represents the supported API of keyring before get_credential
    was added.
    """

    def __init__(self) -> None:
        self.saved_passwords: list[tuple[str, str, str]] = []

    def get_password(self, system: str, username: str) -> str | None:
        if system == "example.com" and username:
            return username + "!netloc"
        if system == "http://example.com/path2/" and username:
            return username + "!url"
        return None

    def set_password(self, system: str, username: str, password: str) -> None:
        self.saved_passwords.append((system, username, password))


@pytest.mark.parametrize(
    "url, expect",
    [
        ("http://example.com/path1", (None, None)),
        ("http://user@example.com/path3", ("user", "user!netloc")),
        ("http://user2@example.com/path3", ("user2", "user2!netloc")),
        ("http://example.com/path2/path3", (None, None)),
        ("http://foo@example.com/path2/path3", ("foo", "foo!url")),
    ],
)
def test_keyring_get_password(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expect: tuple[str | None, str | None],
) -> None:
    keyring = KeyringModuleV1()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    auth = MultiDomainBasicAuth(
        index_urls=["http://example.com/path2", "http://example.com/path3"],
        keyring_provider="import",
    )

    actual = auth.get_new_credentials(url, allow_netrc=False, allow_keyring=True)
    assert actual == expect


def test_keyring_get_password_after_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring = KeyringModuleV1()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    auth = MultiDomainBasicAuth(keyring_provider="import")

    def ask_input(prompt: str) -> str:
        assert prompt == "User for example.com: "
        return "user"

    monkeypatch.setattr("kpip.network.auth.ask_input", ask_input)
    actual = auth.prompt_for_password("example.com")
    assert actual == ("user", "user!netloc", False)


def test_keyring_get_password_after_prompt_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = KeyringModuleV1()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    auth = MultiDomainBasicAuth(keyring_provider="import")

    def ask_input(prompt: str) -> str:
        assert prompt == "User for unknown.com: "
        return "user"

    def ask_password(prompt: str) -> str:
        assert prompt == "Password: "
        return "fake_password"

    monkeypatch.setattr("kpip.network.auth.ask_input", ask_input)
    monkeypatch.setattr("kpip.network.auth.ask_password", ask_password)
    actual = auth.prompt_for_password("unknown.com")
    assert actual == ("user", "fake_password", True)


def test_keyring_get_password_username_in_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = KeyringModuleV1()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    auth = MultiDomainBasicAuth(
        index_urls=["http://user@example.com/path2", "http://example.com/path4"],
        keyring_provider="import",
    )
    get = functools.partial(
        auth.get_new_credentials,
        allow_netrc=False,
        allow_keyring=True,
    )

    assert get("http://example.com/path2/path3") == ("user", "user!url")
    assert get("http://example.com/path4/path1") == (None, None)


@pytest.mark.parametrize(
    "response_status, creds, expect_save",
    [
        (403, ("user", "pass", True), False),
        (
            200,
            ("user", "pass", True),
            True,
        ),
        (
            200,
            ("user", "pass", False),
            False,
        ),
    ],
)
def test_keyring_set_password(
    monkeypatch: pytest.MonkeyPatch,
    response_status: int,
    creds: tuple[str, str, bool],
    expect_save: bool,
) -> None:
    keyring = KeyringModuleV1()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    auth = MultiDomainBasicAuth(prompting=True, keyring_provider="import")
    monkeypatch.setattr(auth, "get_url_and_credentials", lambda u: (u, None, None))
    monkeypatch.setattr(auth, "prompt_for_password", lambda *a: creds)
    if creds[2]:

        def should_save_password_to_keyring(*a: Any) -> bool:
            return True

    else:

        def should_save_password_to_keyring(*a: Any) -> bool:
            pytest.fail("should_save_password_to_keyring_internal should not be called")

    monkeypatch.setattr(
        auth,
        "should_save_password_to_keyring_internal",
        should_save_password_to_keyring,
    )

    url = "https://example.com"
    resp = MockResponse(b"")
    resp.url = url
    resp.status = 401
    session = NetworkSession()
    session.auth = auth

    def request(method: str, url: str, **kwargs: Any) -> MockResponse:
        assert method == "GET"
        assert url == resp.url
        assert "authorization" in kwargs["headers"]
        assert kwargs["stream"] is True
        r = MockResponse(b"")
        r.status = response_status
        return r

    monkeypatch.setattr(session, "request", request)
    retry = session.retry_auth(resp, "GET", {}, None, None, stream=True)

    assert retry is not None

    if expect_save:
        assert keyring.saved_passwords == [("example.com", creds[0], creds[1])]
    else:
        assert keyring.saved_passwords == []


class KeyringModuleV2:
    """Represents the current supported API of keyring"""

    def __init__(self) -> None:
        self.saved_credential_by_username_by_system: dict[
            str,
            dict[str, KeyringModuleV2.Credential],
        ] = {}

    class Credential:
        __slots__ = ("password", "username")

        def __init__(self, username: str, password: str) -> None:
            self.username = username
            self.password = password

        def __eq__(self, other: object) -> bool:
            return (
                isinstance(other, KeyringModuleV2.Credential)
                and self.username == other.username
                and self.password == other.password
            )

    def get_password(self, system: str, username: str) -> None:
        pytest.fail("get_password should not ever be called")

    def get_credential(self, system: str, username: str | None) -> Credential | None:
        credential_by_username = self.saved_credential_by_username_by_system.get(
            system,
            {},
        )
        if username is None:
            credentials = list(credential_by_username.values())
            if len(credentials) == 0:
                return None

            credential = credentials[0]
            return credential

        return credential_by_username.get(username)

    def set_password(self, system: str, username: str, password: str) -> None:
        if system not in self.saved_credential_by_username_by_system:
            self.saved_credential_by_username_by_system[system] = {}

        credential_by_username = self.saved_credential_by_username_by_system[system]
        assert username not in credential_by_username
        credential_by_username[username] = self.Credential(username, password)

    def delete_password(self, system: str, username: str) -> None:
        del self.saved_credential_by_username_by_system[system][username]

    @contextlib.contextmanager
    def add_credential(
        self,
        system: str,
        username: str,
        password: str,
    ) -> Generator[None]:
        """Context manager that adds the given credential to the keyring
        and yields. Once the yield is done, the credential is removed
        from the keyring.

        This is re-entrant safe: it's ok for one thread to call this while in
        the middle of an existing invocation

        This is probably not thread safe: it's not ok for multiple threads to
        simultaneously call this method on the exact same instance of KeyringModuleV2.
        """
        self.set_password(system, username, password)
        try:
            yield
        finally:
            self.delete_password(system, username)


@pytest.mark.parametrize(
    "url, expect",
    [
        ("http://example.com/path1", ("username", "hunter2")),
        ("http://example.com/path2/path3", ("username", "hunter3")),
        ("http://user2@example.com/path2/path3", ("user2", None)),
    ],
)
def test_keyring_get_credential(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expect: tuple[str, str],
) -> None:
    keyring = KeyringModuleV2()
    monkeypatch.setitem(sys.modules, "keyring", keyring)
    auth = MultiDomainBasicAuth(
        index_urls=["http://example.com/path1", "http://example.com/path2"],
        keyring_provider="import",
    )

    with (
        keyring.add_credential("example.com", "username", "hunter2"),
        keyring.add_credential("http://example.com/path2/", "username", "hunter3"),
    ):
        assert (
            auth.get_new_credentials(url, allow_netrc=False, allow_keyring=True)
            == expect
        )


class KeyringModuleBroken:
    """Represents the current supported API of keyring, but broken"""

    def __init__(self) -> None:
        self.call_count_internal = 0

    def get_credential(self, system: str, username: str) -> None:
        self.call_count_internal += 1
        raise Exception("This keyring is broken!")


def test_broken_keyring_disables_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    keyring_broken = KeyringModuleBroken()
    monkeypatch.setitem(sys.modules, "keyring", keyring_broken)

    auth = MultiDomainBasicAuth(
        index_urls=["http://example.com/"],
        keyring_provider="import",
    )

    assert keyring_broken.call_count_internal == 0
    for i in range(5):
        url = "http://example.com/path" + str(i)
        assert auth.get_new_credentials(url, allow_netrc=False, allow_keyring=True) == (
            None,
            None,
        )
        assert keyring_broken.call_count_internal == 1


class KeyringSubprocessResult(KeyringModuleV2):
    """Represents the subprocess call to keyring"""

    returncode = 0

    def __init__(self) -> None:
        super().__init__()
        self.old_version = False

    def __call__(
        self,
        cmd: list[str],
        *,
        env: dict[str, str],
        stdin: Any | None = None,
        stdout: Any | None = None,
        stderr: Any | None = None,
        input: bytes | None = None,
        check: bool | None = None,
    ) -> Any:
        parsed_cmd = list(cmd)
        assert parsed_cmd.pop(0) == "keyring"
        subcommand = [arg for arg in parsed_cmd if not arg.startswith("--")][0]
        subcommand_func = {
            "get": self.get_subcommand,
            "set": self.set_subcommand,
        }[subcommand]

        subcommand_func(
            parsed_cmd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            input=input,
            check=check,
        )

        return self

    def get_subcommand(
        self,
        cmd: list[str],
        *,
        env: dict[str, str],
        stdin: Any | None = None,
        stdout: Any | None = None,
        stderr: Any | None = None,
        input: bytes | None = None,
        check: bool | None = None,
    ) -> None:
        assert cmd.pop(0) == "--mode=creds"
        assert cmd.pop(0) == "--output=json"
        assert stdin == -3
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert check is None
        assert cmd.pop(0) == "get"

        if self.old_version:
            self.stderr = (
                b"usage: keyring [-h] [-p KEYRING_PATH] [-b KEYRING_BACKEND] "
                b"[--list-backends] [--disable] [--print-completion {bash,zsh,tcsh}] "
                b"[{get,set,del,diagnose}] [service] [username]\n"
                b"keyring: error: unrecognized arguments: --mode=creds --output=json"
            )
            self.returncode = 2
            return
        self.stderr = b""

        service = cmd.pop(0)
        username = cmd.pop(0) if len(cmd) > 0 else None
        creds = self.get_credential(service, username)
        if creds is None:
            self.returncode = 1
        else:
            self.returncode = 0
            self.stdout = json.dumps(
                {
                    "username": creds.username,
                    "password": creds.password,
                },
            ).encode("utf-8")

    def set_subcommand(
        self,
        cmd: list[str],
        *,
        env: dict[str, str],
        stdin: Any | None = None,
        stdout: Any | None = None,
        stderr: Any | None = None,
        input: bytes | None = None,
        check: bool | None = None,
    ) -> None:
        assert cmd.pop(0) == "set"
        assert stdin is None
        assert stdout is None
        assert stderr is None
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert input is not None
        assert check

        system, username = cmd
        self.set_password(system, username, input.decode("utf-8").strip(os.linesep))

    def check_returncode(self) -> None:
        if self.returncode:
            raise Exception


@pytest.mark.parametrize(
    "url, expect",
    [
        ("http://example.com/path1", ("saved-user1", "pw1")),
        ("http://saved-user1@example.com/path2", ("saved-user1", "pw1")),
        ("http://saved-user2@example.com/path2", ("saved-user2", "pw2")),
        ("http://new-user@example.com/path2", ("new-user", None)),
        ("http://example.com/path2/path3", ("saved-user1", "pw1")),
        ("http://foo@example.com/path2/path3", ("foo", None)),
    ],
)
def test_keyring_cli_get_password(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expect: tuple[str | None, str | None],
) -> None:
    keyring_subprocess = KeyringSubprocessResult()
    monkeypatch.setattr(kpip.network.auth.shutil, "which", lambda x: "keyring")
    monkeypatch.setattr(subprocess, "run", keyring_subprocess)
    auth = MultiDomainBasicAuth(
        index_urls=["http://example.com/path2", "http://example.com/path3"],
        keyring_provider="subprocess",
    )

    with (
        keyring_subprocess.add_credential("example.com", "example", "!netloc"),
        keyring_subprocess.add_credential(
            "http://example.com/path2/",
            "saved-user1",
            "pw1",
        ),
        keyring_subprocess.add_credential(
            "http://example.com/path2/",
            "saved-user2",
            "pw2",
        ),
    ):
        actual = auth.get_new_credentials(url, allow_netrc=False, allow_keyring=True)
        assert actual == expect


@pytest.mark.parametrize(
    "response_status, creds, expect_save",
    [
        (403, ("user", "pass", True), False),
        (
            200,
            ("user", "pass", True),
            True,
        ),
        (
            200,
            ("user", "pass", False),
            False,
        ),
    ],
)
def test_keyring_cli_set_password(
    monkeypatch: pytest.MonkeyPatch,
    response_status: int,
    creds: tuple[str, str, bool],
    expect_save: bool,
) -> None:
    expected_username, expected_password, save = creds
    monkeypatch.setattr(kpip.network.auth.shutil, "which", lambda x: "keyring")
    keyring = KeyringSubprocessResult()
    monkeypatch.setattr(subprocess, "run", keyring)
    auth = MultiDomainBasicAuth(prompting=True, keyring_provider="subprocess")
    monkeypatch.setattr(auth, "get_url_and_credentials", lambda u: (u, None, None))
    monkeypatch.setattr(auth, "prompt_for_password", lambda *a: creds)
    if save:

        def should_save_password_to_keyring(*a: Any) -> bool:
            return True

    else:

        def should_save_password_to_keyring(*a: Any) -> bool:
            pytest.fail("should_save_password_to_keyring_internal should not be called")

    monkeypatch.setattr(
        auth,
        "should_save_password_to_keyring_internal",
        should_save_password_to_keyring,
    )

    url = "https://example.com"
    resp = MockResponse(b"")
    resp.url = url
    resp.status = 401
    session = NetworkSession()
    session.auth = auth

    def request(method: str, url: str, **kwargs: Any) -> MockResponse:
        assert method == "GET"
        assert url == resp.url
        assert "authorization" in kwargs["headers"]
        assert kwargs["stream"] is False
        r = MockResponse(b"")
        r.status = response_status
        return r

    monkeypatch.setattr(session, "request", request)
    retry = session.retry_auth(resp, "GET", {}, None, None, stream=False)

    assert retry is not None

    if expect_save:
        assert keyring.saved_credential_by_username_by_system == {
            "example.com": {
                expected_username: KeyringModuleV2.Credential(
                    expected_username,
                    expected_password,
                ),
            },
        }
    else:
        assert keyring.saved_credential_by_username_by_system == {}


@pytest.mark.parametrize(
    "url, expect",
    [
        ("http://example.com/path1", ("saved-user1", "pw1")),
        ("http://saved-user1@example.com/path2", ("saved-user1", "pw1")),
        ("http://saved-user2@example.com/path2", ("saved-user2", "pw2")),
        ("http://new-user@example.com/path2", ("new-user", None)),
        ("http://example.com/path2/path3", ("saved-user1", "pw1")),
        ("http://foo@example.com/path2/path3", ("foo", None)),
    ],
)
def test_keyring_cli_outdated_version(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expect: tuple[str | None, str | None],
) -> None:
    keyring_subprocess = KeyringSubprocessResult()
    keyring_subprocess.old_version = True
    warning = Mock()

    monkeypatch.setattr(kpip.network.auth.shutil, "which", lambda x: "keyring")
    monkeypatch.setattr(subprocess, "run", keyring_subprocess)
    monkeypatch.setattr(kpip.network.auth.logger, "warning", warning)
    auth = MultiDomainBasicAuth(
        index_urls=["http://example.com/path2", "http://example.com/path3"],
        keyring_provider="subprocess",
    )

    with (
        keyring_subprocess.add_credential("example.com", "example", "!netloc"),
        keyring_subprocess.add_credential(
            "http://example.com/path2/",
            "saved-user1",
            "pw1",
        ),
        keyring_subprocess.add_credential(
            "http://example.com/path2/",
            "saved-user2",
            "pw2",
        ),
    ):
        actual = auth.get_new_credentials(url, allow_netrc=False, allow_keyring=True)

        assert actual[1] is None

    warning.assert_called_once()
    actual_message = warning.call_args.args[1]
    assert "Keyring util is outdated" in actual_message
    assert "version 25.2.1" in actual_message


@pytest.mark.parametrize("prompting", [True, False])
def test_credentials_after_401_extracts_credentials_embedded_in_url(
    prompting: bool,
) -> None:
    """Credentials embedded in the (redirect) URL must be recovered on 401.

    When an index issues a cross-origin redirect whose ``Location`` carries
    embedded Basic-auth credentials, the redirect policy strips the
    ``Authorization`` header, so the upstream host answers ``401``. The retry
    URL still contains the ``user:password@host`` credentials, which must be
    recovered without user interaction, even under ``--no-input``.

    Regression test: previously the extraction was gated behind ``use_keyring``,
    so ``--no-input`` with the default keyring provider returned the 401 without
    retrying.
    """
    auth = MultiDomainBasicAuth(prompting=prompting, keyring_provider="disabled")

    url_with_creds = "http://user:pass@example.com/simple/pkg/"
    username, password, credentials = auth.credentials_after_401(url_with_creds)

    assert (username, password) == ("user", "pass")
    assert credentials is None
