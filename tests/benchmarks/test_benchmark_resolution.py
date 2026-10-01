"""Benchmarks for requirements parsing and offline dependency resolution.

The resolver runs entirely against a generated local wheelhouse with
``--no-index`` semantics, so the measurements stay deterministic and never
touch the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmark_support import cold_metadata_cache_dir, reset_caches
from kpip._vendor.nab_resolver import decide
from kpip._vendor.nab_resolver.ranges import Range
from kpip._vendor.nab_resolver.resolver import Resolver
from kpip._vendor.nab_resolver.types import Incompatibility, IncompatibilityCause
from kpip.core.errors import ResolutionError
from kpip.core.packaging import parse_requirement
from kpip.index.provider import CandidateProvider
from kpip.resolution.api import ResolutionEngine
from kpip.resolution.files.parser import parse_requirements
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider
from pytest_codspeed import BenchmarkFixture


def resolve(
    wheelhouse: Path,
    requirements: list[str],
    *,
    constraints: list[str] | None = None,
    ignore_installed: bool = True,
) -> int:
    reset_caches()
    resolver = ResolutionEngine(
        provider=CandidateProvider.from_options(
            find_links=[str(wheelhouse)],
            no_index=True,
            wheel_cache_dir=cold_metadata_cache_dir(),
        ),
        constraints=constraints,
        ignore_installed=ignore_installed,
    )
    return len(resolver.resolve(requirements).candidates)


def test_parse_requirements_file(
    benchmark: BenchmarkFixture,
    requirements_file: Path,
) -> None:
    def parse_file() -> int:
        reset_caches()
        return len(parse_requirements(str(requirements_file), session=None))

    assert benchmark(parse_file) > 0


def test_resolve_single_project(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
) -> None:
    def resolve_leaf() -> int:
        return resolve(graph_wheelhouse, ["leaf-0"])

    assert benchmark(resolve_leaf) == 1


def test_resolve_dependency_graph(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
) -> None:
    def resolve_application() -> int:
        return resolve(graph_wheelhouse, ["application"])

    assert benchmark(resolve_application) > 10


def test_resolve_pinned_dependency_graph(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
) -> None:
    requirements = [f"middle-{index}==2.2.0" for index in range(10)]

    def resolve_pinned() -> int:
        return resolve(graph_wheelhouse, requirements)

    assert benchmark(resolve_pinned) > 10


def test_resolve_with_backtracking(
    benchmark: BenchmarkFixture,
    backtracking_wheelhouse: Path,
) -> None:
    def resolve_conflicting() -> int:
        return resolve(backtracking_wheelhouse, ["conflicting"])

    assert benchmark(resolve_conflicting) > 0


def test_selected_dependency_lookahead(
    benchmark: BenchmarkFixture,
    selected_dependency_wheelhouse: Path,
) -> None:
    """Avoid replaying a wide graph for candidates blocked by a decision."""

    def resolve_selected_conflict() -> int:
        return resolve(selected_dependency_wheelhouse, ["selected-application"])

    assert benchmark(resolve_selected_conflict) == 99


def test_conflict_activity_avoids_wide_replay(
    benchmark: BenchmarkFixture,
    conflict_priority_wheelhouse: Path,
) -> None:
    """Choose the conflict package before replaying a wide stable suffix."""
    reset_caches()
    candidate_provider = CandidateProvider.from_options(
        find_links=[str(conflict_priority_wheelhouse)],
        no_index=True,
    )
    provider = NabProvider(
        candidate_provider,
        ResolutionConfig(
            find_links=(str(conflict_priority_wheelhouse),),
            ignore_installed=True,
        ),
    )
    resolver: Resolver[str, Any] = Resolver(provider)
    cause: Incompatibility[str, Any] = Incompatibility(
        [], IncompatibilityCause.DEPENDENCY
    )
    packages = [
        "conflict-priority-hot",
        *(f"conflict-priority-replay-{index}" for index in range(96)),
    ]
    for package in packages:
        provider.requirements[package] = parse_requirement(package)
        resolver.solution.derive(
            package,
            Range.full(),
            positive=True,
            cause=cause,
        )
        provider._versions(package)

    resolver.stats.package_conflict_counts["conflict-priority-hot"] += 1

    assert benchmark(decide.choose_package_to_decide, resolver) == (
        "conflict-priority-hot"
    )


def test_uv_wrong_package_backtracking_families(
    benchmark: BenchmarkFixture,
    wrong_package_wheelhouses: dict[str, Path],
) -> None:
    """Exercise the uv issue corpus' large wrong-package candidate shapes."""

    def resolve_cases() -> int:
        total = 0
        for name, wheelhouse in wrong_package_wheelhouses.items():
            total += resolve(wheelhouse, [f"{name}-root"])
        return total

    assert benchmark(resolve_cases) == 20


