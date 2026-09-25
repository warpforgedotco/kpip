"""Implementation of the ``kpip lock`` command."""

from __future__ import annotations

import os
import time

from kpip.cli.fast import read_requirements
from kpip.cli.lock_format import (
    LOCK_HEADER,
    lock_left_behind,
    lock_preferences,
    previous_lock_digest,
    read_previous_lock,
    toml_string,
    write_lock_output,
)
from kpip.cli.lock_replay import (
    FRESH,
    load_record,
    page_state,
    page_validators,
    replay_key,
    save_record,
    stale_pages,
)
from kpip.cli.parsers.lock import create_parser
from kpip.core.appdirs import command_cache_dir
from kpip.core.expiry import refresh_since
from kpip.core.errors import CommandError, KpipError
from kpip.core.format_control import FormatControl
from kpip.core.hashes import file_hashes
from kpip.core.packaging import (
    marker_applies,
    normalize_python_version,
    parse_requirement,
    set_target_python_version,
    target_python_version,
)
from kpip.core.urls import path_to_url, url_to_path
from kpip.core.versions import InvalidVersion, Version
from kpip.core.wheel import TargetContext
from kpip.index.artifacts import ArtifactLocator
from kpip.index.catalog_cache import serve_summaries_from_snapshot
from kpip.index.config import DEFAULT_INDEX_URL
from kpip.index.provider import CandidateProvider
from kpip.index.vcs_urls import vcs_reference
from kpip.network.deferred import DeferredNetworkSession
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.files import parse_requirements
from kpip.resolution.input_requirements import install_req_from_line

TYPE_CHECKING = False

if TYPE_CHECKING:
    from argparse import Namespace

    from kpip.resolution.req_install import InstallRequirement


def tag_python_version(value: str) -> str:
    """``--python-version`` in the spelling wheel tags use: ``3.8.2`` -> ``3.8``.

    Tags name a minor series and nothing finer, so a three-part operand has
    to lose its patch component; passing it through whole would build the
    tag ``cp382``, which no wheel carries.
    """
    parts = value.split(".")

    return ".".join(parts[:2]) if len(parts) > 1 else value


def applies_to_target(requirement: str | InstallRequirement) -> bool:
    """Whether a requirement's environment marker holds for the lock's target.

    Requirement files routinely carry one line per interpreter --

        aiohttp==3.8.5;python_version<'3.12'
        aiohttp==3.9.0b0;python_version>='3.12'

    -- and only one of them is a requirement of the lock being written. Both
    were being kept, which locked a release the target interpreter cannot
    use, and, where the two lines named the same project, turned a file that
    resolves into "your project's requirements cannot be satisfied".

    The target is already in place when this runs: ``run_lock`` sets it
    before calling ``perform_lock``, so ``--python-version`` is what the
    marker is read against rather than the interpreter kpip happens to be
    running on.

    Anything that carries no marker is kept, and so is anything this parser
    cannot read -- a path, a URL, an option line. Dropping what cannot be
    understood would be a worse answer than handing it to the resolver.
    """
    if not isinstance(requirement, str):
        return requirement.match_markers()

    try:
        parsed = parse_requirement(requirement)

    except ValueError:
        return True

    if parsed.marker is None:
        return True

    return marker_applies(parsed.marker, extras=parsed.extras)


def read_requirement_lines(filename: str) -> list[str]:
    """Read requirement lines, raising ``CommandError`` if the file is unreadable."""
    values = read_requirements(filename)

    if values is None:
        raise CommandError(f"Could not read requirement file: {filename}")

    return values


def remote_hashed_wheel(candidate: object) -> dict[str, object] | None:
    """Render a remote wheel from its index facts without opening the archive."""
    if getattr(candidate, "source_kind", None) != "wheel" or getattr(
        candidate, "source_is_direct", False
    ):
        return None
    source = getattr(candidate, "source_url", None)
    filename = getattr(candidate, "source_filename", None)
    hashes = getattr(candidate, "source_hashes", None) or {}
    digest = hashes.get("sha256")
    if (
        not isinstance(source, str)
        or not source.startswith(("http://", "https://"))
        or not isinstance(filename, str)
        or not filename
        or not isinstance(digest, str)
        or not digest
    ):
        return None
    return {
        "name": getattr(candidate, "name"),
        "version": str(getattr(candidate, "version")),
        "wheels": [
            {
                "name": filename,
                "url": source,
                "hashes": {"sha256": digest},
            },
        ],
    }


