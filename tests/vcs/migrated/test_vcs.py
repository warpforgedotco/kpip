from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any
from unittest import mock

import pytest
from kpip.core.errors import InstallationError
from kpip.core.subprocesses import CommandArgs
from kpip.vcs.bazaar import Bazaar
from kpip.vcs.errors import BadCommand
from kpip.vcs.git import Git, RemoteNotValidError, looks_like_hash
from kpip.vcs.mercurial import Mercurial
from kpip.vcs.subversion import Subversion
from kpip.vcs.support import HiddenText, hide_url, hide_value
from kpip.vcs.versioncontrol import RevOptions, VersionControl, make_vcs_requirement_url
from kpip_test_support import is_svn_installed, need_svn


@pytest.mark.skipif(
    "CI" not in os.environ or sys.platform == "win32",
    reason="Subversion is only required under CI on POSIX runners",
)
def test_ensure_svn_available() -> None:
    """Make sure that svn is available when running in CI."""
    assert is_svn_installed()


@pytest.mark.parametrize(
    "args, expected",
    [
        (
            ("git+https://example.com/pkg", "dev", "myproj"),
            "git+https://example.com/pkg@dev#egg=myproj",
        ),
        (
            ("git+https://example.com/pkg", "dev", "myproj", "sub/dir"),
            "git+https://example.com/pkg@dev#egg=myproj&subdirectory=sub/dir",
        ),
        (
            ("git+https://example.com/pkg", "dev", "myproj", None),
            "git+https://example.com/pkg@dev#egg=myproj",
        ),
        (
            ("git+https://example.com/pkg", "dev", "zope-interface"),
            "git+https://example.com/pkg@dev#egg=zope_interface",
        ),
        (
            ("git+https://example.com/pkg", "dev@1#2", "myproj"),
            "git+https://example.com/pkg@dev%401%232#egg=myproj",
        ),
    ],
)
def test_make_vcs_requirement_url(args: tuple[Any, ...], expected: str) -> None:
    actual = make_vcs_requirement_url(*args)
    assert actual == expected


def test_rev_options_repr() -> None:
    rev_options = RevOptions(Git, "develop")
    assert repr(rev_options) == "<RevOptions git: rev='develop'>"


@pytest.mark.parametrize(
    "vc_class, expected1, expected2, kwargs",
    [
        (Bazaar, [], ["-r", "123"], {}),
        (Git, ["HEAD"], ["123"], {}),
        (Mercurial, [], ["--rev=123"], {}),
        (Subversion, [], ["-r", "123"], {}),
        (
            Git,
            ["HEAD", "opt1", "opt2"],
            ["123", "opt1", "opt2"],
            {"extra_args": ["opt1", "opt2"]},
        ),
    ],
)
def test_rev_options_to_args(
    vc_class: type[VersionControl],
    expected1: list[str],
    expected2: list[str],
    kwargs: dict[str, Any],
) -> None:
    """Test RevOptions.to_args()."""
    assert RevOptions(vc_class, **kwargs).to_args() == expected1
    assert RevOptions(vc_class, "123", **kwargs).to_args() == expected2


def test_rev_options_to_display() -> None:
    """Test RevOptions.to_display()."""
    rev_options = RevOptions(Git)
    assert rev_options.to_display() == ""

    rev_options = RevOptions(Git, "master")
    assert rev_options.to_display() == " (to revision master)"


def test_rev_options_make_new() -> None:
    """Test RevOptions.make_new()."""
    rev_options = RevOptions(Git, "master", extra_args=["foo", "bar"])
    new_options = rev_options.make_new("develop")

    assert new_options is not rev_options
    assert new_options.extra_args == ["foo", "bar"]
    assert new_options.rev == "develop"
    assert new_options.vc_class is Git


@pytest.mark.parametrize(
    "sha, expected",
    [
        ((40 * "a"), True),
        ((40 * "A"), True),
        ((18 * "a" + "0123456789abcdefABCDEF"), True),
        ((40 * "g"), False),
        ((39 * "a"), False),
        ((41 * "a"), False),
    ],
)
def test_looks_like_hash(sha: str, expected: bool) -> None:
    assert looks_like_hash(sha) == expected


