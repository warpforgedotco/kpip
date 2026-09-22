"""Persistent cache for parsed Simple API catalog entries."""

from __future__ import annotations

from kpip.core.utils import versioned_bucket

import hashlib
import marshal
import threading
import urllib.parse

from kpip.core.versions import Version
from kpip.core.wheel import WheelFile, WheelTag, parse_wheel_file, wheel_tag
from kpip.index.datetime import parse_iso_datetime
from kpip.index.directory_index import project_version_from_filename
from kpip.index.links import Link, split_plain_url
from kpip.index.source_models import ArtifactKind, MetadataFile

TYPE_CHECKING = False

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

# Version 3 drops the release tuple from a summary's stored version and
# puts every catalog blob behind a digest, so a store written by an earlier
# kpip is a different bucket rather than a payload this one would misread.
PREFIX = f"{versioned_bucket('kpip-index-catalog', 3)}:"
SUMMARY_PREFIX = f"{versioned_bucket('kpip-index-summary', 3)}:"
CHOICE_PREFIX = f"{versioned_bucket('kpip-index-choice', 3)}:"
CATALOG_HEADER = versioned_bucket("kpip-index-catalog", 3).encode() + b"\0"
SUMMARY_HEADER = versioned_bucket("kpip-index-summary", 3).encode() + b"\0"
CHOICE_HEADER = versioned_bucket("kpip-index-choice", 3).encode() + b"\0"

WHEEL_RECORD = 1
SDIST_RECORD = 2
RECORD_REQUIRES_PYTHON = 3
RECORD_YANKED = 4
RECORD_WHEEL_IDENTITY = 7
RECORD_SIZE = 8
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

_VALIDATED_CATALOGS_LIMIT = 4096
"""Catalogs kept decoded per cache, so a page compiled in this run is not
immediately written, read back and decoded again to answer for its own
releases.  A cold airflow lock compiles ~700; at the previous limit of 8 they
evicted each other and the provider re-decoded 594 MB of payloads it had just
produced."""


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
        payload = decode_checked_payload(raw, CATALOG_HEADER)
        # The digest says these bytes are the ones this kpip wrote, and the
        # bucket version says this kpip wrote them in this shape, so what is
        # left to check is that the blob is a catalog at all. Walking every
        # group and record to re-prove their types was the single largest
        # cost of reading a catalog.
        if (
            not isinstance(payload, tuple)
            or len(payload) != 3
            or payload[0] != "kpip-index-catalog"
            or not isinstance(payload[1], list)
            or not isinstance(payload[2], list)
        ):
            return None
        # Declared rather than proven: the digest and the bucket version are
        # what establish this, and walking the lists to convince a checker
        # is the cost this read is trying not to pay.
        catalog: CatalogData = (payload[1], payload[2])  # ty:ignore[invalid-assignment]
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
    if not isinstance(payload, tuple) or len(payload) != 2 or payload[0] != generation:
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


