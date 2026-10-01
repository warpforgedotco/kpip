"""Implementation of the ``kpip check`` subcommand."""

from __future__ import annotations

import logging
import sys

from kpip.build import query
from kpip.build.metadata import InstalledDistributionStore
from kpip.cli.parsers.inspection import create_check_parser
from kpip.core import packaging, target_python

logger = logging.getLogger(__name__)


def run_check(args: list[str]) -> int:
    create_check_parser().parse_args(args)

    distributions = InstalledDistributionStore().iter()
    package_set = query.package_set_from_dependencies(
        distributions,
        query.installed_dependencies_by_name(distributions),
    )

    errors = query.metadata_errors(distributions)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1

    def supported_tags():  # noqa: ANN202
        return target_python.get_supported()

    unsupported = [
        f"{dist.raw_name} {dist.raw_version} is not supported on this platform"
        for dist in query.unsupported_distributions(distributions, supported_tags)
    ]

    missing, conflicting = query.check_package_set(package_set)

    if not missing and not conflicting and not unsupported:
        logger.info("No broken requirements found.")
        return 0

    for line in unsupported:
        logger.info(line)

    by_name = {dist.canonical_name: dist for dist in distributions}

    for name, requirements in sorted(missing.items()):
        distribution = by_name[packaging.canonicalize_name(name)]
        for _, requirement in requirements:
            logger.info(
                f"{name} {distribution.raw_version} requires "
                f"{packaging.canonicalize_name(requirement.name)}, which is not installed.",
            )

    for name, requirements in sorted(conflicting.items()):
        distribution = by_name[packaging.canonicalize_name(name)]
        for conflict_name, version, requirement in requirements:
            logger.info(
                f"{name} {distribution.raw_version} has requirement {requirement}, "
                f"but you have {conflict_name} {version}.",
            )

    return 1
