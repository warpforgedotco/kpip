"""Installation candidate materialization and ordering."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Protocol, TypeVar

from kpip.core.utils import default_worker_count
from kpip.core.wheel import WheelCandidate
from kpip.index.candidate_materialization import LazyWheelCandidate
from kpip.index.vcs import vcs_scheme
from kpip.install.wheel_archive_cache import INSTALL_WORKERS

_MATERIALIZATION_WORKERS = 32


def fetch_candidate_sources(
    candidates: Sequence[Any],
    fetch_source: Callable[[Any], str],
) -> list[str]:
    """Bring every candidate's artifact local, remote fetches in parallel.

    A VCS sdist may prompt for credentials, so those fetch serially in
    candidate order before the pool starts; everything else goes through a
    thread pool the way wheel materialization does. Results keep candidate
    order.
    """

    sources: list[str] = [""] * len(candidates)

    pooled: list[int] = []

    for index, candidate in enumerate(candidates):
        may_prompt = (
            candidate.source_kind == "sdist"
            and candidate.source_url is not None
            and vcs_scheme(candidate.source_url) is not None
        )

        if may_prompt:
            sources[index] = fetch_source(candidate)

        else:
            pooled.append(index)

    if pooled:
        with ThreadPoolExecutor(
            max_workers=min(default_worker_count(), len(pooled)),
            thread_name_prefix="kpip-download",
        ) as pool:
            fetched = pool.map(
                lambda index: fetch_source(candidates[index]),
                pooled,
            )

            for index, source in zip(pooled, fetched):
                sources[index] = source

    return sources


def _run_candidate_operation(
    candidates: Sequence[WheelCandidate],
    operation: Callable[[WheelCandidate], WheelCandidate],
) -> list[WheelCandidate]:
    """Run an artifact operation with ordered, winner-only wheel concurrency."""

    completed: list[WheelCandidate] = []

    remote_wheels: list[WheelCandidate] = []

    def flush_remote_wheels() -> None:
        if not remote_wheels:
            return

        if len(remote_wheels) == 1:
            completed.append(operation(remote_wheels[0]))

        else:
            with ThreadPoolExecutor(
                max_workers=min(_MATERIALIZATION_WORKERS, len(remote_wheels)),
                thread_name_prefix="kpip-wheel",
            ) as pool:
                completed.extend(pool.map(operation, remote_wheels))

        remote_wheels.clear()

    for candidate in candidates:
        if (
            isinstance(candidate, LazyWheelCandidate)
            and candidate.source_kind == "wheel"
            and not candidate.record_internal.link.is_file
        ):
            remote_wheels.append(candidate)

            continue

        flush_remote_wheels()

        completed.append(operation(candidate))

    flush_remote_wheels()

    return completed


def materialize_candidate(candidate: WheelCandidate) -> WheelCandidate:
    if isinstance(candidate, LazyWheelCandidate):
        return candidate.materialize()

    return candidate


def materialize_candidates(
    candidates: Sequence[WheelCandidate],
) -> list[WheelCandidate]:
    """Materialize remote wheel winners concurrently in installation order."""

    return _run_candidate_operation(candidates, materialize_candidate)


def prepare_install_candidates(
    candidates: Sequence[WheelCandidate],
    cache_dir: str | None,
    prepare_archive: Callable[[WheelCandidate, str], object] | None = None,
) -> list[WheelCandidate]:
    """Materialize winners and pipeline completed wheels into archive storage."""

    if cache_dir is None or not candidates or prepare_archive is None:
        return materialize_candidates(candidates)

    count = len(candidates)

    concrete: list[WheelCandidate | None] = [None] * count

    prepared: list[WheelCandidate | None] = [None] * count

    errors: list[BaseException | None] = [None] * count

    remote: list[tuple[int, WheelCandidate]] = []

    local: list[tuple[int, WheelCandidate]] = []

    for index, candidate in enumerate(candidates):
        if (
            isinstance(candidate, LazyWheelCandidate)
            and candidate.source_kind == "wheel"
            and not candidate.record_internal.link.is_file
        ):
            remote.append((index, candidate))

        else:
            local.append((index, candidate))

    archive_futures: dict[Future[object], int] = {}

    with ThreadPoolExecutor(
        max_workers=min(INSTALL_WORKERS, count),
        thread_name_prefix="kpip-archive",
    ) as archive_pool:

        def submit_archive(index: int, candidate: WheelCandidate) -> None:
            concrete[index] = candidate

            archive_futures[
                archive_pool.submit(prepare_archive, candidate, cache_dir)
            ] = index

        if remote:
            with ThreadPoolExecutor(
                max_workers=min(_MATERIALIZATION_WORKERS, len(remote)),
                thread_name_prefix="kpip-wheel",
            ) as download_pool:
                download_futures = {
                    download_pool.submit(materialize_candidate, candidate): index
                    for index, candidate in remote
                }

                for future in as_completed(download_futures):
                    index = download_futures[future]

                    try:
                        submit_archive(index, future.result())

                    except Exception as exc:
                        errors[index] = exc

        for index, candidate in local:
            try:
                submit_archive(index, materialize_candidate(candidate))

            except Exception as exc:
                errors[index] = exc

        for future in as_completed(tuple(archive_futures)):
            index = archive_futures[future]

            candidate = concrete[index]

            assert candidate is not None

            try:
                archive = future.result()

            except OSError:
                prepared[index] = candidate

            except Exception as exc:
                errors[index] = exc

            else:
                prepared[index] = candidate.copy_with(wheel_layout=archive)

    for error in errors:
        if error is not None:
            raise error

    if any(candidate is None for candidate in prepared):
        raise RuntimeError("candidate preparation did not produce every wheel")

    return [candidate for candidate in prepared if candidate is not None]


class _Named(Protocol):
    @property
    def canonical_name(self) -> str: ...


class _WithDependencies(_Named, Protocol):
    @property
    def dependencies(self) -> Any: ...


_Candidate = TypeVar("_Candidate", bound=_Named)


def dependency_graph(candidates: Sequence[_WithDependencies]) -> dict[str, set[str]]:
    """Each candidate's dependencies among ``candidates``, for
    :func:`installation_order` where no resolution graph is at hand."""

    names = {candidate.canonical_name for candidate in candidates}

    return {
        candidate.canonical_name: {
            dependency.canonical_name
            for dependency in candidate.dependencies or ()
            if dependency.canonical_name in names
        }
        for candidate in candidates
    }


_PYTHON_NODE = "<Python from Requires-Python>"
"""pip's graph node for the interpreter: a child of every distribution that
declares a Requires-Python, which keeps those out of the first leaves."""

_VISITS = 5
"""pip weighs a distribution by at most this many paths to it."""


def installation_order(
    candidates: Sequence[_Candidate],
    graph: Mapping[str, Collection[str]],
    roots: Collection[str],
    *,
    ignore_requires_python: bool = False,
) -> list[_Candidate]:
    """``candidates`` in the order pip installs them.

    The order only shows when two distributions ship the same file: each
    is installed in turn, so the later one's copy is the one left, and
    kpip keeps the same one. pip weighs each distribution by its place in
    the dependency graph (``get_topological_weights``): leaves pruned
    round by round weigh most, the first round most of all, and every
    other distribution weighs its longest path from a requested one, over
    at most five paths; it installs the heaviest first, ties by name, both
    descending.

    pip's graph has one more node, the interpreter, a dependency of every
    distribution with a Requires-Python unless those are ignored: jupyter
    1.0.0 declares none and jupyter-core does, so without dependencies
    jupyter-core is the later of the two and its ``jupyter.py`` is left.

    One difference remains: pip's graph also has a node for each set of
    extras a distribution is asked for (``jsonschema[format-nongpl]``),
    between the dependent and the distribution, which ``graph`` -- names
    only -- does not. The order can differ where one is in play; jupyter's
    98 releases then differ in three places, none of them sharing a file.
    """

    names = {candidate.canonical_name for candidate in candidates}

    remaining: dict[str | None, set[str]] = {
        name: {child for child in graph.get(name, ()) if child in names}
        for name in names
    }

    if not ignore_requires_python:
        for candidate in candidates:
            requires_python = getattr(candidate, "requires_python", None)

            if requires_python and requires_python.strip():
                remaining[candidate.canonical_name].add(_PYTHON_NODE)

                # In pip's graph only once something depends on it.
                remaining[_PYTHON_NODE] = set()

    remaining[None] = {name for name in roots if name in names}

    weights: dict[str, list[int]] = {}

    while True:
        leaves = [
            name
            for name, children in remaining.items()
            if name is not None and not children
        ]

        if not leaves:
            break

        weight = len(remaining) - 1

        for leaf in leaves:
            if leaf in names:
                weights[leaf] = [weight]

            del remaining[leaf]

        for children in remaining.values():
            children.difference_update(leaves)

    path: set[str | None] = set()

    def visit(node: str | None) -> None:
        if node in path:
            return

        node_weights = weights.get(node, []) if node is not None else []

        if len(node_weights) >= _VISITS:
            return

        path.add(node)

        for child in sorted(remaining.get(node, ())):
            visit(child)

        path.remove(node)

        if node in names:
            node_weights.append(len(path))

            weights[node] = node_weights

    visit(None)

    return sorted(
        candidates,
        key=lambda candidate: (
            max(weights.get(candidate.canonical_name, [0])),
            candidate.canonical_name,
        ),
        reverse=True,
    )
