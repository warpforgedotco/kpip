"""``_prefetch_descent_window`` is lookahead: it may cost bandwidth, never
answers. The guards here are that it stays shut when nothing is descending,
opens when something is, and never changes what gets resolved."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import kpip.resolution.nab_provider as nab_provider
from kpip.core.versions import Version
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

from benchmark_support import (  # noqa: E402
    make_transitive_backtracking_graph,
    reset_caches,
)


def _adapter(monkeypatch: pytest.MonkeyPatch) -> tuple[NabProvider, list[Any]]:
    """A provider whose prefetches are recorded rather than sent."""
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    submitted: list[Any] = []

    class _Materializer:
        def prefetch_metadata(self, records: Any, **_: Any) -> None:
            submitted.extend(records)

    monkeypatch.setattr(
        adapter.provider,
        "release_candidates",
        lambda requirement, version: [(requirement.name, version)],
    )
    monkeypatch.setattr(
        adapter.provider,
        "get_materializer_internal",
        lambda: _Materializer(),
    )
    monkeypatch.setattr(adapter, "_pins_are_impossible", lambda package, version: False)
    return adapter, submitted


VERSIONS = [Version(f"1.{index}.0") for index in range(1, 65)]

SEEN_WIDENING = [0, 2, 5, 10, 19, 36, 37]


def test_a_package_decided_once_prefetches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One decision, which stands: speculating is pure cost."""
    adapter, submitted = _adapter(monkeypatch)

    assert adapter._newest_viable("demo", list(VERSIONS)) == VERSIONS[-1]
    assert submitted == []


def test_the_window_opens_and_widens_as_a_descent_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each lower decision is a step of a descent: look ahead, and widen."""
    adapter, submitted = _adapter(monkeypatch)

    seen: list[int] = []
    for step in range(7):
        adapter._newest_viable("demo", list(VERSIONS[: len(VERSIONS) - step]))
        seen.append(len(submitted))

    # Cumulative: each window starts one release lower than the last and is
    # twice as wide, so it re-covers most of the earlier one and adds the
    # rest, until the width caps at 32.
    assert seen == SEEN_WIDENING
    assert nab_provider._DESCENT_PREFETCH_WINDOW == 32


def test_re_asking_the_same_decision_is_not_a_descent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver re-asks open packages when ranking what to decide next,
    and re-affirms pins while merging extras. Neither is backtracking."""
    adapter, submitted = _adapter(monkeypatch)

    for _ in range(7):
        adapter._newest_viable("demo", list(VERSIONS))
    monkeypatch.setattr(adapter, "_versions", lambda package: tuple(VERSIONS))
    for _ in range(7):
        adapter._newest_viable("demo", [VERSIONS[-1]])

    assert submitted == []


def test_no_release_is_fetched_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlapping windows must not re-request what an earlier one covered."""
    adapter, submitted = _adapter(monkeypatch)

    for _ in range(8):
        adapter._newest_viable("demo", list(VERSIONS))

    assert len(submitted) == len(set(submitted))


def test_an_exact_pin_still_looks_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin arrives with one matching version, and is half of a descent."""
    adapter, submitted = _adapter(monkeypatch)
    monkeypatch.setattr(adapter, "_versions", lambda package: tuple(VERSIONS))

    for version in (VERSIONS[-1], VERSIONS[-2], VERSIONS[-3]):
        adapter._newest_viable("demo", [version])

    assert submitted, "an exactly pinned descent prefetched nothing"
    assert all(version < VERSIONS[-1] for _, version in submitted)


