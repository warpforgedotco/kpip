"""Report a resolve that found nothing to install: pip's words, and why.

The failure comes with the requirements the resolver could not meet
together, each with the release that declared it. From those, ``install``
and ``download`` say what pip says, lines users and scripts look for:

* one requirement nothing satisfies::

      ERROR: Could not find a version that satisfies the requirement X==1.0 (from versions: ...)
      ERROR: No matching distribution found for X==1.0

  after pip's notes on yanked releases and ones for another Python;
* a release whose metadata's ``Requires-Python`` leaves this Python out:
  ``Package 'X' requires a different Python: ...``;
* requirements that cannot all be met: pip's "Cannot install ... because
  these package versions have conflicting dependencies" and its causes.

pip stops there. kpip goes on, in ``hint:`` lines, to say why: wheels for
another platform or build of CPython, a format ``--only-binary`` or
``--no-binary`` ruled out, nowhere to look, or which releases do fit.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import tomllib
from typing import TYPE_CHECKING

from kpip.build.build_backend import prepare_project_metadata
from kpip.core.errors import KpipError
from kpip.core.packaging import (
    SpecifierSet,
    canonicalize_name,
    normalize_python_version,
    parse_requirement,
    requires_python_version,
)
from kpip.core.urls import url_to_path
from kpip.core.wheel import (
    parsed_wheel_tags,
    supported_wheel_tags,
    wheel_tag_rank,
)
from kpip.index.source_models import RejectionReason

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

_CONFLICT_HELP = (
    "ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/"
    "topics/dependency-resolution/#dealing-with-dependency-conflicts"
)


def resolution_error_message(
    message: str,
    requirements: list[Any],
    release_control: list[tuple[str, str]],
    make_provider: Any = None,
    requires_python: dict[str, str] | None = None,
    edges: list[tuple[tuple[str, Any] | None, Any]] | None = None,
    constraints: dict[str, tuple[Any, ...]] | None = None,
    located_versions: dict[str, Any] | None = None,
) -> str:
    """The error to raise for a failed resolve, pip's lines logged first.

    ``edges`` are the requirements the resolve could not meet together, each
    with the release that declared it, None for the user's own, and
    ``constraints`` the user's constraints by project. Without edges the
    failure has no proof to explain, and the resolver's words stand.
    """
    if message.startswith("No matching distribution found for "):
        return message.splitlines()[0]

    first_root = next(
        (item.req.raw for item in requirements if item.req is not None), None
    )
    if any(name == "only-final" for name, _ in release_control) and first_root:
        return f"Could not find a final version that satisfies the requirement {first_root}"

    if not edges:
        return message

    provider = make_provider() if make_provider is not None else None
    incoming: dict[str, list[tuple[tuple[str, Any] | None, Any]]] = {}
    for parent, requirement in edges:
        group = incoming.setdefault(canonicalize_name(requirement.name), [])
        if (parent, requirement_text(requirement)) not in (
            (known, requirement_text(other)) for known, other in group
        ):
            group.append((parent, requirement))
    constrained = {
        canonicalize_name(name): tuple(values)
        for name, values in (constraints or {}).items()
        if canonicalize_name(name) in incoming
    }

    # pip's words when every release it would take is for another Python,
    # a limit read from the release's metadata, after its files were chosen.
    rejected_python = {
        canonicalize_name(name): value
        for name, value in (requires_python or {}).items()
    }
    for name, group in incoming.items():
        if name in rejected_python and len(group) == 1:
            _, requirement = group[0]
            return (
                f"Package {requirement.name!r} requires a different Python: "
                f"{python_version_text(provider)} not in {rejected_python[name]!r}"
            )

    fitting: dict[str, list[Any]] = {
        name: fitting_versions(name, provider) for name in incoming
    }
    located = {
        canonicalize_name(name): str(version)
        for name, version in (located_versions or {}).items()
    }
    for name, group in incoming.items():
        if name in located or not (name in constrained or len(group) > 1):
            continue
        for _, requirement in group:
            if requirement.url:
                version = located_version(requirement, provider)
                if version is not None:
                    located[name] = version
                    break
    failing = [
        name
        for name, group in incoming.items()
        if not satisfiable(
            [requirement for _, requirement in group] + list(constrained.get(name, ())),
            fitting[name],
            located.get(name),
        )
    ]
    if not failing:
        return message

    name = failing[0]
    if len(failing) == 1 and len(incoming[name]) == 1 and name not in constrained:
        parent, requirement = incoming[name][0]
        return report_unsatisfied(
            requirement_text(requirement, located),
            None if parent is None else parent[0],
            provider,
        )

    return report_conflict(
        [edge for name in failing for edge in incoming[name]],
        failing,
        fitting,
        constrained,
        located,
    )


def satisfiable(
    requirements: list[Any], versions: list[Any], located: str | None = None
) -> bool:
    """Whether one release could meet all of ``requirements``.

    A requirement on a path or URL is met by what it names alone: two
    different ones cannot both be, and its version -- ``located``, as built,
    or its file's name's -- must meet every specifier. The rest are met by a
    version of those with a file that fits.
    """
    urls = {requirement.url for requirement in requirements if requirement.url}
    if len(urls) > 1:
        return False
    if urls:
        version = located or url_version(next(iter(urls)))
        if version is None:
            return not any(requirement.specifier.text for requirement in requirements)
        return all(
            requirement.specifier.contains(version) for requirement in requirements
        )
    return any(
        all(requirement.specifier.contains(version) for requirement in requirements)
        for version in versions
    )


def located_version(requirement: Any, provider: Any) -> str | None:
    """The version a path or URL requirement names, found as cheaply as it
    can be.

    A distribution file's name gives it; a local project's ``pyproject.toml``
    states it unless it is dynamic. Only then is the project's metadata
    built, and only here: the resolve has already failed, and turned the
    project away before building it, so the build buys the report its
    version, as pip -- which builds first -- would name it.
    """
    version = url_version(requirement.url)
    if version is not None or not requirement.url.startswith("file:"):
        return version
    path = url_to_path(requirement.url)
    if not os.path.isdir(path):
        return None
    static = static_project_version(path)
    if static is not None:
        return static
    try:
        metadata = prepare_project_metadata(
            path,
            build_isolation=getattr(provider, "build_isolation", True),
            build_constraints=getattr(provider, "build_constraints", None),
        )
    except KpipError:
        return None
    return str(metadata.version)


def static_project_version(path: str) -> str | None:
    """``[project] version`` of the project at ``path``, when it states one."""
    try:
        with open(os.path.join(path, "pyproject.toml"), "rb") as file:
            project = tomllib.load(file).get("project")
    except OSError, tomllib.TOMLDecodeError:
        return None
    if not isinstance(project, dict) or "version" in project.get("dynamic", ()):
        return None
    version = project.get("version")
    return version if isinstance(version, str) else None


def url_version(url: str) -> str | None:
    """The version a distribution file's name gives, or None."""
    filename = url.rsplit("/", 1)[-1].split("#", 1)[0]
    if filename.endswith(".whl"):
        parts = filename.split("-")
        return parts[1] if len(parts) >= 5 else None
    match = re.match(
        r"^.+?-(\d[^-]*?)(\.tar\.gz|\.zip|\.tar\.bz2|\.tar\.xz|\.tar)$", filename
    )
    return match.group(1) if match else None


