"""From a requirement command's options to a resolved plan.

``install``, ``download`` and ``wheel`` take the same requirement, index,
selection, hash and build options and resolve them the same way, as pip's
``RequirementCommand`` does for its own; they differ in what they do with the
plan. What they share is here: :func:`prepare` reads the options into a
requirement bundle, and :func:`resolve_requirements` turns that into a plan.
``install`` interleaves steps of its own (what is already installed, the plan
caches, fetching while the solve runs), so it calls the steps
:func:`resolve_requirements` is made of rather than the function itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging
import os

from kpip.build.build_backend import export_build_index_options
from kpip.cli.config import load_source_config
from kpip.cli.dependency_groups import group_items, parse_dependency_groups
from kpip.cli.package_finder import (
    apply_refresh,
    check_release_control,
    format_control,
    release_control,
)
from kpip.cli.requirements import (
    build_options_from_requirements,
    bundle_install_requirements,
    collect_requirements,
    config_settings,
    requirements_from_script,
)
from kpip.cli.resolution_errors import resolution_error_message
from kpip.core.appdirs import command_cache_dir
from kpip.core.errors import (
    CommandError,
    DistributionNotFound,
    HashMismatch,
    InstallationError,
    ResolutionError,
)
from kpip.core.format_control import FormatControl
from kpip.core.hashes import Hashes, file_hashes
from kpip.core.metadata import use_header_cache
from kpip.core.packaging import (
    canonicalize_name,
    normalize_python_version,
    parse_requirement,
    requires_python_version,
)
from kpip.core.urls import url_to_path
from kpip.core.wheel import TargetContext
from kpip.index.links import Link
from kpip.index.metadata_cache import get_wheel_metadata_cache
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.hash_checking import (
    enforce_dependency_hashes,
    enforce_hash_checking,
)
from kpip.resolution.input_requirements import install_req_from_line

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Iterable
    from typing import Any
    from kpip.resolution.models import ResolutionResult
    from kpip.resolution.req_install import InstallRequirement

logger = logging.getLogger(__name__)


class PreparedRequirements:
    """A requirement command's options, read."""

    __slots__ = ("bundle", "cache_dir", "options", "quiet", "release_control")

    def __init__(
        self,
        options: Any,
        bundle: Any,
        cache_dir: str | None,
        quiet: bool,
        release_control: list[tuple[str, str]],
    ) -> None:
        self.options = options
        self.bundle = bundle
        self.cache_dir = cache_dir
        self.quiet = quiet
        self.release_control = release_control


class ResolvedRequirements:
    """What the requirements resolved to, and what they were resolved with."""

    __slots__ = ("build_options", "plan", "requirements", "target")

    def __init__(
        self,
        plan: ResolutionResult,
        requirements: list[InstallRequirement],
        build_options: dict[str, dict[str, object]],
        target: TargetContext,
    ) -> None:
        self.plan = plan
        self.requirements = requirements
        self.build_options = build_options
        self.target = target


def check_only_deps(options: argparse.Namespace) -> None:
    """``--only-deps`` leaves out what the user named, so it cannot go with
    what names nothing or follows no dependencies."""
    if not options.only_deps:
        return

    conflicts = [
        spelling
        for given, spelling in (
            (options.no_deps, "'--no-deps'"),
            (options.requirement_files, "'--requirement'"),
            (options.requirements_from_scripts, "'--requirements-from-script'"),
            (options.groups, "'--group'"),
        )
        if given
    ]

    if not conflicts:
        return

    if len(conflicts) > 1:
        conflicts[-1] = "or " + conflicts[-1]

    raise CommandError(
        "Cannot use '--only-dependencies' in combination with "
        f"{', '.join(conflicts)}. "
        "If this is unexpected, please refer to the user guide:\n"
        "\n"
        "    https://pip.pypa.io/en/stable/user_guide/#installing-only-dependencies"
    )


