"""Implementation of the ``kpip index`` command."""

from __future__ import annotations

lazy import json
lazy import logging

lazy from kpip.cli.config import load_source_config, resolve_sources
lazy from kpip.cli.package_finder import excludes_prereleases, package_finder
lazy from kpip.cli.parsers.index import create_parser
lazy from kpip.cli.requirement_command import target_context
lazy from kpip.core.errors import DistributionNotFound
lazy from kpip.core.packaging import parse_requirement

logger = logging.getLogger(__name__)


def run_index(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    provider = package_finder(
        options,
        resolve_sources(options, load_source_config("index")),
        target=target_context(options),
    )

    requirement = parse_requirement(options.package)

    # What could be installed for the target: a release with nothing usable
    # for it, or none the format and upload-time options admit, is not listed.
    versions = {
        candidate.version for candidate in provider.evaluate_links(requirement).accepted
    }

    if excludes_prereleases(options, requirement.name):
        versions = {version for version in versions if not version.is_prerelease}

    if not versions:
        raise DistributionNotFound(
            f"No matching distribution found for {options.package}"
        )

    available = [str(version) for version in sorted(versions, reverse=True)]

    latest = available[0]

    if options.json:
        logger.info(
            json.dumps(
                {"name": requirement.name, "versions": available, "latest": latest},
            ),
        )

        return 0

    logger.info(f"{requirement.name} ({latest})")

    logger.info(f"Available versions: {', '.join(available)}")

    return 0
