"""Implementation of the ``kpip list`` command."""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from kpip.build.query import (
    format_list_columns,
    format_list_freeze,
    format_list_json,
    select_installed_distributions,
)
from kpip.cli.config import load_source_config, resolve_sources
from kpip.cli.package_finder import (
    check_release_control,
    excludes_prereleases,
    package_finder,
)
from kpip.cli.parsers.list import create_parser
from kpip.cli.target import target_paths
from kpip.core.metadata import stdlib_pkgs, user_lib_path
from kpip.core.packaging import parse_requirement

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)


def run_list(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    check_release_control(options)

    if options.outdated and options.uptodate:
        logger.error("Options --outdated and --uptodate cannot be combined.")

        return 1

    if options.outdated and options.format == "freeze":
        logger.error("List format 'freeze' cannot be used with the --outdated option.")

        return 1

    distributions = select_installed_distributions(
        paths=options.path or target_paths(),
        local_only=options.local,
        user_only=options.user,
        editables_only=options.editable,
        include_editables=options.include_editable,
        excludes=options.exclude,
        not_required=options.not_required,
        skip=stdlib_pkgs,
        user_site=str(user_lib_path()),
    )

    latest: dict[str, tuple[Any, str]] = {}

    if options.outdated or options.uptodate:
        candidate_provider = package_finder(
            options,
            resolve_sources(options, load_source_config("list")),
        )

        for dist in distributions:
            candidates = candidate_provider.evaluate_links(
                parse_requirement(dist.raw_name),
            ).accepted

            if excludes_prereleases(options, dist.raw_name):
                candidates = [
                    candidate
                    for candidate in candidates
                    if not candidate.version.is_prerelease
                ]

            if not candidates:
                continue

            # The one that would be installed: with --prefer-binary, a wheel
            # before a newer source distribution.
            candidate = max(
                candidates,
                key=lambda item: item.sort_key(prefer_binary=options.prefer_binary),
            )

            latest[dist.canonical_name] = (candidate.version, candidate.link.kind.value)

        if options.outdated:
            distributions = [
                dist
                for dist in distributions
                if dist.canonical_name in latest
                and latest[dist.canonical_name][0] > dist.version
            ]

        else:
            distributions = [
                dist
                for dist in distributions
                if dist.canonical_name in latest
                and latest[dist.canonical_name][0] == dist.version
            ]

    distributions.sort(key=lambda dist: dist.canonical_name)

    if options.format == "json":
        logger.info(
            format_list_json(
                distributions,
                outdated=options.outdated,
                verbose=options.verbose > 0,
                latest=latest,
            ),
        )

        return 0

    if options.format == "freeze":
        for requirement in format_list_freeze(
            distributions,
            verbose=options.verbose > 0,
        ):
            logger.info(requirement)

        return 0

    rows, header = format_list_columns(
        distributions,
        outdated=options.outdated,
        verbose=options.verbose > 0,
        latest=latest,
    )

    # As pip: an environment with nothing to list prints nothing, not a
    # header over no rows.
    if not rows:
        return 0

    rows.insert(0, header)

    widths = [
        max(len(str(row[i])) if i < len(row) else 0 for row in rows)
        for i in range(len(rows[0]))
    ]

    lines = [
        " ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)).rstrip()
        for row in rows
    ]

    # pip rules the header off from the rows.
    lines.insert(1, " ".join("-" * width for width in widths))

    logger.info("\n".join(lines))

    return 0
