"""Implementation of the ``kpip download`` command."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from kpip.build.build import build_wheel_from_source
from kpip.cli.config import load_source_config, resolve_sources
from kpip.cli.dependency_groups import group_items, parse_dependency_groups
from kpip.cli.parsers.download import create_parser
from kpip.cli.requirements import (
    apply_proxy_environment,
    bundle_install_requirements,
    collect_requirements,
)
from kpip.cli.resolution_errors import resolution_error_message
from kpip.core.appdirs import command_cache_dir
from kpip.core.errors import DistributionNotFound, ResolutionError
from kpip.core.format_control import FormatControl
from kpip.index.artifacts import ArtifactLocator
from kpip.index.provider import CandidateProvider
from kpip.install.metadata import prepare_editable_source
from kpip.install.output import fetch_candidate_sources
from kpip.resolution.api import ResolutionEngine


def run_download(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    apply_proxy_environment(options.proxy)

    cache_dir = command_cache_dir(options.cache_dir, options.no_cache_dir)

    sources = resolve_sources(options, load_source_config("download"))

    grouped_requirements = parse_dependency_groups(group_items(options.groups))

    bundle = collect_requirements(
        requirements=[*options.requirements, *grouped_requirements],
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        find_links=sources.find_links,
        index_url=sources.index_url,
        extra_index_urls=sources.extra_index_urls,
        no_index=sources.no_index,
        format_control=FormatControl(),
        cert=options.cert,
        client_cert=options.client_cert,
        proxy=options.proxy,
        cache_dir=cache_dir,
    )

    provider = CandidateProvider.from_options(
        find_links=bundle.find_links,
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        no_index=bundle.no_index,
        format_control=bundle.format_control,
        trusted_hosts=options.trusted_hosts,
        session=bundle.session,
        wheel_cache_dir=cache_dir,
    )

    requirements = bundle_install_requirements(bundle)
    try:
        plan = ResolutionEngine(
            provider=provider,
            constraints=bundle.constraints,
            ignore_installed=True,
        ).resolve(requirements)
    except (DistributionNotFound, ResolutionError) as exc:
        detail = resolution_error_message(str(exc), requirements, [])
        raise DistributionNotFound(detail) from exc

    destination = os.fspath(options.dest)

    os.makedirs(destination, exist_ok=True)

    names: list[str] = []

    for editable in bundle.editables:
        source_path, _, _ = prepare_editable_source(editable, prepare_metadata=False)

        wheel = build_wheel_from_source(
            source_path,
            wheel_dir=destination,
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

        print(message)

    return 0
