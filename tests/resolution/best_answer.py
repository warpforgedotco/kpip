"""Check whether a resolution is the best answer its inputs admit.

Packages are ordered breadth-first from the roots: the named packages in the
order given, then the dependencies of the first in the order its metadata
declares them, then those of the second, and so on a level at a time, each
package taking the place of its first appearance.  Answer A beats answer B
when A holds the newer version at the first package in that order where the
two differ.  The best answer is the one no valid answer beats.

The check asks the resolver itself.  For each package in order it resolves
the same roots again with every earlier package constrained to the version
the answer gave it and this one constrained to something newer.  If that
resolves, the result is a counterexample: a valid answer that beats the one
under test.  Nothing here ships; it is a test tool, and a script for running
the same check against a lock made from an index::

    python -m tests.resolution.best_answer -r requirements.in --lock pylock.toml
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import signal
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from kpip.core import errors
from kpip.core.packaging import canonicalize_name, parse_requirement
from kpip.core.versions import Version
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.models import ResolutionResult

logger = logging.getLogger(__name__)

Resolve = Callable[[Sequence[str]], ResolutionResult]
"""Resolve the roots under test with the given extra constraints."""


class InvalidAnswer(Exception):
    """The answer handed to the checker is not a solution of its inputs."""


class QueryTimeout(Exception):
    """One re-resolve ran past the time allowed for it."""


@dataclass(frozen=True)
class Counterexample:
    """A valid answer that beats the one under test, and where."""

    package: str
    position: int
    version: str
    better: str
    witness: Mapping[str, str]


@dataclass
class Report:
    order: list[str]
    checked: list[str] = field(default_factory=list)
    counterexamples: list[Counterexample] = field(default_factory=list)
    # (package, why) for a question the checker could not settle.
    inconclusive: list[tuple[str, str]] = field(default_factory=list)
    # In the answer, but not reached from the roots at these versions.
    unreached: list[str] = field(default_factory=list)

    @property
    def best(self) -> bool:
        return not self.counterexamples


def pins_of(result: ResolutionResult) -> dict[str, str]:
    pins = {
        canonicalize_name(candidate.name): str(candidate.version)
        for candidate in result.candidates
    }
    for resolved in result.satisfied:
        distribution = resolved.distribution
        pins[canonicalize_name(distribution.name)] = str(distribution.version)
    return pins


def solution_order(roots: Sequence[str], result: ResolutionResult) -> list[str]:
    """The packages of ``result`` breadth-first from ``roots``.

    A release's dependencies are walked in the order its metadata declares
    them; the solution's own edges say which of them applied.
    """
    declared: dict[str, list[str]] = {}
    for candidate in result.candidates:
        package = canonicalize_name(candidate.name)
        children = result.graph.get(package, frozenset())
        ordered: list[str] = []
        for dependency in candidate.dependencies:
            name = canonicalize_name(dependency.name)
            if name in children and name not in ordered and name != package:
                ordered.append(name)
        # An edge whose declaration was not found by name (a URL dependency)
        # still belongs to the walk; name order keeps it deterministic.
        ordered.extend(sorted(children - set(ordered) - {package}))
        declared[package] = ordered

    order: list[str] = []
    seen: set[str] = set()
    for root in roots:
        package = root_name(root)
        if package in result.graph and package not in seen:
            seen.add(package)
            order.append(package)
    index = 0
    while index < len(order):
        for dependency in declared.get(order[index], ()):
            if dependency not in seen:
                seen.add(dependency)
                order.append(dependency)
        index += 1
    return order


def root_name(root: str) -> str:
    return parse_requirement(root).canonical_name


def newer_than(package: str, version: str) -> str:
    """A constraint admitting exactly the releases newer than ``version``.

    Not ``>version``: PEP 440 has that exclude the version's own
    post-releases, which are newer.
    """
    return f"{package}>={version},!={version}"


def check_best(
    resolve: Resolve,
    roots: Sequence[str],
    pins: Mapping[str, str],
    *,
    limit: int | None = None,
    time_limit: float | None = None,
    query_timeout: float | None = None,
) -> Report:
    """Look for a valid answer that beats ``pins``.

    ``limit`` bounds how many packages, in order, are asked about;
    ``time_limit`` stops asking once that many seconds have gone by, though
    never before every root has been asked about; ``query_timeout`` abandons
    a single re-resolve (the main thread of a POSIX process only).
    """
    pins = {canonicalize_name(name): str(version) for name, version in pins.items()}
    try:
        baseline = resolve([f"{name}=={version}" for name, version in pins.items()])
    except errors.ResolutionError as error:
        raise InvalidAnswer(f"the answer does not resolve: {error}") from error

    base_pins = pins_of(baseline)
    wrong = {
        name: (pins.get(name), version)
        for name, version in base_pins.items()
        if pins.get(name) != version
    }
    if wrong:
        raise InvalidAnswer(f"the answer is missing or contradicts {wrong}")

    order = solution_order(roots, baseline)
    report = Report(order=order, unreached=sorted(set(pins) - set(order)))
    root_count = len({root_name(root) for root in roots} & set(order))
    started = time.monotonic()

    for position, package in enumerate(order):
        if limit is not None and position >= limit:
            break
        if (
            time_limit is not None
            and position >= root_count
            and time.monotonic() - started > time_limit
        ):
            break
        version = pins[package]
        constraints = [f"{earlier}=={pins[earlier]}" for earlier in order[:position]]
        constraints.append(newer_than(package, version))
        report.checked.append(package)
        try:
            with _deadline(query_timeout):
                witness = pins_of(resolve(constraints))
        except errors.ResolutionError:
            continue
        except QueryTimeout:
            report.inconclusive.append((package, "timed out"))
            continue

        better = witness.get(package)
        moved = [
            earlier
            for earlier in order[:position]
            if witness.get(earlier) != pins[earlier]
        ]
        if moved:
            report.inconclusive.append(
                (package, f"the witness moved earlier packages {moved}")
            )
        elif better is None:
            report.inconclusive.append((package, "the witness does not include it"))
        elif Version(better) <= Version(version):
            report.inconclusive.append((package, f"the witness holds {better}"))
        else:
            report.counterexamples.append(
                Counterexample(package, position, version, better, witness)
            )
    return report


@contextlib.contextmanager
def _deadline(seconds: float | None) -> Iterator[None]:
    if seconds is None:
        yield
        return

    def expire(signum, frame):
        raise QueryTimeout

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def build_wheelhouse(
    wheelhouse: Path, wheels: Mapping[str, Mapping[str, Sequence[str]]]
) -> str:
    """Write ``{project: {version: requirements}}`` as wheels; return the path."""
    from tests.wheel_helpers import make_wheel

    for project, releases in wheels.items():
        for version, requires in releases.items():
            make_wheel(
                wheelhouse,
                project,
                project.replace("-", "_"),
                version,
                requires=list(requires),
            )
    return str(wheelhouse)


def wheelhouse_resolver(wheelhouse: str, roots: Sequence[str]) -> Resolve:
    """Resolve ``roots`` from a directory of wheels, with no index."""

    def resolve(constraints: Sequence[str]) -> ResolutionResult:
        result = ResolutionEngine.resolve_wheelhouse(
            [wheelhouse], list(roots), constraints=list(constraints)
        )
        assert result is not None
        return result

    return resolve


def index_resolver(roots: Sequence[str], cache_dir: str | None) -> Resolve:
    """Resolve ``roots`` from the index, the way ``kpip lock`` does."""
    from kpip.core.appdirs import command_cache_dir
    from kpip.index.catalog_cache import serve_summaries_from_snapshot
    from kpip.index.provider import CandidateProvider
    from kpip.network.deferred import DeferredNetworkSession
    from kpip.resolution.input_requirements import install_req_from_line

    cache = command_cache_dir(cache_dir, False)
    session = DeferredNetworkSession(cache_dir=cache)
    serve_summaries_from_snapshot(session.page_cache())

    def resolve(constraints: Sequence[str]) -> ResolutionResult:
        engine = ResolutionEngine(
            provider=CandidateProvider.from_options(
                find_links=[],
                no_index=False,
                format_control=None,
                build_isolation=True,
                wheel_cache_dir=cache,
                session=session,
                dry_run=True,
            ),
            no_deps=False,
            ignore_installed=True,
            constraints=list(constraints),
        )
        try:
            return engine.resolve([install_req_from_line(root) for root in roots])
        finally:
            engine.close()

    return resolve


def _lock_pins(path: str) -> dict[str, str]:
    import tomllib

    with open(path, "rb") as stream:
        lock = tomllib.load(stream)
    return {
        canonicalize_name(package["name"]): str(package["version"])
        for package in lock.get("packages", ())
        if "version" in package
    }


def main(argv: Sequence[str] | None = None) -> int:
    from kpip.cli.lock import applies_to_target, read_requirement_lines

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("-r", "--requirement", required=True)
    parser.add_argument("--lock", required=True, help="the pylock.toml to check")
    parser.add_argument("--cache-dir")
    parser.add_argument("--limit", type=int, help="packages to ask about, in order")
    parser.add_argument("--time-limit", type=float, help="seconds, past the roots")
    parser.add_argument("--query-timeout", type=float, help="seconds per re-resolve")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args(argv)

    logging.basicConfig(level=logging.ERROR)
    roots = [
        line
        for line in read_requirement_lines(options.requirement)
        if applies_to_target(line)
    ]
    report = check_best(
        index_resolver(roots, options.cache_dir),
        roots,
        _lock_pins(options.lock),
        limit=options.limit,
        time_limit=options.time_limit,
        query_timeout=options.query_timeout,
    )
    if options.json:
        json.dump(
            {
                "packages": len(report.order),
                "checked": report.checked,
                "counterexamples": [
                    {
                        "package": found.package,
                        "position": found.position,
                        "version": found.version,
                        "better": found.better,
                        "witness": dict(found.witness),
                    }
                    for found in report.counterexamples
                ],
                "inconclusive": report.inconclusive,
                "unreached": report.unreached,
            },
            sys.stdout,
            indent=1,
        )
        print()
    else:
        print(f"{len(report.checked)} of {len(report.order)} packages asked about")
        for found in report.counterexamples:
            print(
                f"not best: {found.package} {found.version} at position "
                f"{found.position} could be {found.better}"
            )
        for package, why in report.inconclusive:
            print(f"inconclusive: {package}: {why}")
        if report.unreached:
            print(f"in the lock but not reached: {', '.join(report.unreached)}")
    return 1 if report.counterexamples else 0


if __name__ == "__main__":
    raise SystemExit(main())
