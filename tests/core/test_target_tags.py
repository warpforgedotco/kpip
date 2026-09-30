"""Wheel tags, markers and Requires-Python for the Python kpip resolves for."""

from __future__ import annotations

import itertools
import sys

import pytest
from kpip.core import packaging as kpip_packaging
from kpip.core import wheel
from kpip.core.caches import clear_all
from kpip.core.wheel import WheelTag, supported_wheel_tags, wheel_tag_rank
from kpip.host import interpreter_facts
from kpip.host.interpreter_facts import Interpreter, own_interpreter
from packaging import tags as packaging_tags


@pytest.fixture(autouse=True)
def fresh() -> None:
    clear_all()
    wheel._supported_wheel_tags.cache_clear()
    yield
    clear_all()
    wheel._supported_wheel_tags.cache_clear()


def interpreter_like_this_one(**changes: object) -> Interpreter:
    own = own_interpreter()
    facts = {name: getattr(own, name) for name in Interpreter.__slots__}
    config = dict(own.config)
    config.update(changes.pop("config", {}))  # type: ignore[arg-type]
    facts["config"] = config
    facts.update(changes)
    return Interpreter(facts)


def target(monkeypatch: pytest.MonkeyPatch, interpreter: Interpreter) -> None:
    monkeypatch.setattr(
        interpreter_facts, "target_interpreter", lambda **_: interpreter
    )


def accepts(tag: packaging_tags.Tag) -> bool:
    candidate = (WheelTag(tag.interpreter, tag.abi, tag.platform),)
    return wheel_tag_rank(candidate) is not None


def test_this_python_accepts_what_packaging_says_it_does() -> None:
    """The same verdict as packaging's ``sys_tags`` on every combination of
    interpreter, ABI and platform a wheel might carry."""
    listed = list(packaging_tags.sys_tags())
    supported = set(listed)
    major, minor = sys.version_info[:2]
    interpreters = {
        f"cp{major}{minor}",
        f"cp{major}{minor - 1}",
        f"cp{major}{minor + 1}",
        f"py{major}{minor}",
        f"py{major}",
        f"py{major}{minor - 1}",
        "cp32",
    }
    abis = {
        f"cp{major}{minor}",
        f"cp{major}{minor}t",
        f"cp{major}{minor}d",
        f"cp{major}{minor - 1}",
        "abi3",
        "abi3t",
        "none",
    }
    platforms = {listed[0].platform, "any"}

    for interpreter, abi, platform in itertools.product(interpreters, abis, platforms):
        tag = packaging_tags.Tag(interpreter, abi, platform)
        assert accepts(tag) == (tag in supported), tag


def test_the_best_tag_is_packagings_best() -> None:
    best = next(iter(packaging_tags.sys_tags()))

    assert wheel_tag_rank((WheelTag(best.interpreter, best.abi, best.platform),)) == 0


def test_a_free_threaded_python_takes_its_own_abi_and_abi3t(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target(
        monkeypatch,
        interpreter_like_this_one(version=[3, 15, 0], config={"Py_GIL_DISABLED": 1}),
    )

    abis = {tag.abi for tag in supported_wheel_tags()}

    assert abis == {"cp315t", "abi3t", "none"}


def test_a_debug_python_also_takes_the_release_abi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target(
        monkeypatch,
        interpreter_like_this_one(
            version=[3, 15, 0], config={"Py_DEBUG": 1, "Py_GIL_DISABLED": 0}
        ),
    )

    abis = [tag.abi for tag in supported_wheel_tags()]

    assert list(dict.fromkeys(abis)) == ["cp315d", "cp315", "abi3", "none"]


def test_another_pythons_version_picks_its_wheels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target(
        monkeypatch,
        interpreter_like_this_one(version=[3, 14, 7], config={"Py_GIL_DISABLED": 0}),
    )
    platform = supported_wheel_tags()[0].platform

    assert wheel_tag_rank((WheelTag("cp314", "cp314", platform),)) is not None
    assert wheel_tag_rank((WheelTag("cp315", "cp315", platform),)) is None


def test_markers_are_the_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    other = interpreter_like_this_one()
    other.markers = {
        **other.markers,
        "python_version": "3.14",
        "python_full_version": "3.14.7",
        "implementation_version": "3.14.7",
    }
    target(monkeypatch, other)

    environment = kpip_packaging.default_environment()

    assert environment["python_full_version"] == "3.14.7"
    assert environment["python_version"] == "3.14"
    assert kpip_packaging.marker_applies('python_version < "3.15"')


def test_requires_python_takes_a_release_candidate_for_its_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As pip, which checks ``sys.version_info[:3]``: 3.15.0rc2 is 3.15.0,
    so ``>=3.15`` admits it."""
    target(monkeypatch, interpreter_like_this_one(version=[3, 15, 0]))

    assert kpip_packaging.requires_python_version() == "3.15.0"
