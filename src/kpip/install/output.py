"""Installation candidate materialization and ordering."""

from __future__ import annotations

import os
import queue
import sys
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from kpip.core.appdirs import archive_entry_root
from kpip.core.digests import valid_sha256
from kpip.core.utils import default_worker_count
from kpip.core.wheel import WheelCandidate
from kpip.index.vcs import vcs_scheme
from kpip.install.wheel_archive_cache import EXTRACT_WORKERS

if TYPE_CHECKING:
    from typing import TypeGuard

    from kpip.index.candidate_materialization import LazyWheelCandidate

_CANDIDATE_MATERIALIZATION = "kpip.index.candidate_materialization"


def _is_lazy(candidate: object) -> TypeGuard[LazyWheelCandidate]:
    """Whether ``candidate`` is a :class:`LazyWheelCandidate`."""
    module = sys.modules.get(_CANDIDATE_MATERIALIZATION)

    return module is not None and isinstance(candidate, module.LazyWheelCandidate)


_MATERIALIZATION_WORKERS = 32

_LOCAL_WORKERS = 4
"""Local candidates, source distributions to build among them, prepared at
once. Each build is a process of its own; four keep a few busy without
compiling every sdist of a large install at the same time."""


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
            _is_lazy(candidate)
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
    if _is_lazy(candidate):
        return candidate.materialize()

    return candidate


def materialize_candidates(
    candidates: Sequence[WheelCandidate],
) -> list[WheelCandidate]:
    """Materialize remote wheel winners concurrently in installation order."""

    return _run_candidate_operation(candidates, materialize_candidate)


_PREFETCH_WORKERS = 4
"""Wheels fetched and unpacked at once while the solve goes on."""


def _remote_wheel_url(candidate: object) -> str | None:
    """The URL of a wheel still to be downloaded from an index, or None."""
    if (
        _is_lazy(candidate)
        and candidate.source_kind == "wheel"
        and not candidate.record_internal.link.is_file
    ):
        return candidate.record_internal.link.url
    return None


class WheelPrefetch:
    """Download and unpack wheels while the solve is still going.

    An install used to resolve, then download every wheel it chose, then
    unpack them, strictly in turn: a cold trio install spent 0.6 s resolving
    before the first download began. This is told of wheels the install will
    most likely want -- an exactly pinned release as soon as its page is read
    (``CandidateProvider.on_likely``), and every release as the solve decides
    on it (``ResolutionEngine(on_decided=...)``) -- and fetches and unpacks
    each into the artifact and archive caches in the background.

    It only warms those caches, which are keyed by content: the install
    still prepares exactly the candidates it ends up with, as before, and
    finds them there. A wheel fetched for a release the solve backtracked
    from, or that failed, costs time and nothing else; the install fetches
    it again and reports what fails.
    """

    def __init__(
        self,
        cache_dir: str,
        prepare_archive: Callable[[WheelCandidate, str], object],
    ) -> None:
        self._cache_dir = cache_dir

        self._prepare_archive = prepare_archive

        self._lock = threading.Lock()

        self._futures: dict[str, Future[None]] = {}

        # Daemon threads, not a ThreadPoolExecutor: an install that fails
        # after the solve must not wait at exit for wheels it no longer
        # wants, and every cache write is a rename, so one cut short leaves
        # nothing behind.
        self._queue: queue.SimpleQueue[tuple[Future[None], WheelCandidate] | None] = (
            queue.SimpleQueue()
        )

        self._workers = 0

        self._closed = False

    def __call__(self, candidate: object) -> None:
        url = _remote_wheel_url(candidate)

        if url is None or self._unpacked(candidate):
            return

        with self._lock:
            if self._closed or url in self._futures:
                return

            future: Future[None] = Future()

            self._futures[url] = future

            if self._workers < _PREFETCH_WORKERS:
                self._workers += 1

                threading.Thread(
                    target=self._work,
                    name="kpip-prefetch",
                    daemon=True,
                ).start()

        self._queue.put((future, candidate))  # ty: ignore[invalid-argument-type]

    def _work(self) -> None:
        while (item := self._queue.get()) is not None:
            future, candidate = item

            if not future.set_running_or_notify_cancel():
                continue

            try:
                self._fetch(candidate)

            except BaseException as exc:
                future.set_exception(exc)

            else:
                future.set_result(None)

    def _unpacked(self, candidate: object) -> bool:
        """Whether the wheel, named by its index digest, is unpacked already.

        A warm install finds every wheel so, and fetching them again only
        took turns with the solve: one ``stat`` answers before any of that.
        """

        digest = (getattr(candidate, "source_hashes", None) or {}).get("sha256")

        return (
            isinstance(digest, str)
            and valid_sha256(digest)
            and os.path.isdir(archive_entry_root(self._cache_dir, digest))
        )

    def _fetch(self, candidate: WheelCandidate) -> None:
        self._prepare_archive(materialize_candidate(candidate), self._cache_dir)

    def wait(self, candidates: Sequence[object]) -> None:
        """Let any fetch of one of ``candidates`` finish, however it ends, so
        the install does not fetch the same wheel alongside it."""
        with self._lock:
            futures = [
                future
                for candidate in candidates
                if (url := _remote_wheel_url(candidate)) is not None
                and (future := self._futures.get(url)) is not None
            ]

        for future in futures:
            try:
                future.result()

            except BaseException:  # noqa: BLE001 - redone, and reported, by the install
                pass

    def close(self) -> None:
        """Drop what was never started; what is running finishes on its own."""
        with self._lock:
            self._closed = True

            futures = list(self._futures.values())

            workers = self._workers

        for future in futures:
            future.cancel()

        for _ in range(workers):
            self._queue.put(None)


