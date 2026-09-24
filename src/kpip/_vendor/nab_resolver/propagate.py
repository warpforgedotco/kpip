"""Unit propagation for the PubGrub resolver.

When an incompatibility has all but one term satisfied, the
remaining term's negation is derived (unit rule).  This module
owns that loop plus the per-term/per-incompatibility evaluators
that classify each term as SATISFIED, CONTRADICTED, or UNDETERMINED.

Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from heapq import merge
from itertools import chain
from typing import TYPE_CHECKING, Any

from .types import IncompatibilityState, SetRelation, Term

if TYPE_CHECKING:
    from .resolver import Resolver
    from .types import Incompatibility, RangeProtocol

# Bound once so the hot paths load a module global instead of a class attribute.
_SATISFIED_REL = SetRelation.SATISFIED
_CONTRADICTED_REL = SetRelation.CONTRADICTED
_UNDETERMINED_REL = SetRelation.UNDETERMINED
_CONTRADICTED_STATE = IncompatibilityState.CONTRADICTED
_CONFLICT_STATE = IncompatibilityState.CONFLICT

__all__ = [
    "classify_relation",
    "evaluate_incompatibility",
    "term_relation",
    "unit_propagation",
]

# Upper bound on relation_cache size; cleared on overflow to bound memory.
RELATION_CACHE_MAX = 100_000

# A probe that misses builds its key for nothing, and on a conflict-heavy
# resolve most probes miss.  So the memo runs only while its hit rate over a
# window of probes pays for the key, and a longer window re-samples it later in
# case the resolve changes shape.
RELATION_GATE_WINDOW = 4_096
RELATION_GATE_MIN_HITS = 1_024
RELATION_GATE_RECHECK = 65_536

# Marks a package absent from the solution's effective-range cache.
_MISSING = object()

# Upper bound on the address-keyed token memo, cleared on overflow together
# with the range objects it holds alive.  A larger cap wipes less often and so
# holds on to more ranges, which costs peak memory.
RANGE_ID_MEMO_MAX = 8_192


def _related_incompatibility_groups(
    resolver: Resolver[Any, Any], package: Any
) -> tuple[Sequence[int], ...]:
    """Return sorted clause-index groups relevant to ``package`` now."""
    general = resolver.package_to_incompatibilities.get(package, ())
    decision = resolver.solution.decided_version(package)
    if decision is None:
        return general, resolver.dependency_parent_incompatibilities.get(package, ())

    by_version = resolver.dependency_parent_versions.get(package)
    try:
        exact = () if by_version is None else by_version.get(decision, ())
    except TypeError:
        return general, resolver.dependency_parent_incompatibilities.get(package, ())
    return general, resolver.dependency_parent_fallbacks.get(package, ()), exact


def unit_propagation(
    resolver: Resolver[Any, Any], changed_package: Any
) -> Incompatibility[Any, Any] | None:
    """Propagate constraints from incompatibilities.

    When an incompatibility has all but one term satisfied, the
    remaining term's negation is derived (unit rule). Returns a
    conflicting incompatibility if all terms are satisfied, or
    None if propagation completes without conflict.

    One contradicted term settles a clause for the rest of the solution's
    contradiction epoch.  Each clause records the epoch it was last settled in,
    and is skipped while that stamp is current.

    The per-term relation check stays inline: this loop evaluates tens of
    thousands of terms during deep backtracking, so routing each one through
    ``evaluate_incompatibility`` and ``term_relation`` dominates the solve.

    Reference: https://github.com/dart-lang/pub/blob/master/doc/solver.md#unit-propagation
    """
    groups = _related_incompatibility_groups(resolver, changed_package)
    epoch = resolver.solution.contradiction_epoch
    contradicted_at = resolver.clause_contradicted_at
    for group in groups:
        for incompatibility_index in group:
            if contradicted_at[incompatibility_index] != epoch:
                return _unit_propagation_core(resolver, changed_package, groups, epoch)
    return None


def _unit_propagation_core(
    resolver: Resolver[Any, Any],
    changed_package: Any,
    initial_groups: tuple[Sequence[int], ...],
    epoch: int,
) -> Incompatibility[Any, Any] | None:
    """Run propagation after the entry package has relevant clauses."""
    propagation_queue: deque[Any] = deque([changed_package])
    in_queue: set[Any] = {changed_package}

    contradicted_at = resolver.clause_contradicted_at
    solution = resolver.solution
    solution_get = solution.get
    # The cache ``solution.get`` answers from first; it is cleared per package
    # in place, never rebound, so reading it here skips a call on each hit.
    effective_get = solution._effective_range_cache.get  # noqa: SLF001
    has_positive_constraint = solution.has_positive_constraint
    derive = solution.derive
    incompatibilities = resolver.incompatibilities
    stats = resolver.stats
    observer = resolver.observer
    cache = resolver.relation_cache
    cache_get = cache.get
    # Cleared in place on overflow, never rebound, so the hoist is sound.
    id_tokens = resolver.range_token_by_id
    id_tokens_get = id_tokens.get
    satisfied = _SATISFIED_REL
    contradicted = _CONTRADICTED_REL
    undetermined = _UNDETERMINED_REL
    # The memo gate's state lives in locals while the loop runs, and goes back
    # to the resolver before each resample and on return.
    cache_on = resolver.relation_cache_on
    gate_hits = resolver.relation_gate_hits
    probes_left = resolver.relation_gate_probes_left

    while propagation_queue:
        package = propagation_queue.popleft()
        in_queue.discard(package)
        if initial_groups:
            groups = initial_groups
            initial_groups = ()
        else:
            groups = _related_incompatibility_groups(resolver, package)
        # Production dispatch returns two or three groups.  Keep that path
        # allocation-free while retaining arbitrary arity for private callers.
        group_count = len(groups)
        if group_count not in (2, 3):
            nonempty = tuple(group for group in groups if group)
            if not nonempty:
                continue
            if len(nonempty) == 1:
                related_indices = nonempty[0]
            elif len(nonempty) == 2:
                left = nonempty[0]
                right = nonempty[1]
                if left[-1] < right[0]:
                    related_indices = chain(left, right)
                elif right[-1] < left[0]:
                    related_indices = chain(right, left)
                else:
                    related_indices = merge(left, right)
            else:
                related_indices = merge(*nonempty)
        else:
            first = groups[0]
            second = groups[1]
            third: Sequence[int] = groups[2] if group_count == 3 else ()
            if first and second and third:
                related_indices = merge(first, second, third)
            else:
                if first:
                    left = first
                    right = second if second else third
                elif second:
                    left = second
                    right = third
                else:
                    left = third
                    right = ()

                if not left:
                    continue
                if not right:
                    related_indices = left
                elif left[-1] < right[0]:
                    related_indices = chain(left, right)
                elif right[-1] < left[0]:
                    related_indices = chain(right, left)
                else:
                    related_indices = merge(left, right)

        previous_index = -1
        for incompatibility_index in related_indices:
            if incompatibility_index == previous_index:
                continue
            previous_index = incompatibility_index
            if contradicted_at[incompatibility_index] == epoch:
                continue

            incompatibility = incompatibilities[incompatibility_index]

            undetermined_term = None
            undetermined_assignment: RangeProtocol[Any] | None = None
            conflict = True
            for term in incompatibility.terms:
                assignment = effective_get(term.package, _MISSING)
                if assignment is _MISSING:
                    assignment = solution_get(term.package)
                if assignment is None:
                    relation = None
                else:
                    positive = term._positive  # noqa: SLF001
                    constraint = term.constraint

                    key = None
                    relation = None
                    if cache_on:
                        assignment_token = id_tokens_get(id(assignment))
                        if assignment_token is None:
                            assignment_token = _intern_range(resolver, assignment)
                        constraint_token = id_tokens_get(id(constraint))
                        if constraint_token is None:
                            constraint_token = _intern_range(resolver, constraint)
                        # Packed int key; see resolver.relation_cache.  Tokens
                        # stay far below 2**32, so the fields cannot collide.
                        key = (
                            (assignment_token << 33)
                            | (constraint_token << 1)
                            | positive
                        )
                        relation = cache_get(key)

                    if relation is None:
                        range_relation = assignment.relation(constraint)
                        subset = range_relation.is_subset
                        disjoint = range_relation.is_disjoint
                        if positive:
                            relation = (
                                satisfied
                                if subset
                                else (
                                    contradicted
                                    if disjoint
                                    else undetermined
                                )
                            )
                        else:
                            relation = (
                                satisfied
                                if disjoint
                                else (
                                    contradicted if subset else undetermined
                                )
                            )
                        probes_left -= 1
                        if key is not None:
                            if len(cache) >= RELATION_CACHE_MAX:
                                cache.clear()
                            cache[key] = relation
                        if probes_left <= gate_hits:
                            resolver.relation_gate_hits = gate_hits
                            resolver.relation_gate_probes_left = probes_left
                            _resample_relation_gate(resolver)
                            cache_on = resolver.relation_cache_on
                            gate_hits = resolver.relation_gate_hits
                            probes_left = resolver.relation_gate_probes_left
                    else:
                        gate_hits += 1

                    if (
                        (positive and relation is satisfied)
                        or (not positive and relation is contradicted)
                    ) and not has_positive_constraint(term.package):
                        relation = None

                if relation is satisfied:
                    continue
                if relation is contradicted:
                    contradicted_at[incompatibility_index] = epoch
                    conflict = False
                    undetermined_term = None
                    break
                if undetermined_term is not None:
                    conflict = False
                    undetermined_term = None
                    break
                undetermined_term = term
                undetermined_assignment = assignment

            if undetermined_term is None:
                if conflict:
                    resolver.relation_gate_hits = gate_hits
                    resolver.relation_gate_probes_left = probes_left
                    return incompatibility
                continue

            negated_package = undetermined_term.package
            negated_positive = not undetermined_term._positive  # noqa: SLF001
            range_before = undetermined_assignment
            range_after = derive(
                negated_package,
                undetermined_term.constraint,
                positive=negated_positive,
                cause=incompatibility,
            )

            # A derive that empties a range advances the epoch, which retires
            # stamps taken before it.
            epoch = solution.contradiction_epoch

            if range_before != range_after:
                stats.derivations += 1
                observer.on_derivation(
                    negated_package,
                    positive=negated_positive,
                    cause=incompatibility,
                )
                if negated_package not in in_queue:
                    propagation_queue.append(negated_package)
                    in_queue.add(negated_package)

    resolver.relation_gate_hits = gate_hits
    resolver.relation_gate_probes_left = probes_left
    return None


def evaluate_incompatibility(
    resolver: Resolver[Any, Any], incompatibility: Incompatibility[Any, Any]
) -> IncompatibilityState | Term[Any, Any] | None:
    """Evaluate an incompatibility against the current partial solution.

    Returns:
      ``IncompatibilityState.CONFLICT``: all terms satisfied
      ``IncompatibilityState.CONTRADICTED``: some term is contradicted
      ``Term``: exactly one undetermined term (unit propagation candidate)
      ``None``: two or more undetermined terms (nothing to do yet)
    """
    undetermined_term: Term[Any, Any] | None = None

    for term in incompatibility.terms:
        relation = term_relation(resolver, term)
        if relation is _SATISFIED_REL:
            continue
        if relation is _CONTRADICTED_REL:
            return _CONTRADICTED_STATE
        if undetermined_term is not None:
            return None
        undetermined_term = term

    if undetermined_term is not None:
        return undetermined_term
    return _CONFLICT_STATE


def _intern_range(resolver: Resolver[Any, Any], range_: RangeProtocol[Any]) -> int:
    """Return the token for ``range_``, minting one the first time it is seen.

    Equal ranges share a token, so the relation cache keys on range value
    rather than on identity.  The address memo the caller reads first is only
    sound while the object it answers for is alive, so ``interned_ranges``
    holds on to every range that memo records.
    """
    tokens = resolver.range_tokens
    token = tokens.get(range_)
    if token is None:
        if len(tokens) >= RANGE_ID_MEMO_MAX:
            tokens.clear()
        token = resolver.next_range_token
        resolver.next_range_token = token + 1
        tokens[range_] = token

    id_tokens = resolver.range_token_by_id
    if len(id_tokens) >= RANGE_ID_MEMO_MAX:
        id_tokens.clear()
        resolver.interned_ranges.clear()
    id_tokens[id(range_)] = token
    resolver.interned_ranges.append(range_)

    return token


def _resample_relation_gate(resolver: Resolver[Any, Any]) -> None:
    """Judge the window of probes that just ended and open the next one.

    While the memo is on, the window counts hits, and too few switch the memo
    off and drop the entries it collected.  While it is off, the window is only
    the wait before the memo is tried again.
    """
    window = RELATION_GATE_WINDOW
    if not resolver.relation_cache_on:
        resolver.relation_cache_on = True
    elif resolver.relation_gate_hits < RELATION_GATE_MIN_HITS:
        resolver.relation_cache_on = False
        resolver.relation_cache.clear()
        window = RELATION_GATE_RECHECK

    resolver.relation_gate_hits = 0
    resolver.relation_gate_probes_left = window


def term_relation(resolver: Resolver[Any, Any], term: Term[Any, Any]) -> SetRelation:
    """Check how the partial solution relates to this term.

    A term can only be satisfied or contradicted when the package has a
    positive constraint; without one it might not participate in the
    solution (negative terms are trivially true for absent packages).

    See: https://github.com/dart-lang/pub/blob/master/doc/solver.md#term
    """
    assignment = resolver.solution.get(term.package)
    if assignment is None:
        return _UNDETERMINED_REL

    positive = term.is_positive()
    constraint = term.constraint

    # key stays None while the memo is off, so a miss below stores nothing.
    key = None
    result = None
    if resolver.relation_cache_on:
        # Both lookups stay inline because most probes hit while the memo is
        # on; only a miss pays for the call into _intern_range.
        id_tokens = resolver.range_token_by_id
        assignment_token = id_tokens.get(id(assignment))
        if assignment_token is None:
            assignment_token = _intern_range(resolver, assignment)
        constraint_token = id_tokens.get(id(constraint))
        if constraint_token is None:
            constraint_token = _intern_range(resolver, constraint)

        # Packed int key; see resolver.relation_cache.
        key = (assignment_token << 33) | (constraint_token << 1) | positive
        result = resolver.relation_cache.get(key)

    if result is None:
        relation = assignment.relation(constraint)
        result = classify_relation(
            term, subset=relation.is_subset, disjoint=relation.is_disjoint
        )

        # A hit must not write this counter, so it is charged here and not above.
        probes_left = resolver.relation_gate_probes_left - 1
        resolver.relation_gate_probes_left = probes_left

        if key is not None:
            cache = resolver.relation_cache
            if len(cache) >= RELATION_CACHE_MAX:
                cache.clear()
            cache[key] = result

        # Nothing hits while the memo is off, so the recheck wait runs its
        # full length.
        if probes_left <= resolver.relation_gate_hits:
            _resample_relation_gate(resolver)
    else:
        resolver.relation_gate_hits += 1

    needs_positive = (positive and result is _SATISFIED_REL) or (
        not positive and result is _CONTRADICTED_REL
    )
    if needs_positive and not resolver.solution.has_positive_constraint(term.package):
        return _UNDETERMINED_REL

    return result


def classify_relation(
    term: Term[Any, Any],
    *,
    subset: bool,
    disjoint: bool,
) -> SetRelation:
    """Classify a term from the assignment's relation to its constraint.

    ``subset`` and ``disjoint`` describe the assignment against
    ``term.constraint``, as returned by ``RangeProtocol.relation``.

    Positive term: satisfied when the assignment is a subset of the
    constraint; contradicted when the two are disjoint.
    Negative term: the two swap, since a negative term forbids the
    constraint's versions.
    """
    if term.is_positive():
        satisfied, contradicted = subset, disjoint
    else:
        satisfied, contradicted = disjoint, subset

    if satisfied:
        return _SATISFIED_REL
    if contradicted:
        return _CONTRADICTED_REL
    return _UNDETERMINED_REL