@pytest.mark.parametrize(
    "vcs_cls, remote_url, expected",
    [
        (Mercurial, "hg://user@example.com/MyProject", False),
        (Mercurial, "http://example.com/MyProject", True),
        (Git, "git://example.com/MyProject", True),
        (Git, "http://example.com/MyProject", True),
        (Subversion, "svn://example.com/MyProject", True),
    ],
)
def test_should_add_vcs_url_prefix(
    vcs_cls: type[VersionControl],
    remote_url: str,
    expected: bool,
) -> None:
    actual = vcs_cls.should_add_vcs_url_prefix(remote_url)
    assert actual == expected


@pytest.mark.parametrize(
    "url, target",
    [
        ("ssh://bob@server/foo/bar.git", "ssh://bob@server/foo/bar.git"),
        ("git://bob@server/foo/bar.git", "git://bob@server/foo/bar.git"),
        ("ssh://server/foo/bar.git", "ssh://server/foo/bar.git"),
        ("git@example.com:foo/bar.git", "ssh://git@example.com/foo/bar.git"),
        ("example.com:foo.git", "ssh://example.com/foo.git"),
        ("https://example.com/foo", "https://example.com/foo"),
        ("http://example.com/foo/bar.git", "http://example.com/foo/bar.git"),
        ("https://bob@example.com/foo", "https://bob@example.com/foo"),
    ],
)
def test_git_remote_url_to_pip(url: str, target: str) -> None:
    assert Git.git_remote_to_kpip_url(url) == target


@pytest.mark.parametrize(
    "url, platform",
    [
        ("c:/piffle/wiffle/waffle/poffle.git", "nt"),
        (r"c:\faffle\waffle\woffle\piffle.git", "nt"),
        ("/muffle/fuffle/pufffle/fluffle.git", "posix"),
    ],
)
def test_paths_are_not_mistaken_for_scp_shorthand(url: str, platform: str) -> None:
    from kpip.vcs.git import SCP_REGEX

    assert not SCP_REGEX.match(url)

    if platform == os.name:
        with pytest.raises(RemoteNotValidError):
            Git.git_remote_to_kpip_url(url)


def test_git_remote_local_path(tmp_path: pathlib.Path) -> None:
    path = pathlib.Path(tmp_path, "project.git")
    path.mkdir()
    assert Git.git_remote_to_kpip_url(str(path)) == path.as_uri()


@mock.patch("kpip.vcs.git.Git.get_remote_url")
@mock.patch("kpip.vcs.git.Git.get_revision")
@mock.patch("kpip.vcs.git.Git.get_subdirectory")
@pytest.mark.parametrize(
    "git_url, target_url_prefix",
    [
        (
            "https://github.com/pypa/pip-test-package",
            "git+https://github.com/pypa/pip-test-package",
        ),
        (
            "git@github.com:pypa/pip-test-package",
            "git+ssh://git@github.com/pypa/pip-test-package",
        ),
    ],
    ids=["https", "ssh"],
)
@pytest.mark.network
def test_git_get_src_requirements(
    mock_get_subdirectory: mock.Mock,
    mock_get_revision: mock.Mock,
    mock_get_remote_url: mock.Mock,
    git_url: str,
    target_url_prefix: str,
) -> None:
    sha = "5547fa909e83df8bd743d3978d6667497983a4b7"

    mock_get_remote_url.return_value = Git.git_remote_to_kpip_url(git_url)
    mock_get_revision.return_value = sha
    mock_get_subdirectory.return_value = None

    ret = Git.get_src_requirement(".", "pip-test-package")

    target = f"{target_url_prefix}@{sha}#egg=pip_test_package"
    assert ret == target


@mock.patch("kpip.vcs.git.Git.get_revision_sha")
def test_git_resolve_revision_rev_exists(get_sha_mock: mock.Mock) -> None:
    get_sha_mock.return_value = ("123456", False)
    url = HiddenText("git+https://git.example.com", redacted="*")
    rev_options = Git.make_rev_options("develop")

    new_options = Git.resolve_revision(".", url, rev_options)
    assert new_options.rev == "123456"


@mock.patch("kpip.vcs.git.Git.get_revision_sha")
def test_git_resolve_revision_rev_not_found(get_sha_mock: mock.Mock) -> None:
    get_sha_mock.return_value = (None, False)
    url = HiddenText("git+https://git.example.com", redacted="*")
    rev_options = Git.make_rev_options("develop")

    new_options = Git.resolve_revision(".", url, rev_options)
    assert new_options.rev == "develop"


