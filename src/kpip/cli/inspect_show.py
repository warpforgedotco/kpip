"""Implementation of the ``kpip show`` subcommand."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run_show(args: list[str]) -> int:
    from kpip.cli.parsers.inspection import create_show_parser

    options = create_show_parser().parse_args(args)

    if not options.packages:
        logger.error("Please provide a package name or names.")
        return 1

    from kpip.build import query
    from kpip.core import packaging

    infos = {
        info.distribution.canonical_name: info
        for info in query.iter_installed_package_info(
            options.packages,
            include_files=options.files,
        )
    }

    missing = sorted(
        package
        for package in options.packages
        if packaging.canonicalize_name(package) not in infos
    )

    printed = 0
    for package in options.packages:
        info = infos.get(packaging.canonicalize_name(package))
        if info is None:
            continue

        dist = info.distribution
        if printed:
            logger.info("---")
        printed += 1

        metadata = dist.metadata
        project_urls = metadata.get_all("Project-URL", [])

        logger.info(f"Name: {dist.raw_name}")
        logger.info(f"Version: {dist.raw_version}")
        logger.info(f"Summary: {metadata.get('Summary', '')}")
        logger.info(f"Home-page: {info.homepage}")
        logger.info(f"Author: {metadata.get('Author', '')}")
        logger.info(f"Author-email: {metadata.get('Author-email', '')}")

        metadata_version = dist.metadata_version or ""
        parts = metadata_version.split(".") if metadata_version else []
        metadata_version_tuple = (
            tuple(int(part) for part in parts)
            if parts and all(part.isdigit() for part in parts)
            else ()
        )

        if metadata_version_tuple >= (2, 4) and metadata.get("License-Expression"):
            logger.info(f"License-Expression: {metadata.get('License-Expression', '')}")
        else:
            logger.info(f"License: {metadata.get('License', '')}")

        logger.info(f"Location: {dist.location}")
        if dist.editable and dist.editable_project_location is not None:
            logger.info(f"Editable project location: {dist.editable_project_location}")

        logger.info(f"Requires: {', '.join(info.requires)}")
        logger.info(f"Required-by: {', '.join(info.required_by)}")

        if options.verbose:
            logger.info(f"Metadata-Version: {dist.metadata_version or ''}")
            logger.info(f"Installer: {dist.installer}")
            logger.info("Classifiers:")
            for classifier in metadata.get_all("Classifier", []):
                logger.info(f"  {classifier}")
            logger.info("Entry-points:")
            for entry_point in info.entry_points:
                logger.info(f"  {entry_point}")
            logger.info("Project-URLs:")
            for project_url in project_urls:
                logger.info(f"  {project_url}")

        if options.files:
            logger.info("Files:")
            files = info.files or []
            if files:
                for filename in files:
                    logger.info(f"  {filename}")
            else:
                logger.info("Cannot locate RECORD or installed-files.txt")
            logger.info("")

    if missing:
        logger.warning(f"Package(s) not found: {', '.join(missing)}")
        return 1 if len(missing) == len(options.packages) else 0

    return 0