def remote_hashed_sdist(candidate: object) -> dict[str, object] | None:
    """Render a remote index sdist without downloading or unpacking it."""
    if getattr(candidate, "source_kind", None) != "sdist" or getattr(
        candidate, "source_is_direct", False
    ):
        return None
    source = getattr(candidate, "source_url", None)
    filename = getattr(candidate, "source_filename", None)
    hashes = getattr(candidate, "source_hashes", None) or {}
    digest = hashes.get("sha256")
    if (
        not isinstance(source, str)
        or not source.startswith(("http://", "https://"))
        or not isinstance(filename, str)
        or not filename
        or not isinstance(digest, str)
        or not digest
    ):
        return None
    return {
        "name": getattr(candidate, "name"),
        "version": str(getattr(candidate, "version")),
        "sdist": {
            "name": filename,
            "url": source,
            "hashes": {"sha256": digest},
        },
    }


def render_lock(packages: list[dict[str, object]]) -> str:
    lines = list(LOCK_HEADER)

    if not packages:
        # ``packages`` is required by PEP 751, and an array of tables with no
        # entries writes no key at all. A lock whose requirements were all
        # for another interpreter is the reachable case, and without this it
        # is a file kpip itself refuses to read back.
        lines.append("packages = []")

        lines.append("")

        return "\n".join(lines)

    for package in packages:
        lines.append("[[packages]]")

        lines.append(f"name = {toml_string(str(package['name']))}")

        if "version" in package:
            lines.append(f"version = {toml_string(str(package['version']))}")

        if "vcs" in package:
            vcs = package["vcs"]

            assert isinstance(vcs, dict)

            lines.append("[packages.vcs]")

            lines.append(f"type = {toml_string(str(vcs['type']))}")  # ty:ignore[invalid-argument-type]

            lines.append(f"url = {toml_string(str(vcs['url']))}")  # ty:ignore[invalid-argument-type]

            lines.append(
                f"requested-revision = {toml_string(str(vcs['requested-revision']))}",  # ty:ignore[invalid-argument-type]
            )

            lines.append(f"commit-id = {toml_string(str(vcs['commit-id']))}")  # ty:ignore[invalid-argument-type]

        if "archive" in package:
            archive = package["archive"]

            assert isinstance(archive, dict)

            hashes = archive["hashes"]  # ty:ignore[invalid-argument-type]

            assert isinstance(hashes, dict)

            lines.append("[packages.archive]")

            lines.append(f"url = {toml_string(str(archive['url']))}")  # ty:ignore[invalid-argument-type]

            lines.append("[packages.archive.hashes]")

            lines.append(f"sha256 = {toml_string(str(hashes['sha256']))}")  # ty:ignore[invalid-argument-type]

        if "directory" in package:
            directory = package["directory"]

            lines.append("[packages.directory]")

            if isinstance(directory, dict) and directory.get("editable"):
                lines.append("editable = true")

            lines.append('path = "."')

        for artifact_key in ("sdist", "wheels"):
            artifact = package.get(artifact_key)

            if artifact is None:
                continue

            artifacts = artifact if isinstance(artifact, list) else [artifact]

            for entry in artifacts:
                assert isinstance(entry, dict)

                header = (
                    f"[[packages.{artifact_key}]]"
                    if artifact_key == "wheels"
                    else f"[packages.{artifact_key}]"
                )

                lines.append(header)

                lines.append(f"name = {toml_string(str(entry['name']))}")  # ty:ignore[invalid-argument-type]

                lines.append(f"url = {toml_string(str(entry['url']))}")  # ty:ignore[invalid-argument-type]

                hashes = entry["hashes"]  # ty:ignore[invalid-argument-type]

                assert isinstance(hashes, dict)

                lines.append(f"[packages.{artifact_key}.hashes]")

                lines.append(f"sha256 = {toml_string(str(hashes['sha256']))}")  # ty:ignore[invalid-argument-type]

        lines.append("")

    return "\n".join(lines)


