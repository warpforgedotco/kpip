"""Implementation of the ``kpip uninstall`` command."""

from __future__ import annotations

import os
import site

from kpip.build.metadata import InstalledDistributionStore
from kpip.cli.parsers.uninstall import create_parser
from kpip.cli.target import target_paths
from kpip.core.packaging import parse_requirement
from kpip.install.requirements import RequirementInstaller

import logging

logger = logging.getLogger(__name__)


def run_uninstall(args: list[str]) -> int:
    parser = create_parser()

    options = parser.parse_args(args)

    packages = list(options.packages)

    for filename in options.requirement_files:
        with open(filename, encoding="utf-8") as file:
            lines = file.read().splitlines()

        for line in lines:
            requirement = line.partition("#")[0].strip()

            if not requirement:
                continue

            packages.append(parse_requirement(requirement).name)

    if not packages:
        parser.error("You must give at least one package to uninstall")

    removed: list[str] = []

    for package in packages:
        paths = target_paths()

        distribution = InstalledDistributionStore(
            paths=paths,
            user_site=site.getusersitepackages(),
        ).find(package)

        if options.verbose and distribution is not None:
            location = distribution.location

            parent = os.path.dirname(
                os.path.dirname(os.path.dirname(location)),
            )

            if parent != os.path.dirname(parent):
                scripts = "Scripts" if os.name == "nt" else "bin"

                logger.info(f"Uninstalling files from {os.path.join(parent, scripts)}")

        if paths is None:
            removed_now = RequirementInstaller().uninstall(package)

        else:
            removed_now = RequirementInstaller().uninstall(package, paths=paths)

        if removed_now:
            removed.append(package)

    for package in removed:
        logger.info(f"Successfully uninstalled {package}")

    return 0
