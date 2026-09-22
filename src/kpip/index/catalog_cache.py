"""Persistent cache for parsed Simple API catalog entries."""

from __future__ import annotations

from kpip.core.utils import versioned_bucket

import hashlib
import marshal
import threading
import urllib.parse

from kpip.core.versions import InvalidVersion, Version, is_version_wire
from kpip.core.wheel import WheelFile, WheelTag, parse_wheel_file
from kpip.index.datetime import parse_iso_datetime
from kpip.index.directory_index import project_version_from_filename
from kpip.index.links import Link
from kpip.index.source_models import ArtifactKind, MetadataFile

TYPE_CHECKING = False

if TYPE_CHECKING:
    from typing import Any

PREFIX = f"{versioned_bucket('kpip-index-catalog', 1)}:"
SUMMARY_PREFIX = f"{versioned_bucket('kpip-index-summary', 1)}:"
CHOICE_PREFIX = f"{versioned_bucket('kpip-index-choice', 1)}:"
SUMMARY_HEADER = versioned_bucket("kpip-index-summary", 1).encode() + b"\0"
CHOICE_HEADER = versioned_bucket("kpip-index-choice", 1).encode() + b"\0"

WHEEL_RECORD = 1
SDIST_RECORD = 2
RECORD_REQUIRES_PYTHON = 3
RECORD_YANKED = 4
RECORD_WHEEL_IDENTITY = 7
WHEEL_IDENTITY_NAME = 0
WHEEL_IDENTITY_VERSION = 1
WHEEL_IDENTITY_BUILD_TAG = 2
WHEEL_IDENTITY_TAGS = 3

CatalogRecord = tuple[object, ...]
CatalogArtifact = tuple[int, CatalogRecord]
CatalogFact = tuple[int, str | None, str | None]
CatalogGroup = tuple[str, str, list[CatalogArtifact], list[CatalogFact]]
CatalogData = tuple[list[CatalogGroup], list[CatalogRecord]]
CatalogSummaryGroup = tuple[str, str, tuple[object, ...], list[CatalogFact]]
CatalogChoice = tuple[CatalogRecord, int, int | None]
CatalogChoices = dict[str, CatalogChoice | None]
CatalogChoiceProfiles = dict[tuple[str, bool, bool], CatalogChoices]
CatalogSummary = tuple[
    str,
    list[CatalogSummaryGroup],
    bool,
    CatalogChoiceProfiles,
]

_PENDING_CATALOGS_ATTRIBUTE = "_kpip_pending_catalogs"
_PENDING_CATALOGS_LIMIT = 64
_VALIDATED_CATALOGS_ATTRIBUTE = "_kpip_validated_catalogs"

_VALIDATED_CATALOGS_LOCK = threading.Lock()
"""Guards the memo's ordering, which several worker threads maintain.

Reading a dict is atomic, but moving a key to the end and evicting the
oldest are each several operations, and a thread switch between picking
the oldest key and removing it raised ``KeyError`` when another thread had
already removed it.  The lock is held only for those dict operations, never
across a decode or a read.
"""
_VALIDATED_CATALOGS_LIMIT = 8


def cache_key(url: str) -> str:
    return PREFIX + url


def summary_key(url: str) -> str:
    return SUMMARY_PREFIX + url


def choice_key(
    url: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
) -> str:
    return f"{CHOICE_PREFIX}{target_key}:{int(allow_binary)}:{int(allow_source)}:{url}"


def load_links(cache: Any, url: str) -> list[Link] | None:
    records = load_records(cache, url)
    return (
        None
        if records is None
        else [link_from_record(record, source_url=url) for record in records]
    )


def load_records(cache: Any, url: str) -> list[tuple[object, ...]] | None:
    catalog = load_catalog(cache, url)
    if catalog is None:
        return None
    groups, unparsed = catalog
    return [
        *(
            record
            for _name, _version, artifacts, _facts in groups
            for _kind, record in artifacts
        ),
        *unparsed,
    ]


def load_catalog(cache: Any, url: str) -> CatalogData | None:
    pending = _pending_catalogs(cache)
    if pending is not None:
        catalog = pending.pop(url, None)
        if catalog is not None:
            return catalog
    loaded = _load_catalog_uncached(cache, url)
    return None if loaded is None else loaded[0]