def _resolved_metadata_name(candidate: object) -> str | None:
    """The project name from the metadata the resolver already read.

    The lock used to unpack and build every URL archive again at the end,
    only to learn the name its metadata declares; the resolver had read
    that metadata to resolve the archive, and holds it.  ``None`` when the
    candidate carries no loaded metadata, and the caller builds as before.
    """
    record = getattr(candidate, "record_internal", None)
    if record is None or getattr(record, "metadata_loader", None) is None:
        return None
    try:
        return record.metadata().name
    except (KpipError, OSError, ValueError, RuntimeError):
        return None


def lock_replay_key(
    options: Namespace, cache_dir: str | None, previous: bytes | None
) -> bytes | None:
    """The key this lock is replayed under, when it can be at all.

    ``previous`` is the lock it starts from, as ``read_previous_lock`` read it.
    """

    if cache_dir is None or options.no_index or options.find_links or options.editable:
        return None

    return replay_key(
        requirements=options.requirements,
        requirement_files=options.requirement,
        constraint_files=options.constraints,
        index_urls=(DEFAULT_INDEX_URL,),
        no_binary=options.no_binary,
        no_build_isolation=options.no_build_isolation,
        python_version=options.python_version,
        previous_lock=previous_lock_digest(previous, options.upgrade_packages),
    )


def replay_after_revalidation(
    options: Namespace,
    cache_dir: str | None,
    session: DeferredNetworkSession,
    previous: bytes | None,
) -> bool:
    """Replay the recorded lock once every page it read has been revalidated.

    The fast path already replays a lock whose pages are all fresh. After
    the index's ``max-age`` they are stale, and resolving would revalidate
    them one dependency level at a time as it walked the graph; asking about
    every recorded page at once costs one round of mostly-304 answers, and
    if none changed the recorded lock is still the answer. If one did, the
    resolve that follows finds every page fresh in the cache.
    """

    key = lock_replay_key(options, cache_dir, previous)
    http_cache = session.cache

    if key is None or http_cache is None:
        return False

    assert cache_dir is not None

    record = load_record(cache_dir, key)

    if record is None:
        return False

    if page_state(http_cache, record.pages) != FRESH:
        from kpip.index.source_locations import SimpleIndexSource, refresh_pages

        refresh_pages(
            SimpleIndexSource(DEFAULT_INDEX_URL, (), session),
            stale_pages(http_cache, record.pages),
        )

        if page_state(http_cache, record.pages) != FRESH:
            return False

    write_lock_output(options.output, record.rendered)

    return True


def record_replayable_lock(
    options: Namespace,
    cache_dir: str | None,
    provider: CandidateProvider,
    session: DeferredNetworkSession,
    rendered: str,
    previous: bytes | None,
) -> None:
    """Keep this lock so an identical one can replay it while its pages are unchanged.

    It is kept for a lock that starts from ``previous``, as this one did,
    and for the next lock, which starts from this one: preferring the
    versions of a lock that satisfies the same inputs gives that lock back,
    so the answer is known without resolving. With ``--upgrade-package`` it
    is kept only for the first; the package resolved afresh could come out
    differently when the rest are preferred from the start.
    """

    http_cache = session.cache
    starts = [previous]

    if not options.upgrade_packages:
        starts.append(lock_left_behind(options.output, options.upgrade, rendered))

    keys = {
        key
        for start in starts
        if (key := lock_replay_key(options, cache_dir, start)) is not None
    }

    if not keys or http_cache is None:
        return

    assert cache_dir is not None

    pages: set[str] = set()

    for source in provider.index_sources:
        pages.update(source.pages_read)

    validators = page_validators(http_cache, pages) if pages else None

    if validators is not None:
        for key in keys:
            save_record(cache_dir, key, validators, rendered)