def check_dist_restriction(options: argparse.Namespace) -> None:
    """A distribution chosen for another interpreter cannot be built here, so
    asking for one rules source distributions out."""
    if not (
        options.python_version
        or options.platform
        or options.abi
        or options.implementation
    ):
        return

    if format_control(options) != FormatControl(set(), {":all:"}) and not (
        options.no_deps
    ):
        raise CommandError(
            "When restricting platform and interpreter constraints using "
            "--python-version, --platform, --abi, or --implementation, "
            "either --no-deps must be set, or --only-binary=:all: must be "
            "set and --no-binary must not be set (or must be set to "
            ":none:)."
        )

    for filename in options.requirement_files:
        if os.path.basename(filename).startswith("pylock.") and filename.endswith(
            ".toml"
        ):
            raise CommandError(
                "Platform and interpreter constraints using "
                "--python-version, --platform, --abi, or --implementation, "
                f"are not supported when selecting requirements from {filename!r}"
            )


def python_version(options: argparse.Namespace) -> str:
    requested = getattr(options, "python_version", None)
    if not requested:
        return requires_python_version()
    return normalize_python_version(str(requested))


def target_context(options: argparse.Namespace) -> TargetContext:
    """The interpreter distributions are chosen for: the one kpip installs
    for, unless the command takes pip's target options and was given some."""
    requested = getattr(options, "python_version", None)
    return TargetContext(
        platforms=tuple(getattr(options, "platform", ())),
        implementation=getattr(options, "implementation", None),
        python_version=str(requested) if requested else None,
        abis=tuple(getattr(options, "abi", ())),
    )


def prepare(
    command: str,
    options: argparse.Namespace,
    parser: Any,
    *,
    validate: Callable[[], None] | None = None,
) -> PreparedRequirements:
    """Read a requirement command's parsed options into its requirements.

    ``validate`` checks what only that command takes, before anything is
    read from a file or an index.
    """
    check_only_deps(options)

    if len(options.requirements_from_scripts) > 1:
        raise CommandError("--requirements-from-script can only be given once")

    if options.no_input:
        os.environ["GIT_TERMINAL_PROMPT"] = "0"

    if any(
        os.path.basename(value) == "requirements.txt" for value in options.requirements
    ):
        logger.info(
            "Hint: It looks like you are trying to install a requirements file. "
            "Use the -r option to install the file, or provide a package literally "
            'named "requirements.txt".',
        )

    for filename in options.build_constraint_files:
        if not os.path.isfile(filename):
            raise InstallationError(
                f"Could not open requirements file: {filename}: No such file or directory",
            )

    for feature in options.use_features:
        if feature == "build-constraint":
            logger.warning(
                "--use-feature=build-constraint is always enabled; "
                "the option is a no-op."
            )

    quiet = options.quiet > 0
    if quiet:
        os.environ["KPIP_QUIET"] = "1"
    else:
        os.environ.pop("KPIP_QUIET", None)

    cache_dir = command_cache_dir(options.cache_dir, options.no_cache_dir)
    config = load_source_config(command)

    if cache_dir is not None:
        use_header_cache(get_wheel_metadata_cache(cache_dir))

    parsed_config_settings = config_settings(getattr(options, "config_settings", []))
    parsed_group_items = group_items(options.groups)
    for path, _ in parsed_group_items:
        if os.path.basename(os.path.normpath(path)) != "pyproject.toml":
            parser.error("group paths use 'pyproject.toml' filenames")

    grouped_requirements = (
        parse_dependency_groups(parsed_group_items) if parsed_group_items else []
    )

    script_requirements: list[str] = []
    if options.requirements_from_scripts:
        script_requirements = requirements_from_script(
            options.requirements_from_scripts[0],
            ignore_requires_python=options.ignore_requires_python,
        )

    if validate is not None:
        validate()

    check_release_control(options)

    if options.require_hashes and options.no_require_hashes:
        raise CommandError(
            "--require-hashes and --no-require-hashes are mutually exclusive"
        )

    if (
        options.index_url
        and "://" not in options.index_url
        and not options.index_url.startswith(("http:", "https:", "file:"))
    ):
        logger.warning(
            f'The index url "{options.index_url}" seems invalid, please provide a scheme.'
        )

    parsed_release_control = release_control(options)
    editables = list(getattr(options, "editables", []))

    bundle = collect_requirements(
        requirements=[
            *options.requirements,
            *grouped_requirements,
            *script_requirements,
        ],
        requirement_files=options.requirement_files,
        constraint_files=options.constraint_files,
        editables=editables,
        requirement_config_settings={
            requirement: dict(parsed_config_settings)
            for requirement in options.requirements
        },
        editable_config_settings={
            editable: dict(parsed_config_settings) for editable in editables
        },
        find_links=[*config.find_links, *options.find_links],
        index_url=(
            options.index_url if options.index_url is not None else config.index_url
        ),
        extra_index_urls=[*config.extra_index_urls, *options.extra_index_url],
        no_index=options.no_index or config.no_index,
        format_control=format_control(options),
        release_control_args=parsed_release_control,
        require_hashes=options.require_hashes,
        no_require_hashes=options.no_require_hashes,
        cert=options.cert,
        client_cert=options.client_cert,
        no_input=options.no_input,
        keyring_provider=options.keyring_provider or "auto",
        proxy=options.proxy,
        cache_dir=cache_dir,
    )

    constraint_hashes_by_name: dict[str, list[set[str]]] = {}
    for raw, hashes in bundle.constraint_hashes.items():
        constraint_hashes_by_name.setdefault(
            canonicalize_name(raw.split("==", 1)[0].strip()),
            [],
        ).append(set(hashes.get("sha256", ())))
    for values in constraint_hashes_by_name.values():
        if values and set.intersection(*values) == set():
            raise InstallationError(
                "Hashes are required in --require-hashes mode, but they are missing "
                "from some requirements.",
            )

    if bundle.find_links:
        os.environ["KPIP_FIND_LINKS"] = " ".join(bundle.find_links)

    if bundle.no_index:
        os.environ["KPIP_NO_INDEX"] = "1"

    export_build_index_options(
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        trusted_hosts=getattr(options, "trusted_hosts", None) or [],
        cert=options.cert,
        client_cert=options.client_cert,
        pre=getattr(options, "pre", False),
    )

    if (
        not bundle.requirements
        and not bundle.editables
        and not options.groups
        and not options.requirement_files
    ):
        raise CommandError(
            f"You must give at least one requirement to {command} "
            f'(see "kpip help {command}")',
        )

    apply_refresh(options)

    return PreparedRequirements(
        options=options,
        bundle=bundle,
        cache_dir=cache_dir,
        quiet=quiet,
        release_control=parsed_release_control,
    )