def requirement_text(requirement: Any, located: dict[str, str] | None = None) -> str:
    """A requirement as pip shows it in an error.

    One on a path or URL is shown as pip shows what it names -- ``name
    version (from path)`` -- and the rest as name, extras and specifier.
    """
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    if requirement.url:
        version = (located or {}).get(
            canonicalize_name(requirement.name)
        ) or url_version(requirement.url)
        location = requirement.url.removeprefix("file://")
        shown = canonicalize_name(requirement.name) + (f" {version}" if version else "")
        return f"{shown} (from {location})"
    return f"{requirement.name}{extras}{requirement.specifier.text}"


def constraint_text(name: str, constraints: tuple[Any, ...]) -> str:
    """pip's line for a project's constraints: its specifiers, or the files
    they name."""
    urls = [constraint.url for constraint in constraints if constraint.url]
    if urls:
        return f"{name} (from {', '.join(urls)})"
    return f"{name}{''.join(constraint.specifier.text for constraint in constraints)}"


def fitting_versions(name: str, provider: Any) -> list[Any]:
    """The versions of ``name`` with a file that fits, as pip lists them."""
    if provider is None:
        return []
    selection = provider.evaluate_links(parse_requirement(name))
    return sorted({candidate.version for candidate in selection.accepted})


def report_conflict(
    causes: list[tuple[tuple[str, Any] | None, Any]],
    failing: list[str],
    fitting: dict[str, list[Any]],
    constrained: dict[str, tuple[Any, ...]],
    located: dict[str, str],
) -> str:
    """pip's report of requirements that cannot all be met at once."""
    triggers = sorted(
        {
            requirement_text(requirement, located)
            if parent is None
            else f"{parent[0]}=={parent[1]}"
            for parent, requirement in causes
        }
    )
    shown = (
        triggers[0]
        if len(triggers) == 1
        else ", ".join(triggers[:-1]) + " and " + triggers[-1]
    )
    logger.critical(
        "Cannot install %s because these package versions have conflicting "
        "dependencies.",
        shown or "the requested packages",
    )

    lines = ["", "The conflict is caused by:"]
    for parent, requirement in causes:
        who = (
            "The user requested"
            if parent is None
            else f"{parent[0]} {parent[1]} depends on"
        )
        lines.append(f"    {who} {requirement_text(requirement, located)}")
    for name in failing:
        if name in constrained:
            lines.append(
                "    The user requested (constraint) "
                + constraint_text(name, constrained[name])
            )
    lines += [
        "",
        "Additionally, some packages in these conflicts have no matching "
        "distributions available for your environment:",
        *(f"    {name}" for name in sorted(failing)),
    ]
    for parent, requirement in causes:
        name = canonicalize_name(requirement.name)
        version = located.get(name)
        if parent is None and requirement.url and version is not None:
            ruling = [
                constraint
                for constraint in constrained.get(name, ())
                if constraint.specifier.text
                and not constraint.specifier.contains(version)
            ]
            if ruling:
                location = requirement.url.removeprefix("file://")
                lines += [
                    "",
                    f"hint: {location} is {name} {version}, and the constraint "
                    f"{name}{ruling[0].specifier.text} rules it out",
                ]
    for name in failing:
        if fitting[name]:
            versions = ", ".join(str(version) for version in fitting[name])
            lines += ["", f"hint: the releases of {name} that fit are {versions}"]
    lines += [
        "",
        "To fix this you could try to:",
        "1. loosen the range of package versions you've specified",
        "2. remove package versions to allow pip to attempt to solve the "
        "dependency conflict",
        "",
    ]
    logger.info("\n".join(lines))
    return _CONFLICT_HELP