def run_lock(args: list[str]) -> int:
    options = create_parser().parse_args(args)

    resolvers: list[ResolutionEngine] = []

    if not options.python_version:
        try:
            return perform_lock(options, resolvers)

        finally:
            close_resolvers(resolvers)

    target = normalize_python_version(str(options.python_version))

    try:
        Version(target)

    except InvalidVersion:
        # Caught here rather than deep in the resolve, where it would
        # surface as a traceback from whichever candidate was examined first.
        raise CommandError(
            "--python-version expects a version like 3.8, "
            f"not {options.python_version!r}",
        ) from None

    # The target is process-global while the resolve runs -- markers,
    # Requires-Python and wheel tags all have to agree on which interpreter
    # the lock is for -- so it is restored even when the resolve raises,
    # which matters to every caller that runs a command in-process. A caller
    # that had a target of its own gets it back, rather than the running
    # interpreter.
    previous = target_python_version()

    set_target_python_version(target)

    try:
        return perform_lock(options, resolvers)

    finally:
        # Before the target is restored, not after: a metadata worker still
        # running would evaluate markers against this interpreter and
        # persist the answer under the lock's own key.
        close_resolvers(resolvers)

        set_target_python_version(previous)


def close_resolvers(resolvers: list[ResolutionEngine]) -> None:
    """Release each resolver, waiting for the work still in its hands."""
    while resolvers:
        resolvers.pop().close()


