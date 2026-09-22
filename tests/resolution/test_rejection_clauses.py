"""A rejected release's dependency fact reaches the resolver as a clause.

The forward check rejects a release whose dependencies exclude what the
partial solution holds, and the resolver, told only which release to decide
instead, learned the same fact one conflict at a time when a backjump
brought it back to the package.  The clause a decision would have added is
queued for ``consume_pending_clauses`` instead; the clause index merges the
clauses of neighbouring releases into one, which propagates in one step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kpip._vendor.nab_resolver.ranges import Range
from kpip._vendor.nab_resolver.types import IncompatibilityCause
from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.resolution.nab_provider import NabProvider

from .test_forward_check import build_random_graph, resolve
from .test_provider_memos import make_provider


class _Candidate:
    source_kind = "wheel"

    def __init__(self, *requirements: str) -> None:
        self.dependencies = tuple(parse_requirement(r) for r in requirements)


def _provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NabProvider:
    from benchmark_support import make_wheel, reset_caches

    reset_caches()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("1.0", "2.0", "3.0", "3.1"):
        make_wheel(wheelhouse, "dep", version)
    provider = make_provider(wheelhouse)
    provider.requirements["parent"] = parse_requirement("parent")
    provider.requirements["dep"] = parse_requirement("dep")
    monkeypatch.setattr(
        provider,
        "_catalog_candidate",
        lambda package, version: _Candidate("dep<3"),
    )
    return provider


def test_a_rejection_queues_the_release_dependency_clause_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    provider._active_decisions["dep"] = Version("3.1")

    assert provider._selected_dependency_rejects("parent", Version("9.0"))
    assert provider._selected_dependency_rejects("parent", Version("9.0"))
    clauses = provider.consume_pending_clauses()

    assert len(clauses) == 1
    (clause,) = clauses
    assert clause.cause is IncompatibilityCause.DEPENDENCY
    parent, dependency = clause.terms
    assert parent.package == "parent"
    assert parent.is_positive()
    assert parent.constraint == Range.singleton(Version("9.0"))
    assert dependency.package == "dep"
    assert not dependency.is_positive()
    assert dependency.constraint == Range.from_versions(
        [Version("1.0"), Version("2.0")]
    )
    assert provider.consume_pending_clauses() == []


def test_a_release_the_check_accepts_queues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    provider._active_decisions["dep"] = Version("2.0")

    assert not provider._selected_dependency_rejects("parent", Version("9.0"))

    assert provider.consume_pending_clauses() == []


def test_a_derived_range_rejection_queues_the_clause_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path, monkeypatch)
    provider._active_positive_ranges = {"dep": Range.at_least(Version("3.0"))}

    assert provider._selected_dependency_rejects("parent", Version("9.0"))

    assert len(provider.consume_pending_clauses()) == 1


@pytest.mark.parametrize("seed", range(20))
def test_rejection_clauses_never_change_the_answer(
    seed: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    roots = build_random_graph(wheelhouse, seed)

    with_clauses = resolve(wheelhouse, roots)

    monkeypatch.setattr(NabProvider, "_queue_rejection_clause", lambda self, *a: None)
    without_clauses = resolve(wheelhouse, roots)

    assert with_clauses == without_clauses, f"seed {seed}"


def test_the_clauses_reach_the_resolver_during_a_descent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every newer parent release excludes the child already decided.

    The check rejects each of them while choosing the parent's version, and
    the resolver takes the queued clauses before deciding; on a graph this
    small the check alone already avoids the conflicts, so what is pinned
    here is the wiring: one clause per rejected release, all handed over.
    """
    from benchmark_support import make_wheel, reset_caches

    reset_caches()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    make_wheel(wheelhouse, "child", "1.0")
    make_wheel(wheelhouse, "child", "2.0")
    # ``anchor`` makes the resolver decide child 2.0 before parent.
    make_wheel(wheelhouse, "anchor", "1.0", requires=["child"])
    for minor in range(12):
        make_wheel(wheelhouse, "parent", f"1.{minor}", requires=["child<2"])
    make_wheel(wheelhouse, "parent", "0.9", requires=["child>=1"])

    handed_over: list[Any] = []
    real = NabProvider.consume_pending_clauses

    def counting(self: NabProvider) -> list[Any]:
        clauses = real(self)
        handed_over.extend(clauses)
        return clauses

    monkeypatch.setattr(NabProvider, "consume_pending_clauses", counting)

    result = resolve(wheelhouse, ["anchor", "parent"])

    assert result is not None
    assert result["parent"] == "0.9"
    assert result["child"] == "2.0"
    rejected = {str(clause.terms[0].constraint) for clause in handed_over}
    assert len(handed_over) == 12
    assert rejected == {f"1.{minor}" for minor in range(12)}
    assert all(clause.terms[1].package == "child" for clause in handed_over)