def _pending_catalogs(cache: Any) -> dict[str, CatalogData] | None:
    """Return the per-cache handoff used between summary and catalog loads."""
    try:
        attributes = vars(cache)
    except TypeError:
        return None
    pending = attributes.get(_PENDING_CATALOGS_ATTRIBUTE)
    if pending is None:
        pending = {}
        attributes[_PENDING_CATALOGS_ATTRIBUTE] = pending
    return pending


def _remember_pending_catalog(
    cache: Any,
    url: str,
    catalog: CatalogData,
) -> None:
    pending = _pending_catalogs(cache)
    if pending is None:
        return
    pending[url] = catalog
    while len(pending) > _PENDING_CATALOGS_LIMIT:
        pending.pop(next(iter(pending)))


def _validated_catalogs(
    cache: Any,
) -> dict[str, tuple[bytes, CatalogData]] | None:
    """Return a small per-cache memo of blobs already validated in this process."""
    try:
        attributes = vars(cache)
    except TypeError:
        return None
    validated = attributes.get(_VALIDATED_CATALOGS_ATTRIBUTE)
    if validated is None:
        validated = {}
        attributes[_VALIDATED_CATALOGS_ATTRIBUTE] = validated
    return validated


def _remember_validated_catalog(
    cache: Any,
    url: str,
    raw: bytes,
    catalog: CatalogData,
) -> None:
    validated = _validated_catalogs(cache)
    if validated is None:
        return
    with _VALIDATED_CATALOGS_LOCK:
        validated.pop(url, None)
        validated[url] = raw, catalog
        while len(validated) > _VALIDATED_CATALOGS_LIMIT:
            oldest = next(iter(validated), None)
            if oldest is None:
                break
            validated.pop(oldest, None)


def _load_catalog_uncached(
    cache: Any,
    url: str,
) -> tuple[CatalogData, bytes | None] | None:
    """Load compact records grouped by their target-independent release."""
    if cache is None:
        return None
    raw = cache.get_atomic(cache_key(url))
    if raw is None:
        return None
    validated = _validated_catalogs(cache)
    if validated is not None:
        known = validated.get(url)
        if known is not None and known[0] == raw:
            with _VALIDATED_CATALOGS_LOCK:
                # Another thread may have evicted it since the read above;
                # the entry is still valid, it just returns to the end.
                validated.pop(url, None)
                validated[url] = known
            return known[1], raw
    try:
        payload = marshal.loads(raw)
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or payload[0] != "kpip-index-catalog"
            or not isinstance(payload[1], list)
            or not isinstance(payload[2], list)
        ):
            return None
        groups = payload[1]
        unparsed = payload[2]
        if not all(valid_group(group) for group in groups) or not all(
            valid_record(record) for record in unparsed
        ):
            return None
        catalog = groups, unparsed
        _remember_validated_catalog(cache, url, raw, catalog)
        return catalog, raw
    except (EOFError, TypeError, ValueError, KeyError, IndexError):
        return None


def load_catalog_checked(cache: Any, url: str, generation: str) -> CatalogData | None:
    """The stored catalog only if its payload still hashes to ``generation``.

    Bypasses the pending-catalog handoff on purpose: the handoff carries no
    generation, and the hash check is the point -- callers persist derived
    data (choices) under this generation and must not do so from a blob that
    was evicted or replaced since the summary was read.
    """
    loaded = _load_catalog_uncached(cache, url)
    if loaded is None:
        return None
    catalog, raw = loaded
    if raw is None or catalog_generation(raw) != generation:
        return None
    return catalog


def group_artifacts_by_version(
    catalog: CatalogData,
    name: str,
) -> dict[str, list[CatalogArtifact]]:
    """Collect one project's artifacts per version text, merging duplicates."""
    groups: dict[str, list[CatalogArtifact]] = {}
    for group_name, version_text, artifacts, _facts in catalog[0]:
        if group_name == name:
            existing = groups.get(version_text)
            if existing is None:
                groups[version_text] = list(artifacts)
            else:
                existing.extend(artifacts)
    return groups