def save_links(cache: Any, url: str, links: list[Link]) -> CatalogSummary | None:
    if cache is None:
        return None
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
    return save_catalog(
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


def save_catalog(cache: Any, url: str, catalog: CatalogData) -> CatalogSummary | None:
    """Persist a catalog and return the summary derived from it.

    A cold fetch has just compiled the catalog and would otherwise re-read
    and re-decode the summary it wrote a moment earlier.
    """
    try:
        payload = encode_checked_payload(
            CATALOG_HEADER,
            ("kpip-index-catalog", catalog[0], catalog[1]),
        )
    except (TypeError, ValueError):
        return None
    generation = catalog_generation(payload)
    cache.set_atomic(cache_key(url), payload)
    _remember_validated_catalog(cache, url, payload, catalog)
    summary = summary_from_catalog(catalog, generation)
    save_summary_value(cache, url, summary)
    return summary


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
    # The key is the second half of the wire now that the release is gone.
    return group[2][1]


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
        return WHEEL_RECORD, parsed_wheel.name, parsed_wheel.version.public
    return identity_for(link.kind, str(link.filename), parsed_wheel=None)


def parsed_wheel_from_link(link: Link) -> WheelFile | None:
    """Parse a wheel link's filename exactly once at catalog build time."""
    # The link already unquoted its path and caches the basename.
    return parsed_wheel_for(link.kind, str(link.filename))


def parsed_wheel_for(kind: ArtifactKind, filename: str) -> WheelFile | None:
    """``parsed_wheel_from_link`` for a caller that has no link to hand."""
    if kind is not ArtifactKind.WHEEL:
        return None
    return parse_wheel_file(filename)


def identity_for(
    kind: ArtifactKind,
    filename: str,
    *,
    parsed_wheel: WheelFile | None = None,
) -> tuple[int, str, str] | None:
    """``artifact_identity`` for a caller that has no link to hand."""
    if parsed_wheel is None:
        parsed_wheel = parsed_wheel_for(kind, filename)
    if parsed_wheel is not None:
        return WHEEL_RECORD, parsed_wheel.name, parsed_wheel.version.public
    if kind is not ArtifactKind.SDIST:
        return None
    parsed_identity = project_version_from_filename(filename)
    if parsed_identity is None:
        return None
    name, version = parsed_identity
    return SDIST_RECORD, name, str(version)


def wheel_identity(parsed_wheel: WheelFile | None) -> tuple[object, ...] | None:
    """Marshal-safe parsed identity embedded in a wheel catalog record."""
    if parsed_wheel is None:
        return None
    return (
        parsed_wheel.name,
        parsed_wheel.version.public,
        parsed_wheel.build_tag,
        tuple([tag.triple for tag in parsed_wheel.tags]),
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
        tags.append(wheel_tag(interpreter, abi, platform))
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
    upload_time = link.upload_time
    return record_fields(
        url=link.url,
        text=link.text,
        hashes=link.hashes,
        requires_python=link.requires_python,
        yanked_reason=link.yanked_reason,
        metadata_file=link.metadata_file,
        upload_time=None if upload_time is None else upload_time.isoformat(),
        parsed_wheel=parsed_wheel,
        size=link.size,
    )


def record_fields(
    *,
    url: str,
    text: str,
    hashes: Mapping[str, str] | str,
    requires_python: str | None,
    yanked_reason: str | None,
    metadata_file: MetadataFile | None,
    upload_time: str | None,
    parsed_wheel: WheelFile | None,
    size: int | None,
) -> tuple[object, ...]:
    """The stored shape of one artifact, from its fields rather than a link.

    Three things an index sends are stored as they arrive rather than as the
    objects they become, which is what a page listing thousands of files
    pays for.  Every file PyPI serves carries exactly one ``sha256``, so a
    lone digest is kept as its string instead of a one-entry dict.  The
    upload time is kept as the index's own text and parsed only if something
    asks, rather than parsed and re-formatted while building the catalog.
    The split URL is not stored at all: it is a regex match away from the
    URL beside it, and storing it repeated the scheme and host of every file.

    Together those make a catalog payload 31% smaller, which is less to
    marshal, less to write, and less to decode on the next run.
    """
    return (
        url,
        text,
        _stored_hashes(hashes),
        requires_python,
        yanked_reason,
        _stored_metadata(metadata_file),
        upload_time,
        wheel_identity(parsed_wheel),
        size,
    )


def _stored_hashes(hashes: Mapping[str, str] | str) -> object:
    """A lone sha256 as its digest, anything else as a dict."""
    if isinstance(hashes, str):
        return hashes
    if len(hashes) == 1:
        digest = hashes.get("sha256")
        if digest is not None:
            return digest
    return dict(hashes)


def _restored_hashes(stored: object) -> dict[str, str]:
    if type(stored) is str:
        return {"sha256": stored}
    return {str(name): str(digest) for name, digest in stored.items()}  # ty:ignore[unresolved-attribute]


def _stored_metadata(metadata_file: MetadataFile | None) -> object:
    """``None`` absent, ``True`` present without hashes, else its digest."""
    if metadata_file is None:
        return None
    hashes = metadata_file.hashes
    if not hashes:
        return True
    return _stored_hashes(hashes)


def _restored_metadata(stored: object) -> MetadataFile | None:
    if stored is None:
        return None
    if stored is True:
        return MetadataFile(None)
    return MetadataFile(_restored_hashes(stored))


def link_from_record(record: object, *, source_url: str | None = None) -> Link:
    """Materialize a record that ``valid_record`` accepted at load time."""
    if not isinstance(record, tuple) or len(record) != 9:
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
        size,
    ) = record
    if type(url) is not str or type(text) is not str:
        raise ValueError("invalid catalog record")
    # The split URL is not stored: it is a regex match away from the URL.
    parsed = split_plain_url(url) or urllib.parse.urlsplit(url)
    link = Link.from_cached_record(
        url,
        parsed_url=parsed,
        source_url=source_url,
        text=text,
        hashes=_restored_hashes(hashes),
        requires_python=requires_python,  # ty:ignore[invalid-argument-type]
        yanked_reason=yanked,  # ty:ignore[invalid-argument-type]
        metadata_file=_restored_metadata(metadata),
        upload_time=(
            parse_iso_datetime(upload_time) if upload_time is not None else None  # ty:ignore[invalid-argument-type]
        ),
    )
    # valid_record vetted the value; the type check narrows it for ty.
    if type(size) is int:
        link.size = size
    return link