def intersect_hashes(left: Hashes, right: Hashes) -> Hashes:
    return Hashes(
        {
            algorithm: [
                digest
                for digest in left.allowed_internal.get(algorithm, [])
                if digest in right.allowed_internal.get(algorithm, [])
            ]
            for algorithm in left.allowed_internal.keys()
            & right.allowed_internal.keys()
        },
    )


def create_candidate_provider(
    options: Any,
    bundle: Any,
    requirements: list[Any],
    build_options: dict[str, dict[str, object]],
    target: Any,
    *,
    cache_dir: str | None,
) -> Any:
    provider = CandidateProvider.from_options(
        find_links=bundle.find_links,
        index_url=bundle.index_url,
        extra_index_urls=bundle.extra_index_urls,
        no_index=bundle.no_index,
        format_control=bundle.format_control,
        prefer_binary=options.prefer_binary,
        build_options=build_options,
        build_constraints=options.build_constraint_files,
        wheel_cache_dir=cache_dir,
        trusted_hosts=options.trusted_hosts,
        session=bundle.session,
        dry_run=getattr(options, "dry_run", False),
        build_isolation=not options.no_build_isolation,
        locked_links={name: Link(url) for name, url in bundle.locked_links.items()},
        target=target,
        uploaded_prior_to=options.uploaded_prior_to,
    )

    provider.release_control = bundle.release_control
    provider.hashes_by_name = {}

    for item in requirements:
        if item.req is None or not item.hash_options:
            continue
        hashes = item.hashes()
        previous = provider.hashes_by_name.get(item.req.canonical_name)
        provider.hashes_by_name[item.req.canonical_name] = (
            hashes if previous is None else intersect_hashes(previous, hashes)
        )

    for raw, hashes in (
        *bundle.constraint_hashes.items(),
        *bundle.requirement_hashes.items(),
    ):
        name = parse_requirement(raw).canonical_name
        current = provider.hashes_by_name.get(name)
        provider.hashes_by_name[name] = (
            Hashes(hashes)
            if current is None
            else intersect_hashes(current, Hashes(hashes))
        )

    return provider