def load_summary(cache: Any, url: str) -> CatalogSummary | None:
    """Load the release-only resolver view, compiling it locally if needed."""
    if cache is None:
        return None
    raw = cache.get_atomic(summary_key(url))
    if raw is not None:
        summary = decode_summary(raw)
        if summary is not None:
            return summary
    pending = _pending_catalogs(cache)
    catalog = pending.pop(url, None) if pending is not None else None
    catalog_raw: bytes | None = None
    if catalog is None:
        loaded = _load_catalog_uncached(cache, url)
        if loaded is not None:
            catalog, catalog_raw = loaded
    if catalog is None:
        return None
    _remember_pending_catalog(cache, url, catalog)
    if catalog_raw is None:
        catalog_raw = cache.get_atomic(cache_key(url))
    if catalog_raw is None:
        return None
    generation = catalog_generation(catalog_raw)
    save_summary(cache, url, catalog, generation)
    return summary_from_catalog(catalog, generation)


def decode_summary(raw: bytes) -> CatalogSummary | None:
    if not raw.startswith(SUMMARY_HEADER):
        return None
    payload = decode_checked_payload(raw, SUMMARY_HEADER)
    if (
        not isinstance(payload, tuple)
        or len(payload) != 4
        or not isinstance(payload[0], str)
        or not isinstance(payload[1], list)
        or not isinstance(payload[2], bool)
        or not valid_choice_profiles(payload[3])
        or not all(valid_summary_group(group) for group in payload[1])
    ):
        return None
    return payload[0], payload[1], payload[2], payload[3]  # ty:ignore[invalid-return-type]


def load_choices(
    cache: Any,
    url: str,
    generation: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
) -> CatalogChoices:
    if cache is None:
        return {}
    raw = cache.get_atomic(choice_key(url, target_key, allow_binary, allow_source))
    if raw is None or not raw.startswith(CHOICE_HEADER):
        return {}
    payload = decode_checked_payload(raw, CHOICE_HEADER)
    if (
        not isinstance(payload, tuple)
        or len(payload) != 2
        or payload[0] != generation
        or not valid_choices(payload[1])
    ):
        return {}
    choices = payload[1]
    embed_summary_choices(
        cache,
        url,
        generation,
        target_key,
        allow_binary,
        allow_source,
        choices,  # ty:ignore[invalid-argument-type]
    )
    return choices  # ty:ignore[invalid-return-type]


def save_choices(
    cache: Any,
    url: str,
    generation: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
    choices: CatalogChoices,
) -> None:
    if cache is None:
        return
    try:
        payload = encode_checked_payload(
            CHOICE_HEADER,
            (generation, choices),
        )
    except (TypeError, ValueError):
        return
    cache.set_atomic(
        choice_key(url, target_key, allow_binary, allow_source),
        payload,
    )
    embed_summary_choices(
        cache,
        url,
        generation,
        target_key,
        allow_binary,
        allow_source,
        choices,
    )


def embed_summary_choices(
    cache: Any,
    url: str,
    generation: str,
    target_key: str,
    allow_binary: bool,
    allow_source: bool,
    choices: CatalogChoices,
) -> None:
    """Co-locate the hot target profile with its generation-scoped summary."""
    summary = load_summary(cache, url)
    if summary is None or summary[0] != generation:
        return
    profile_key = target_key, allow_binary, allow_source
    if summary[3].get(profile_key) == choices:
        return
    profiles = dict(summary[3])
    profiles[profile_key] = choices
    save_summary_value(
        cache,
        url,
        (summary[0], summary[1], summary[2], profiles),
    )


def valid_version_text(value: object) -> bool:
    """A version string the summary can compile: a corrupt one is a miss,
    not an exception out of load_summary. Version interns by text, so the
    parse here is the one the summary needs anyway."""
    if not isinstance(value, str):
        return False
    try:
        Version(value)
    except InvalidVersion:
        return False
    return True


def valid_group(value: object) -> bool:
    if type(value) is not tuple or len(value) != 4:
        return False
    name, version, artifacts, facts = value
    if (
        type(name) is not str
        or type(artifacts) is not list
        or type(facts) is not list
        or not valid_version_text(version)
    ):
        return False
    for artifact in artifacts:
        if (
            type(artifact) is not tuple
            or len(artifact) != 2
            or type(artifact[0]) is not int
            or not valid_record(artifact[1])
        ):
            return False
    for fact in facts:
        if not valid_fact(fact):
            return False
    return True


