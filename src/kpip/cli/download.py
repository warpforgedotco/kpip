"""Implementation of the ``kpip download`` command."""

from __future__ import annotations

lazy import logging
lazy import os
lazy import shutil
lazy import sys
lazy from typing import Any

lazy from kpip.build.build import build_wheel_from_source
lazy from kpip.cli.parsers.download import create_parser
lazy from kpip.cli.requirement_command import (
    check_dist_restriction,
    prepare,
    resolve_requirements,
)
lazy from kpip.cli.requirements import apply_proxy_environment
lazy from kpip.index.artifacts import ArtifactLocator
lazy from kpip.install.metadata import prepare_editable_source
lazy from kpip.install.output import fetch_candidate_sources

logger = logging.getLogger(__name__)


def run_download(args: list[str]) -> int:
    parser = create_parser()
    options = parser.parse_args(args)

    apply_proxy_environment(options.proxy)

    prepared = prepare(
        "download",
        options,
        parser,
        validate=lambda: check_dist_restriction(options),
    )
    bundle = prepared.bundle
    cache_dir = prepared.cache_dir

    plan = resolve_requirements(prepared).plan

    destination = os.fspath(options.dest)

    os.makedirs(destination, exist_ok=True)

    names: list[str] = []

    for editable in bundle.editables:
        source_path, _, _ = prepare_editable_source(
            editable, prepare_metadata=False, src_dir=options.src_dir
        )

        wheel = build_wheel_from_source(
            source_path,
            wheel_dir=destination,
            build_constraints=options.build_constraint_files,
            build_isolation=not options.no_build_isolation,
        )

        names.append(os.path.basename(wheel).split("-", 1)[0])

    artifact_locator = ArtifactLocator(bundle.session, cache_dir=cache_dir)

    def fetch_source(candidate: Any) -> str:
        source = candidate.path

        if candidate.source_kind == "sdist" and candidate.source_url is not None:
            source = artifact_locator.ensure_local(candidate.source_url)

            if candidate.canonical_name == "setuptools":
                source = candidate.path

        return os.fspath(source)

    candidates = list(plan.candidates)

    for candidate, source_text in zip(
        candidates, fetch_candidate_sources(candidates, fetch_source)
    ):
        shutil.copy2(
            source_text, os.path.join(destination, os.path.basename(source_text))
        )

        names.append(candidate.name)

    if names:
        message = f"Successfully downloaded {' '.join(sorted(names))}"

        if (
            sys.stdout.isatty()
            and not options.no_color
            and "NO_COLOR" not in os.environ
        ):
            message = f"\x1b[32m{message}\x1b[0m"

        logger.info(message)

    return 0
