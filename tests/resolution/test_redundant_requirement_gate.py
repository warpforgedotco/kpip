"""``absorb_redundant_requirement`` derives only for a range type that can refine.

The derivation exists for range types whose ``&`` folds in state beyond
the version set (a pre-release opt-in, say): a requirement that does not
narrow a package's range can still refine it. For the plain ``Range`` the
intersection of a subset is the subset, so the derivation can never fire,
and on a large graph the intersections it computed to find that out were a
tenth of the resolver's compute. A type opts in with
``refines_on_intersection``.
"""

from __future__ import annotations

from typing import Any

from kpip._vendor.nab_resolver import decide
from kpip._vendor.nab_resolver.ranges import Range
from kpip.core.versions import Version

V1, V2 = Version("1.0"), Version("2.0")


class _Solution:
    def __init__(self, positive: Any) -> None:
        self.positive = positive
        self.derived: list[Any] = []

    def positive_range(self, package: str) -> Any:
        return self.positive

    def derive(self, package: str, requirement: Any, **kwargs: Any) -> None:
        self.derived.append((package, requirement))


class _Stats:
    derivations = 0


class _Observer:
    @staticmethod
    def on_derivation(*args: Any, **kwargs: Any) -> None:
        pass


class _Resolver:
    def __init__(self, positive: Any) -> None:
        self.solution = _Solution(positive)
        self.stats = _Stats()
        self.observer = _Observer()


class Refining(Range[Version]):
    """A range whose intersection carries extra state, as a subclass models it."""

    refines_on_intersection = True

    def __and__(self, other: object) -> Range[Version]:  # type: ignore[override]
        return Range.from_versions([V1, V2])  # "refined": differs from self


def test_a_plain_range_derives_nothing_and_never_intersects(monkeypatch: Any) -> None:
    positive = Range.from_versions([V1])
    calls: list[str] = []
    monkeypatch.setattr(
        Range, "__and__", lambda self, other: calls.append("and") or self
    )
    resolver = _Resolver(positive)

    decide.absorb_redundant_requirement(
        resolver, "demo", Range.from_versions([V1, V2]), None
    )

    assert resolver.solution.derived == []
    assert calls == []


def test_a_refining_range_still_derives() -> None:
    positive = Refining.from_versions([V1])
    resolver = _Resolver(positive)

    decide.absorb_redundant_requirement(
        resolver, "demo", Range.from_versions([V1, V2]), None
    )

    assert len(resolver.solution.derived) == 1
