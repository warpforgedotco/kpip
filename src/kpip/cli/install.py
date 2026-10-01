from __future__ import annotations

from typing import TYPE_CHECKING
import logging
import os

from kpip.build.build import build_editable_from_source
from kpip.build.metadata import InstalledDistributionStore
from kpip.build.query import (
    check_package_set,
    installed_dependencies_by_name,
    package_set_from_dependencies,
)
from kpip.cli.lock_replay import (
    FRESH,
    open_http_cache,
    page_state,
    page_validators,
    resolution_environment,
)
from kpip.cli.parsers.install import create_parser
from kpip.cli.requirement_command import (
    PreparedRequirements,
    check_dependency_hashes,
    check_local_archive_hashes,
    create_candidate_provider,
    one_candidate_per_project,
    prepare,
    python_version,
    requested_requirements,
    resolve,
    target_context,
    warn_about_yanked,
    without_user_requested,
)
from kpip.cli.requirements import build_options_from_requirements
from kpip.core.code_identity import code_identity
from kpip.core.errors import (
    CommandError,
    InstallationError,
    ResolutionError,
)
from kpip.core.kpip_version import KPIP_DISTRIBUTION_NAMES
from kpip.core.metadata import find_installed, installed_index, user_lib_path
from kpip.core.packaging import (
    canonicalize_name,
    default_environment,
    marker_applies,
    parse_requirement,
)
from kpip.core.wheel import (
    TargetContext,
    supported_wheel_tags,
    wheel_candidate_from_path,
)
from kpip.host.environment_checks import (
    check_externally_managed,
    warn_if_run_as_root,
)
from kpip.host.interpreter_facts import target_interpreter
from kpip.index.candidate_materialization import LazyWheelCandidate
from kpip.install.archive_workers import ArchiveWorkers
from kpip.install.metadata import (
    ReportItem,
    direct_url_from_link,
    prepare_editable_source,
    write_install_report,
)
from kpip.install.output import (
    WheelPrefetch,
    installation_order,
    prepare_install_candidates,
)
from kpip.install.target import InstallTarget
from kpip.install.wheel_archive_cache import prepare_cached_wheel
from kpip.install.wheel_install_plan_cache import (
    REMOTE_EXACT_CONTEXT,
    exact_install_plan_key,
    load_cached_install_plan,
    load_plan_pages,
    plain_install_plan_key,
    save_cached_install_plan,
    save_plan_pages,
)
from kpip.install.wheel_transaction import (
    WheelInstaller,
    install_wheels_transactionally,
)
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.input_requirements import install_req_from_line

if TYPE_CHECKING:
    import argparse
    from collections.abc import Mapping
    from typing import Any
    from kpip.core.metadata import InstalledDistribution
    from kpip.resolution.models import ResolutionResult
    from kpip.resolution.req_install import InstallRequirement

logger = logging.getLogger(__name__)


INDEX_URL_OPTIONS = frozenset(("-i", "--index-url"))


class InstallExecutionContext:
    __slots__ = (
        "bundle",
        "cache_dir",
        "options",
        "python_version",
        "quiet",
        "requirements",
        "target",
    )

    def __init__(
        self,
        options: Any,
        bundle: Any,
        target: TargetContext,
        requirements: list[Any],
        cache_dir: str | None,
        quiet: bool,
        python_version: str,
    ) -> None:
        self.options = options
        self.bundle = bundle
        self.target = target
        self.requirements = requirements
        self.cache_dir = cache_dir
        self.quiet = quiet
        self.python_version = python_version


class InstallRequirementState:
    __slots__ = (
        "build_options",
        "requested_extras_by_name",
        "requested_order",
        "requested_source_urls",
        "source_requirements_by_name",
        "source_requirements_by_url",
        "summary_root_source_urls",
    )

    def __init__(
        self,
        requested_order: dict[str, int],
        requested_source_urls: set[str],
        summary_root_source_urls: set[str],
        build_options: dict[str, dict[str, object]],
        source_requirements_by_name: dict[str, Any],
        source_requirements_by_url: dict[str, Any],
        requested_extras_by_name: dict[str, set[str]],
    ) -> None:
        self.requested_order = requested_order
        self.requested_source_urls = requested_source_urls
        self.summary_root_source_urls = summary_root_source_urls
        self.build_options = build_options
        self.source_requirements_by_name = source_requirements_by_name
        self.source_requirements_by_url = source_requirements_by_url
        self.requested_extras_by_name = requested_extras_by_name


class InstallOutcome:
    __slots__ = (
        "installed",
        "installed_canonical_names",
        "newly_installed_names",
        "report_enabled",
        "report_items",
        "reported_satisfied",
        "satisfied_requirements",
        "summary_root_names",
    )

    def __init__(self, report_enabled: bool = False) -> None:
        self.report_enabled = report_enabled
        self.installed: list[str] = []
        self.installed_canonical_names: list[str] = []
        self.summary_root_names: set[str] = set()
        self.newly_installed_names: set[str] = set()
        self.reported_satisfied: set[str] = set()
        self.satisfied_requirements: list[str] = []
        self.report_items: list[Any] = []

    def record_installed(self, display: str, canonical_name: str) -> None:
        self.installed.append(display)
        self.installed_canonical_names.append(canonical_name)

    def add_report_item(self, **fields: Any) -> None:
        if not self.report_enabled:
            return
        self.report_items.append(ReportItem(**fields))