@mock.patch("kpip.vcs.git.Git.get_revision_sha")
def test_git_resolve_revision_not_found_warning(
    get_sha_mock: mock.Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    get_sha_mock.return_value = (None, False)
    url = HiddenText("git+https://git.example.com", redacted="*")
    sha = 40 * "a"
    rev_options = Git.make_rev_options(sha)

    rev_options = Git.make_rev_options(sha[:6])
    new_options = Git.resolve_revision(".", url, rev_options)
    assert new_options.rev == "aaaaaa"

    messages = [r.getMessage() for r in caplog.records]
    messages = [msg for msg in messages if msg.startswith("Did not find ")]
    assert messages == [
        "Did not find branch or tag 'aaaaaa', assuming revision or ref.",
    ]


@pytest.mark.parametrize(
    "rev_name,result",
    [
        ("5547fa909e83df8bd743d3978d6667497983a4b7", True),
        ("5547fa909", False),
        ("5678", False),
        ("abc123", False),
        ("foo", False),
        (None, False),
    ],
)
@mock.patch("kpip.vcs.git.Git.get_revision")
def test_git_is_commit_id_equal(
    mock_get_revision: mock.Mock,
    rev_name: str | None,
    result: bool,
) -> None:
    """Test Git.is_commit_id_equal()."""
    mock_get_revision.return_value = "5547fa909e83df8bd743d3978d6667497983a4b7"
    assert Git.is_commit_id_equal("/path", rev_name) is result


@pytest.mark.parametrize(
    "args, expected",
    [
        (("example.com", "https"), ("example.com", (None, None))),
        (("user:pass@example.com", "https"), ("user:pass@example.com", (None, None))),
    ],
)
def test_git__get_netloc_and_auth(
    args: tuple[str, str],
    expected: tuple[str, tuple[None, None]],
) -> None:
    """Test VersionControl.get_netloc_and_auth()."""
    netloc, scheme = args
    actual = Git.get_netloc_and_auth(netloc, scheme)
    assert actual == expected


@pytest.mark.parametrize(
    "args, expected",
    [
        (("example.com", "https"), ("example.com", (None, None))),
        (("user@example.com", "https"), ("example.com", ("user", None))),
        (("user:pass@example.com", "https"), ("example.com", ("user", "pass"))),
        (
            ("user%3Aname:%23%40%5E@example.com", "https"),
            ("example.com", ("user:name", "#@^")),
        ),
        (("user:pass@example.com", "ssh"), ("user:pass@example.com", (None, None))),
    ],
)
def test_subversion__get_netloc_and_auth(
    args: tuple[str, str],
    expected: tuple[str, tuple[str | None, str | None]],
) -> None:
    """Test Subversion.get_netloc_and_auth()."""
    netloc, scheme = args
    actual = Subversion.get_netloc_and_auth(netloc, scheme)
    assert actual == expected


def test_git__get_url_rev__idempotent() -> None:
    """Check that Git.get_url_rev_and_auth() is idempotent for what the code calls
    "stub URLs" (i.e. URLs that don't contain "://").

    Also check that it doesn't change self.url.
    """
    url = "git+git@git.example.com:MyProject#egg=MyProject"
    result1 = Git.get_url_rev_and_auth(url)
    result2 = Git.get_url_rev_and_auth(url)
    expected = ("git@git.example.com:MyProject", None, (None, None))
    assert result1 == expected
    assert result2 == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "svn+https://svn.example.com/MyProject",
            ("https://svn.example.com/MyProject", None, (None, None)),
        ),
        (
            "svn+https://svn.example.com/My+Project",
            ("https://svn.example.com/My+Project", None, (None, None)),
        ),
        (
            "svn+https://svn.example.com/MyProject@dev%401%232",
            ("https://svn.example.com/MyProject", "dev@1#2", (None, None)),
        ),
    ],
)
def test_version_control__get_url_rev_and_auth(
    url: str,
    expected: tuple[str, None, tuple[None, None]],
) -> None:
    """Test the basic case of VersionControl.get_url_rev_and_auth()."""
    actual = VersionControl.get_url_rev_and_auth(url)
    assert actual == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://svn.example.com/MyProject",
        "https://svn.example.com/My+Project",
    ],
)
def test_version_control__get_url_rev_and_auth__missing_plus(url: str) -> None:
    """Test passing a URL to VersionControl.get_url_rev_and_auth() with a "+"
    missing from the scheme.
    """
    with pytest.raises(ValueError) as excinfo:
        VersionControl.get_url_rev_and_auth(url)

    assert "malformed VCS url" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "git+https://github.com/MyUser/myProject.git@#egg=py_pkg",
    ],
)
def test_version_control__get_url_rev_and_auth__no_revision(url: str) -> None:
    """Test passing a URL to VersionControl.get_url_rev_and_auth() with
    empty revision
    """
    with pytest.raises(InstallationError) as excinfo:
        VersionControl.get_url_rev_and_auth(url)

    assert "an empty revision (after @)" in str(excinfo.value)


