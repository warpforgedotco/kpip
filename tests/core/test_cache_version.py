"""Every cache store carries its own format version, inside a versioned root.

Two levels, for two different jobs. The ``v<N>`` root retires the whole tree
at once and is what ``kpip cache purge`` keys on. Each store then versions
itself, so a format change to one does not throw away the others -- stores
cost wildly different amounts to refill, and sharing a version lets the
cheapest one decide when the most expensive is discarded.

These tests pin both: that no store escapes the root, and that none of them
forgets to version itself.
"""

from __future__ import annotations

import os
import re

import pytest
from kpip.cli import fast, fast_install
from kpip.core.appdirs import (
    cache_root,
    command_cache_dir,
    resolve_cache_dir,
    versioned_cache_dir,
)
from kpip.core.utils import CACHE_INTERPRETER_TAG, CACHE_VERSION, CACHE_VERSION_TAG
from kpip.index import (
    artifact_cache,
    cache as wheel_cache,
    candidate_metadata_cache,
    catalog_cache,
    metadata_cache,
    release_facts_cache,
)
from kpip.install import wheel_archive_cache, wheel_install_plan_cache
from kpip.core.appdirs import HTTP_CACHE_BUCKET

STORAGE_NAMES = (
    HTTP_CACHE_BUCKET,
    artifact_cache.ARTIFACT_CACHE_BUCKET,
    wheel_cache.WHEEL_CACHE_BUCKET,
    metadata_cache.NAME,
    candidate_metadata_cache.NAME,
    release_facts_cache.NAME,
    fast.FAST_LOCK_PLAN_BUCKET,
    fast_install.NAME,
    fast_install.TREE_CACHE_BUCKET,
    wheel_archive_cache.ARCHIVE_CACHE_BUCKET,
    wheel_install_plan_cache.RESOLUTION_CACHE_BUCKET,
    wheel_install_plan_cache.REMOTE_EXACT_CONTEXT,
    catalog_cache.PREFIX,
    catalog_cache.SUMMARY_PREFIX,
    catalog_cache.CHOICE_PREFIX,
    catalog_cache.SUMMARY_HEADER.decode(),
    catalog_cache.CHOICE_HEADER.decode(),
)


def test_the_cache_layout_carries_a_root_version() -> None:
    assert CACHE_VERSION >= 1
    assert CACHE_VERSION_TAG == f"v{CACHE_VERSION}"


def test_every_writer_lands_under_the_versioned_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert versioned_cache_dir("/root") == os.path.join("/root", CACHE_VERSION_TAG)
    assert resolve_cache_dir("/explicit") == os.path.join(
        "/explicit",
        CACHE_VERSION_TAG,
    )
    monkeypatch.setenv("KPIP_CACHE_DIR", "/configured")
    assert cache_root() == "/configured"
    assert resolve_cache_dir() == os.path.join("/configured", CACHE_VERSION_TAG)
    assert command_cache_dir(None, False) == os.path.join(
        "/configured", CACHE_VERSION_TAG
    )
    monkeypatch.delenv("KPIP_CACHE_DIR")
    assert resolve_cache_dir() == versioned_cache_dir(cache_root())
    assert command_cache_dir(None, False) == resolve_cache_dir()


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_caching_can_be_turned_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    assert command_cache_dir("/explicit", True) is None
    monkeypatch.setenv("KPIP_NO_CACHE_DIR", value)
    assert command_cache_dir("/explicit", False) is None


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_a_false_no_cache_dir_variable_keeps_the_cache(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("KPIP_NO_CACHE_DIR", value)
    assert command_cache_dir("/explicit", False) == os.path.join(
        "/explicit",
        CACHE_VERSION_TAG,
    )


@pytest.mark.parametrize("name", STORAGE_NAMES)
def test_every_storage_name_carries_its_own_version(name: str) -> None:
    """A store that forgets its version shares the root's, which means a
    format change to it can only be released by discarding everything."""
    assert re.search(r"-v\d+(\b|_)", name), f"{name!r} carries no store version"


_MARSHAL_STORES = (
    release_facts_cache.NAME,
    fast_install.NAME,
    fast_install.TREE_CACHE_BUCKET,
    wheel_archive_cache.ARCHIVE_CACHE_BUCKET,
    wheel_install_plan_cache.RESOLUTION_CACHE_BUCKET,
)


@pytest.mark.parametrize("name", _MARSHAL_STORES)
def test_interpreter_bound_stores_are_scoped_to_one(name: str) -> None:
    """``marshal`` payloads and installed trees are not portable across
    interpreters, so their stores have to be scoped to the one that wrote
    them as well as versioned."""
    assert CACHE_INTERPRETER_TAG in name


def test_store_names_are_distinct() -> None:
    """Two stores sharing a name share a directory, and each would read the
    other's payloads as its own."""
    assert len(set(STORAGE_NAMES)) == len(STORAGE_NAMES)


def test_bumping_one_store_leaves_the_others_alone() -> None:
    from kpip.core.utils import versioned_bucket

    assert versioned_bucket("simple", 1) != versioned_bucket("simple", 2)
    assert versioned_bucket("simple", 2) != versioned_bucket("archive", 2)
    assert versioned_bucket("archive", 1, interpreter=True).endswith(
        CACHE_INTERPRETER_TAG,
    )