def prepare_install_candidates(
    candidates: Sequence[WheelCandidate],
    cache_dir: str | None,
    prepare_archive: Callable[[WheelCandidate, str], object] | None = None,
    prefetched: WheelPrefetch | None = None,
) -> list[WheelCandidate]:
    """Materialize winners and pipeline completed wheels into archive storage.

    With ``prefetched``, its fetches of these candidates finish first; each is
    then prepared as without it, finding the caches it warmed.

    What is not a wheel to download -- a source distribution to build above
    all -- starts first, on threads of its own: it used to wait for every
    wheel download, a quarter second of a cold trio install before its one
    sdist build began, with the whole build still to run.
    """

    if cache_dir is None or not candidates or prepare_archive is None:
        if prefetched is not None:
            prefetched.wait(candidates)

        return materialize_candidates(candidates)

    count = len(candidates)

    concrete: list[WheelCandidate | None] = [None] * count

    prepared: list[WheelCandidate | None] = [None] * count

    errors: list[BaseException | None] = [None] * count

    remote: list[tuple[int, WheelCandidate]] = []

    local: list[tuple[int, WheelCandidate]] = []

    for index, candidate in enumerate(candidates):
        if _remote_wheel_url(candidate) is not None:
            remote.append((index, candidate))

        else:
            local.append((index, candidate))

    archive_futures: dict[Future[object], int] = {}

    local_pool = (
        ThreadPoolExecutor(
            max_workers=min(_LOCAL_WORKERS, len(local)),
            thread_name_prefix="kpip-local",
        )
        if local
        else None
    )

    local_futures = (
        {
            local_pool.submit(materialize_candidate, candidate): index
            for index, candidate in local
        }
        if local_pool is not None
        else {}
    )

    try:
        if prefetched is not None:
            prefetched.wait(candidates)

        with ThreadPoolExecutor(
            max_workers=min(EXTRACT_WORKERS, count),
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

            for future in as_completed(local_futures):
                index = local_futures[future]

                try:
                    submit_archive(index, future.result())

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

    finally:
        if local_pool is not None:
            # All done by now, unless this is leaving early: then nothing not
            # yet started is wanted, and a build already running finishes on
            # its own.
            local_pool.shutdown(wait=False, cancel_futures=True)

    for error in errors:
        if error is not None:
            raise error

    if any(candidate is None for candidate in prepared):
        raise RuntimeError("candidate preparation did not produce every wheel")

    return [candidate for candidate in prepared if candidate is not None]


class _Named(Protocol):
    @property
    def canonical_name(self) -> str: ...


_Candidate = TypeVar("_Candidate", bound=_Named)


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