def report_unsatisfied(requirement: str, parent: str | None, provider: Any) -> str:
    """Log pip's lines for ``requirement``; return the error, with hints."""
    shown = requirement if parent is None else f"{requirement} (from {parent})"

    if provider is None:
        logger.critical(
            "Could not find a version that satisfies the requirement %s", shown
        )
        return f"No matching distribution found for {requirement}"

    parsed = parse_requirement(requirement)
    everything = provider.evaluate_links(parse_requirement(parsed.name))

    versions = sorted({candidate.version for candidate in everything.accepted})
    yanked = yanked_versions(parsed.name, provider, versions)
    other_python = {
        rejection_version(rejected): rejected.detail
        for rejected in everything.rejected
        if rejected.reason is RejectionReason.REQUIRES_PYTHON
    }
    other_python.pop(None, None)

    if yanked:
        logger.critical(
            "Ignored the following yanked versions: %s",
            ", ".join(str(version) for version in yanked),
        )
    if other_python:
        logger.critical(
            "Ignored the following versions that require a different python "
            "version: %s",
            "; ".join(
                f"{version} {detail.replace('requires Python', 'Requires-Python')}"
                for version, detail in sorted(other_python.items())
            ),
        )
    logger.critical(
        "Could not find a version that satisfies the requirement %s (from versions: %s)",
        shown,
        ", ".join(str(version) for version in versions) or "none",
    )

    hints = why_nothing_fits(parsed, provider, versions)
    return "\n".join((f"No matching distribution found for {requirement}", *hints))


def yanked_versions(name: str, provider: Any, versions: list[Any]) -> list[Any]:
    """The yanked releases that would otherwise fit, as pip lists them."""
    selection = provider.with_yanked_policy(True).evaluate_links(
        parse_requirement(name)
    )
    return sorted(
        {
            candidate.version
            for candidate in selection.accepted
            if candidate.link.yanked_reason is not None
        }
        - set(versions)
    )


