"""Implementation of the ``kpip wheel`` command."""

from __future__ import annotations

import logging
import os
import shutil

from kpip.build.build import build_wheel_from_source
from kpip.cli.parsers.wheel import create_parser
from kpip.cli.requirement_command import prepare, resolve_requirements
from kpip.cli.requirements import apply_proxy_environment
from kpip.core.wheel import wheel_candidate_from_path

logger = logging.getLogger(__name__)


def run_wheel(args: list[str]) -> int:
    parser = create_parser()
    options = parser.parse_args([arg for arg in args if arg])

    apply_proxy_environment(options.proxy)

    resolved = resolve_requirements(
        prepare("wheel", options, parser), with_editables=True
    )
    plan = resolved.plan
    build_options = resolved.build_options

    wheel_dir = os.fspath(options.wheel_dir)

    os.makedirs(wheel_dir, exist_ok=True)

    built_names: list[str] = []

    for candidate in plan.candidates:
        source = candidate.path

        if os.path.splitext(os.fspath(source))[1] != ".whl":
            source = build_wheel_from_source(
                source,
                wheel_dir=wheel_dir,
                config_settings=build_options.get(candidate.source_url or ""),
                build_constraints=options.build_constraint_files,
                build_isolation=not options.no_build_isolation,
            )

        else:
            destination = os.path.join(wheel_dir, os.path.basename(source))

            if os.path.realpath(source) != os.path.realpath(
                destination,
            ):
                shutil.copy2(source, destination)

            source = destination

        built_names.append(wheel_candidate_from_path(source).name)

    if built_names:
        logger.info(f"Successfully built {' '.join(sorted(set(built_names)))}")

    return 0