def valid_summary_group(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 4
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and is_version_wire(value[2])
        and valid_version_text(value[2][0])  # ty:ignore[not-subscriptable]
        and isinstance(value[3], list)
        and all(valid_fact(fact) for fact in value[3])
    )


def valid_str_dict(value: object) -> bool:
    if type(value) is not dict:
        return False
    for key, item in value.items():
        if type(key) is not str or type(item) is not str:
            return False
    return True


def valid_record(value: object) -> bool:
    """One full validation at load time: link_from_record trusts its input.

    Marshal only rebuilds exact built-in types, so ``type(...) is`` checks
    are equivalent to ``isinstance`` here and keep this loop cheap.
    """
    # Records written before the PEP 700 size field are 9-tuples; accepting
    # both widths keeps every warm catalog cache valid across the upgrade.
    if type(value) is not tuple or len(value) not in {9, 10}:
        return False
    (url, text, hashes, requires_python, yanked, metadata, upload_time, _, parts) = (
        value[:9]
    )
    if type(url) is not str or type(text) is not str:
        return False
    if len(value) == 10:
        size = value[9]
        if size is not None and (type(size) is not int or size < 0):
            return False
    if not valid_str_dict(hashes):
        return False
    if requires_python is not None and type(requires_python) is not str:
        return False
    if yanked is not None and type(yanked) is not str:
        return False
    if metadata is not None and not valid_str_dict(metadata):
        return False
    if upload_time is not None:
        if type(upload_time) is not str:
            return False
        try:
            parse_iso_datetime(upload_time)
        except ValueError:
            return False
    if type(parts) is not tuple or len(parts) != 5:
        return False
    for part in parts:
        if type(part) is not str:
            return False
    identity = value[RECORD_WHEEL_IDENTITY]
    if identity is None:
        return True
    if type(identity) is not tuple or len(identity) != 4:
        return False
    tags = identity[WHEEL_IDENTITY_TAGS]
    if (
        type(tags) is not tuple
        or type(identity[WHEEL_IDENTITY_NAME]) is not str
        or type(identity[WHEEL_IDENTITY_VERSION]) is not str
    ):
        return False
    build_tag = identity[WHEEL_IDENTITY_BUILD_TAG]
    if build_tag is not None and type(build_tag) is not str:
        return False
    for tag in tags:
        if type(tag) is not tuple or len(tag) != 3:
            return False
        for part in tag:
            if type(part) is not str:
                return False
    return True


def valid_fact(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 3
        and type(value[0]) is int
        and (value[1] is None or type(value[1]) is str)
        and (value[2] is None or type(value[2]) is str)
    )


def valid_choice(value: object) -> bool:
    return value is None or (
        isinstance(value, tuple)
        and len(value) == 3
        and valid_record(value[0])
        and isinstance(value[1], int)
        and (value[2] is None or isinstance(value[2], int))
    )


def valid_choices(value: object) -> bool:
    """A version-text -> choice map of the exact shape the provider unpacks;
    anything else is a miss rather than a ValueError deep in resolution."""
    return isinstance(value, dict) and all(
        isinstance(version, str) and valid_choice(choice)
        for version, choice in value.items()
    )


def valid_choice_profiles(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(profile, tuple)
        and len(profile) == 3
        and isinstance(profile[0], str)
        and isinstance(profile[1], bool)
        and isinstance(profile[2], bool)
        and valid_choices(choices)
        for profile, choices in value.items()
    )


def save_links(cache: Any, url: str, links: list[Link]) -> None:
    if cache is None:
        return
    grouped: dict[tuple[str, str], list[CatalogArtifact]] = {}
    unparsed: list[CatalogRecord] = []
    for link in links:
        parsed_wheel = parsed_wheel_from_link(link)
        record = link_record(link, parsed_wheel=parsed_wheel)
        identity = artifact_identity(link, parsed_wheel=parsed_wheel)
        if identity is None:
            unparsed.append(record)
            continue
        kind, name, version = identity
        grouped.setdefault((name, version), []).append((kind, record))
    save_catalog(
        cache,
        url,
        (
            compile_groups(grouped),
            unparsed,
        ),
    )


