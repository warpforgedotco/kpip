"""nab-resolver searches for the answer as resolvelib does, around the same
pip: each test resolves one index with both and compares."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from kpip._internal.cli.req_command import RequirementCommand
from kpip._internal.commands.install import InstallCommand
from kpip._internal.exceptions import DistributionNotFound, ResolutionTooDeepError
from kpip._internal.index.collector import LinkCollector
from kpip._internal.index.package_finder import PackageFinder
from kpip._internal.models.search_scope import SearchScope
from kpip._internal.models.selection_prefs import SelectionPreferences
from kpip._internal.network.session import PipSession
from kpip._internal.operations.build.build_tracker import get_build_tracker
from kpip._internal.req.constructors import install_req_from_line
from kpip._internal.resolution.resolvelib.resolver import Resolver
from kpip._internal.utils.temp_dir import TempDirectory, global_tempdir_manager

from tests.lib.wheel import make_wheel

SOLVERS = ("resolvelib", "nab")


def _index(path: Path, packages: dict[str, list[str]]) -> Path:
    """Wheels for ``packages``: "name==version" -> its requirements."""
    path.mkdir()
    for release, requires in packages.items():
        name, _, version = release.partition("==")
        extras = sorted(
            {
                requirement.split('extra == "')[1].split('"')[0]
                for requirement in requires
                if 'extra == "' in requirement
            }
        )
        make_wheel(
            name,
            version,
            metadata_updates={"Requires-Dist": requires, "Provides-Extra": extras},
        ).save_to_dir(path)
    return path


@pytest.fixture
def resolve(tmp_path: Path) -> Iterator[object]:
    """Resolve requirements against an index with one solver; returns the
    picked releases as {name: version}."""

    def resolve(
        packages: dict[str, list[str]], requirements: list[str], solver: str
    ) -> dict[str, str]:
        index = _index(
            tmp_path / f"index-{solver}-{len(list(tmp_path.iterdir()))}", packages
        )
        session = PipSession()
        finder = PackageFinder.create(
            LinkCollector(session, SearchScope([str(index)], [], False)),
            SelectionPreferences(allow_yanked=False),
        )
        options, _ = InstallCommand("x", "y").parse_args(["--no-build-isolation"])
        with global_tempdir_manager(), TempDirectory() as tmp:
            with get_build_tracker() as tracker:
                preparer = RequirementCommand.make_requirement_preparer(
                    tmp,
                    options=options,
                    build_tracker=tracker,
                    session=session,
                    finder=finder,
                    use_user_site=False,
                    verbosity=0,
                    allow_editables=True,
                )
                resolver = Resolver(
                    preparer=preparer,
                    finder=finder,
                    wheel_cache=None,
                    make_install_req=install_req_from_line,
                    use_user_site=False,
                    ignore_dependencies=False,
                    only_dependencies=False,
                    ignore_installed=True,
                    ignore_requires_python=False,
                    force_reinstall=False,
                    upgrade_strategy="to-satisfy-only",
                    solver=solver,
                )
                requirement_set = resolver.resolve(
                    [install_req_from_line(r) for r in requirements],
                    check_supported_wheels=True,
                )
                return {
                    name: ireq.metadata["Version"]
                    for name, ireq in requirement_set.requirements.items()
                }

    yield resolve


def both(resolve: object, packages: dict[str, list[str]], requirements: list[str]):
    answers = {
        solver: resolve(packages, requirements, solver)  # type: ignore[operator]
        for solver in SOLVERS
    }
    assert answers["nab"] == answers["resolvelib"]
    return answers["nab"]


def test_newest_versions_and_their_dependencies(resolve: object) -> None:
    packages = {
        "app==1.0": ["lib>=1"],
        "app==2.0": ["lib>=2"],
        "lib==1.0": [],
        "lib==2.0": ["leaf"],
        "lib==3.0": ["leaf<2"],
        "leaf==1.0": [],
        "leaf==2.0": [],
    }
    assert both(resolve, packages, ["app"]) == {
        "app": "2.0",
        "lib": "3.0",
        "leaf": "1.0",
    }


def test_backtracking_to_an_older_release(resolve: object) -> None:
    # app 2.0 needs a lib that conflicts with the requested other.
    packages = {
        "app==1.0": ["lib<2"],
        "app==2.0": ["lib>=2"],
        "lib==1.0": [],
        "lib==2.0": ["other>=2"],
        "other==1.0": [],
        "other==2.0": [],
    }
    assert both(resolve, packages, ["app", "other<2"]) == {
        "app": "1.0",
        "lib": "1.0",
        "other": "1.0",
    }


def test_extras(resolve: object) -> None:
    packages = {
        "app==1.0": ["lib[fast]"],
        "lib==1.0": ['speedup; extra == "fast"'],
        "speedup==1.0": [],
    }
    assert both(resolve, packages, ["app"]) == {
        "app": "1.0",
        "lib": "1.0",
        "speedup": "1.0",
    }


def test_markers_are_the_targets(resolve: object) -> None:
    packages = {
        "app==1.0": ['never; python_version < "3"', "always"],
        "never==1.0": [],
        "always==1.0": [],
    }
    assert both(resolve, packages, ["app"]) == {"app": "1.0", "always": "1.0"}


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_conflict_is_reported_as_pip_reports_it(
    resolve: object, solver: str, caplog: pytest.LogCaptureFixture
) -> None:
    packages = {
        "app==1.0": ["lib==1.0"],
        "other==1.0": ["lib==2.0"],
        "lib==1.0": [],
        "lib==2.0": [],
    }
    caplog.set_level(logging.INFO)
    with pytest.raises(DistributionNotFound, match="ResolutionImpossible"):
        resolve(packages, ["app", "other"], solver)  # type: ignore[operator]
    report = caplog.text
    assert "The conflict is caused by:" in report
    assert "app 1.0 depends on lib==1.0" in report
    assert "other 1.0 depends on lib==2.0" in report


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_missing_version_is_reported_as_pip_reports_it(
    resolve: object, solver: str
) -> None:
    packages = {"lib==1.0": []}
    with pytest.raises(
        DistributionNotFound, match="No matching distribution found for lib>=2"
    ):
        resolve(packages, ["lib>=2"], solver)  # type: ignore[operator]


def test_too_deep_is_reported_as_pip_reports_it(
    resolve: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip._internal.resolution.resolvelib import nab

    original = nab.NabSolver

    def shallow(*args: object, **kwargs: object) -> object:
        kwargs["max_iterations"] = 1
        return original(*args, **kwargs)

    monkeypatch.setattr(nab, "NabSolver", shallow)
    packages = {"app==1.0": ["lib"], "lib==1.0": []}
    with pytest.raises(ResolutionTooDeepError):
        resolve(packages, ["app"], "nab")  # type: ignore[operator]