@pytest.mark.parametrize("vcs_cls", [Bazaar, Git, Mercurial, Subversion])
@pytest.mark.parametrize(
    "exc_cls, msg_re",
    [
        (FileNotFoundError, r"Cannot find command '{name}'"),
        (PermissionError, r"No permission to execute '{name}'"),
        (NotADirectoryError, "Cannot find command '{name}' - invalid PATH"),
    ],
    ids=["FileNotFoundError", "PermissionError", "NotADirectoryError"],
)
def test_version_control__run_command__fails(
    vcs_cls: type[VersionControl],
    exc_cls: type[Exception],
    msg_re: str,
) -> None:
    """Test that ``VersionControl.run_command()`` raises ``BadCommand``
    when the command is not found or when the user have no permission
    to execute it. The error message must contains the command name.
    """
    with mock.patch("kpip.vcs.versioncontrol.call_subprocess") as call:
        call.side_effect = exc_cls
        with pytest.raises(BadCommand, match=msg_re.format(name=vcs_cls.name)):
            vcs_cls.run_command([])


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "bzr+http://bzr.myproject.org/MyProject/trunk/#egg=MyProject",
            "http://bzr.myproject.org/MyProject/trunk/",
        ),
        (
            "bzr+https://bzr.myproject.org/MyProject/trunk/#egg=MyProject",
            "https://bzr.myproject.org/MyProject/trunk/",
        ),
        (
            "bzr+ftp://bzr.myproject.org/MyProject/trunk/#egg=MyProject",
            "ftp://bzr.myproject.org/MyProject/trunk/",
        ),
        (
            "bzr+sftp://bzr.myproject.org/MyProject/trunk/#egg=MyProject",
            "sftp://bzr.myproject.org/MyProject/trunk/",
        ),
        ("bzr+lp:MyLaunchpadProject#egg=MyLaunchpadProject", "lp:MyLaunchpadProject"),
        (
            "bzr+ssh://bzr.myproject.org/MyProject/trunk/#egg=MyProject",
            "bzr+ssh://bzr.myproject.org/MyProject/trunk/",
        ),
    ],
)
def test_bazaar__get_url_rev_and_auth(url: str, expected: str) -> None:
    """Test Bazaar.get_url_rev_and_auth()."""
    actual = Bazaar.get_url_rev_and_auth(url)
    assert actual == (expected, None, (None, None))


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "svn+https://svn.example.com/MyProject#egg=MyProject",
            ("https://svn.example.com/MyProject", None, (None, None)),
        ),
        (
            "svn+https://user:pass@svn.example.com/MyProject#egg=MyProject",
            ("https://svn.example.com/MyProject", None, ("user", "pass")),
        ),
        (
            "svn+ssh://svn.example.com/MyProject#egg=MyProject",
            ("svn+ssh://svn.example.com/MyProject", None, (None, None)),
        ),
        (
            "svn+ssh://user@svn.example.com/MyProject#egg=MyProject",
            ("svn+ssh://user@svn.example.com/MyProject", None, (None, None)),
        ),
    ],
)
def test_subversion__get_url_rev_and_auth(
    url: str,
    expected: tuple[str, None, tuple[str | None, str | None]],
) -> None:
    """Test Subversion.get_url_rev_and_auth()."""
    actual = Subversion.get_url_rev_and_auth(url)
    assert actual == expected


