"""Public orchestration for the canonical resolution engine."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Mapping

from kpip.core import errors
from kpip.core.metadata import installed_index
from kpip.core.versions import ZERO_VERSION
from kpip.index.provider import CandidateProvider
from kpip.resolution.inputs import (
    coerce_requirements,
)
from kpip.resolution.models import (
    ResolutionConfig,
    ResolutionResult,
    ResolvedRequirement,
)
from kpip.resolution.nab_provider import NabProvider
from kpip.resolution.nab_types import InstalledCandidate

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

    from kpip.install.requirement_set import RequirementSet
    from kpip.resolution.req_install import InstallRequirement


class ResolutionEngine:
    """Canonical resolution entry point.



    The current generic search implementation is used as the authoritative

    state machine while candidate-source and propagation boundaries are

    migrated behind this API.  Keeping orchestration here small prevents the

    public entry point from becoming another resolver context.

    """

    def __init__(
        self,
        *,
        config: ResolutionConfig | None = None,
        **kwargs: Any,
    ) -> None:
        if config is None:
            index_urls = kwargs.pop("index_urls", None)

            config = ResolutionConfig(
                find_links=tuple(kwargs.pop("find_links", ()) or ()),
                index_urls=tuple(index_urls) if index_urls is not None else None,
                no_index=kwargs.pop("no_index", False),
                no_deps=kwargs.pop("no_deps", False),
                upgrade=kwargs.pop("upgrade", False),
                ignore_installed=kwargs.pop("ignore_installed", False),
                constraints=tuple(kwargs.pop("constraints", ()) or ()),
                allow_prereleases=kwargs.pop("allow_prereleases", False),
                require_hashes=kwargs.pop("require_hashes", False),
                compute_source_hashes=kwargs.pop("compute_source_hashes", True),
                upgrade_strategy=kwargs.pop("upgrade_strategy", "only-if-needed"),
                ignore_requires_python=kwargs.pop("ignore_requires_python", False),
                python_version=kwargs.pop("python_version", None),
                preferences=kwargs.pop("preferences", None),
            )

        self.config = config

        provider = kwargs.pop("provider", None)
        if provider is None:
            session = kwargs.pop("session", None)
            provider = CandidateProvider.from_options(
                find_links=list(config.find_links),
                index_url=config.index_urls[0] if config.index_urls else None,
                extra_index_urls=config.index_urls[1:] if config.index_urls else (),
                no_index=config.no_index,
                session=session,
            )
        self.provider = provider

        if kwargs:
            unexpected = ", ".join(sorted(kwargs))

            raise TypeError(f"unexpected resolution options: {unexpected}")

    def resolve(
        self,
        requirements_input: RequirementSet[InstallRequirement]
        | Iterable[InstallRequirement]
        | list[str],
    ) -> ResolutionResult:
        from kpip._vendor.nab_resolver.resolver import Resolver

        requirements = coerce_requirements(requirements_input)
        installed = {} if self.config.ignore_installed else installed_index()
        adapter = NabProvider(
            self.provider,
            context=self.config,
            installed=installed,
        )
        roots = adapter.add_roots(requirements)
        resolver = Resolver(adapter, root_version=ZERO_VERSION)
        try:
            solution = resolver.solve(roots)
        except Exception as error:
            from kpip._vendor.nab_resolver.errors import ResolutionError
            from kpip._vendor.nab_resolver.report import format_error

            if isinstance(error, ResolutionError):
                message = (
                    format_error(
                        error.incompatibility,
                        narrow=adapter.narrow_for_display,
                    )
                    if error.incompatibility is not None
                    else str(error)
                )

                def restore_requirement(match: re.Match[str]) -> str:
                    name = match.group(1)
                    requirement = adapter.display_requirements.get(
                        name
                    ) or adapter.requirements.get(name)
                    if requirement is None or not str(requirement.specifier):
                        return match.group(0)
                    return f"depends on {name}{requirement.specifier}"

                message = re.sub(
                    r"depends on ([A-Za-z0-9_.-]+)(?: <empty>)?",
                    restore_requirement,
                    message,
                )
                raise errors.ResolutionError(message) from error
            raise
        selected = solution.pins
        selected_records = tuple(
            (package, adapter.records[(package, version)])
            for package, version in selected.items()
        )
        graph_children: dict[str, set[str]] = {package: set() for package in selected}
        for package, dependency in solution.edges:
            graph_children[package].add(dependency)
        graph = {
            package: frozenset(dependencies)
            for package, dependencies in graph_children.items()
        }
        satisfied = tuple(
            ResolvedRequirement(adapter.requirements[package], record.distribution)
            for package, record in selected_records
            if isinstance(record, InstalledCandidate)
        )
        candidates = tuple(
            record
            for _, record in selected_records
            if not isinstance(record, InstalledCandidate)
        )
        result = ResolutionResult(
            candidates=candidates,
            graph=graph,
            satisfied=satisfied,
            metrics={
                "nab_rounds": resolver.stats.rounds,
                "nab_conflicts": resolver.stats.conflicts,
            },
        )

        if os.environ.get("KPIP_RESOLUTION_STATS") == "1":
            print(
                json.dumps({"kpip_resolution": dict(result.metrics)}, sort_keys=True),
                file=sys.stderr,
            )

        return result

    @staticmethod
    def resolve_wheelhouse(
        find_links: list[str],
        requirements: list[str],
        *,
        constraints: list[str] | None = None,
        session: Any = None,
        preferences: Mapping[str, str] | None = None,
    ) -> ResolutionResult | None:
        """Resolve a local wheel directory through the normal nab path."""

        resolver = ResolutionEngine(
            find_links=tuple(find_links),
            no_index=True,
            constraints=tuple(constraints or ()),
            ignore_installed=True,
            session=session,
            preferences=preferences,
        )
        try:
            return resolver.resolve(requirements)
        finally:
            resolver.close()

    def close(self) -> None:
        self.provider.close()


__all__ = ["ResolutionConfig", "ResolutionEngine"]
