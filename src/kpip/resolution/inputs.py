"""Convert install inputs into packaging requirements for resolution."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast
from urllib.parse import unquote, urlsplit

from kpip.core.packaging import (
    Requirement,
    SpecifierSet,
    parse_requirement,
)
from kpip.install.requirement_set import RequirementSet
from kpip.resolution.req_install import (
    InstallRequirement,
)


def as_requirement_strings(
    requirements_input: RequirementSet[InstallRequirement]
    | Iterable[InstallRequirement]
    | list[str],
) -> list[str] | None:
    if isinstance(requirements_input, list) and (
        not requirements_input
        or all(isinstance(requirement, str) for requirement in requirements_input)
    ):
        return cast("list[str]", requirements_input)

    return None


def as_install_requirements(
    requirements_input: RequirementSet[InstallRequirement]
    | Iterable[InstallRequirement]
    | list[str],
) -> list[InstallRequirement]:
    if isinstance(requirements_input, RequirementSet):
        return cast(
            "list[InstallRequirement]",
            list(requirements_input.all_requirements),
        )

    string_requirements = as_requirement_strings(requirements_input)

    if string_requirements is not None:
        return []

    return list(cast("Iterable[InstallRequirement]", requirements_input))


def coerce_requirements(
    requirements_input: RequirementSet[InstallRequirement]
    | Iterable[InstallRequirement]
    | list[str],
) -> list[Requirement]:
    string_requirements = as_requirement_strings(requirements_input)

    if string_requirements is not None:
        return [parse_requirement(req) for req in string_requirements]

    requirements = as_install_requirements(requirements_input)

    result: list[Requirement] = []

    for requirement in requirements:
        if requirement.req is None:
            continue

        local_link = requirement.link is not None and (
            requirement.link.is_existing_dir or requirement.link.is_file
        )
        local_name = (
            requirement.metadata_internal.get("name")
            if local_link and requirement.metadata_internal is not None
            else None
        )
        local_version = (
            requirement.metadata_internal.get("version")
            if local_link and requirement.metadata_internal is not None
            else None
        )
        requirement_name = local_name or requirement.req.name
        if requirement_name.startswith(("file://", "http://", "https://")):
            path = unquote(urlsplit(requirement_name).path)
            requirement_name = path.rstrip("/").rsplit("/", 1)[-1] or requirement_name

        result.append(
            Requirement(
                name=requirement_name,
                specifier=(
                    SpecifierSet(f"=={local_version}")
                    if local_version
                    else (SpecifierSet() if local_link else requirement.req.specifier)
                ),
                extras=requirement.req.extras,
                url=(
                    requirement.req.url
                    or (
                        requirement.link.url
                        if requirement.link is not None
                        and (
                            requirement.link.is_existing_dir
                            or requirement.link.is_file
                            or requirement.link.is_vcs
                        )
                        else None
                    )
                ),
                marker=requirement.markers,
                raw=requirement.req.raw,
            ),
        )

    return result
