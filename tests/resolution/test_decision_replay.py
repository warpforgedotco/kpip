"""Replaying undone decisions may only spare work, never change the answer.

After a backjump the resolver re-makes the decisions it undid, and it makes
them the same way: the prefix it replays derives the same ranges, and a
learned clause can only narrow one further. ``Resolver._replay_version``
hands back an undone version while the queue picks the same package next
and that version is still in range, sparing the provider's version choice.
Propagation still runs, so the replayed decision meets every clause.

These tests are differential, like the forward-check tests: every graph is
resolved with replay and with it disabled, and the two must agree on
whether they solved it and on every selected version.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from kpip._vendor.nab_resolver.resolver import Resolver
from kpip.core.errors import ResolutionError
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import reset_caches  # noqa: E402
from tests.resolution.test_forward_check import build_random_graph  # noqa: E402


def resolve(wheelhouse: Path, roots: list[str]) -> dict[str, str] | None:
    reset_caches()
    engine = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
        ),
        ignore_installed=True,
    )
    try:
        result = engine.resolve(roots)
    except ResolutionError:
        return None
    return {c.name: str(c.version) for c in result.candidates}


@pytest.mark.parametrize("seed", range(40))
def test_replay_never_changes_the_answer(
    seed: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    with_replay = resolve(wheelhouse, roots)

    monkeypatch.setattr(Resolver, "_replay_version", lambda self, package: None)
    without_replay = resolve(wheelhouse, roots)

    assert (with_replay is None) == (without_replay is None), (
        f"seed {seed}: replay changed whether the graph is solvable"
    )
    assert with_replay == without_replay, f"seed {seed}: replay changed the versions"


def test_replay_actually_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the differential tests above could pass vacuously.

    A graph whose every backjump excludes the very version it undid never
    replays, so the random graphs are searched for one that does.
    """
    replayed: list[int] = []
    real_solve = Resolver.solve

    def counting(self: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return real_solve(self, *args, **kwargs)
        finally:
            replayed.append(self.replayed_decisions)

    monkeypatch.setattr(Resolver, "solve", counting)

    for seed in range(40):
        wheelhouse = tmp_path / f"wheelhouse-{seed}"
        wheelhouse.mkdir()
        resolve(wheelhouse, build_random_graph(wheelhouse, seed))
        if replayed and replayed[-1] > 0:
            return

    pytest.fail(
        "no random graph replayed a decision, so the differential tests prove nothing"
    )