@pytest.mark.parametrize(
    "username, password, expected",
    [
        (None, None, []),
        ("user", None, []),
        ("user", hide_value("pass"), []),
    ],
)
def test_git__make_rev_args(
    username: str | None,
    password: HiddenText | None,
    expected: CommandArgs,
) -> None:
    """Test VersionControl.make_rev_args()."""
    actual = Git.make_rev_args(username, password)
    assert actual == expected


@pytest.mark.parametrize(
    "username, password, expected",
    [
        (None, None, []),
        ("user", None, ["--username", "user"]),
        (
            "user",
            hide_value("pass"),
            ["--username", "user", "--password", hide_value("pass")],
        ),
    ],
)
def test_subversion__make_rev_args(
    username: str | None,
    password: HiddenText | None,
    expected: CommandArgs,
) -> None:
    """Test Subversion.make_rev_args()."""
    actual = Subversion.make_rev_args(username, password)
    assert actual == expected


def test_subversion__get_url_rev_options() -> None:
    """Test Subversion.get_url_rev_options()."""
    secret_url = "svn+https://user:pass@svn.example.com/MyProject@v1.0#egg=MyProject"
    hidden_url = hide_url(secret_url)
    url, rev_options = Subversion().get_url_rev_options(hidden_url)
    assert url == hide_url("https://svn.example.com/MyProject")
    assert rev_options.rev == "v1.0"
    assert rev_options.extra_args == (
        ["--username", "user", "--password", hide_value("pass")]
    )


def test_get_git_version() -> None:
    git_version = Git().get_git_version()
    assert git_version >= (1, 0, 0)


@pytest.mark.parametrize(
    "version, expected",
    [
        ("git version 2.17", (2, 17)),
        ("git version 2.18.1", (2, 18)),
        ("git version 2.35.GIT", (2, 35)),
        ("oh my git version 2.37.GIT", ()),
        ("git version 2.GIT", ()),
    ],
)
def test_get_git_version_parser(version: str, expected: tuple[int, int]) -> None:
    with mock.patch("kpip.vcs.git.Git.run_command", return_value=version):
        assert Git().get_git_version() == expected


@pytest.mark.parametrize(
    "use_interactive,is_atty,expected",
    [
        (None, False, False),
        (None, True, True),
        (False, False, False),
        (False, True, False),
        (True, False, True),
        (True, True, True),
    ],
)
@mock.patch("sys.stdin.isatty")
def test_subversion__init_use_interactive(
    mock_isatty: mock.Mock,
    use_interactive: bool,
    is_atty: bool,
    expected: bool,
) -> None:
    """Test Subversion.__init__() with mocked sys.stdin.isatty() output."""
    mock_isatty.return_value = is_atty
    svn = Subversion(use_interactive=use_interactive)
    assert svn.use_interactive == expected


@need_svn
def test_subversion__call_vcs_version() -> None:
    """Test Subversion.call_vcs_version() against local ``svn``."""
    version = Subversion().call_vcs_version()
    assert len(version) == 3
    for part in version:
        assert isinstance(part, int)
    assert version[0] >= 1


@pytest.mark.parametrize(
    "svn_output, expected_version",
    [
        (
            "svn, version 1.10.3 (r1842928)\n"
            "   compiled Feb 25 2019, 14:20:39 on x86_64-apple-darwin17.0.0",
            (1, 10, 3),
        ),
        (
            "svn, version 1.12.0-SlikSvn (SlikSvn/1.12.0)\n"
            "   compiled May 28 2019, 13:44:56 on x86_64-microsoft-windows6.2",
            (1, 12, 0),
        ),
        ("svn, version 1.9.7 (r1800392)", (1, 9, 7)),
        ("svn, version 1.9.7a1 (r1800392)", ()),
        ("svn, version 1.9 (r1800392)", (1, 9)),
        ("svn, version .9.7 (r1800392)", ()),
        ("svn version 1.9.7 (r1800392)", ()),
        ("svn 1.9.7", ()),
        ("svn, version . .", ()),
        ("", ()),
    ],
)
@mock.patch("kpip.vcs.subversion.Subversion.run_command")
def test_subversion__call_vcs_version_patched(
    mock_run_command: mock.Mock,
    svn_output: str,
    expected_version: tuple[int, ...],
) -> None:
    """Test Subversion.call_vcs_version() against patched output."""
    mock_run_command.return_value = svn_output
    version = Subversion().call_vcs_version()
    assert version == expected_version


