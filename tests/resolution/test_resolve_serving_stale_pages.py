"""A resolve from stale pages is kept only if they were unchanged."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine


class Provider(CandidateProvider):
    def __init__(self, unchanged: bool) -> None:  # noqa: D107 - no sources needed
        self.unchanged = unchanged
        self.serving_stale = False

    def serve_stale_pages(self) -> None:
        self.serving_stale = True

    def stale_pages_unchanged(self) -> bool:
        return self.unchanged


class Harness:
    """Each resolve in turn: whether its stale pages are unchanged, and
    whether it raises."""

    def __init__(self, *attempts: tuple[bool, bool]) -> None:
        self.attempts = list(attempts)
        self.resolved: list[bool] = []
        self.closed: list[Any] = []
        self.engines: list[Any] = []

    def build(self) -> Any:
        harness = self
        unchanged, raises = self.attempts[len(self.resolved)]

        class Engine:
            provider = Provider(unchanged)

            def resolve(self, requirements: Any) -> Any:
                harness.resolved.append(self.provider.serving_stale)
                if raises:
                    raise RuntimeError("impossible from these pages")
                return SimpleNamespace(engine=self)

            def close(self) -> None:
                harness.closed.append(self)

        return Engine()

    def run(self) -> Any:
        return ResolutionEngine.resolve_serving_stale_pages(
            self.build, ["demo"], engines=self.engines
        )


def test_unchanged_stale_pages_resolve_once() -> None:
    harness = Harness((True, False))

    result = harness.run()

    assert harness.resolved == [True]
    assert harness.engines == [result.engine]
    assert harness.closed == []


def test_a_changed_page_resolves_again_from_fresh_pages() -> None:
    harness = Harness((False, False), (True, False))

    result = harness.run()

    assert harness.resolved == [True, False]
    assert harness.engines == [result.engine]
    assert len(harness.closed) == 1


def test_a_failure_from_changed_pages_resolves_again() -> None:
    """A new release can make a lock possible that stale pages said was not."""
    harness = Harness((False, True), (True, False))

    harness.run()

    assert harness.resolved == [True, False]


def test_a_failure_from_unchanged_pages_is_the_answer() -> None:
    harness = Harness((True, True))

    with pytest.raises(RuntimeError):
        harness.run()
    assert harness.resolved == [True]
    assert len(harness.engines) == 1


def test_the_fresh_resolve_is_the_answer_whatever_it_says() -> None:
    harness = Harness((False, False), (False, True))

    with pytest.raises(RuntimeError):
        harness.run()
    assert harness.resolved == [True, False]


def test_a_provider_without_index_pages_resolves_once() -> None:
    engines: list[Any] = []

    class Engine:
        provider = object()

        def resolve(self, requirements: Any) -> str:
            return "answer"

    assert (
        ResolutionEngine.resolve_serving_stale_pages(Engine, [], engines=engines)
        == "answer"
    )
    assert len(engines) == 1
