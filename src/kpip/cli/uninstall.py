"""Implementation of the ``kpip uninstall`` command."""

from __future__ import annotations

import logging
import os
from kpip.core.metadata import user_lib_path

from kpip.build.metadata import InstalledDistributionStore
from kpip.cli.parsers.uninstall import create_parser
from kpip.core.packaging import parse_requirement
from kpip.host.environment_checks import (
    check_externally_managed,
    check_system_python,
    warn_if_run_as_root,
)
from kpip.install.requirements import RequirementInstaller

logger = logging.getLogger(__name__)


def run_uninstall(args: list[str]) -> int:
    parser = create_parser()

    options = parser.parse_args(args)

    if options.system:
        # Before the target is looked for: --system changes where.
        os.environ["KPIP_SYSTEM_PYTHON"] = "1"

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

    check_system_python()
    if not options.break_system_packages:
        check_externally_managed()

    removed: list[str] = []

    for package in packages:
        distribution = InstalledDistributionStore(
            user_site=user_lib_path(),
        ).find(package)

        if options.verbose and distribution is not None:
            location = distribution.location

            parent = os.path.dirname(
                os.path.dirname(os.path.dirname(location)),
            )

            if parent != os.path.dirname(parent):
                scripts = "Scripts" if os.name == "nt" else "bin"

                logger.info(f"Uninstalling files from {os.path.join(parent, scripts)}")

        removed_now = RequirementInstaller().uninstall(package)

        if removed_now:
            removed.append(package)

    for package in removed:
        logger.info(f"Successfully uninstalled {package}")

    if options.root_user_action == "warn":
        warn_if_run_as_root()

    return 0