@mock.patch("kpip.vcs.subversion.Subversion.run_command")
def test_subversion__call_vcs_version_svn_not_installed(
    mock_run_command: mock.Mock,
) -> None:
    """Test Subversion.call_vcs_version() when svn is not installed."""
    mock_run_command.side_effect = BadCommand
    with pytest.raises(BadCommand):
        Subversion().call_vcs_version()


@pytest.mark.parametrize(
    "version",
    [
        (),
        (1,),
        (1, 8),
        (1, 8, 0),
    ],
)
def test_subversion__get_vcs_version_cached(version: tuple[int, ...]) -> None:
    """Test Subversion.get_vcs_version() with previously cached result."""
    svn = Subversion()
    svn.vcs_version_internal = version
    assert svn.get_vcs_version() == version


@pytest.mark.parametrize(
    "vcs_version",
    [
        (),
        (1, 7),
        (1, 8, 0),
    ],
)
@mock.patch("kpip.vcs.subversion.Subversion.call_vcs_version")
def test_subversion__get_vcs_version_call_vcs(
    mock_call_vcs: mock.Mock,
    vcs_version: tuple[int, ...],
) -> None:
    """Test Subversion.get_vcs_version() with mocked output from
    call_vcs_version().
    """
    mock_call_vcs.return_value = vcs_version
    svn = Subversion()
    assert svn.get_vcs_version() == vcs_version

    assert svn.vcs_version_internal == vcs_version


@pytest.mark.parametrize(
    "use_interactive,vcs_version,expected_options",
    [
        (False, (), ["--non-interactive"]),
        (False, (1, 7, 0), ["--non-interactive"]),
        (False, (1, 8, 0), ["--non-interactive"]),
        (True, (), []),
        (True, (1, 7, 0), []),
        (True, (1, 8, 0), ["--force-interactive"]),
    ],
)
def test_subversion__get_remote_call_options(
    use_interactive: bool,
    vcs_version: tuple[int, ...],
    expected_options: list[str],
) -> None:
    """Test Subversion.get_remote_call_options()."""
    svn = Subversion(use_interactive=use_interactive)
    svn.vcs_version_internal = vcs_version
    assert svn.get_remote_call_options() == expected_options


class TestVcsArgs:
    @pytest.fixture(autouse=True)
    def setup_base(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path,
    ) -> None:
        self.dest = os.fspath(tmp_path / "dest")
        self.call_subprocess_mock = mock.MagicMock()
        monkeypatch.setattr(
            "kpip.vcs.versioncontrol.call_subprocess",
            self.call_subprocess_mock,
        )

    def assert_call_args(self, args: CommandArgs) -> None:
        assert self.call_subprocess_mock.call_args[0][0] == args


class TestBazaarArgs(TestVcsArgs):
    def setup_method(self) -> None:
        self.url = "bzr+http://username:password@bzr.example.com/"
        self.svn = Bazaar()
        self.rev_options = RevOptions(Bazaar)

    def test_fetch_new(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=1)
        self.assert_call_args(
            [
                "bzr",
                "checkout",
                "--lightweight",
                hide_url("bzr+http://username:password@bzr.example.com/"),
                self.dest,
            ],
        )

    def test_fetch_new_quiet(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=0)
        self.assert_call_args(
            [
                "bzr",
                "checkout",
                "--lightweight",
                "--quiet",
                hide_url("bzr+http://username:password@bzr.example.com/"),
                self.dest,
            ],
        )

    def test_fetch_new_very_verbose(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=2)
        self.assert_call_args(
            [
                "bzr",
                "checkout",
                "--lightweight",
                "-vv",
                hide_url("bzr+http://username:password@bzr.example.com/"),
                self.dest,
            ],
        )

    def test_update(self) -> None:
        self.svn.update(self.dest, hide_url(self.url), self.rev_options, verbosity=1)
        self.assert_call_args(
            [
                "bzr",
                "update",
            ],
        )

    def test_update_quiet(self) -> None:
        self.svn.update(self.dest, hide_url(self.url), self.rev_options, verbosity=0)
        self.assert_call_args(
            [
                "bzr",
                "update",
                "-q",
            ],
        )