def requested_requirements(
    prepared: PreparedRequirements,
    target: TargetContext,
) -> list[InstallRequirement]:
    """The requirements the user wrote, held to hash-checking mode when that
    is on."""
    bundle = prepared.bundle

    requirements = (
        bundle_install_requirements(bundle, target=target)
        if bundle.requirements
        else []
    )

    if bundle.require_hashes:
        # Before anything that may set a requirement aside as satisfied:
        # hash-checking mode is a claim about the requirements the user
        # wrote, and one that happens to be installed already must not
        # escape the pin and digest rules just because nothing would be
        # fetched for it.

        enforce_hash_checking(
            [*requirements, *editable_requirements(bundle)],
            constraints=bundle.constraints,
        )

    return requirements


def editable_requirements(bundle: Any) -> list[InstallRequirement]:
    requirements = []
    for editable in bundle.editables:
        item = install_req_from_line(editable)
        item.editable = True
        item.config_settings = bundle.editable_config_settings.get(editable, {})
        requirements.append(item)
    return requirements


def resolve(
    prepared: PreparedRequirements,
    requirements: list[InstallRequirement],
    make_provider: Callable[[], Any],
    *,
    ignore_installed: bool,
    upgrade: bool = False,
    upgrade_strategy: str = "only-if-needed",
    compute_source_hashes: bool = False,
    on_decided: Callable[[Any], None] | None = None,
) -> ResolutionResult:
    """Resolve ``requirements`` as the command's options say to."""
    options = prepared.options
    bundle = prepared.bundle
    # The report on a failure reads the pages the resolve already read.
    providers: list[Any] = []

    def providing() -> Any:
        providers.append(make_provider())
        return providers[-1]

    try:
        if os.environ.get("KPIP_RESOLVER_DEBUG") == "1":
            logger.info("Reporter.starting()")
        return ResolutionEngine.resolve_serving_stale_pages(
            lambda: ResolutionEngine(
                provider=providing(),
                on_decided=on_decided,
                no_deps=options.no_deps or bundle.only_locked,
                upgrade=upgrade,
                upgrade_strategy=upgrade_strategy,
                ignore_installed=ignore_installed,
                constraints=bundle.constraints,
                allow_prereleases=options.pre,
                require_hashes=bundle.require_hashes,
                compute_source_hashes=(
                    compute_source_hashes
                    or bundle.require_hashes
                    or bool(bundle.requirement_hashes)
                ),
                ignore_requires_python=options.ignore_requires_python,
                python_version=(
                    python_version(options)
                    if getattr(options, "python_version", None)
                    else None
                ),
            ),
            requirements,
        )

    except (DistributionNotFound, ResolutionError) as exc:
        if os.environ.get("KPIP_RESOLVER_DEBUG") == "1":
            logger.info("conflict is caused by the requested requirements")
        detail = resolution_error_message(
            str(exc),
            requirements,
            prepared.release_control,
            lambda: providers[-1] if providers else make_provider(),
            getattr(exc, "requires_python", None),
            getattr(exc, "edges", None),
            getattr(exc, "constraints", None),
            getattr(exc, "located_versions", None),
        )
        if prepared.quiet:
            # A quiet run shows only the error, and a build that
            # installs its requirements quietly has nowhere else to
            # say which constraints the failed resolve was held to. A
            # run that is not quiet names them in the report, as pip does.
            detail = "\n".join(
                (
                    detail,
                    *(
                        f"The user requested (constraint) {raw}"
                        for raw in bundle.constraints
                    ),
                )
            )
        if options.verbose:
            logger.info(f"DistributionNotFound: {detail}")
        raise DistributionNotFound(detail) from exc


def one_candidate_per_project(plan: ResolutionResult) -> ResolutionResult:
    unique_candidates: dict[str, Any] = {}
    for candidate in plan.candidates:
        unique_candidates.setdefault(candidate.canonical_name, candidate)
    return plan.replace(candidates=tuple(unique_candidates.values()))


def without_user_requested(
    plan: ResolutionResult,
    names: Iterable[str],
    source_urls: Iterable[str],
) -> ResolutionResult:
    """The plan ``--only-deps`` asks for: nothing the user named, even where
    another of their requirements depends on it."""
    names = set(names)
    source_urls = set(source_urls)
    return plan.replace(
        candidates=tuple(
            candidate
            for candidate in plan.candidates
            if candidate.canonical_name not in names
            and candidate.source_url not in source_urls
        )
    )


