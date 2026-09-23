from __future__ import annotations

import os
import re
import sys

from kpip.core.logger import get_logger
from kpip.core.errors import InstallationError
from kpip.core.subprocesses import CommandArgs
from kpip.core.urls import split_auth_from_netloc
from kpip.core.utils import AuthInfo, display_path

from .subprocesses import make_command
from .support import (
    HiddenText,
    is_installable_dir,
)
from .versioncontrol import (
    RemoteNotFoundError,
    RevOptions,
    VersionControl,
    vcs,
)

logger = get_logger(__name__)

svn_xml_url_re = re.compile('url="([^"]+)"')
svn_rev_re = re.compile(r'committed-rev="(\d+)"')
svn_info_xml_rev_re = re.compile(r'\s*revision="(\d+)"')
svn_info_xml_url_re = re.compile(r"<url>(.*)</url>")


class Subversion(VersionControl):
    name = "svn"
    dirname = ".svn"
    repo_name = "checkout"
    schemes = ("svn+ssh", "svn+http", "svn+https", "svn+svn", "svn+file")

    @classmethod
    def should_add_vcs_url_prefix(cls, remote_url: str) -> bool:
        return True

    @staticmethod
    def get_base_rev_args(rev: str) -> list[str]:
        return ["-r", rev]

    @classmethod
    def get_revision(cls, location: str) -> str:
        """Return the maximum revision for all files under a given location"""
        revision = 0

        for base, dirs, _ in os.walk(location):
            if cls.dirname not in dirs:
                dirs[:] = []
                continue
            dirs.remove(cls.dirname)
            dirurl, localrev = cls.get_svn_url_rev(base)
            if dirurl is None:
                continue

            if base == location:
                assert dirurl is not None
                base = dirurl + "/"
            elif not dirurl or not dirurl.startswith(base):
                dirs[:] = []
                continue
            revision = max(revision, localrev)
        return str(revision)

    @classmethod
    def get_netloc_and_auth(
        cls,
        netloc: str,
        scheme: str,
    ) -> tuple[str, tuple[str | None, str | None]]:
        """This override allows the auth information to be passed to svn via the
        --username and --password options instead of via the URL.
        """
        if scheme == "ssh":
            return super().get_netloc_and_auth(netloc, scheme)

        return split_auth_from_netloc(netloc)

    @classmethod
    def get_url_rev_and_auth(cls, url: str) -> tuple[str, str | None, AuthInfo]:
        url, rev, user_pass = super().get_url_rev_and_auth(url)
        if url.startswith("ssh://"):
            url = "svn+" + url
        return url, rev, user_pass

    @staticmethod
    def make_rev_args(username: str | None, password: HiddenText | None) -> CommandArgs:
        extra_args: CommandArgs = []
        if username:
            extra_args += ["--username", username]
        if password:
            extra_args += ["--password", password]

        return extra_args

    @classmethod
    def get_remote_url(cls, location: str) -> str:
        orig_location = location
        while not is_installable_dir(location):
            last_location = location
            location = os.path.dirname(location)
            if location == last_location:
                logger.warning(
                    "Could not find Python project for directory %s (tried all "
                    "parent directories)",
                    orig_location,
                )
                raise RemoteNotFoundError

        url, rev_internal = cls.get_svn_url_rev(location)
        if url is None:
            raise RemoteNotFoundError

        return url

    @classmethod
    def get_svn_url_rev(cls, location: str) -> tuple[str | None, int]:
        entries_path = os.path.join(location, cls.dirname, "entries")
        try:
            with open(entries_path) as f:
                data = f.read()
        except FileNotFoundError:
            data = ""

        url = None
        if data.startswith(("8", "9", "10")):
            entries = list(map(str.splitlines, data.split("\n\x0c\n")))
            del entries[0][0]
            url = entries[0][3]
            revs = [int(d[9]) for d in entries if len(d) > 9 and d[9]] + [0]
        elif data.startswith("<?xml"):
            match = svn_xml_url_re.search(data)
            if not match:
                raise ValueError(f"Badly formatted data: {data!r}")
            url = match.group(1)
            revs = [int(m.group(1)) for m in svn_rev_re.finditer(data)] + [0]
        else:
            try:
                xml = cls.run_command(
                    ["info", "--xml", location],
                    show_stdout=False,
                    stdout_only=True,
                )
                match = svn_info_xml_url_re.search(xml)
                assert match is not None
                url = match.group(1)
                revs = [int(m.group(1)) for m in svn_info_xml_rev_re.finditer(xml)]
            except InstallationError:
                url, revs = None, []

        if revs:
            rev = max(revs)
        else:
            rev = 0

        return url, rev

    def __init__(self, use_interactive: bool | None = None) -> None:
        if use_interactive is None:
            use_interactive = sys.stdin is not None and sys.stdin.isatty()
        self.use_interactive = use_interactive

        self.vcs_version_internal: tuple[int, ...] | None = None

        super().__init__()

    def call_vcs_version(self) -> tuple[int, ...]:
        """Query the version of the currently installed Subversion client.

        :return: A tuple containing the parts of the version information or
            ``()`` if the version returned from ``svn`` could not be parsed.
        :raises: BadCommand: If ``svn`` is not installed.
        """
        version_prefix = "svn, version "
        version = self.run_command(["--version"], show_stdout=False, stdout_only=True)
        if not version.startswith(version_prefix):
            return ()

        version = version[len(version_prefix) :].split()[0]
        version_list = version.partition("-")[0].split(".")
        try:
            parsed_version = tuple(map(int, version_list))
        except ValueError:
            return ()

        return parsed_version

    def get_vcs_version(self) -> tuple[int, ...]:
        """Return the version of the currently installed Subversion client.

        If the version of the Subversion client has already been queried,
        a cached value will be used.

        :return: A tuple containing the parts of the version information or
            ``()`` if the version returned from ``svn`` could not be parsed.
        :raises: BadCommand: If ``svn`` is not installed.
        """
        if self.vcs_version_internal is not None:
            return self.vcs_version_internal

        vcs_version = self.call_vcs_version()
        self.vcs_version_internal = vcs_version
        return vcs_version

    def get_remote_call_options(self) -> CommandArgs:
        """Return options to be used on calls to Subversion that contact the server.

        These options are applicable for the following ``svn`` subcommands used
        in this class.

            - checkout
            - switch
            - update

        :return: A list of command line arguments to pass to ``svn``.
        """
        if not self.use_interactive:
            return ["--non-interactive"]

        svn_version = self.get_vcs_version()
        if svn_version >= (1, 8):
            return ["--force-interactive"]

        return []

    def fetch_new(
        self,
        dest: str,
        url: HiddenText,
        rev_options: RevOptions,
        verbosity: int,
    ) -> None:
        rev_display = rev_options.to_display()
        logger.info(
            "Checking out %s%s to %s",
            url,
            rev_display,
            display_path(dest),
        )
        if verbosity <= 0:
            flags = ["--quiet"]
        else:
            flags = []
        cmd_args = make_command(
            "checkout",
            *flags,
            self.get_remote_call_options(),
            rev_options.to_args(),
            url,
            dest,
        )
        self.run_command(cmd_args)

    def switch(
        self,
        dest: str,
        url: HiddenText,
        rev_options: RevOptions,
        verbosity: int = 0,
    ) -> None:
        cmd_args = make_command(
            "switch",
            self.get_remote_call_options(),
            rev_options.to_args(),
            url,
            dest,
        )
        self.run_command(cmd_args)

    def update(
        self,
        dest: str,
        url: HiddenText,
        rev_options: RevOptions,
        verbosity: int = 0,
    ) -> None:
        cmd_args = make_command(
            "update",
            self.get_remote_call_options(),
            rev_options.to_args(),
            dest,
        )
        self.run_command(cmd_args)


vcs.register(Subversion)