def compile_groups(
    grouped: dict[tuple[str, str], list[CatalogArtifact]],
) -> list[CatalogGroup]:
    result: list[CatalogGroup] = []
    for (name, version), artifacts in grouped.items():
        result.append(
            (
                name,
                version,
                artifacts,
                release_facts(artifacts),
            ),
        )
    return result


def release_facts(artifacts: list[CatalogArtifact]) -> list[CatalogFact]:
    """Summarize target-independent artifact eligibility for one release."""
    fact_masks: dict[tuple[str | None, str | None], int] = {}
    for kind, record in artifacts:
        requires_python = record[RECORD_REQUIRES_PYTHON]
        yanked = record[RECORD_YANKED]
        fact_key = (
            requires_python if isinstance(requires_python, str) else None,
            yanked if isinstance(yanked, str) else None,
        )
        fact_masks[fact_key] = fact_masks.get(fact_key, 0) | kind
    return [
        (kind_mask, requires_python, yanked)
        for (requires_python, yanked), kind_mask in fact_masks.items()
    ]


def save_catalog(cache: Any, url: str, catalog: CatalogData) -> None:
    try:
        payload = marshal.dumps(
            ("kpip-index-catalog", catalog[0], catalog[1]),
        )
    except (TypeError, ValueError):
        return
    generation = catalog_generation(payload)
    cache.set_atomic(cache_key(url), payload)
    _remember_validated_catalog(cache, url, payload, catalog)
    save_summary(cache, url, catalog, generation)


