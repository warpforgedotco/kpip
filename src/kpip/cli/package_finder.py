"""pip's package index and package selection options, read.

Every command that looks packages up takes them -- ``index``, ``list`` and
the commands that resolve requirements -- and means the same by them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import time

from kpip.core.errors import CommandError
from kpip.core.expiry import refresh_since
from kpip.core.format_control import FormatControl
from kpip.core.release_control import ReleaseControl

if TYPE_CHECKING:
    import argparse
    from kpip.cli.config import SourceConfig
    from kpip.core.wheel import TargetContext
    from kpip.index.provider import CandidateProvider

RELEASE_OPTIONS = frozenset(("pre", "all-releases"))


def format_control(options: argparse.Namespace) -> FormatControl:
    control = FormatControl()
    for option, value in options.format_control:
        control.apply(option, value)
    return control


def release_control(options: argparse.Namespace) -> list[tuple[str, str]]:
    """``--all-releases`` and ``--only-final`` in the order given; ``--pre``
    is ``--all-releases :all:``."""
    control = list(options.release_control)
    if options.pre:
        control.append(("pre", ":all:"))
    return control


def release_control_from(args: list[tuple[str, str]]) -> ReleaseControl:
    """What ``(option, value)`` pairs of release control add up to, in order."""
    control = ReleaseControl()
    for kind, value in args:
        control.apply(
            "all_releases" if kind in RELEASE_OPTIONS else "only_final",
            value,
        )
    return control


def check_release_control(options: argparse.Namespace) -> None:
    if options.pre and options.release_control:
        raise CommandError("--pre cannot be used with --all-releases or --only-final")


def excludes_prereleases(options: argparse.Namespace, project_name: str) -> bool:
    """As pip: a pre-release counts only where release control allows it."""
    control = release_control_from(release_control(options))
    return control.allows_prereleases(project_name) is not True


def apply_refresh(options: argparse.Namespace) -> None:
    """Revalidate what the index said before it is believed again.

    ``--refresh-package`` names the projects to do that for; their pages are
    not told apart from the rest where freshness is decided, so naming any
    revalidates every page, which is more than was asked and never less.
    """
    if getattr(options, "refresh", False) or options.refresh_package:
        refresh_since(time.time())


def package_finder(
    options: argparse.Namespace,
    sources: SourceConfig,
    *,
    target: TargetContext | None = None,
) -> CandidateProvider:
    """A provider that looks where the options say and selects as they say."""
    check_release_control(options)
    apply_refresh(options)

    from kpip.index.provider import CandidateProvider

    provider = CandidateProvider.from_options(
        find_links=sources.find_links,
        index_url=sources.index_url,
        extra_index_urls=sources.extra_index_urls,
        no_index=sources.no_index,
        format_control=format_control(options),
        prefer_binary=options.prefer_binary,
        trusted_hosts=options.trusted_hosts,
        uploaded_prior_to=options.uploaded_prior_to,
        ignore_requires_python=getattr(options, "ignore_requires_python", False),
        target=target,
    )
    provider.release_control = release_control_from(release_control(options))

    return provider
