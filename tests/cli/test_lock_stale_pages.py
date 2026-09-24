"""A lock resolved from stale pages is kept only if they were unchanged."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kpip.cli import lock


class Provider:
    def __init__(self, unchanged: bool) -> None:
        self.unchanged = unchanged
        self.serving_stale = False
        self.index_sources = ()

    def serve_stale_pages(self) -> None:
        self.serving_stale = True

    def stale_pages_unchanged(self) -> bool:
        return self.unchanged


class Harness:
    """Each resolve in turn: whether its stale pages are unchanged, and
    whether it raises."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *attempts: tuple[bool, bool]):
        self.attempts = list(attempts)
        self.resolved: list[bool] = []
        self.closed = 0
        harness = self

        def from_options(**kwargs: Any) -> Provider:
            unchanged, _ = harness.attempts[len(harness.resolved)]
            return Provider(unchanged)

        class Engine:
            def __init__(self, *, provider: Provider, **kwargs: Any) -> None:
                self.provider = provider

            def resolve(self, requirements: Any) -> Any:
                _, raises = harness.attempts[len(harness.resolved)]
                harness.resolved.append(self.provider.serving_stale)
                if raises:
                    raise RuntimeError("impossible from these pages")
                return SimpleNamespace(candidates=[])

            def close(self) -> None:
                harness.closed += 1

        monkeypatch.setattr(lock.CandidateProvider, "from_options", from_options)
        monkeypatch.setattr(lock, "ResolutionEngine", Engine)

    def run(self, tmp_path: Path) -> int:
        return lock.run_lock(
            ["demo", "--no-cache-dir", "--output", str(tmp_path / "pylock.toml")]
        )


def test_unchanged_stale_pages_resolve_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, (True, False))

    assert harness.run(tmp_path) == 0
    assert harness.resolved == [True]


def test_a_changed_page_resolves_again_from_fresh_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, (False, False), (True, False))

    assert harness.run(tmp_path) == 0
    assert harness.resolved == [True, False]
    assert harness.closed == 2


def test_a_failure_from_changed_pages_resolves_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A new release can make a lock possible that stale pages said was not."""
    harness = Harness(monkeypatch, (False, True), (True, False))

    assert harness.run(tmp_path) == 0
    assert harness.resolved == [True, False]


def test_a_failure_from_unchanged_pages_is_the_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, (True, True))

    with pytest.raises(RuntimeError):
        harness.run(tmp_path)
    assert harness.resolved == [True]


def test_the_fresh_resolve_is_the_answer_whatever_it_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = Harness(monkeypatch, (False, False), (False, True))

    with pytest.raises(RuntimeError):
        harness.run(tmp_path)
    assert harness.resolved == [True, False]