def normalize_install_args(args: list[str], options: frozenset[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in options and index + 1 < len(args):
            normalized.append(f"{token}={args[index + 1]}")
            index += 2
            continue
        normalized.append(token)
        index += 1
    return normalized


def requirement_state(requirements: list[Any], bundle: Any) -> InstallRequirementState:
    requested_order = {
        requirement.req.canonical_name: index
        for index, requirement in enumerate(requirements)
        if requirement.req is not None
    }
    requested_source_urls = {
        url
        for requirement in requirements
        for url in (
            requirement.link.url if requirement.link is not None else None,
            requirement.req.url if requirement.req is not None else None,
        )
        if url is not None
    }
    summary_root_source_urls = {
        requirement.link.url
        for requirement in requirements
        if requirement.link is not None and requirement.link.is_existing_dir
    }
    build_options = build_options_from_requirements(requirements)

    source_requirements_by_name: dict[str, Any] = {}
    requested_extras_by_name: dict[str, set[str]] = {}
    source_requirements_by_url: dict[str, Any] = {}
    for requirement in requirements:
        if requirement.req is None:
            continue
        name = canonicalize_name(requirement.req.name)
        source_requirements_by_name[name] = requirement
        extras_for_name = requested_extras_by_name.get(name)
        if extras_for_name is None:
            extras_for_name = set()
            requested_extras_by_name[name] = extras_for_name
        extras_for_name.update(requirement.req.extras)
        if requirement.link is not None:
            source_requirements_by_url[requirement.link.url] = requirement
        if requirement.req.url is not None:
            source_requirements_by_url[requirement.req.url] = requirement

    for constraint in bundle.constraints:
        constraint_requirement = install_req_from_line(constraint)
        if constraint_requirement.req is None:
            continue
        name = canonicalize_name(constraint_requirement.req.name)
        source_requirements_by_name[name] = constraint_requirement
        if constraint_requirement.link is not None:
            source_requirements_by_url[constraint_requirement.link.url] = (
                constraint_requirement
            )
        if constraint_requirement.req.url is not None:
            source_requirements_by_url[constraint_requirement.req.url] = (
                constraint_requirement
            )

    return InstallRequirementState(
        requested_order=requested_order,
        requested_source_urls=requested_source_urls,
        summary_root_source_urls=summary_root_source_urls,
        build_options=build_options,
        source_requirements_by_name=source_requirements_by_name,
        source_requirements_by_url=source_requirements_by_url,
        requested_extras_by_name=requested_extras_by_name,
    )


def filter_already_satisfied_requirements(
    requirements: list[InstallRequirement],
    outcome: InstallOutcome,
    *,
    allow_prereleases: bool,
    follow_dependencies: bool,
) -> list[InstallRequirement]:
    """Drop requirements already satisfied by an installed distribution.

    Recording each dropped requirement's raw text on ``outcome`` is what lets
    the final report list it without re-deriving satisfaction later.
    """
    unresolved_requirements: list[InstallRequirement] = []
    installed = installed_index()
    satisfied: list[tuple[InstalledDistribution, frozenset[str]]] = []
    for requirement in requirements:
        installed_dist = (
            installed.get(requirement.req.canonical_name)
            if requirement.req is not None and requirement.req.url is None
            else None
        )
        if (
            requirement.req is not None
            and installed_dist is not None
            and not requirement.req.extras
            and installed_dist.version is not None
            and requirement.req.is_satisfied_by(
                installed_dist.version,
                allow_prereleases=allow_prereleases,
            )
        ):
            outcome.satisfied_requirements.append(requirement.req.raw)
            satisfied.append((installed_dist, requirement.req.extras))
        else:
            unresolved_requirements.append(requirement)
    if follow_dependencies:
        named = {
            requirement.req.canonical_name
            for requirement in unresolved_requirements
            if requirement.req is not None
        }
        unresolved_requirements.extend(
            install_req_from_line(dependency)
            for name, dependency in unmet_dependencies(installed, satisfied).items()
            if name not in named
        )
    return unresolved_requirements


def unmet_dependencies(
    installed: Mapping[str, InstalledDistribution],
    satisfied: list[tuple[InstalledDistribution, frozenset[str]]],
) -> dict[str, str]:
    """What the dependencies of satisfied requirements still need installed.

    pip keeps a requirement the environment satisfies and goes on to its
    dependencies: one that is missing, or installed in a version the
    requirement on it rules out, is installed. Each is given by canonical
    name, as the requirement to resolve for it.
    """
    unmet: dict[str, str] = {}
    seen: set[tuple[str, frozenset[str]]] = set()
    pending = list(satisfied)
    while pending:
        distribution, extras = pending.pop()
        for dependency in distribution.dependencies(extras):
            key = (dependency.canonical_name, dependency.extras)
            if key in seen:
                continue
            seen.add(key)
            found = installed.get(dependency.canonical_name)
            if found is not None and (
                dependency.url is not None
                or (
                    found.version is not None
                    and dependency.is_satisfied_by(found.version)
                )
            ):
                pending.append((found, dependency.extras))
                continue
            text = dependency.name
            if dependency.extras:
                text += f"[{','.join(sorted(dependency.extras))}]"
            text += (
                f" @ {dependency.url}"
                if dependency.url is not None
                else dependency.specifier.text
            )
            previous = unmet.get(dependency.canonical_name)
            # Two requirements on one name: both hold, as one requirement.
            unmet[dependency.canonical_name] = (
                text
                if previous is None or dependency.url is not None
                else f"{previous},{dependency.specifier.text}".rstrip(",")
            )
    return unmet


def validate_option_combinations(options: argparse.Namespace) -> None:
    if options.user and options.target:
        raise ValueError("Can not combine '--user' and '--target'")
    if options.user and options.prefix:
        raise ValueError("Can not combine '--user' and '--prefix'")
    if (
        not options.target
        and not options.dry_run
        and (options.platform or options.python_version or options.abi)
    ):
        raise ValueError(
            "Can not use any platform or abi specific options unless installing via '--target'",
        )


def prepare_install(args: list[str], parser: Any) -> PreparedRequirements:
    options = parser.parse_args(normalize_install_args(args, INDEX_URL_OPTIONS))

    if options.target:
        # As pip: a target directory is a library of its own, which what the
        # running environment has installed does not satisfy. A requirement
        # installed there was left out of the target, and the target could
        # not be used without it.
        options.ignore_installed = True

    def validate() -> None:
        try:
            validate_option_combinations(options)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options.user:
            validate_user_install(options)

    return prepare("install", options, parser, validate=validate)


def validate_user_install(options: argparse.Namespace) -> None:
    if not target_interpreter().user_site_enabled:
        if target_interpreter().in_virtualenv:
            raise InstallationError(
                "Can not perform a '--user' install. User site-packages are "
                "not visible in this virtualenv.",
            )
        raise InstallationError(
            "Can not perform a '--user' install. User site-packages are "
            "disabled for this Python.",
        )

    if target_interpreter().in_virtualenv:
        for raw_requirement in options.requirements:
            item = install_req_from_line(raw_requirement)
            if item.req is None:
                continue
            installed_dist = InstalledDistributionStore().find(item.req.name)
            if (
                installed_dist is not None
                and not options.ignore_installed
                and os.path.commonpath(
                    (
                        os.path.abspath(installed_dist.location),
                        os.path.abspath(user_lib_path()),
                    ),
                )
                != os.path.abspath(user_lib_path())
                and installed_dist.in_site_packages
            ):
                raise InstallationError(
                    "Will not install to the user site because it will lack "
                    f"sys.path precedence to {installed_dist.raw_name} in "
                    f"{installed_dist.location}",
                )


def _plan_cacheable(options: Any, bundle: Any) -> bool:
    """Whether an install's plan depends only on its requirements, its target
    context and the index: nothing installed, local or overridden."""
    return not (
        options.no_cache_dir
        or options.refresh
        or options.target is None
        or not options.ignore_installed
        or options.dry_run
        or options.report
        or options.user
        or options.root is not None
        or options.prefix is not None
        or options.no_deps
        or options.only_deps
        or options.prefer_binary
        or options.refresh_package
        or options.no_require_hashes
        or options.src_dir
        or options.upgrade
        or options.pre
        or options.require_hashes
        or options.ignore_requires_python
        or options.platform
        or options.implementation
        or options.python_version
        or options.abi
        or options.uploaded_prior_to
        or options.groups
        or options.requirements_from_scripts
        or options.constraint_files
        or options.build_constraint_files
        or options.config_settings
        or options.no_binary
        or options.only_binary
        or options.all_releases
        or options.only_final
        or bundle.no_index
        or bundle.find_links
        or bundle.extra_index_urls
        or bundle.constraints
        or bundle.editables
        or bundle.require_hashes
        or bundle.locked_links
        or bundle.requirement_hashes
        or bundle.constraint_hashes
        or bundle.format_control.no_binary
        or bundle.format_control.only_binary
        or bundle.release_control.all_releases
        or bundle.release_control.only_final
    )


def _plan_context(options: Any, bundle: Any, target: Any) -> tuple[object, ...]:
    return (
        bundle.index_url,
        tuple(bundle.extra_index_urls),
        tuple(target.platforms),
        target.implementation,
        target.python_version,
        tuple(target.abis),
        # The interpreter planned for, when options do not name it.
        tuple(sorted(default_environment().items())),
        supported_wheel_tags(target),
        options.upgrade_strategy,
        bool(options.force_reinstall),
    )


def cached_remote_plan_key(
    options: Any,
    bundle: Any,
    requirements: list[Any],
    target: Any,
) -> str | None:
    if not _plan_cacheable(options, bundle):
        return None

    return exact_install_plan_key(
        tuple(requirements),
        (REMOTE_EXACT_CONTEXT, *_plan_context(options, bundle, target)),
    )


REPLAY_CONTEXT = "install-replay-1"


def replayable_install_plan_key(
    options: Any,
    bundle: Any,
    requirements: list[Any],
    target: Any,
) -> str | None:
    """The key an install's plan is replayed under while the index pages it
    was resolved from are unchanged, like a lock's (``lock_replay``); None
    when it cannot be.

    For requirements not all pinned, which the exact-pin receipts do not
    take: their answer is whatever the index says, so it is kept by the pages
    read rather than for a time, and keyed on the code and the interpreter
    that resolved it as well.
    """
    if not _plan_cacheable(options, bundle):
        return None

    return plain_install_plan_key(
        tuple(requirements),
        (
            REPLAY_CONTEXT,
            code_identity(),
            resolution_environment(),
            *_plan_context(options, bundle, target),
        ),
    )


def load_replayable_install_plan(cache_dir: str, key: str) -> ResolutionResult | None:
    """The plan kept under ``key`` if every page it was resolved from is
    unchanged and fresh: what resolving again would read. A page merely
    stale is revalidated by resolving, as ever."""
    pages = load_plan_pages(cache_dir, key)

    if pages is None:
        return None

    if page_state(open_http_cache(cache_dir), pages) != FRESH:
        return None

    return load_cached_install_plan(cache_dir, key, max_age=None)


def record_replayable_install_plan(
    cache_dir: str, key: str, plan: ResolutionResult, provider: Any
) -> None:
    """Keep a freshly resolved plan with the index pages it was read from.

    Only a plan of wheels is kept: one that built a source distribution is
    not, and is known so before reading its pages or its archives.
    """
    if any(candidate.source_kind != "wheel" for candidate in plan.candidates):
        return

    urls: set[str] = set()

    for source in getattr(provider, "index_sources", ()):
        urls.update(source.pages_read)

    if not urls:
        return

    validators = page_validators(open_http_cache(cache_dir), urls)

    if validators is None:
        return

    if save_cached_install_plan(cache_dir, key, tuple(plan.candidates), plan.graph):
        save_plan_pages(cache_dir, key, validators)


def target_library_is_empty(target: InstallTarget) -> bool:
    seen: set[str] = set()
    for root in target.library_roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            with os.scandir(root) as entries:
                if next(entries, None) is not None:
                    return False
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            return False
    return True


def install_candidate(
    candidate: Any,
    options: Any,
    *,
    requested: bool,
    reinstall: bool,
    direct_url: Any = None,
) -> None:
    target = InstallTarget.from_options(
        candidate.canonical_name,
        target=options.target,
        user=options.user,
        root=options.root,
        prefix=options.prefix,
    )
    WheelInstaller(
        target,
        pycompile=not options.no_compile,
        force=reinstall or (direct_url is not None and direct_url.is_local_editable()),
        preserve_existing=options.ignore_installed,
    ).install(candidate.path, requested=requested, direct_url=direct_url)


def warn_about_install_conflicts(changed_names: set[str]) -> None:
    distributions = InstalledDistributionStore().iter(skip=KPIP_DISTRIBUTION_NAMES)
    distributions_by_name = {dist.canonical_name: dist for dist in distributions}
    dependencies_by_name = installed_dependencies_by_name(distributions)

    dependents_by_name: dict[str, set[str]] = {}
    for name, dependencies in dependencies_by_name.items():
        for requirement in dependencies:
            dependents_by_name.setdefault(
                canonicalize_name(requirement.name),
                set(),
            ).add(name)

    package_set = package_set_from_dependencies(distributions, dependencies_by_name)
    affected = set(changed_names)
    pending = list(changed_names)

    while pending:
        dependency = pending.pop()
        for dependent in dependents_by_name.get(dependency, ()):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)

    missing, conflicting = check_package_set(package_set)

    conflicts: list[str] = []

    for name, requirements in sorted(missing.items()):
        if name not in affected:
            continue
        distribution = distributions_by_name[name]
        for _, requirement in requirements:
            conflicts.append(
                f"{distribution.canonical_name} {distribution.raw_version} requires "
                f"{requirement.name}, which is not installed."
            )

    for name, requirements in sorted(conflicting.items()):
        if name not in affected:
            continue
        distribution = distributions_by_name[name]
        for dependency_name, version, requirement in requirements:
            conflicts.append(
                f"{distribution.canonical_name} {distribution.raw_version} requires "
                f"{requirement}, but you have {dependency_name} {version} which is incompatible."
            )

    if conflicts:
        # One record, as pip reports it: the lines after the first carry no
        # prefix of their own.
        logger.critical(
            "\n".join(
                (
                    "kpip's dependency resolver does not currently take into "
                    "account all the packages that are installed. This behaviour "
                    "is the source of the following dependency conflicts.",
                    *conflicts,
                )
            )
        )


def report_nothing_installed(
    execution: Any,
    outcome: InstallOutcome,
) -> None:
    installed = installed_index()
    for requirement in outcome.satisfied_requirements:
        if requirement not in outcome.reported_satisfied and not execution.quiet:
            logger.info(f"Requirement already satisfied: {requirement}")
            outcome.reported_satisfied.add(requirement)

    for requirement in execution.bundle.requirements:
        item = install_req_from_line(requirement)
        # Skipped for its markers, as bundle_install_requirements logged:
        # installed or not, it was never a requirement here.
        if not item.match_markers():
            continue
        requirement_name = item.req.name if item.req is not None else requirement
        if (
            installed.get(canonicalize_name(requirement_name)) is not None
            and requirement not in outcome.reported_satisfied
            and not execution.quiet
        ):
            logger.info(f"Requirement already satisfied: {requirement}")


def report_install_summary(
    execution: Any,
    outcome: InstallOutcome,
    plan: Any,
) -> None:
    for requirement in outcome.satisfied_requirements:
        if requirement not in outcome.reported_satisfied and not execution.quiet:
            logger.info(f"Requirement already satisfied: {requirement}")

    if execution.options.report:
        session = execution.bundle.session
        write_install_report(
            execution.options.report,
            outcome.report_items,
            network_stats=(
                session.network_stats.as_dict()
                if session is not None and session.network_stats is not None
                else None
            ),
            resolution_metrics=dict(plan.metrics) if plan is not None else None,
        )

    if (
        outcome.installed
        and not execution.options.dry_run
        and not execution.options.no_deps
        and not execution.options.no_warn_conflicts
        and execution.options.target is None
    ):
        warn_about_install_conflicts(outcome.newly_installed_names)

    if outcome.installed and execution.options.dry_run and not execution.quiet:
        logger.info(f"Would install {' '.join(outcome.installed)}")
    elif outcome.installed and not execution.quiet:
        locked_order = {
            name: index for index, name in enumerate(execution.bundle.locked_links)
        }
        outcome.installed = [
            value
            for _, value in sorted(
                zip(outcome.installed_canonical_names, outcome.installed, strict=True),
                key=lambda item: (
                    0
                    if item[0] in locked_order
                    else item[0] not in outcome.summary_root_names,
                    locked_order.get(item[0], len(locked_order)),
                ),
            )
        ]
        logger.info(f"Successfully installed {' '.join(outcome.installed)}")
        for item in outcome.installed:
            logger.info(f"installed {item}")


def install_editables(
    execution: Any,
    outcome: InstallOutcome,
    *,
    reinstall: bool,
    build_options: dict[str, dict[str, object]],
    preinstalled_editables: set[str],
    preinstalled_editable_reports: dict[str, tuple[Any, Any]],
) -> None:
    for editable in execution.bundle.editables:
        if editable in preinstalled_editables:
            candidate, direct_url = preinstalled_editable_reports[editable]
            outcome.add_report_item(
                candidate_name=candidate.name,
                candidate_version=str(candidate.version),
                requested=True,
                source_url=direct_url.url if direct_url is not None else None,
                source_hashes=None,
                yanked=False,
                is_direct=direct_url is not None,
                editable=True,
            )
            continue

        source_path, direct_url, metadata = prepare_editable_source(
            editable, src_dir=execution.options.src_dir
        )

        built = build_editable_from_source(
            source_path,
            config_settings=execution.bundle.editable_config_settings.get(editable),
            build_constraints=execution.options.build_constraint_files,
            build_isolation=not execution.options.no_build_isolation,
        )

        built_candidate = wheel_candidate_from_path(built)
        editable_requirement = install_req_from_line(editable)

        for raw_constraint in execution.bundle.constraints:
            constraint = parse_requirement(raw_constraint)
            if constraint.canonical_name != built_candidate.canonical_name:
                continue
            if constraint.url is None and not constraint.is_satisfied_by(
                built_candidate.version,
                allow_prereleases=execution.options.pre,
            ):
                raise InstallationError(
                    f"Cannot install {built_candidate.name} "
                    f"{built_candidate.version} because it does not satisfy "
                    f"the constraint {raw_constraint}",
                )

        editable_dependencies = [
            dependency
            for dependency in built_candidate.dependencies
            if marker_applies(
                parse_requirement(str(dependency)).marker,
                extras=(
                    editable_requirement.req.extras
                    if editable_requirement.req is not None
                    else ()
                ),
            )
        ]

        if metadata is not None and editable_requirement.req is not None:
            editable_dependencies = [
                dependency
                for dependency in metadata.dependencies
                if marker_applies(
                    parse_requirement(str(dependency)).marker,
                    extras=editable_requirement.req.extras,
                )
            ]
            for extra in editable_requirement.req.extras:
                editable_dependencies.extend(
                    metadata.optional_dependencies.get(extra, ()),
                )

        if not execution.options.no_deps and editable_dependencies:
            dependency_plan = ResolutionEngine(
                provider=create_candidate_provider(
                    execution.options,
                    execution.bundle,
                    execution.requirements,
                    build_options,
                    execution.target,
                    cache_dir=execution.cache_dir,
                ),
                no_deps=False,
                upgrade=execution.options.upgrade
                and execution.options.upgrade_strategy == "eager",
                upgrade_strategy=execution.options.upgrade_strategy,
                ignore_installed=reinstall,
                constraints=execution.bundle.constraints,
                allow_prereleases=execution.options.pre,
                require_hashes=execution.bundle.require_hashes,
                compute_source_hashes=(
                    bool(execution.options.report)
                    or execution.bundle.require_hashes
                    or bool(execution.bundle.requirement_hashes)
                ),
                ignore_requires_python=execution.options.ignore_requires_python,
                python_version=(
                    execution.python_version
                    if execution.options.python_version
                    else None
                ),
            ).resolve(
                [
                    install_req_from_line(str(requirement))
                    for requirement in editable_dependencies
                ],
            )

            for candidate in dependency_plan.candidates:
                outcome.add_report_item(
                    candidate_name=candidate.name,
                    candidate_version=str(candidate.version),
                    requested=False,
                    source_url=candidate.source_url,
                    source_hashes=(
                        candidate.source_hashes if execution.options.report else None
                    ),
                    yanked=candidate.yanked_reason is not None,
                )

                if not execution.options.dry_run:
                    existing = find_installed(candidate.name)
                    if (
                        execution.options.upgrade
                        and existing is not None
                        and existing.version == candidate.version
                    ):
                        if not execution.quiet:
                            logger.info(
                                f"Requirement already satisfied: {candidate.name}=={candidate.version}"
                            )
                        continue
                    install_candidate(
                        candidate,
                        execution.options,
                        requested=False,
                        reinstall=reinstall,
                    )

                outcome.record_installed(
                    f"{candidate.name}-{candidate.version}", candidate.canonical_name
                )

        if execution.options.only_deps:
            continue

        if execution.options.dry_run:
            candidate = wheel_candidate_from_path(built)
        else:
            candidate = wheel_candidate_from_path(built)
            install_candidate(
                candidate,
                execution.options,
                requested=True,
                reinstall=reinstall,
                direct_url=direct_url,
            )

        outcome.record_installed(
            f"{candidate.name}-{candidate.version}", candidate.canonical_name
        )
        outcome.newly_installed_names.add(candidate.canonical_name)
        outcome.add_report_item(
            candidate_name=candidate.name,
            candidate_version=str(candidate.version),
            requested=True,
            source_url=direct_url.url if direct_url is not None else None,
            source_hashes=None,
            yanked=False,
            is_direct=direct_url is not None,
            requested_extras=(
                tuple(sorted(editable_requirement.req.extras))
                if editable_requirement.req is not None
                else ()
            ),
            requires_dist=tuple(
                str(dependency) for dependency in editable_dependencies
            ),
            editable=True,
        )


def run_install(args: list[str]) -> int:
    prepared = prepare_install(args, create_parser())

    options = prepared.options
    bundle = prepared.bundle
    cache_dir = prepared.cache_dir
    quiet = prepared.quiet

    # As pip: --root, --target and --prefix install somewhere the marker
    # cannot be looked for, and a dry run that only reports changes nothing.
    installs_into_this_environment = (
        not (options.dry_run and options.report)
        and options.root is None
        and options.target is None
        and options.prefix is None
    )

    if installs_into_this_environment and not options.break_system_packages:
        check_externally_managed()

    outcome = InstallOutcome(report_enabled=bool(options.report))
    reinstall = options.force_reinstall or options.ignore_installed

    requested_roots: set[str] = set()
    requested_names: dict[str, str] = {}

    for requirement in bundle.requirements:
        item = install_req_from_line(requirement)
        name = item.req.name if item.req is not None else requirement
        canonical_name = canonicalize_name(name)
        requested_roots.add(canonical_name)
        requested_names.setdefault(canonical_name, name)

    resolved_python_version = python_version(options)
    target = target_context(options)

    requirements = requested_requirements(prepared, target)

    # --only-deps installs what the requirements need and not the
    # requirements, so one that is installed is still resolved: its
    # dependencies are what was asked for.
    if not reinstall and not options.upgrade and not options.only_deps:
        requirements = filter_already_satisfied_requirements(
            requirements,
            outcome,
            allow_prereleases=options.pre,
            follow_dependencies=not options.no_deps,
        )

    execution = InstallExecutionContext(
        options=options,
        bundle=bundle,
        target=target,
        requirements=requirements,
        cache_dir=cache_dir,
        quiet=quiet,
        python_version=resolved_python_version,
    )

    requirement_metadata = requirement_state(requirements, bundle)
    requested_order = requirement_metadata.requested_order
    requested_source_urls = requirement_metadata.requested_source_urls
    summary_root_source_urls = requirement_metadata.summary_root_source_urls
    build_options = requirement_metadata.build_options
    source_requirements_by_name = requirement_metadata.source_requirements_by_name
    source_requirements_by_url = requirement_metadata.source_requirements_by_url
    requested_extras_by_name = requirement_metadata.requested_extras_by_name

    def get_provider() -> Any:
        return create_candidate_provider(
            execution.options,
            execution.bundle,
            execution.requirements,
            build_options,
            execution.target,
            cache_dir=execution.cache_dir,
        )

    if options.verbose and bundle.no_index:
        logger.info("Ignoring indexes:")

    if options.verbose and bundle.index_url:
        for requirement in requirements:
            if requirement.req is None:
                continue
            logger.info(
                f"Getting page {bundle.index_url.rstrip('/')}/{requirement.req.canonical_name}",
            )

    if options.verbose and bundle.find_links:
        for find_link in bundle.find_links:
            if find_link.startswith(("http://", "https://")):
                logger.info(f"Fetching project page and analyzing links: {find_link}")

    preinstalled_editables: set[str] = set()
    preinstalled_editable_reports: dict[str, tuple[Any, Any]] = {}

    # An editable that depends on nothing is installed before the resolve;
    # with --only-deps it is not installed at all.
    if bundle.editables and not options.only_deps:
        for editable in bundle.editables:
            source_path, direct_url, metadata = prepare_editable_source(
                editable,
                build_isolation=not options.no_build_isolation,
                src_dir=options.src_dir,
            )
            if (
                metadata is None
                or metadata.dependencies
                or metadata.optional_dependencies
            ):
                continue

            built = build_editable_from_source(
                source_path,
                config_settings=bundle.editable_config_settings.get(editable),
                build_constraints=options.build_constraint_files,
                build_isolation=not options.no_build_isolation,
            )
            candidate = wheel_candidate_from_path(built)

            for raw_constraint in bundle.constraints:
                constraint = parse_requirement(raw_constraint)
                if (
                    constraint.canonical_name == candidate.canonical_name
                    and constraint.url is None
                    and not constraint.is_satisfied_by(
                        candidate.version,
                        allow_prereleases=options.pre,
                    )
                ):
                    raise InstallationError(
                        f"Cannot install {candidate.name} {candidate.version} because "
                        f"these package versions have conflicting dependencies.",
                    )

            if not options.dry_run:
                install_candidate(
                    candidate,
                    options,
                    requested=True,
                    reinstall=reinstall,
                    direct_url=direct_url,
                )

            outcome.record_installed(
                f"{candidate.name}-{candidate.version}", candidate.canonical_name
            )
            outcome.newly_installed_names.add(candidate.canonical_name)
            preinstalled_editables.add(editable)
            preinstalled_editable_reports[editable] = (candidate, direct_url)

    if bundle.find_links and not quiet:
        logger.info(f"Looking in links: {', '.join(bundle.find_links)}")

    if options.verbose and bundle.requirement_hashes:
        for raw, hashes in bundle.requirement_hashes.items():
            name = canonicalize_name(raw.split("==", 1)[0].strip())
            constraint_hashes = getattr(bundle, "constraint_hashes", {}).get(raw, {})
            digest_count = len(hashes.get("sha256", ()))
            if constraint_hashes:
                digest_count = min(
                    digest_count, len(constraint_hashes.get("sha256", ()))
                )
            logger.info(
                f"Using {digest_count} sha256 hashes for requirement {name!r}",
            )

    plan: ResolutionResult | None = None

    # Nothing is resolved for requirements the environment already satisfies.
    if execution.bundle.requirements and execution.requirements:
        plan_cache_key = cached_remote_plan_key(
            execution.options,
            execution.bundle,
            execution.requirements,
            execution.target,
        )

        replay_key = None

        if plan_cache_key is not None and execution.cache_dir is not None:
            plan = load_cached_install_plan(execution.cache_dir, plan_cache_key)

        elif execution.cache_dir is not None:
            replay_key = replayable_install_plan_key(
                execution.options,
                execution.bundle,
                execution.requirements,
                execution.target,
            )

            if replay_key is not None:
                plan = load_replayable_install_plan(execution.cache_dir, replay_key)

        resolved_fresh = plan is None

        # The last one made answered: the resolve makes a second engine only
        # when its first answer does not stand.
        providers: list[Any] = []

        pycompile = not execution.options.no_compile

        # Started before resolving, so the workers are up by the first wheel.
        archive_workers = (
            ArchiveWorkers.if_available()
            if execution.cache_dir is not None and not execution.options.dry_run
            else None
        )

        def prepare_archive(candidate: Any, cache_dir: str) -> object:
            if archive_workers is not None:
                return archive_workers.prepare(
                    candidate, cache_dir, pycompile=pycompile
                )
            return prepare_cached_wheel(candidate, cache_dir, pycompile=pycompile)

        # Wheels are fetched and unpacked while the solve goes on -- but only
        # onto subinterpreters: on threads the unpacking takes turns with the
        # solve under one interpreter lock, and a cold jupyter install took
        # longer than not prefetching at all.
        prefetch = (
            WheelPrefetch(execution.cache_dir, prepare_archive)
            if plan is None
            and archive_workers is not None
            and execution.cache_dir is not None
            else None
        )

        def prefetching_provider() -> Any:
            provider = get_provider()

            providers.append(provider)

            if prefetch is not None:

                def on_likely(requirement: Any, record: Any) -> None:
                    prefetch(
                        LazyWheelCandidate(
                            record, requirement, provider.get_materializer_internal()
                        )
                    )

                provider.on_likely = on_likely

            return provider

        if plan is None:
            plan = resolve(
                prepared,
                execution.requirements,
                prefetching_provider,
                ignore_installed=reinstall,
                upgrade=execution.options.upgrade,
                upgrade_strategy=execution.options.upgrade_strategy,
                compute_source_hashes=bool(execution.options.report),
                on_decided=prefetch,
            )

        assert plan is not None

        installed = installed_index()

        if plan.metrics.get("nab_conflicts", 0) and not execution.quiet:
            logger.info("This could take a while.")
            if plan.metrics.get("nab_conflicts", 0) >= 8:
                logger.info("This could take a while.")
            if plan.metrics.get("nab_conflicts", 0) >= 13:
                logger.info("This could take a while. press Ctrl + C to cancel.")

        if not execution.quiet and not execution.options.ignore_installed:
            for candidate in plan.candidates:
                for dependency in candidate.dependencies:
                    installed_dependency = installed.get(dependency.canonical_name)
                    if installed_dependency is not None and dependency.is_satisfied_by(
                        installed_dependency.version,
                        allow_prereleases=True,
                    ):
                        logger.info(
                            f"Requirement already satisfied: {dependency.raw or dependency.name}"
                        )

        plan = one_candidate_per_project(plan)

        if options.only_deps:
            plan = without_user_requested(plan, requested_roots, requested_source_urls)

        if (
            options.upgrade
            and options.upgrade_strategy == "only-if-needed"
            and not reinstall
        ):
            needed_versions = {
                dependency.canonical_name: dependency
                for parent in plan.candidates
                for dependency in parent.dependencies
            }
            retained = []
            for candidate in plan.candidates:
                existing = installed.get(candidate.canonical_name)
                dependency = needed_versions.get(candidate.canonical_name)
                if existing is not None and (
                    existing.version == candidate.version
                    or (
                        candidate.canonical_name not in requested_roots
                        and dependency is not None
                        and existing.version is not None
                        and dependency.is_satisfied_by(
                            existing.version, allow_prereleases=True
                        )
                    )
                ):
                    if not quiet:
                        logger.info(
                            f"Requirement already satisfied: {candidate.name}=={candidate.version}"
                        )
                else:
                    retained.append(candidate)
            plan = plan.replace(candidates=tuple(retained))

        warn_about_yanked(plan)

        check_dependency_hashes(prepared, plan, execution.requirements, requested_roots)

        if plan.candidates and (
            not execution.options.dry_run
            or bool(execution.bundle.requirement_hashes)
            or bool(execution.bundle.constraint_hashes)
        ):
            check_local_archive_hashes(execution.bundle, plan)
            try:
                materialized_candidates = prepare_install_candidates(
                    plan.candidates,
                    execution.cache_dir,
                    prepare_archive,
                    prefetch,
                )
            finally:
                if prefetch is not None:
                    prefetch.close()
                if archive_workers is not None:
                    archive_workers.close()
            plan = plan.replace(candidates=tuple(materialized_candidates))

        parsed_constraints = map(parse_requirement, execution.bundle.constraints)
        active_constraints_by_name: dict[str, list[Any]] = {}
        for constraint in parsed_constraints:
            if constraint.marker is None and constraint.url is None:
                active_constraints_by_name.setdefault(
                    constraint.canonical_name,
                    [],
                ).append(constraint)
        for candidate in plan.candidates:
            matching_constraints = active_constraints_by_name.get(
                candidate.canonical_name,
                (),
            )
            if matching_constraints and not all(
                constraint.specifier.contains(candidate.version, allow_prereleases=True)
                for constraint in matching_constraints
            ):
                raise ResolutionError(
                    f"Cannot install {candidate.name} {candidate.version} because these package versions have conflicting dependencies."
                )

        for candidate in plan.candidates:
            requested = requested_extras_by_name.get(candidate.canonical_name, set())
            # Compared normalized, as core metadata requires: a header of
            # ``Foo_Bar`` provides the requested ``foo-bar``.
            provided = {
                canonicalize_name(extra)
                for extra in getattr(candidate, "provided_extras", ())
            }
            for extra in sorted(requested - provided):
                logger.warning(
                    f"{candidate.name} {candidate.version} does not provide the extra '{extra}'"
                )

        for item in plan.satisfied:
            requested = item.requirement.raw or item.requirement.name
            if not quiet:
                if options.upgrade:
                    logger.info(
                        f"Requirement already satisfied: {requested} in "
                        f"{item.distribution.location}",
                    )
                else:
                    logger.info(f"Requirement already satisfied: {requested}")
            outcome.reported_satisfied.add(requested)

        if plan.candidates and not quiet:
            display_candidates = sorted(
                plan.candidates,
                key=lambda candidate: candidate.canonical_name in requested_names,
            )
            logger.info(
                "Installing collected packages: "
                + ", ".join(
                    requested_names.get(candidate.canonical_name, candidate.name)
                    for candidate in display_candidates
                ),
            )

        if plan.candidates:
            batch_target = InstallTarget.from_options(
                plan.candidates[0].canonical_name,
                target=options.target,
                user=options.user,
                root=options.root,
                prefix=options.prefix,
            )

            candidate_direct_urls: dict[str, Any] = {}
            for candidate in plan.candidates:
                source_requirement = source_requirements_by_name.get(
                    candidate.canonical_name,
                ) or source_requirements_by_url.get(candidate.source_url or "")
                direct_url = None
                if (
                    source_requirement is not None
                    and source_requirement.link is not None
                    and source_requirement.req is not None
                    and source_requirement.req.url is not None
                ):
                    direct_url = direct_url_from_link(source_requirement.link)
                candidate_direct_urls[candidate.canonical_name] = direct_url

            if not options.dry_run:
                target_is_empty = target_library_is_empty(batch_target)

                install_order = installation_order(
                    plan.candidates,
                    plan.graph,
                    requested_roots,
                    ignore_requires_python=execution.options.ignore_requires_python,
                )
                try:
                    install_wheels_transactionally(
                        [
                            (
                                candidate.path,
                                candidate.canonical_name in requested_roots,
                                candidate_direct_urls[candidate.canonical_name],
                            )
                            for candidate in install_order
                        ],
                        target=batch_target,
                        pycompile=not execution.options.no_compile,
                        force=reinstall,
                        preserve_existing=execution.options.ignore_installed,
                        lookup_existing=not (
                            execution.options.target is not None
                            and execution.options.ignore_installed
                            and target_is_empty
                        ),
                        candidates=install_order,
                        cache_dir=execution.cache_dir,
                        # pip does not warn for --target or --prefix.
                        warn_script_location=not (
                            execution.options.no_warn_script_location
                            or execution.options.target
                            or execution.options.prefix
                        ),
                    )

                except InstallationError as exc:
                    prefix = "Cannot install "
                    message = str(exc)
                    if message.startswith(prefix):
                        conflict_name = message[len(prefix) :].split(":", 1)[0]
                        for candidate in plan.candidates:
                            if candidate.canonical_name == conflict_name:
                                logger.info(
                                    f"The user requested {candidate.canonical_name} "
                                    f"{candidate.version}",
                                )
                    raise

                if (
                    resolved_fresh
                    and plan_cache_key is not None
                    and execution.cache_dir is not None
                ):
                    save_cached_install_plan(
                        execution.cache_dir,
                        plan_cache_key,
                        tuple(plan.candidates),
                        plan.graph,
                    )

                elif (
                    resolved_fresh
                    and replay_key is not None
                    and execution.cache_dir is not None
                    and providers
                ):
                    record_replayable_install_plan(
                        execution.cache_dir, replay_key, plan, providers[-1]
                    )

        plan_order = {
            id(candidate): index for index, candidate in enumerate(plan.candidates)
        }
        ordered_candidates = (
            sorted(
                plan.candidates,
                key=lambda candidate: (
                    (
                        0,
                        requested_order[candidate.canonical_name],
                    )
                    if candidate.canonical_name in requested_order
                    else (1, plan_order[id(candidate)])
                ),
            )
            if options.user
            else plan.candidates
        )

        for candidate in ordered_candidates:
            display_name = requested_names.get(candidate.canonical_name, candidate.name)
            outcome.record_installed(
                f"{display_name}-{candidate.version}", candidate.canonical_name
            )
            outcome.newly_installed_names.add(candidate.canonical_name)

        report_candidates = sorted(
            plan.candidates,
            key=lambda candidate: (
                (
                    0,
                    requested_order[candidate.canonical_name],
                )
                if candidate.canonical_name in requested_order
                else (1, plan_order[id(candidate)])
            ),
        )

        provenance_by_name: dict[str, tuple[str, tuple[str, ...]]] = {}
        provenance_with_extras: set[str] = set()

        if not quiet:
            for parent in plan.candidates:
                parent_name = requested_names.get(parent.canonical_name, parent.name)
                parent_extras = tuple(
                    sorted(requested_extras_by_name.get(parent.canonical_name, ())),
                )
                for child_name in plan.graph.get(parent.canonical_name, ()):
                    if child_name in provenance_with_extras:
                        continue
                    provenance_by_name[child_name] = (parent_name, parent_extras)
                    if parent_extras:
                        provenance_with_extras.add(child_name)

        for candidate in report_candidates:
            if candidate.source_url in requested_source_urls:
                requested_roots.add(candidate.canonical_name)
                requested_names.setdefault(candidate.canonical_name, candidate.name)

            if candidate.source_url in summary_root_source_urls:
                outcome.summary_root_names.add(candidate.canonical_name)

            if not quiet:
                provenance_value = provenance_by_name.get(candidate.canonical_name)
                provenance = None
                if provenance_value is not None:
                    parent_name, parent_extras = provenance_value
                    provenance = (
                        f"{parent_name}[{','.join(parent_extras)}]"
                        if parent_extras
                        else parent_name
                    )
                suffix = f" (from {provenance})" if provenance else ""
                logger.info(f"Processing {candidate.path}{suffix}")

            source_requirement = source_requirements_by_name.get(
                candidate.canonical_name,
            ) or source_requirements_by_url.get(candidate.source_url or "")

            requested_extras = tuple(
                sorted(requested_extras_by_name.get(candidate.canonical_name, ())),
            )
            if source_requirement is not None and source_requirement.req is not None:
                requested_extras = tuple(
                    sorted(set(requested_extras) | set(source_requirement.req.extras)),
                )

            outcome.add_report_item(
                candidate_name=candidate.name,
                candidate_version=str(candidate.version),
                requested=candidate.canonical_name in requested_roots,
                source_url=candidate.source_url,
                source_hashes=(candidate.source_hashes if options.report else None),
                yanked=candidate.yanked_reason is not None,
                is_direct=(
                    candidate.canonical_name in bundle.locked_direct_names
                    or (
                        source_requirement is not None
                        and source_requirement.req is not None
                        and source_requirement.req.url is not None
                    )
                ),
                requested_extras=requested_extras,
                requires_dist=tuple(
                    str(dependency) for dependency in candidate.dependencies
                ),
            )

    install_editables(
        execution,
        outcome,
        reinstall=reinstall,
        build_options=build_options,
        preinstalled_editables=preinstalled_editables,
        preinstalled_editable_reports=preinstalled_editable_reports,
    )

    if options.root_user_action == "warn":
        warn_if_run_as_root()

    if not outcome.installed and execution.bundle.requirements:
        report_nothing_installed(execution, outcome)
        return 0

    report_install_summary(execution, outcome, plan)
    return 0