def test_the_window_does_not_change_what_is_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same graph, window on and off, same answer."""
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    make_transitive_backtracking_graph(wheelhouse, "descent", versions=64)

    def resolve() -> dict[str, str]:
        reset_caches()
        engine = ResolutionEngine(
            provider=CandidateProvider.from_options(
                find_links=[str(wheelhouse)],
                no_index=True,
            ),
            ignore_installed=True,
        )
        result = engine.resolve(["descent-root"])
        return {c.name: str(c.version) for c in result.candidates}

    opened = 0
    real_window = NabProvider._prefetch_descent_window

    def counting(
        self: NabProvider, package: str, newest_first: Any, index: int
    ) -> None:
        nonlocal opened
        before = len(self._descent_prefetched)
        real_window(self, package, newest_first, index)
        opened += len(self._descent_prefetched) - before

    monkeypatch.setattr(NabProvider, "_prefetch_descent_window", counting)
    with_window = resolve()

    # Without this the comparison below is vacuous.
    assert opened > 0, "the window never opened, so this proves nothing"

    monkeypatch.setattr(nab_provider, "_DESCENT_PREFETCH_WINDOW", 0)
    without_window = resolve()

    assert with_window == without_window


@pytest.mark.parametrize(
    "target, error",
    [
        ("release_candidates", KeyError("nope")),
        ("get_materializer_internal", RuntimeError("nope")),
    ],
)
def test_a_failing_lookahead_does_not_fail_the_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: Exception,
) -> None:
    """Speculation is discardable, so nothing it raises may reach the resolver."""
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    make_transitive_backtracking_graph(wheelhouse, "boom", versions=64)

    def resolve() -> dict[str, str]:
        reset_caches()
        engine = ResolutionEngine(
            provider=CandidateProvider.from_options(
                find_links=[str(wheelhouse)],
                no_index=True,
            ),
            ignore_installed=True,
        )
        return {
            c.name: str(c.version) for c in engine.resolve(["boom-root"]).candidates
        }

    expected = resolve()

    inside = threading.local()
    real_window = NabProvider._prefetch_descent_window

    def window(self: NabProvider, package: str, newest_first: Any, index: int) -> None:
        inside.on = True
        try:
            real_window(self, package, newest_first, index)
        finally:
            inside.on = False

    real_target = getattr(CandidateProvider, target)

    def failing(*args: Any, **kwargs: Any) -> Any:
        if getattr(inside, "on", False):
            raise error
        return real_target(*args, **kwargs)

    monkeypatch.setattr(NabProvider, "_prefetch_descent_window", window)
    monkeypatch.setattr(CandidateProvider, target, failing)

    assert resolve() == expected


def test_the_two_hop_check_looks_ahead_down_the_child_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each root release's dependency window sits just below the last one's.

    Checking a window fetched only its one or two new child releases and
    blocked on them, so a long descent was as many serial round trips.  The
    check starts metadata for a window of the child's catalog below the
    lowest release it just checked, widening as the descent continues, and
    starts every release once.
    """
    adapter, submitted = _adapter(monkeypatch)
    ascending = tuple(sorted(VERSIONS))
    adapter._forward_catalog_versions["child"] = ascending

    seen: list[int] = []
    for step in range(6):
        # A window of eight releases, moving down one release per parent.
        top = len(ascending) - step
        adapter._prefetch_child_lookahead("child", list(ascending[top - 8 : top]))
        seen.append(len(submitted))

    # Below 1.57.0 the first window starts two releases, then four below
    # those, then eight, sixteen, and the 26 left: cumulative counts, no
    # release twice, the buffer ahead of the descent growing every step.
    assert seen == [2, 6, 14, 30, 56, 56]
    assert len(submitted) == len({tuple(record) for record in submitted})
    assert all(record[1] < Version("1.57.0") for record in submitted)


def test_a_window_above_the_lookahead_floor_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, submitted = _adapter(monkeypatch)
    ascending = tuple(sorted(VERSIONS))
    adapter._forward_catalog_versions["child"] = ascending

    adapter._prefetch_child_lookahead("child", list(ascending[40:48]))
    started = len(submitted)
    assert started > 0

    # Re-asking about the same window, or a higher one, is not a descent.
    adapter._prefetch_child_lookahead("child", list(ascending[40:48]))
    adapter._prefetch_child_lookahead("child", list(ascending[50:58]))

    assert len(submitted) == started