def test_transitive_boto3_urllib3_backtracking(
    benchmark: BenchmarkFixture,
    transitive_boto3_wheelhouse: Path,
) -> None:
    """Track the transitive Boto3 topology without changing an old benchmark."""

    def resolve_case() -> int:
        return resolve(
            transitive_boto3_wheelhouse,
            [
                "boto3-urllib3-root",
                "boto3-urllib3-shared==1.1.0",
                "boto3-urllib3-left",
            ],
        )

    assert benchmark(resolve_case) == 4


def test_top88_requirements_stress(
    benchmark: BenchmarkFixture,
    stress_wheelhouse: Path,
) -> None:
    requirements = [f"stress-{index}" for index in range(88)]

    def resolve_stress() -> int:
        return resolve(stress_wheelhouse, requirements)

    assert benchmark(resolve_stress) == 176


def test_large_constraint_file_lookup(
    benchmark: BenchmarkFixture,
    stress_wheelhouse: Path,
) -> None:
    requirements = [f"stress-{index}" for index in range(88)]
    constraints = [f"unrelated-{index}>=1" for index in range(1_000)]

    def resolve_constrained() -> int:
        return resolve(
            stress_wheelhouse,
            requirements,
            constraints=constraints,
        )

    assert benchmark(resolve_constrained) == 176


def test_resolution_installed_state_snapshot(
    benchmark: BenchmarkFixture,
    stress_wheelhouse: Path,
) -> None:
    requirements = [f"stress-{index}" for index in range(88)]

    def resolve_with_installed_state() -> int:
        return resolve(
            stress_wheelhouse,
            requirements,
            ignore_installed=False,
        )

    assert benchmark(resolve_with_installed_state) == 176


def test_candidate_scan_scaling(
    benchmark: BenchmarkFixture,
    candidate_scan_wheelhouse: Path,
) -> None:
    """Scan many releases while rejecting a Requires-Python-heavy tail."""

    def resolve_candidate_scan() -> int:
        return resolve(candidate_scan_wheelhouse, ["candidate-scan"])

    assert benchmark(resolve_candidate_scan) == 1


def test_resolvelib_backjump_pattern(
    benchmark: BenchmarkFixture,
    backjump_wheelhouse: Path,
) -> None:
    def resolve_conflict() -> int:
        reset_caches()
        try:
            resolve(
                backjump_wheelhouse,
                [
                    "python>=3.12",
                    "lz4==4.3.3",
                    "clickhouse-driver>=0.2.9",
                ],
            )
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError("backjump workload unexpectedly resolved")

    assert benchmark(resolve_conflict) > 0


def test_unsatisfiable_error_reporting(
    benchmark: BenchmarkFixture,
    unsatisfiable_wheelhouse: Path,
) -> None:
    requirements = ["unsatisfiable-root"]

    def resolve_and_format_error() -> int:
        reset_caches()
        try:
            resolve(unsatisfiable_wheelhouse, requirements)
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError("unsatisfiable workload unexpectedly resolved")

    assert benchmark(resolve_and_format_error) > 100


def test_constraints_and_pins(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
) -> None:
    requirements = [f"middle-{index}>=2.0.0" for index in range(10)]
    constraints = [f"middle-{index}==2.2.0" for index in range(10)]

    def resolve_constrained() -> int:
        reset_caches()
        resolver = ResolutionEngine(
            provider=CandidateProvider.from_options(
                find_links=[str(graph_wheelhouse)],
                no_index=True,
            ),
            ignore_installed=True,
            constraints=constraints,
        )
        return len(resolver.resolve(requirements).candidates)

    assert benchmark(resolve_constrained) > 10


def test_conflicting_direct_requirements(
    benchmark: BenchmarkFixture,
    graph_wheelhouse: Path,
) -> None:
    requirements = ["middle-0==2.1.0", "middle-0==2.2.0"]

    def resolve_conflicting_direct() -> int:
        reset_caches()
        try:
            resolve(graph_wheelhouse, requirements)
        except ResolutionError as error:
            return len(str(error))
        raise AssertionError("conflicting direct requirements unexpectedly resolved")

    assert benchmark(resolve_conflicting_direct) > 0
