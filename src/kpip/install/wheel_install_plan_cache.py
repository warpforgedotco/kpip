"""Short-lived exact-pin install-plan receipts backed by the archive cache.

An exact-pin command (every root requirement is a plain ``==`` specifier with
no URL, hash, or config-settings override) can skip resolution entirely on a
warm cache: the receipt records which archive-cache entries satisfied the
previous resolve, and loading it revalidates the receipt's shape and confirms
each referenced archive tree still exists before reuse -- it trusts, rather
than re-hashes, an existing tree's contents against the recorded entries.
"""

from __future__ import annotations

lazy import hashlib
lazy import json
lazy import marshal
lazy import os
lazy import tempfile
lazy import time
lazy from types import MappingProxyType

lazy from kpip.core.packaging import parse_requirement
lazy from kpip.core.utils import key_bytes, versioned_bucket
lazy from kpip.core.versions import Version
lazy from kpip.core.wheel import WheelCandidate
lazy from kpip.install.wheel_archive_cache import (
    CachedWheelArchive,
    archive_entry_root,
    load_archive,
    valid_archive_entries,
    valid_sha256,
    wheel_digest,
)
lazy from kpip.resolution.models import ResolutionResult

RESOLUTION_CACHE_BUCKET = versioned_bucket("resolution", 1, interpreter=True)

REMOTE_EXACT_CONTEXT = versioned_bucket("remote-exact", 1)

RESOLUTION_CACHE_TTL_SECONDS = 600.0


def exact_install_plan_key(
    requirements: tuple[object, ...],
    context: tuple[object, ...],
) -> str | None:
    """Return a stable key when every root is an ordinary exact pin."""

    normalized: list[tuple[str, str, tuple[str, ...]]] = []

    seen: set[str] = set()

    for item in requirements:
        requirement = getattr(item, "req", None)

        if (
            requirement is None
            or requirement.url is not None
            or getattr(item, "link", None) is not None
            or getattr(item, "hash_options", None)
            or getattr(item, "config_settings", None)
        ):
            return None

        item_normalized = _normalized_exact_requirement(requirement)

        if item_normalized is None:
            return None

        name = item_normalized[0]

        if name in seen:
            return None

        seen.add(name)

        normalized.append(item_normalized)

    return _exact_install_plan_key(normalized, context)


def exact_install_plan_key_from_strings(
    requirements: tuple[str, ...],
    context: tuple[object, ...],
) -> tuple[str, frozenset[str]] | None:
    """Build the same key from a conservative plain exact-pin command shape."""

    normalized: list[tuple[str, str, tuple[str, ...]]] = []

    seen: set[str] = set()

    try:
        for raw in requirements:
            if ";" in raw or "#" in raw or "\\" in raw:
                return None

            requirement = parse_requirement(raw)

            item_normalized = _normalized_exact_requirement(requirement)

            if item_normalized is None or item_normalized[0] in seen:
                return None

            seen.add(item_normalized[0])

            normalized.append(item_normalized)

    except (TypeError, ValueError):
        return None

    key = _exact_install_plan_key(normalized, context)

    return None if key is None else (key, frozenset(seen))


def _normalized_exact_requirement(
    requirement: object,
) -> tuple[str, str, tuple[str, ...]] | None:
    if getattr(requirement, "url", None) is not None or getattr(
        requirement, "is_unnamed_direct", False
    ):
        return None

    exact_version = getattr(
        getattr(requirement, "specifier", None), "exact_version", None
    )

    if exact_version is None:
        return None

    canonical_name = getattr(requirement, "canonical_name", None)

    extras = getattr(requirement, "extras", ())

    if not isinstance(canonical_name, str) or not isinstance(
        extras,
        (frozenset, list, set, tuple),
    ):
        return None

    if not all(isinstance(extra, str) for extra in extras):
        return None

    return (
        canonical_name,
        str(exact_version),
        tuple(sorted(extra for extra in extras if isinstance(extra, str))),
    )


def _exact_install_plan_key(
    normalized: list[tuple[str, str, tuple[str, ...]]],
    context: tuple[object, ...],
) -> str | None:
    if not normalized:
        return None

    payload = json.dumps(
        (tuple(sorted(normalized)), context),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")

    return hashlib.sha256(payload).hexdigest()


def _resolution_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, RESOLUTION_CACHE_BUCKET, key[:2], f"{key}.bin")


