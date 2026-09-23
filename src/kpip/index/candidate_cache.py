"""Artifact hash and built-wheel cache operations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sysconfig
import tempfile

from kpip.core.logger import get_logger
from kpip.core.hashes import file_hashes
from kpip.core.utils import CACHE_INTERPRETER_TAG
from kpip.core.versions import ZERO_VERSION
from kpip.core.wheel import WheelCandidate, wheel_candidate_from_path
from kpip.index.artifacts import ArtifactLocator
from kpip.index.cache import origin_hashes, wheel_cache_path
from kpip.index.links import Link
from kpip.index.source_models import ArtifactKind, CandidateRecord
from kpip.index.vcs import is_immutable_vcs_link, vcs_reference

logger = get_logger(__name__)


def source_hashes_for_link(
    link: Link,
    *,
    local_path: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    hashes = dict(link.hashes)

    if hashes:
        return hashes

    local = (
        os.fspath(local_path)
        if local_path is not None
        else ArtifactLocator().local_path(link.url)
    )

    if local is not None:
        try:
            return file_hashes(local)

        except OSError:
            return {}

    return {}


def cache_identity(url: str) -> str:
    """Return the stable cache key for an artifact URL."""

    if is_immutable_vcs_link(url):
        reference = vcs_reference(url)

        return f"{reference.vcs}+{reference.repo_url}@{reference.requested_revision}"

    return url


def _json_cache_value(value: object) -> object:
    """Return a deterministic JSON value, or raise for an unsafe cache key."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, dict):
        return {
            str(key): _json_cache_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }

    if isinstance(value, (list, tuple)):
        return [_json_cache_value(item) for item in value]

    if isinstance(value, (set, frozenset)):
        normalized = [_json_cache_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )

    raise TypeError(f"unsupported built-wheel cache key value: {type(value).__name__}")


def _constraint_identity(value: str) -> object:
    try:
        local = ArtifactLocator().local_path(value)
    except (OSError, ValueError):
        return value

    if local is None:
        return value

    try:
        hashes = file_hashes(local)

    except OSError:
        return {"input": value, "path": os.path.abspath(local), "missing": True}

    return {
        "input": value,
        "path": os.path.abspath(local),
        "hashes": hashes,
    }


