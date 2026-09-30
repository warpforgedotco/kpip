"""Direct-install fast path for fresh wheel batches."""

from __future__ import annotations

from typing import TYPE_CHECKING
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor

from kpip.core.errors import InstallationError
from kpip.core.wheel import WheelCandidate
from kpip.install.target import InstallTarget
from kpip.install.transaction import InstallTransaction
from kpip.install.wheel_archive import (
    DestinationCache,
    ResolvedRoots,
    destination_internal_parts_text,
    validate_member_parts,
)
from kpip.install.wheel_archive_cache import INSTALL_WORKERS

if TYPE_CHECKING:
    from typing import Any
    from kpip.core.direct_url import DirectUrl

DIRECT_CONTENT_BATCH_LIMIT = 4 * 1024 * 1024
DIRECT_MEMBER_BATCH_THRESHOLD = 1_024


def direct_batch_preflight(
    requests: tuple[tuple[str, bool, DirectUrl | None], ...],
    candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
    pycompile: bool,
) -> DestinationCache | None:
    """Check whether a batch can write final paths without staging files."""
    if os.path.normcase(os.path.normpath(target.purelib)) != os.path.normcase(
        os.path.normpath(target.platlib),
    ):
        return None
    destinations: set[str] = set()
    resolved_directories: DestinationCache = {}
    resolved_roots: ResolvedRoots = target.resolved_roots_internal
    member_sets: list[tuple[tuple[str, int, int, int, int, int], ...]] = []
    member_count = 0
    total_size = 0
    for request, candidate in zip(requests, candidates):
        if request[2] is not None or not isinstance(candidate.wheel_layout, tuple):
            return None
        _, raw_members, _ = candidate.wheel_layout
        member_sets.append(raw_members)  # ty:ignore[invalid-argument-type]
        member_count += sum(
            not raw_member[0].endswith("/")
            for raw_member in raw_members  # ty:ignore[not-iterable]
        )
        total_size += sum(
            raw_member[4]
            for raw_member in raw_members  # ty:ignore[not-iterable]
            if not raw_member[0].endswith("/")
        )
    # Small batches are cheaper to stage. Direct installation pays off once
    # either the buffered content or per-member filesystem work grows large.
    if (
        total_size <= DIRECT_CONTENT_BATCH_LIMIT
        and member_count <= DIRECT_MEMBER_BATCH_THRESHOLD
    ):
        return None
    for raw_members in member_sets:
        for raw_member in raw_members:
            name = raw_member[0]
            if name.endswith("/"):
                continue
            try:
                relative_parts = validate_member_parts(name)
            except InstallationError:
                return None
            if (
                (relative_parts[-1] if relative_parts else "")
                in {"INSTALLER", "REQUESTED", "direct_url.json"}
                or (relative_parts[-1] if relative_parts else "") == "entry_points.txt"
                or (len(relative_parts) >= 2 and relative_parts[-2] == "scripts")
            ):
                return None
            destination_text = destination_internal_parts_text(
                target,
                relative_parts,
                name,
                resolved_directories=resolved_directories,
                resolved_roots=resolved_roots,
            )
            if destination_text in destinations or os.path.lexists(destination_text):
                return None
            destinations.add(destination_text)
            if pycompile and os.path.splitext(destination_text)[1] == ".py":
                compiled_destination = importlib.util.cache_from_source(
                    destination_text,
                )
                if compiled_destination in destinations or os.path.lexists(
                    compiled_destination
                ):
                    return None
                destinations.add(compiled_destination)
    return resolved_directories


def install_wheels_directly(
    requests: tuple[tuple[str, bool, DirectUrl | None], ...],
    candidates: tuple[WheelCandidate, ...],
    *,
    target: InstallTarget,
    pycompile: bool,
    installer: Any,
    destination_cache: DestinationCache,
) -> tuple[WheelCandidate, ...]:
    """Install a preflighted fresh batch directly with transactional rollback."""
    with InstallTransaction() as transaction:
        parallel = 4 <= len(requests) <= 64

        def install_one(
            index: int,
            request: tuple[str, bool, DirectUrl | None],
            candidate: WheelCandidate,
        ) -> tuple[int, InstallTransaction, WheelCandidate]:
            local_transaction = InstallTransaction()
            local_transaction.owner = candidate.canonical_name
            try:
                result = installer.install(
                    request[0],
                    candidate=candidate,
                    requested=request[1],
                    direct_url=request[2],
                    existing=None,
                    lookup_existing=False,
                    destination_cache=destination_cache,
                    transaction=local_transaction,
                    direct=True,
                )
            except Exception:
                local_transaction.rollback()
                raise
            return index, local_transaction, result

        futures = []
        staged_results: list[tuple[int, InstallTransaction, WheelCandidate]] = []
        try:
            if parallel:
                with ThreadPoolExecutor(
                    max_workers=min(INSTALL_WORKERS, len(requests))
                ) as pool:
                    futures = [
                        pool.submit(install_one, index, request, candidate)
                        for index, (request, candidate) in enumerate(
                            zip(requests, candidates),
                        )
                    ]
                    staged_results = [future.result() for future in futures]
                ordered_results = sorted(staged_results, key=lambda item: item[0])
                for _, local_transaction, _ in ordered_results:
                    transaction.adopt(local_transaction)
                transaction.finish_successfully()
                for _, local_transaction, _ in ordered_results:
                    local_transaction.finalize()
                return tuple(result for _, _, result in ordered_results)

            installed: list[WheelCandidate] = []
            for (path, requested, direct_url), candidate in zip(
                requests,
                candidates,
            ):
                transaction.owner = candidate.canonical_name
                installed.append(
                    installer.install(
                        path,
                        candidate=candidate,
                        requested=requested,
                        direct_url=direct_url,
                        existing=None,
                        lookup_existing=False,
                        destination_cache=destination_cache,
                        transaction=transaction,
                        direct=True,
                    )
                )
            results = tuple(installed)
            transaction.finish_successfully()
            return results
        except Exception:
            for future in futures:
                if not future.done() or future.cancelled():
                    continue
                try:
                    _, local_transaction, _ = future.result()
                except Exception:
                    continue
                local_transaction.rollback()
            transaction.rollback()
            raise