def plain_install_plan_key(
    requirements: tuple[object, ...],
    context: tuple[object, ...],
) -> str | None:
    """A stable key when every root is a plain index requirement, pinned or not.

    Its plan holds only while the index pages the resolve read are unchanged,
    which the caller records with :func:`save_plan_pages` and checks before
    loading it; ``context`` carries the rest of what the resolve depended on.
    """

    normalized: list[tuple[str, str, tuple[str, ...], str]] = []

    for item in requirements:
        requirement = getattr(item, "req", None)

        if (
            requirement is None
            or requirement.url is not None
            or getattr(requirement, "is_unnamed_direct", False)
            or getattr(item, "link", None) is not None
            or getattr(item, "hash_options", None)
            or getattr(item, "config_settings", None)
        ):
            return None

        marker = getattr(item, "markers", None) or getattr(requirement, "marker", None)

        normalized.append(
            (
                requirement.canonical_name,
                str(requirement.specifier),
                tuple(sorted(requirement.extras)),
                "" if marker is None else str(marker),
            ),
        )

    if not normalized:
        return None

    return hashlib.sha256(key_bytes((tuple(sorted(normalized)), context))).hexdigest()


def _pages_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, RESOLUTION_CACHE_BUCKET, key[:2], f"{key}.pages")


def save_plan_pages(cache_dir: str, key: str, pages: tuple[object, ...]) -> None:
    """Record the index pages, with their validators, a plan was resolved from."""

    path = _pages_path(cache_dir, key)
    directory = os.path.dirname(path)

    try:
        os.makedirs(directory, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{key[:12]}-", dir=directory)

        try:
            with os.fdopen(descriptor, "wb") as file:
                marshal.dump((key, pages), file)

            os.replace(temporary, path)

        except BaseException:
            try:
                os.unlink(temporary)

            except FileNotFoundError:
                pass

            raise

    except (OSError, TypeError, ValueError):
        pass


def load_plan_pages(
    cache_dir: str, key: str
) -> tuple[tuple[str, str | None, str | None], ...] | None:
    """The pages :func:`save_plan_pages` recorded for ``key``, each a URL with
    its ETag and Last-Modified; None on a miss."""
    try:
        with open(_pages_path(cache_dir, key), "rb") as file:
            stored_key, pages = marshal.load(file)

    except (EOFError, OSError, TypeError, ValueError):
        return None

    if (
        stored_key != key
        or not isinstance(pages, tuple)
        or not pages
        or not all(
            isinstance(page, tuple)
            and len(page) == 3
            and isinstance(page[0], str)
            and all(value is None or isinstance(value, str) for value in page[1:])
            for page in pages
        )
    ):
        return None

    return pages


def save_cached_install_plan(
    cache_dir: str,
    key: str,
    candidates: tuple[WheelCandidate, ...],
    graph: object,
) -> bool:
    """Publish a short-lived plan receipt after a successful installation."""

    if not valid_sha256(key) or not candidates:
        return False

    records = []

    try:
        for candidate in candidates:
            if candidate.source_kind != "wheel":
                return False

            digest = wheel_digest(candidate, cache_dir)

            archive = load_archive(archive_entry_root(cache_dir, digest), digest)

            if archive is None:
                return False

            source_hashes = dict(candidate.source_hashes or {})

            source_hashes["sha256"] = digest

            records.append(
                (
                    candidate.name,
                    str(candidate.version),
                    digest,
                    tuple(str(dependency) for dependency in candidate.dependencies),
                    tuple(sorted(candidate.provided_extras)),
                    candidate.requires_python,
                    candidate.source_url,
                    tuple(sorted(source_hashes.items())),
                    candidate.source_kind,
                    candidate.source_vcs,
                    candidate.yanked_reason,
                    archive.dist_info,
                    archive.entries,
                ),
            )

        graph_items = tuple(
            sorted(
                (str(name), tuple(sorted(str(child) for child in children)))
                for name, children in getattr(graph, "items", lambda: ())()
            ),
        )

        value = (
            time.time(),
            key,
            tuple(records),
            graph_items,
        )

        path = _resolution_path(cache_dir, key)

        directory = os.path.dirname(path)

        os.makedirs(directory, exist_ok=True)

        descriptor, temporary = tempfile.mkstemp(prefix=f".{key[:12]}-", dir=directory)

        try:
            with os.fdopen(descriptor, "wb") as file:
                marshal.dump(value, file)

            os.replace(temporary, path)

        except BaseException:
            try:
                os.unlink(temporary)

            except FileNotFoundError:
                pass

            raise

    except (OSError, TypeError, ValueError):
        return False

    return True


def _candidate_from_record(cache_dir: str, record: object) -> WheelCandidate | None:
    """Decode one marshal-safe plan record after validating its wire shape."""
    if not isinstance(record, tuple) or len(record) != 13:
        return None

    (
        name,
        version_text,
        digest,
        dependency_texts,
        provided_extras,
        requires_python,
        source_url,
        source_hash_items,
        source_kind,
        source_vcs,
        yanked_reason,
        dist_info,
        archive_entries,
    ) = record

    if not (
        isinstance(name, str)
        and isinstance(version_text, str)
        and isinstance(digest, str)
        and valid_sha256(digest)
        and isinstance(dependency_texts, tuple)
        and all(isinstance(item, str) for item in dependency_texts)
        and isinstance(provided_extras, tuple)
        and all(isinstance(item, str) for item in provided_extras)
        and (requires_python is None or isinstance(requires_python, str))
        and (source_url is None or isinstance(source_url, str))
        and isinstance(source_hash_items, tuple)
        and all(
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(part, str) for part in item)
            for item in source_hash_items
        )
        and isinstance(source_kind, str)
        and source_kind == "wheel"
        and (source_vcs is None or isinstance(source_vcs, str))
        and (yanked_reason is None or isinstance(yanked_reason, str))
        and isinstance(dist_info, str)
        and isinstance(archive_entries, tuple)
        and valid_archive_entries(archive_entries)
    ):
        return None

    tree = os.path.join(archive_entry_root(cache_dir, digest), "tree")

    if not os.path.isdir(tree):
        return None

    typed_archive_entries = tuple(
        (entry[0], entry[1], entry[2], entry[3])
        for entry in archive_entries
        if isinstance(entry, tuple)
        and len(entry) == 4
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
        and isinstance(entry[2], str)
        and isinstance(entry[3], int)
    )
    source_hashes = {
        item[0]: item[1]
        for item in source_hash_items
        if isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], str)
    }

    archive = CachedWheelArchive(digest, tree, dist_info, typed_archive_entries)

    return WheelCandidate(
        name=name,
        version=Version(version_text),
        path=archive.tree,
        dependencies=tuple(
            parse_requirement(item)
            for item in dependency_texts
            if isinstance(item, str)
        ),
        provided_extras=frozenset(
            item for item in provided_extras if isinstance(item, str)
        ),
        requires_python=requires_python,
        source_url=source_url,
        source_hashes=source_hashes,
        source_kind=source_kind,
        source_vcs=source_vcs,
        from_cache=True,
        yanked_reason=yanked_reason,
        wheel_layout=archive,
    )