class TestGitArgs(TestVcsArgs):
    def setup_method(self) -> None:
        self.url = "git+http://username:password@git.example.com/"
        self.svn = Git()
        self.rev_options = RevOptions(Git)

    def test_fetch_new(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(2, 17)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.fetch_new(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=1,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "clone",
            "--filter=blob:none",
            hide_url("git+http://username:password@git.example.com/"),
            self.dest,
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=1)

    def test_fetch_new_partial_clone_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KPIP_NO_PARTIAL_CLONE_FOR_BROKEN_GIT_SERVER", "1")
        with mock.patch.object(self.svn, "get_git_version", return_value=(2, 17)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.fetch_new(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=1,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "clone",
            hide_url("git+http://username:password@git.example.com/"),
            self.dest,
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=1)

    def test_fetch_new_legacy(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(1, 0)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.fetch_new(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=1,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "clone",
            hide_url("git+http://username:password@git.example.com/"),
            self.dest,
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=1)

    def test_fetch_new_legacy_quiet(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(1, 0)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.fetch_new(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=0,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "clone",
            "--quiet",
            hide_url("git+http://username:password@git.example.com/"),
            self.dest,
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=0)

    def test_fetch_new_quiet(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(2, 17)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.fetch_new(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=0,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "clone",
            "--filter=blob:none",
            "--quiet",
            hide_url("git+http://username:password@git.example.com/"),
            self.dest,
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=0)

    def test_switch(self) -> None:
        with mock.patch.object(self.svn, "update_submodules") as update_submodules_mock:
            self.svn.switch(
                self.dest,
                hide_url(self.url),
                self.rev_options,
                verbosity=1,
            )

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "git",
            "checkout",
            "HEAD",
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=1)

    def test_switch_quiet(self) -> None:
        with mock.patch.object(self.svn, "update_submodules") as update_submodules_mock:
            self.svn.switch(
                self.dest,
                hide_url(self.url),
                self.rev_options,
                verbosity=0,
            )

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "git",
            "checkout",
            "-q",
            "HEAD",
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=0)

    def test_update(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(1, 9)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.update(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=1,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "fetch",
            "--tags",
        ]

        assert self.call_subprocess_mock.call_args_list[2][0][0] == [
            "git",
            "reset",
            "--hard",
            "HEAD",
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=1)

    def test_update_legacy(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(1, 8)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.update(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=1,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "fetch",
        ]

        assert self.call_subprocess_mock.call_args_list[2][0][0] == [
            "git",
            "reset",
            "--hard",
            "HEAD",
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=1)

    def test_update_legacy_quiet(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(1, 9)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.update(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=0,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "fetch",
            "--tags",
            "-q",
        ]

        assert self.call_subprocess_mock.call_args_list[2][0][0] == [
            "git",
            "reset",
            "--hard",
            "-q",
            "HEAD",
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=0)

    def test_update_quiet(self) -> None:
        with mock.patch.object(self.svn, "get_git_version", return_value=(1, 8)):
            with mock.patch.object(
                self.svn,
                "update_submodules",
            ) as update_submodules_mock:
                self.svn.update(
                    self.dest,
                    hide_url(self.url),
                    self.rev_options,
                    verbosity=0,
                )

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "git",
            "fetch",
            "-q",
        ]

        assert self.call_subprocess_mock.call_args_list[2][0][0] == [
            "git",
            "reset",
            "--hard",
            "-q",
            "HEAD",
        ]

        update_submodules_mock.assert_called_with(self.dest, verbosity=0)


class TestMercurialArgs(TestVcsArgs):
    def setup_method(self) -> None:
        self.url = "hg+http://username:password@hg.example.com/"
        self.svn = Mercurial()
        self.rev_options = RevOptions(Mercurial)

    def test_fetch_new(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=1)

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "hg",
            "clone",
            "--noupdate",
            hide_url("hg+http://username:password@hg.example.com/"),
            self.dest,
        ]

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "hg",
            "update",
        ]

    def test_fetch_new_quiet(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=0)

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "hg",
            "clone",
            "--noupdate",
            "--quiet",
            hide_url("hg+http://username:password@hg.example.com/"),
            self.dest,
        ]

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "hg",
            "update",
            "--quiet",
        ]

    def test_fetch_new_very_verbose(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=2)

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "hg",
            "clone",
            "--noupdate",
            "--verbose",
            hide_url("hg+http://username:password@hg.example.com/"),
            self.dest,
        ]

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "hg",
            "update",
            "--verbose",
        ]

    def test_fetch_new_debug(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=3)

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "hg",
            "clone",
            "--noupdate",
            "--verbose",
            "--debug",
            hide_url("hg+http://username:password@hg.example.com/"),
            self.dest,
        ]

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "hg",
            "update",
            "--verbose",
            "--debug",
        ]

    def test_update(self) -> None:
        self.svn.update(self.dest, hide_url(self.url), self.rev_options, verbosity=1)

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "hg",
            "pull",
        ]

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "hg",
            "update",
        ]

    def test_update_quiet(self) -> None:
        self.svn.update(self.dest, hide_url(self.url), self.rev_options, verbosity=0)

        assert self.call_subprocess_mock.call_args_list[0][0][0] == [
            "hg",
            "pull",
            "-q",
        ]

        assert self.call_subprocess_mock.call_args_list[1][0][0] == [
            "hg",
            "update",
            "-q",
        ]