def built_wheel_cache_key(
    candidate: CandidateRecord,
    *,
    source_hashes: dict[str, str] | None,
    config_settings: dict[str, object] | None,
    build_constraints: list[str] | None,
    build_isolation: bool,
    target_key: str | None,
    vcs_commit: str | None = None,
) -> str | None:
    """Return the complete identity of a reusable source build.

    An sdist without a content hash cannot be distinguished from a different
    body later served at the same URL, so it is deliberately ineligible for
    persistent reuse.  A VCS checkout is reusable when its URL pins a full
    commit, or when the caller resolved its reference to one
    (``vcs_commit``), which then becomes part of the identity.
    """

    if candidate.link.kind is ArtifactKind.SDIST:
        if not source_hashes:
            return None

    elif not (
        candidate.link.kind is ArtifactKind.SOURCE_TREE
        and (is_immutable_vcs_link(candidate.link.url) or vcs_commit is not None)
    ):
        return None

    try:
        settings = _json_cache_value(config_settings or {})

    except TypeError:
        return None

    identity = {
        "schema": 2,
        "source": cache_identity(candidate.link.url),
        "subdirectory": candidate.link.subdirectory_fragment,
        "source_hashes": dict(sorted((source_hashes or {}).items())),
        "vcs_commit": vcs_commit,
        "build": {
            "config_settings": settings,
            "constraints": [
                _constraint_identity(value) for value in build_constraints or ()
            ],
            "isolation": build_isolation,
            "interpreter": CACHE_INTERPRETER_TAG,
            "platform": sysconfig.get_platform(),
            "target": target_key,
        },
    }

    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def _discard_invalid_entry(path: str) -> None:
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def cached_wheel_for_link(
    wheel_cache_dir: str | os.PathLike[str] | None,
    candidate: CandidateRecord,
    cache_key: str | None,
    *,
    requested_extras: frozenset[str] = frozenset(),
    candidate_name_is_authoritative: bool = True,
) -> tuple[str, dict[str, str], WheelCandidate] | None:
    if wheel_cache_dir is None or cache_key is None:
        return None

    entry_dir_text = wheel_cache_path(os.fspath(wheel_cache_dir), cache_key)

    try:
        with os.scandir(entry_dir_text) as entries:
            children = tuple(entries)

        wheels = sorted(
            entry.path
            for entry in children
            if entry.name.endswith(".whl") and entry.is_file(follow_symlinks=False)
        )
        actual_names = {entry.name for entry in children}
        origin_is_file = any(
            entry.name == "origin.json" and entry.is_file(follow_symlinks=False)
            for entry in children
        )

    except OSError:
        return None

    complete_names = {
        "origin.json",
        *(os.path.basename(wheel) for wheel in wheels),
    }

    if len(wheels) != 1 or actual_names != complete_names or not origin_is_file:
        logger.warning("Ignoring invalid built-wheel cache entry %s", entry_dir_text)
        _discard_invalid_entry(entry_dir_text)
        return None

    hashes = origin_hashes(
        os.path.join(entry_dir_text, "origin.json"),
        expected_cache_key=cache_key,
    )

    if hashes is None:
        logger.warning("Ignoring incomplete built-wheel cache entry %s", entry_dir_text)
        _discard_invalid_entry(entry_dir_text)
        return None

    wheel = wheels[0]

    try:
        built = wheel_candidate_from_path(wheel, requested_extras)

    except Exception as exc:
        logger.warning(
            "Ignoring invalid built-wheel cache entry %s: %s",
            wheel,
            exc,
        )
        _discard_invalid_entry(entry_dir_text)
        return None

    if (
        candidate_name_is_authoritative
        and built.canonical_name != candidate.canonical_name
    ) or (candidate.version != ZERO_VERSION and built.version != candidate.version):
        logger.warning(
            "Ignoring built-wheel cache entry %s for unexpected project %s==%s",
            wheel,
            built.name,
            built.version,
        )
        _discard_invalid_entry(entry_dir_text)
        return None

    return wheel, hashes, built


def cache_built_wheel(
    wheel_cache_dir: str | os.PathLike[str] | None,
    candidate: CandidateRecord,
    wheel: str,
    cache_key: str | None,
    *,
    source_hashes: dict[str, str] | None,
    candidate_name_is_authoritative: bool = True,
) -> None:
    if wheel_cache_dir is None or cache_key is None:
        return

    entry_dir_text = wheel_cache_path(os.fspath(wheel_cache_dir), cache_key)
    parent = os.path.dirname(entry_dir_text)
    temporary = ""

    try:
        os.makedirs(parent, exist_ok=True)
        temporary = tempfile.mkdtemp(prefix=".built-wheel-", dir=parent)

        staged_wheel = os.path.join(temporary, os.path.basename(wheel))
        shutil.copy2(wheel, staged_wheel)

        try:
            built = wheel_candidate_from_path(staged_wheel, include_layout=False)
        except Exception as exc:
            logger.warning("Not caching invalid built wheel %s: %s", wheel, exc)
            return

        if (
            candidate_name_is_authoritative
            and built.canonical_name != candidate.canonical_name
        ) or (candidate.version != ZERO_VERSION and built.version != candidate.version):
            logger.warning(
                "Not caching built wheel %s for unexpected project %s==%s",
                wheel,
                built.name,
                built.version,
            )
            return

        origin = {
            "archive_info": {"hashes": dict(source_hashes or {})},
            "cache_key_hash": hashlib.sha256(cache_key.encode()).hexdigest(),
        }

        with open(
            os.path.join(temporary, "origin.json"),
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(origin, file, sort_keys=True)

        try:
            os.rename(temporary, entry_dir_text)
        except OSError:
            if not os.path.isdir(entry_dir_text):
                raise
        else:
            temporary = ""

    except OSError as exc:
        logger.debug(
            "Could not write built-wheel cache entry %s: %s", entry_dir_text, exc
        )

    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)


def emit_build_message(message: str) -> None:
    if not os.environ.get("KPIP_QUIET"):
        print(message)