def catalog_generation(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def summary_from_catalog(
    catalog: CatalogData,
    generation: str,
) -> CatalogSummary:
    groups, unparsed = catalog
    summary_groups = [
        (
            name,
            version,
            Version(version).to_wire(),
            facts,
        )
        for name, version, _artifacts, facts in groups
    ]
    summary_groups.sort(key=summary_group_sort_key)
    return generation, summary_groups, bool(unparsed), {}  # ty:ignore[invalid-return-type]


def summary_group_sort_key(group: CatalogSummaryGroup) -> Any:
    return group[2][2]


def save_summary(
    cache: Any,
    url: str,
    catalog: CatalogData,
    generation: str,
) -> None:
    summary = summary_from_catalog(catalog, generation)
    save_summary_value(cache, url, summary)


def save_summary_value(
    cache: Any,
    url: str,
    summary: CatalogSummary,
) -> None:
    try:
        payload = encode_checked_payload(SUMMARY_HEADER, summary)
    except (TypeError, ValueError):
        return
    cache.set_atomic(summary_key(url), payload)


def encode_checked_payload(header: bytes, payload: object) -> bytes:
    body = marshal.dumps(payload)  # ty: ignore[invalid-argument-type]
    return header + hashlib.sha256(body).digest() + body


def decode_checked_payload(raw: bytes, header: bytes) -> object | None:
    digest_start = len(header)
    body_start = digest_start + hashlib.sha256().digest_size
    if len(raw) < body_start:
        return None
    body = raw[body_start:]
    if raw[digest_start:body_start] != hashlib.sha256(body).digest():
        return None
    try:
        return marshal.loads(body)
    except (EOFError, TypeError, ValueError):
        return None


def artifact_identity(
    link: Link,
    *,
    parsed_wheel: WheelFile | None = None,
) -> tuple[int, str, str] | None:
    """Compile the artifact identity once when its Simple API page changes."""
    if parsed_wheel is None:
        parsed_wheel = parsed_wheel_from_link(link)
    if parsed_wheel is not None:
        return WHEEL_RECORD, parsed_wheel.name, str(parsed_wheel.version)
    if link.kind is not ArtifactKind.SDIST:
        return None
    parsed_identity = project_version_from_filename(str(link.filename))
    if parsed_identity is None:
        return None
    name, version = parsed_identity
    return SDIST_RECORD, name, str(version)


def parsed_wheel_from_link(link: Link) -> WheelFile | None:
    """Parse a wheel link's filename exactly once at catalog build time."""
    if link.kind is not ArtifactKind.WHEEL:
        return None
    # The link already unquoted its path and caches the basename.
    return parse_wheel_file(str(link.filename))


def wheel_identity(parsed_wheel: WheelFile | None) -> tuple[object, ...] | None:
    """Marshal-safe parsed identity embedded in a wheel catalog record."""
    if parsed_wheel is None:
        return None
    return (
        parsed_wheel.name,
        str(parsed_wheel.version),
        parsed_wheel.build_tag,
        tuple((tag.interpreter, tag.abi, tag.platform) for tag in parsed_wheel.tags),
    )


def wheel_file_from_identity(
    identity: object,
    *,
    name: str,
    version: Version,
) -> WheelFile | None:
    """Reconstruct a wheel from its cached identity without reparsing a name."""
    if not isinstance(identity, tuple) or len(identity) != 4:
        return None
    identity_name = identity[WHEEL_IDENTITY_NAME]
    version_text = identity[WHEEL_IDENTITY_VERSION]
    build_tag = identity[WHEEL_IDENTITY_BUILD_TAG]
    tag_triples = identity[WHEEL_IDENTITY_TAGS]
    if (
        not isinstance(identity_name, str)
        or not isinstance(version_text, str)
        or (build_tag is not None and not isinstance(build_tag, str))
        or not isinstance(tag_triples, tuple)
    ):
        return None
    if identity_name != name or version_text != str(version):
        return None
    tags: list[WheelTag] = []
    for tag in tag_triples:
        if not isinstance(tag, tuple) or len(tag) != 3:
            continue
        interpreter, abi, platform = tag
        if (
            not isinstance(interpreter, str)
            or not isinstance(abi, str)
            or not isinstance(platform, str)
        ):
            continue
        tags.append(WheelTag(interpreter, abi, platform))
    if not tags:
        return None
    return WheelFile(
        name=identity_name,
        version=version,
        build_tag=build_tag,
        tags=tuple(tags),
    )


def wheel_file_from_record(
    record: tuple[object, ...],
    *,
    name: str,
    version: Version,
) -> WheelFile | None:
    """Reconstruct a wheel from its catalog record's cached identity."""
    return wheel_file_from_identity(
        record[RECORD_WHEEL_IDENTITY],
        name=name,
        version=version,
    )


def link_record(
    link: Link,
    *,
    parsed_wheel: WheelFile | None = None,
) -> tuple[object, ...]:
    metadata = link.metadata_file
    upload_time = link.upload_time
    return (
        link.url,
        link.text,
        dict(link.hashes),
        link.requires_python,
        link.yanked_reason,
        None if metadata is None else dict(metadata.hashes or {}),
        None if upload_time is None else upload_time.isoformat(),
        wheel_identity(parsed_wheel),
        tuple(link.parsed_url_internal),
        link.size,
    )


def link_from_record(record: object, *, source_url: str | None = None) -> Link:
    """Materialize a record that ``valid_record`` accepted at load time."""
    if not isinstance(record, tuple) or len(record) not in {9, 10}:
        raise ValueError("invalid catalog record")
    (
        url,
        text,
        hashes,
        requires_python,
        yanked,
        metadata,
        upload_time,
        _wheel_identity,
        parts,
    ) = record[:9]
    size = record[9] if len(record) == 10 else None
    link = Link.from_cached_record(
        url,  # ty:ignore[invalid-argument-type]
        parsed_url=urllib.parse.SplitResult(*parts),  # ty:ignore[not-iterable]
        source_url=source_url,
        text=text,  # ty:ignore[invalid-argument-type]
        hashes=hashes,  # ty:ignore[invalid-argument-type]
        requires_python=requires_python,  # ty:ignore[invalid-argument-type]
        yanked_reason=yanked,  # ty:ignore[invalid-argument-type]
        metadata_file=(
            MetadataFile(metadata) if metadata is not None else None  # ty:ignore[invalid-argument-type]
        ),
        upload_time=(
            parse_iso_datetime(upload_time) if upload_time is not None else None  # ty:ignore[invalid-argument-type]
        ),
    )
    # valid_record vetted the value; the type check narrows it for ty.
    if type(size) is int:
        link.size = size
    return link
