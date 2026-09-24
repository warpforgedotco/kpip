"""A lock starts from the one already at its output.

Each package keeps the version it has there while the requirements still
allow it, so changing one requirement moves no other pin; ``--upgrade``
and ``--upgrade-package`` let versions move again.
"""

from __future__ import annotations

import pytest
import tomllib
from kpip_test_support import KpipTestEnvironment, create_basic_wheel_for_package


def locked_versions(script: KpipTestEnvironment) -> dict[str, str]:
    pylock = tomllib.loads(script.scratch_path.joinpath("pylock.toml").read_text())
    return {package["name"]: package["version"] for package in pylock["packages"]}


def lock(script: KpipTestEnvironment, *args: str) -> dict[str, str]:
    script.kpip(
        "lock",
        *args,
        "--no-index",
        "--find-links",
        str(script.scratch_path),
        "--output",
        "pylock.toml",
        expect_stderr=True,
    )
    return locked_versions(script)


# The wheelhouse fast path, and the full command it hands anything else to.
PATHS = pytest.mark.parametrize("extra", [(), ("--python-version", "3.12")])


@pytest.fixture
def released(script: KpipTestEnvironment) -> KpipTestEnvironment:
    """``app`` and ``lib`` locked at 1.0, then 2.0 of both released."""
    create_basic_wheel_for_package(script, "app", "1.0", depends=["lib"])
    create_basic_wheel_for_package(script, "lib", "1.0")
    create_basic_wheel_for_package(script, "other", "1.0")

    assert lock(script, "app") == {"app": "1.0", "lib": "1.0"}

    create_basic_wheel_for_package(script, "app", "2.0", depends=["lib"])
    create_basic_wheel_for_package(script, "lib", "2.0")
    return script


@PATHS
def test_adding_a_requirement_keeps_every_other_pin(
    released: KpipTestEnvironment, extra: tuple[str, ...]
) -> None:
    assert lock(released, "app", "other", *extra) == {
        "app": "1.0",
        "lib": "1.0",
        "other": "1.0",
    }


@PATHS
def test_a_pin_the_requirements_exclude_moves(
    released: KpipTestEnvironment, extra: tuple[str, ...]
) -> None:
    assert lock(released, "app", "lib>=2", *extra) == {"app": "1.0", "lib": "2.0"}


@PATHS
def test_upgrade_starts_afresh(
    released: KpipTestEnvironment, extra: tuple[str, ...]
) -> None:
    assert lock(released, "app", "--upgrade", *extra) == {"app": "2.0", "lib": "2.0"}


@PATHS
def test_upgrade_package_moves_only_that_package(
    released: KpipTestEnvironment, extra: tuple[str, ...]
) -> None:
    assert lock(released, "app", "-P", "LIB", *extra) == {"app": "1.0", "lib": "2.0"}


def test_a_cached_lock_answers_only_for_the_lock_it_started_from(
    released: KpipTestEnvironment,
) -> None:
    """The same inputs give a different lock from a different starting point.

    Each run below has inputs a previous run cached an answer for, so a
    cache keyed on the inputs alone would write back the wrong one.
    """
    assert lock(released, "app") == {"app": "1.0", "lib": "1.0"}
    assert lock(released, "app", "-U") == {"app": "2.0", "lib": "2.0"}
    assert lock(released, "app") == {"app": "2.0", "lib": "2.0"}

    pylock = released.scratch_path / "pylock.toml"
    pylock.write_text(pylock.read_text().replace('"2.0"', '"1.0"'))

    assert lock(released, "app") == {"app": "1.0", "lib": "1.0"}


def test_an_unreadable_previous_lock_is_resolved_afresh(
    released: KpipTestEnvironment,
) -> None:
    released.scratch_path.joinpath("pylock.toml").write_text("not [ toml")

    assert lock(released, "app") == {"app": "2.0", "lib": "2.0"}