class TestSubversionArgs(TestVcsArgs):
    def setup_method(self) -> None:
        self.url = "svn+http://username:password@svn.example.com/"
        self.svn = Subversion(use_interactive=False)
        self.rev_options = RevOptions(Subversion)

    def test_obtain(self) -> None:
        self.svn.obtain(self.dest, hide_url(self.url), verbosity=1)
        self.assert_call_args(
            [
                "svn",
                "checkout",
                "--non-interactive",
                "--username",
                "username",
                "--password",
                hide_value("password"),
                hide_url("http://svn.example.com/"),
                self.dest,
            ],
        )

    def test_obtain_quiet(self) -> None:
        self.svn.obtain(self.dest, hide_url(self.url), verbosity=0)
        self.assert_call_args(
            [
                "svn",
                "checkout",
                "--quiet",
                "--non-interactive",
                "--username",
                "username",
                "--password",
                hide_value("password"),
                hide_url("http://svn.example.com/"),
                self.dest,
            ],
        )

    def test_fetch_new(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=1)
        self.assert_call_args(
            [
                "svn",
                "checkout",
                "--non-interactive",
                hide_url("svn+http://username:password@svn.example.com/"),
                self.dest,
            ],
        )

    def test_fetch_new_quiet(self) -> None:
        self.svn.fetch_new(self.dest, hide_url(self.url), self.rev_options, verbosity=0)
        self.assert_call_args(
            [
                "svn",
                "checkout",
                "--quiet",
                "--non-interactive",
                hide_url("svn+http://username:password@svn.example.com/"),
                self.dest,
            ],
        )

    def test_fetch_new_revision(self) -> None:
        rev_options = RevOptions(Subversion, "123")
        self.svn.fetch_new(self.dest, hide_url(self.url), rev_options, verbosity=1)
        self.assert_call_args(
            [
                "svn",
                "checkout",
                "--non-interactive",
                "-r",
                "123",
                hide_url("svn+http://username:password@svn.example.com/"),
                self.dest,
            ],
        )

    def test_fetch_new_revision_quiet(self) -> None:
        rev_options = RevOptions(Subversion, "123")
        self.svn.fetch_new(self.dest, hide_url(self.url), rev_options, verbosity=0)
        self.assert_call_args(
            [
                "svn",
                "checkout",
                "--quiet",
                "--non-interactive",
                "-r",
                "123",
                hide_url("svn+http://username:password@svn.example.com/"),
                self.dest,
            ],
        )

    def test_switch(self) -> None:
        self.svn.switch(self.dest, hide_url(self.url), self.rev_options)
        self.assert_call_args(
            [
                "svn",
                "switch",
                "--non-interactive",
                hide_url("svn+http://username:password@svn.example.com/"),
                self.dest,
            ],
        )

    def test_update(self) -> None:
        self.svn.update(self.dest, hide_url(self.url), self.rev_options)
        self.assert_call_args(
            [
                "svn",
                "update",
                "--non-interactive",
                self.dest,
            ],
        )