def why_nothing_fits(requirement: Any, provider: Any, versions: list[Any]) -> list[str]:
    """``hint:`` lines on why the releases ``requirement`` matches were
    turned down, or on what does exist when none matches."""
    specifier = SpecifierSet(requirement.specifier.text)
    selection = provider.evaluate_links(requirement)
    rejected = [
        rejected
        for rejected in selection.rejected
        if rejected.reason is not RejectionReason.VERSION_MISMATCH
    ]
    hints: list[str] = []

    if any(r.reason is RejectionReason.UNSUPPORTED_WHEEL for r in rejected):
        wheels = [
            link.filename
            for link in provider.catalog_links(requirement)
            if link.filename.endswith(".whl")
            and specifier.contains(link.filename.split("-")[1])
        ]
        hints.append(wheel_hint(requirement, wheels, provider))

    for rejected_candidate in rejected:
        if rejected_candidate.reason is RejectionReason.REQUIRES_PYTHON:
            version = rejection_version(rejected_candidate)
            hints.append(
                f"hint: {requirement.name} {version} {rejected_candidate.detail}; "
                f"this is Python {python_version_text(provider)}"
            )
            break

    format_hint = excluded_format_hint(requirement, provider)
    if format_hint is not None:
        hints.append(format_hint)

    if not versions and not provider.index_sources and not provider.find_links:
        hints.append(
            "hint: --no-index was given without --find-links, so there was "
            "nowhere to look"
        )

    if (
        not hints
        and versions
        and not any(specifier.contains(version) for version in versions)
    ):
        hints.append(
            f"hint: no release of {requirement.name} matches "
            f"{requirement.specifier.text}; the newest is {versions[-1]}"
        )
    return hints


def wheel_hint(requirement: Any, wheels: list[str], provider: Any) -> str:
    """Why the wheels do not fit, naming the one nearest to fitting."""
    supported = supported_wheel_tags(getattr(provider, "target", None))
    best = supported[0]
    text = (
        f"hint: none of the {len(wheels)} wheel{'s' if len(wheels) != 1 else ''} "
        f"for {requirement.raw} is for this Python, which takes "
        f"{best.interpreter}-{best.abi}-{best.platform} wheels"
    )
    other_build = other_build_wheel(wheels, best, supported)
    if other_build is not None:
        filename, build = other_build
        text += f"; {filename} is for the {build} build"
    return text


def other_build_wheel(
    wheels: list[str], best: Any, supported: Any
) -> tuple[str, str] | None:
    """A wheel that would fit but for being the other build of CPython: the
    GIL one when this Python is free-threaded, or the free-threaded one."""
    threaded = best.abi.startswith("cp") and best.abi.endswith("t")
    wanted = best.abi[:-1] if threaded else f"{best.abi}t"
    for filename in wheels:
        parts = filename[: -len(".whl")].split("-")
        if len(parts) < 5 or wanted not in parts[-2].split("."):
            continue
        as_this_build = parsed_wheel_tags(parts[-3], best.abi, parts[-1])
        if wheel_tag_rank(as_this_build, supported) is not None:
            return filename, "GIL" if threaded else "free-threaded"
    return None


def excluded_format_hint(requirement: Any, provider: Any) -> str | None:
    """Whether a file the format rules turned away would have fit."""
    allow_binary, allow_source = provider.allowed_formats_internal(requirement)
    if allow_binary and allow_source:
        return None
    # A view, as with_yanked_policy makes one: the provider's catalog
    # prefetch may still be running, and must not see the rules lifted.
    open_provider = copy.copy(provider)
    open_provider.format_control = None
    open_selection = open_provider.evaluate_links(requirement)
    if not open_selection.accepted:
        return None
    if not allow_source:
        return (
            f"hint: {requirement.raw} has a source distribution, but "
            "--only-binary rules source distributions out"
        )
    return f"hint: {requirement.raw} has a wheel, but --no-binary rules wheels out"


def rejection_version(rejected: Any) -> Any:
    """The version a rejected file is of, from its name, or None."""
    filename = rejected.link.filename
    if filename.endswith(".whl"):
        parts = filename.split("-")
        return parts[1] if len(parts) >= 5 else None
    match = re.match(
        r"^(.+?)-(\d[^-]*?)(\.tar\.gz|\.zip|\.tar\.bz2|\.tar\.xz|\.tar)$", filename
    )
    return match.group(2) if match else None


def python_version_text(provider: Any) -> str:
    target = getattr(provider, "target", None)
    if target is not None and target.python_version:
        return normalize_python_version(target.python_version)
    return requires_python_version()