def requested_source_urls(requirements: Iterable[InstallRequirement]) -> set[str]:
    return {
        url
        for requirement in requirements
        for url in (
            requirement.link.url if requirement.link is not None else None,
            requirement.req.url if requirement.req is not None else None,
        )
        if url is not None
    }


def warn_about_yanked(plan: ResolutionResult) -> None:
    for candidate in plan.candidates:
        yanked_reason = getattr(candidate, "yanked_reason", None)
        if yanked_reason is not None:
            # PEP 592: installing a yanked release should warn, with the
            # reason the index gave.
            logger.warning(
                "The candidate selected for download or install is "
                f"a yanked version: {candidate.name!r} candidate (version "
                f"{candidate.version} at {getattr(candidate, 'source_url', '')})"
                f"\nReason for being yanked: {yanked_reason or '<none given>'}"
            )


def check_dependency_hashes(
    prepared: PreparedRequirements,
    plan: ResolutionResult,
    requirements: list[InstallRequirement],
    requested_names: set[str],
) -> None:
    """In hash-checking mode, what the requirements brought in is hashed too."""
    bundle = prepared.bundle

    if not bundle.require_hashes:
        return

    enforce_dependency_hashes(
        plan.candidates,
        hashed_names={
            *(
                item.req.canonical_name
                for item in requirements
                if item.req is not None and item.hash_options
            ),
            *(
                parse_requirement(raw).canonical_name
                for raw in (
                    *bundle.requirement_hashes,
                    *bundle.constraint_hashes,
                )
            ),
        },
        checked_names=requested_names,
    )


def check_local_archive_hashes(bundle: Any, plan: ResolutionResult) -> None:
    """Check each local archive against every requirement and constraint
    that hashes it.

    A local archive is used where it lies, not downloaded, so nothing else
    has compared its digest; each set of hashes must allow it.
    """
    user_hashes = [
        *bundle.requirement_hashes.items(),
        *bundle.constraint_hashes.items(),
    ]
    if not user_hashes:
        return

    for candidate in plan.candidates:
        if not (candidate.source_url and candidate.source_url.startswith("file:")):
            continue
        expected = [
            hashes
            for raw, hashes in user_hashes
            if parse_requirement(raw).canonical_name == candidate.canonical_name
        ]
        if not expected:
            continue
        actual = file_hashes(url_to_path(candidate.source_url))["sha256"]
        for hashes in expected:
            allowed = hashes.get("sha256", [])
            if actual not in allowed:
                raise HashMismatch(
                    f"{HashMismatch.head}\n"
                    f"    {candidate.name}=={candidate.version} "
                    f"from {candidate.source_url}:\n"
                    + "".join(f"        Expected sha256 {value}\n" for value in allowed)
                    + f"             Got        {actual}",
                )


def resolve_requirements(
    prepared: PreparedRequirements,
    *,
    with_editables: bool = False,
) -> ResolvedRequirements:
    """Resolve a command's requirements without regard to what is installed.

    For the commands that fetch or build what was asked for rather than
    install it. ``with_editables`` resolves ``-e`` requirements along with
    the rest, for a command that treats them as any other source tree.
    """
    options = prepared.options
    bundle = prepared.bundle
    target = target_context(options)

    requirements = requested_requirements(prepared, target)
    if with_editables:
        requirements.extend(editable_requirements(bundle))

    build_options = build_options_from_requirements(requirements)

    if bundle.find_links and not prepared.quiet:
        logger.info(f"Looking in links: {', '.join(bundle.find_links)}")

    plan = resolve(
        prepared,
        requirements,
        lambda: create_candidate_provider(
            options,
            bundle,
            requirements,
            build_options,
            target,
            cache_dir=prepared.cache_dir,
        ),
        ignore_installed=True,
    )

    plan = one_candidate_per_project(plan)
    requested_names = {
        item.req.canonical_name for item in requirements if item.req is not None
    }

    if options.only_deps:
        plan = without_user_requested(
            plan, requested_names, requested_source_urls(requirements)
        )

    warn_about_yanked(plan)
    check_dependency_hashes(prepared, plan, requirements, requested_names)
    check_local_archive_hashes(bundle, plan)

    return ResolvedRequirements(plan, requirements, build_options, target)