def perform_lock(options: Namespace, resolvers: list[ResolutionEngine]) -> int:
    if options.refresh:
        refresh_since(time.time())

    cache_dir = command_cache_dir(options.cache_dir, options.no_cache_dir)

    resolution_session = DeferredNetworkSession(cache_dir=cache_dir)

    previous = read_previous_lock(options.output, options.upgrade)

    if replay_after_revalidation(options, cache_dir, resolution_session, previous):
        return 0

    page_cache = resolution_session.page_cache()

    serve_summaries_from_snapshot(page_cache)

    preferences = lock_preferences(previous, options.upgrade_packages)

    artifact_locator = ArtifactLocator(resolution_session, cache_dir=cache_dir)

    quiet_environment = os.environ.get("KPIP_QUIET")

    if options.quiet:
        os.environ["KPIP_QUIET"] = "1"

    format_control = None

    if options.no_binary:
        format_control = FormatControl()

        for value in options.no_binary:
            format_control.apply("no-binary", value)

    requirements: list[str | InstallRequirement] = []

    locked_order: list[str] = []

    archive_packages: list[dict] = []

    directory_packages: list[dict] = []

    for value in options.requirements:
        local_directory = os.path.abspath(value)

        if os.path.isdir(local_directory):
            from kpip.build.build_backend import prepare_project_metadata

            metadata = prepare_project_metadata(
                local_directory,
                build_isolation=False,
            )

            directory_packages.append(
                {"name": metadata.name, "directory": {"path": "."}},
            )

            continue

        if "://" not in value and not value.startswith(("git+", "hg+", "svn+", "bzr+")):
            requirements.append(value)

            continue

        parsed = parse_requirement(value)

        if parsed.url is None or parsed.name != parsed.url:
            requirements.append(value)

            continue

        item = install_req_from_line(value)

        if item.link is not None and not item.link.is_vcs:
            from kpip.build.build_backend import prepare_project_metadata

            source = artifact_locator.ensure_local(item.link.url)

            if os.path.isdir(source):
                metadata = prepare_project_metadata(source, build_isolation=False)

                directory_packages.append(
                    {"name": metadata.name, "directory": {"path": "."}},
                )

                continue

            import shutil
            import tempfile

            from kpip.build.build import unpack_source

            with tempfile.TemporaryDirectory(prefix="kpip-lock-source-") as directory:
                archive = os.path.join(directory, "source.tar.gz")

                shutil.copyfile(source, archive)

                project = unpack_source(archive, os.path.join(directory, "project"))

                metadata = prepare_project_metadata(project, build_isolation=False)

            source_digest = file_hashes(source)["sha256"]

            archive_packages.append(
                {
                    "name": metadata.name,
                    "archive": {
                        "url": value,
                        "hashes": {
                            "sha256": source_digest,
                        },
                    },
                },
            )

            continue

        requirements.append(value)

    editable_packages: list[dict] = []

    for value in options.editable:
        from kpip.build.build_backend import prepare_project_metadata

        item = install_req_from_line(value)

        item.editable = True

        requirements.append(item)

        editable_path = os.path.realpath(value)

        metadata = prepare_project_metadata(editable_path)

        editable_packages.append(
            {
                "name": metadata.name,
                "directory": {"editable": True, "path": "."},
            },
        )

    for filename in options.requirement:
        if os.path.basename(filename).startswith("pylock") and filename.endswith(
            ".toml",
        ):
            for item in parse_requirements(filename, resolution_session):
                if item.locked_name is not None:
                    locked_order.append(item.locked_name)

                if (
                    item.locked_direct
                    and item.locked_name is not None
                    and item.locked_link is not None
                    and not item.locked_link.startswith(("git+", "hg+", "svn+", "bzr+"))
                    and not (
                        item.locked_link.startswith("file:")
                        and os.path.isdir(url_to_path(item.locked_link))
                    )
                ):
                    archive_packages.append(
                        {
                            "name": item.locked_name,
                            "archive": {
                                "url": item.locked_link,
                                "hashes": {
                                    algorithm: values[0]
                                    for algorithm, values in (
                                        item.locked_hashes or {}
                                    ).items()
                                    if values
                                },
                            },
                        },
                    )

                    continue

                requirements.append(
                    install_req_from_line(
                        f"{item.locked_name} @ {item.locked_link}"
                        if item.locked_name is not None and item.locked_link is not None
                        else item.requirement,
                    ),
                )

        else:
            requirements.extend(read_requirement_lines(filename))

    constraints = [
        requirement
        for filename in options.constraints
        for requirement in read_requirement_lines(filename)
    ]

    if not requirements and not archive_packages and not directory_packages:
        raise CommandError("You must give at least one requirement")

    # After the emptiness check, not before: a file whose every line is for
    # another interpreter is a file that asks for nothing here, and the
    # honest answer to that is an empty lock rather than an error.
    requirements = [item for item in requirements if applies_to_target(item)]

    constraints = [value for value in constraints if applies_to_target(value)]

    plan = None

    provider = None

    string_requirements = [item for item in requirements if isinstance(item, str)]

    if (
        len(string_requirements) == len(requirements)
        and string_requirements
        and options.no_index
        and not options.no_binary
        # The wheelhouse path builds its own provider with no target, so it
        # would rank wheels for this interpreter rather than the one asked for.
        and not options.python_version
    ):
        plan = ResolutionEngine.resolve_wheelhouse(
            options.find_links,
            string_requirements,
            constraints=constraints,
            session=resolution_session,
            preferences=preferences,
        )

    if plan is None and requirements:

        def build_resolver() -> ResolutionEngine:
            return ResolutionEngine(
                provider=CandidateProvider.from_options(
                    find_links=options.find_links,
                    no_index=options.no_index,
                    format_control=format_control,
                    build_isolation=not options.no_build_isolation,
                    wheel_cache_dir=cache_dir,
                    session=resolution_session,
                    dry_run=True,
                    target=(
                        TargetContext(
                            python_version=tag_python_version(
                                str(options.python_version)
                            ),
                        )
                        if options.python_version
                        else None
                    ),
                ),
                no_deps=False,
                ignore_installed=True,
                constraints=constraints,
                preferences=preferences,
                python_version=(
                    normalize_python_version(str(options.python_version))
                    if options.python_version
                    else None
                ),
            )

        # Closed by the caller rather than here: the candidates it produced
        # are read below, and closing takes the prepared sources with it.
        plan = ResolutionEngine.resolve_serving_stale_pages(
            build_resolver,
            [
                item if not isinstance(item, str) else install_req_from_line(item)
                for item in requirements
            ],
            engines=resolvers,
        )
        provider = resolvers[-1].provider

    packages: list[dict] = [
        *editable_packages,
        *directory_packages,
        *archive_packages,
    ]

    editable_names = {str(package["name"]) for package in editable_packages}

    # Only a lock of hashed index wheels is replayed: the dependencies of an
    # sdist come from building it, which the index pages do not pin.
    every_package_is_an_index_wheel = not packages and not locked_order

    for candidate in plan.candidates if plan is not None else []:
        source = candidate.source_url

        if source is None:
            continue

        remote_artifact = remote_hashed_wheel(candidate)
        if remote_artifact is None:
            every_package_is_an_index_wheel = False
            remote_artifact = remote_hashed_sdist(candidate)
        if remote_artifact is not None:
            packages.append(remote_artifact)
            continue

        candidate_path = None

        if candidate.source_kind == "wheel" and not getattr(
            candidate,
            "source_is_direct",
            False,
        ):
            candidate_path = candidate.path

        if candidate_path is not None:
            source_path = candidate_path

        elif source.startswith("file:"):
            source_path = url_to_path(source)

        else:
            source_path = None

        if candidate.source_vcs:
            from kpip.index.vcs import (
                git_revision,
                materialize_vcs,
                release_checkout,
            )

            reference = vcs_reference(source)

            commit_id = getattr(candidate, "source_vcs_revision", None)

            if commit_id is None:
                checkout = materialize_vcs(source, emit_resolution=False)

                commit_id = git_revision(checkout)

                release_checkout(checkout)

            packages.append(
                {
                    "name": candidate.name,
                    "vcs": {
                        "type": candidate.source_vcs,
                        "url": reference.repo_url,
                        "requested-revision": reference.requested_revision,
                        "commit-id": commit_id,
                    },
                },
            )

            continue

        if candidate.source_kind == "source-tree" and candidate.name in editable_names:
            continue

        if candidate.source_kind == "source-tree":
            packages.append({"name": candidate.name, "directory": {"path": "."}})

            continue

        if source_path is None:
            if source.startswith(("http://", "https://")):
                archive_digest = (candidate.source_hashes or {}).get("sha256")
                archive_path = artifact_locator.ensure_local(source)

                if archive_digest is None:
                    archive_digest = file_hashes(archive_path)["sha256"]

                package_name = _resolved_metadata_name(candidate)

                if package_name is None:
                    # The resolver did not read this artifact's metadata, so
                    # learn the project name the way it would have.
                    package_name = candidate.name

                    import tempfile

                    from kpip.build.build import unpack_source

                    with tempfile.TemporaryDirectory(prefix="kpip-lock-") as temp_dir:
                        from kpip.build.build_backend import prepare_project_metadata

                        try:
                            project = prepare_project_metadata(
                                unpack_source(archive_path, temp_dir),
                                build_isolation=False,
                            )
                        except (KpipError, OSError, ValueError):
                            pass
                        else:
                            package_name = project.name

                packages.append(
                    {
                        "name": package_name,
                        "archive": {
                            "url": source,
                            "hashes": {
                                "sha256": archive_digest,
                            },
                        },
                    },
                )

            continue

        digest = (candidate.source_hashes or {}).get("sha256")

        if digest is None:
            digest = file_hashes(source_path)["sha256"]

        if candidate_path is not None:
            artifact_url = source

        else:
            artifact_url = path_to_url(str(source_path))

        artifact = {
            "name": os.path.basename(source_path),
            "url": artifact_url,
            "hashes": {"sha256": digest},
        }

        key = "sdist" if candidate.source_kind == "sdist" else "wheels"

        value: object = [artifact] if key == "wheels" else artifact

        packages.append(
            {"name": candidate.name, "version": str(candidate.version), key: value},
        )

    if locked_order:
        order = {name: index for index, name in enumerate(locked_order)}

        packages.sort(key=lambda package: order.get(str(package["name"]), len(order)))

    rendered = render_lock(packages)

    write_lock_output(options.output, rendered)

    if page_cache is not None:
        page_cache.save_snapshot()

    if every_package_is_an_index_wheel and provider is not None:
        record_replayable_lock(
            options, cache_dir, provider, resolution_session, rendered, previous
        )

    if quiet_environment is None:
        os.environ.pop("KPIP_QUIET", None)

    else:
        os.environ["KPIP_QUIET"] = quiet_environment

    return 0
