"""A cleared decision queue must re-key every undecided package.

``clear`` runs mid-solve when the resolver backtracks wholesale after a
dependency invalidation. The scan after it decides what to re-evaluate by
comparing epochs; a clear that reset the stored epoch to zero was
indistinguishable from "nothing moved" whenever the resolver's epoch had
never advanced, so only the packages in that scan's changed set got keys.
The heap then ran dry with packages still undecided.
"""

from __future__ import annotations

from kpip._vendor.nab_resolver.decision_queue import DecisionQueue


def _key(package: str) -> tuple[int, str]:
    return (0, package)


def test_a_cleared_queue_keys_every_undecided_package_at_the_same_epoch() -> None:
    queue: DecisionQueue[str] = DecisionQueue()
    undecided = {"a", "b", "c", "d"}

    assert queue.pick(undecided, _key, set(undecided), 0) == "a"

    queue.clear()

    # Only one package is reported changed, at the epoch the queue last saw.
    assert queue.pick(undecided, _key, {"c"}, 0) == "a"
    assert set(queue._keys) == undecided


def test_picks_keep_working_as_the_keyed_packages_are_decided() -> None:
    queue: DecisionQueue[str] = DecisionQueue()
    undecided = {"a", "b", "c", "d"}
    queue.pick(undecided, _key, set(undecided), 0)
    queue.clear()

    # Decide packages one at a time, reporting only the one that left.
    remaining = set(undecided)
    order = []
    changed = {"c"}
    while remaining:
        chosen = queue.pick(remaining, _key, changed, 0)
        order.append(chosen)
        remaining.remove(chosen)
        changed = {chosen}

    assert order == ["a", "b", "c", "d"]
