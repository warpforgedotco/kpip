"""Implementation of the ``kpip list`` command."""

from __future__ import annotations


from kpip.build.query import (
    format_list_columns,
    format_list_freeze,
    format_list_json,
    select_installed_distributions,
)
from kpip.cli.parsers.list import create_parser
from kpip.cli.target import target_paths
from kpip.core.metadata import stdlib_pkgs, user_lib_path

from kpip.core.logger import get_logger

logger = get_logger(__name__)

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any


def run_list(args: list[str]) -> int:
    options = create_parser().parse_args(args)

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
        from kpip.cli import config
        from kpip.core import format_control, packaging
        from kpip.index import provider

        sources = config.resolve_sources(options, config.load_source_config("list"))

        candidate_provider = provider.CandidateProvider.from_options(
            find_links=sources.find_links,
            index_url=sources.index_url,
            extra_index_urls=sources.extra_index_urls,
            no_index=sources.no_index,
            format_control=format_control.FormatControl(),
        )

        assert candidate_provider.release_control is not None

        for value in options.all_releases:
            candidate_provider.release_control.apply("all_releases", value)

        for value in options.only_final:
            candidate_provider.release_control.apply("only_final", value)

        for dist in distributions:
            candidates = candidate_provider.evaluate_links(
                packaging.parse_requirement(dist.raw_name),
            ).accepted

            allow_prereleases = candidate_provider.release_control.allows_prereleases(
                dist.raw_name,
            )

            if not options.pre and allow_prereleases is not True:
                candidates = [
                    candidate
                    for candidate in candidates
                    if not candidate.version.is_prerelease
                ]

            if not candidates:
                continue

            candidate = max(candidates, key=lambda item: item.version)

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