def load_cached_install_plan(
    cache_dir: str,
    key: str,
    *,
    max_age: float | None = RESOLUTION_CACHE_TTL_SECONDS,
) -> ResolutionResult | None:
    """Load and validate a fresh plan receipt and all referenced archives.

    ``max_age`` None is for a plan whose freshness the caller has checked
    another way: its index pages, unchanged.
    """

    if not valid_sha256(key):
        return None

    path = _resolution_path(cache_dir, key)

    try:
        if (
            max_age is not None
            and time.time() - os.stat(path, follow_symlinks=False).st_mtime > max_age
        ):
            return None

        with open(path, "rb") as file:
            value = marshal.load(file)

    except (EOFError, OSError, TypeError, ValueError):
        return None

    if not (
        isinstance(value, tuple)
        and len(value) == 4
        and isinstance(value[0], float)
        and value[1] == key
        and isinstance(value[2], tuple)
        and isinstance(value[3], tuple)
    ):
        return None

    if max_age is not None and time.time() - value[0] > max_age:
        return None

    candidates: list[WheelCandidate] = []

    try:
        for record in value[2]:
            candidate = _candidate_from_record(cache_dir, record)

            if candidate is None:
                return None

            candidates.append(candidate)

        graph: dict[str, set[str]] = {}

        for graph_record in value[3]:
            if not (
                isinstance(graph_record, tuple)
                and len(graph_record) == 2
                and isinstance(graph_record[0], str)
                and isinstance(graph_record[1], tuple)
                and all(isinstance(child, str) for child in graph_record[1])
            ):
                return None

            graph[graph_record[0]] = {
                child for child in graph_record[1] if isinstance(child, str)
            }

    except (TypeError, ValueError):
        return None

    if len(graph) != len(value[3]):
        return None

    selected = {candidate.canonical_name: candidate for candidate in candidates}

    if len(selected) != len(candidates):
        return None

    for candidate in candidates:
        for dependency in candidate.dependencies:
            selected_dependency = selected.get(dependency.canonical_name)

            if selected_dependency is None or not dependency.is_satisfied_by(
                selected_dependency.version,
            ):
                return None

    return ResolutionResult(
        candidates=tuple(candidates),
        graph={name: frozenset(children) for name, children in graph.items()},
        metrics=MappingProxyType({"warm_resolution_cache_hit": 1}),
    )
