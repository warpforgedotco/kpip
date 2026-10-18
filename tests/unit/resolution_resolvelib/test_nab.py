"""nab-resolver searches for the answer as resolvelib does, around the same
pip: each test resolves one index with both and compares."""

from __future__ import annotations

import html
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kpip._internal.build_env.installer import SubprocessBuildEnvironmentInstaller
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

from tests.lib import make_test_finder
from tests.lib.wheel import make_wheel

SOLVERS = ("resolvelib", "nab")


def _wheel(name: str, version: str, requires: list[str]) -> Any:
    extras = sorted(
        {
            requirement.split('extra == "')[1].split('"')[0]
            for requirement in requires
            if 'extra == "' in requirement
        }
    )
    # Written out rather than as metadata_updates, which wraps a long line --
    # a URL requirement's -- into one pip cannot read.
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [
        r.replace("Requires-Python:", "Requires-Python: ")
        for r in requires
        if r.startswith("Requires-Python:")
    ]
    requires = [r for r in requires if not r.startswith("Requires-Python:")]
    lines += [f"Provides-Extra: {extra}" for extra in extras]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    return make_wheel(name, version, metadata="\n".join(lines) + "\n")


def _index(path: Path, packages: dict[str, list[str]], yanked: set[str]) -> str:
    """A PEP 503 index of wheels for ``packages`` ("name==version" -> its
    requirements), the releases in ``yanked`` marked yanked."""
    files = path / "files"
    files.mkdir(parents=True)
    pages: dict[str, list[str]] = {}
    for release, requires in packages.items():
        name, _, version = release.partition("==")
        filename = Path(_wheel(name, version, requires).save_to_dir(files)).name
        mark = ' data-yanked=""' if release in yanked else ""
        pages.setdefault(name, []).append(
            f'<a href="../../files/{html.escape(filename)}"{mark}>'
            f"{html.escape(filename)}</a><br/>"
        )
    for name, links in pages.items():
        page = path / "simple" / name
        page.mkdir(parents=True)
        (page / "index.html").write_text(f"<html><body>{''.join(links)}</body></html>")
    return (path / "simple").as_uri()


@pytest.fixture
def direct(tmp_path: Path) -> object:
    """A wheel outside the index, for a requirement to name by URL."""

    def direct(name: str, version: str, requires: list[str] = []) -> str:
        directory = tmp_path / "direct"
        directory.mkdir(exist_ok=True)
        return Path(_wheel(name, version, requires).save_to_dir(directory)).as_uri()

    return direct


def _describe(candidate: Any) -> str:
    """A picked candidate's version, and where it is from when not the index."""
    version = str(candidate.version)
    if candidate.is_installed:
        return f"{version} (installed)"
    link = candidate.source_link
    if link is not None and "direct" in Path(link.file_path).parts:
        return f"{version} (direct)"
    return version


@pytest.fixture
def resolve(tmp_path: Path) -> Iterator[object]:
    """Resolve requirements with one solver, against wheels for ``packages``
    (on an index page when some are ``yanked``) or a find-links directory
    ``find_links``; returns the picked releases as {name: version}."""

    def resolve(
        packages: dict[str, list[str]],
        requirements: list[str],
        solver: str,
        *,
        yanked: set[str] = set(),
        find_links: Path | None = None,
        installed: dict[str, list[str]] | None = None,
        upgrade_strategy: str = "to-satisfy-only",
    ) -> dict[str, str]:
        where = tmp_path / f"index-{solver}-{len(list(tmp_path.iterdir()))}"
        if yanked:
            # Only an index page can say a file is yanked.
            scope = SearchScope([], [_index(where, packages, yanked)], False)
        else:
            if find_links is None:
                find_links = where
                find_links.mkdir()
                for release, requires in packages.items():
                    name, _, version = release.partition("==")
                    _wheel(name, version, requires).save_to_dir(find_links)
            scope = SearchScope([str(find_links)], [], False)
        session = PipSession()
        finder = PackageFinder.create(
            LinkCollector(session, scope),
            SelectionPreferences(allow_yanked=True),
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
                    upgrade_strategy=upgrade_strategy,
                    solver=solver,
                )
                # What is installed, without installing it anywhere.
                resolver.factory._installed_dists = {
                    name: _wheel(name, version, requires).as_distribution(name)
                    for name, _, version, requires in (
                        (*release.partition("=="), requires)
                        for release, requires in (installed or {}).items()
                    )
                }
                ireqs = [install_req_from_line(r) for r in requirements]
                for ireq in ireqs:
                    ireq.user_supplied = True
                resolver.resolve(ireqs, check_supported_wheels=True)
                return {
                    identifier: _describe(candidate)
                    for identifier, candidate in resolver._result.mapping.items()
                    if not identifier.startswith("<") and "[" not in identifier
                }

    yield resolve


def both(
    resolve: object,
    packages: dict[str, list[str]],
    requirements: list[str],
    **options: Any,
) -> Any:
    answers = {}
    for solver in SOLVERS:
        try:
            answers[solver] = resolve(  # type: ignore[operator]
                packages, requirements, solver, **options
            )
        except DistributionNotFound as error:
            answers[solver] = f"{type(error).__name__}: {error}"
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


def test_an_extra_leaves_its_base_free_to_backtrack(resolve: object) -> None:
    # Having tried x 2.0 for x[fast], nab must still be able to choose x 1.0.
    packages = {
        "web==1.0": ["x[fast]"],
        "app==1.0": ["x<2"],
        "x==1.0": ['speedup; extra == "fast"'],
        "x==2.0": ['speedup; extra == "fast"'],
        "speedup==1.0": [],
    }
    assert both(resolve, packages, ["web", "app"]) == {
        "web": "1.0",
        "app": "1.0",
        "x": "1.0",
        "speedup": "1.0",
    }


def test_an_extra_backtracks_past_an_unmet_newest(resolve: object) -> None:
    packages = {
        "x==1.0": ['speedup; extra == "fast"'],
        "x==2.0": ['speedup>=3; extra == "fast"'],
        "speedup==1.0": [],
    }
    assert both(resolve, packages, ["x[fast]"]) == {"x": "1.0", "speedup": "1.0"}


def test_a_url_named_only_by_a_rejected_version_is_not_used(
    resolve: object, direct: Any
) -> None:
    packages = {
        "app==2.0": [f"lib @ {direct('lib', '1.0')}", "other>=2"],
        "app==1.0": ["lib"],
        "other==2.0": ["missing"],
        "lib==2.0": [],
    }
    assert both(resolve, packages, ["lib", "app"]) == {"lib": "2.0", "app": "1.0"}


def test_a_url_a_dependency_names_is_used(resolve: object, direct: Any) -> None:
    packages = {"app==1.0": [f"lib @ {direct('lib', '1.0')}"], "lib==2.0": []}
    assert both(resolve, packages, ["lib", "app"]) == {
        "lib": "1.0 (direct)",
        "app": "1.0",
    }


def test_many_urls_learnt_one_at_a_time(resolve: object, direct: Any) -> None:
    # Each URL restarts the resolution; however many there are, it ends.
    count = 25
    packages: dict[str, list[str]] = {
        "b==1.0": [f"a{i} @ {direct(f'a{i}', '1.0')}" for i in range(count)],
    }
    packages.update({f"a{i}==1.0": [] for i in range(count)})
    requirements = [f"a{i}" for i in range(count)] + ["b"]
    answer = resolve(packages, requirements, "nab")  # type: ignore[operator]
    assert answer == {"b": "1.0", **{f"a{i}": "1.0 (direct)" for i in range(count)}}


def test_a_prerelease_a_dependency_names(resolve: object) -> None:
    packages = {
        "numpy==1.26": [],
        "numpy==2.0rc1": [],
        "lib==1.0": ["numpy>=2.0rc1"],
    }
    assert both(resolve, packages, ["numpy", "lib"]) == {
        "numpy": "2.0rc1",
        "lib": "1.0",
    }


def test_a_prerelease_one_of_two_dependents_names(resolve: object) -> None:
    packages = {
        "lib==1.0": [],
        "lib==2.0b1": [],
        "a==1.0": ["lib"],
        "b==1.0": ["lib>=2.0b1"],
    }
    assert both(resolve, packages, ["a", "b"]) == {
        "a": "1.0",
        "b": "1.0",
        "lib": "2.0b1",
    }


def test_no_prerelease_unless_named(resolve: object) -> None:
    packages = {"lib==1.0": [], "lib==2.0b1": [], "app==1.0": ["lib"]}
    assert both(resolve, packages, ["app"]) == {"app": "1.0", "lib": "1.0"}


def test_a_yanked_release_a_dependency_pins(resolve: object) -> None:
    packages = {
        "pkg==1.0": [],
        "pkg==2.0": [],
        "lib1==1.0": ["pkg>=1"],
        "lib2==1.0": ["pkg==2.0"],
    }
    assert both(resolve, packages, ["lib1", "lib2"], yanked={"pkg==2.0"}) == {
        "lib1": "1.0",
        "lib2": "1.0",
        "pkg": "2.0",
    }


def test_no_yanked_release_unless_pinned(resolve: object) -> None:
    packages = {"pkg==1.0": [], "pkg==2.0": [], "lib==1.0": ["pkg>=1"]}
    assert both(resolve, packages, ["lib"], yanked={"pkg==2.0"}) == {
        "lib": "1.0",
        "pkg": "1.0",
    }


def test_an_installed_version_is_preferred_not_required(resolve: object) -> None:
    # Upgrading app needs a newer lib than the installed one.
    packages = {
        "app==1.0": ["lib"],
        "app==2.0": ["lib", "other>=2"],
        "other==2.0": ["lib>=2"],
        "lib==1.0": [],
        "lib==2.0": [],
    }
    assert both(
        resolve,
        packages,
        ["app"],
        installed={"app==1.0": ["lib"], "lib==1.0": []},
        upgrade_strategy="only-if-needed",
    ) == {"app": "2.0", "other": "2.0", "lib": "2.0"}


def test_an_installed_version_is_kept(resolve: object) -> None:
    packages = {"app==1.0": ["lib"], "lib==1.0": [], "lib==2.0": []}
    assert both(
        resolve,
        packages,
        ["app"],
        installed={"lib==1.0": []},
        upgrade_strategy="only-if-needed",
    ) == {"app": "1.0", "lib": "1.0 (installed)"}


def test_an_upgrade_falls_back_to_the_index_copy_of_the_installed_version(
    resolve: object,
) -> None:
    # The installed foo 2.0's own metadata cannot be met; the index's can.
    packages = {"foo==1.0": [], "foo==2.0": ["bar"], "bar==1.0": []}
    assert both(
        resolve,
        packages,
        ["foo", "bar>=1"],
        installed={"foo==2.0": ["bar<1"], "bar==0.5": []},
        upgrade_strategy="only-if-needed",
    ) == {"foo": "2.0", "bar": "1.0"}


def test_a_version_falls_back_to_its_next_file(resolve: object, tmp_path: Path) -> None:
    # The preferred 2.0 wheel (build tag 1) says it is 2.0.1; pip builds the
    # other 2.0 wheel instead.
    links = tmp_path / "links"
    links.mkdir()
    for filename, version in (
        ("lib-1.0-py2.py3-none-any.whl", "1.0"),
        ("lib-2.0-py2.py3-none-any.whl", "2.0"),
        ("lib-2.0-1-py2.py3-none-any.whl", "2.0.1"),
    ):
        metadata = f"Metadata-Version: 2.1\nName: lib\nVersion: {version}\n"
        make_wheel("lib", filename.split("-")[1], metadata=metadata).save_to(
            links / filename
        )
    assert both(resolve, {}, ["lib"], find_links=links) == {"lib": "2.0"}


def test_preference_reads_only_the_requirements_in_play(resolve: object) -> None:
    # app 2.0, rejected, pins lib==1.0: that must not make lib "pinned" for
    # the rest of the resolution.
    packages = {
        "app==2.0": ["lib==1.0", "other<1"],
        "app==1.0": ["lib"],
        "lib==1.0": [],
        "lib==0.9": [],
        "b==2.0": ["lib<1"],
        "b==1.0": [],
        "other==0.5": [],
        "other==1.0": [],
    }
    assert both(resolve, packages, ["app", "b", "other>=1"]) == {
        "app": "1.0",
        "b": "2.0",
        "lib": "0.9",
        "other": "1.0",
    }


def test_a_requirement_nothing_meets_is_not_met_by_the_users(
    resolve: object, tmp_path: Path
) -> None:
    # app 2.0's lib URL says it is libx: pip cannot use it, so app 1.0 is
    # chosen. (resolvelib fails on this with InconsistentCandidate.)
    links = tmp_path / "links"
    links.mkdir()
    make_wheel(
        "lib", "2.0", metadata="Metadata-Version: 2.1\nName: libx\nVersion: 2.0\n"
    ).save_to(tmp_path / "lib-2.0-py2.py3-none-any.whl")
    url = (tmp_path / "lib-2.0-py2.py3-none-any.whl").as_uri()
    for release, requires in {
        "lib==1.0": [],
        "lib==2.0": [],
        "app==2.0": [f"lib @ {url}"],
        "app==1.0": ["lib"],
    }.items():
        name, _, version = release.partition("==")
        _wheel(name, version, requires).save_to_dir(links)
    answer = resolve({}, ["lib", "app"], "nab", find_links=links)  # type: ignore[operator]
    assert answer == {"lib": "2.0", "app": "1.0"}


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_failure_names_the_requirement_no_version_meets(
    resolve: object, solver: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Each version of a pins a different b, and every b needs a c>=2 that is
    # not there: c is the problem, not a's two pins.
    packages = {
        "a==2.0": ["b==2.0"],
        "a==1.0": ["b==1.0"],
        "b==1.0": ["c>=2"],
        "b==2.0": ["c>=2"],
        "c==1.0": [],
    }
    caplog.set_level(logging.INFO)
    with pytest.raises(DistributionNotFound) as failure:
        resolve(packages, ["a"], solver)  # type: ignore[operator]
    report = f"{failure.value}\n{caplog.text}"
    assert "c>=2" in report
    assert "depends on b==" not in report


def test_the_iteration_budget_is_scaled_to_nabs_iterations(
    resolve: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kpip._internal.resolution.resolvelib import nab

    budgets = []
    original = nab.NabSolver

    def record(*args: Any, **kwargs: Any) -> Any:
        budgets.append(kwargs["max_iterations"])
        return original(*args, **kwargs)

    monkeypatch.setattr(nab, "NabSolver", record)
    resolve({"app==1.0": []}, ["app"], "nab")  # type: ignore[operator]
    assert budgets == [200000 * nab._ITERATIONS_PER_ROUND]


def test_resolver_option() -> None:
    options, _ = InstallCommand("x", "y").parse_args([])
    assert options.resolver == "resolvelib"
    options, _ = InstallCommand("x", "y").parse_args(["--resolver=nab"])
    assert options.resolver == "nab"
    # Not a --use-feature value: pip, sharing the configuration, rejects one
    # it does not know.
    with pytest.raises(SystemExit):
        InstallCommand("x", "y").parse_args(["--use-feature=nab-resolver"])


@pytest.mark.parametrize("solver", SOLVERS)
def test_build_dependencies_resolve_with_the_same_resolver(solver: str) -> None:
    installer = SubprocessBuildEnvironmentInstaller(make_test_finder(), solver=solver)
    prefix = mock.Mock(path="/prefix", venv_executable=None)
    with mock.patch("kpip._internal.build_env.installer.call_subprocess") as call:
        installer.install(["setuptools"], prefix, kind="build", for_req=None)
    args = call.call_args.args[0]
    assert args[args.index("--resolver") + 1] == solver


def test_a_url_that_is_gone_on_a_candidate_never_chosen(
    resolve: object, tmp_path: Path
) -> None:
    gone = (tmp_path / "gone" / "lib-1.0-py3-none-any.whl").as_uri()
    packages = {
        "base==1.0": ["other<2"],
        "zapp==2.0": ["other>=2", f"lib @ {gone}"],
        "zapp==1.0": [],
        "other==1.0": [],
        "other==2.0": [],
    }
    assert both(resolve, packages, ["base", "zapp"]) == {
        "base": "1.0",
        "other": "1.0",
        "zapp": "1.0",
    }


def test_a_url_that_is_gone_is_reported_when_nothing_else_resolves(
    resolve: object, tmp_path: Path
) -> None:
    gone = (tmp_path / "gone" / "lib-1.0-py3-none-any.whl").as_uri()
    packages = {"zapp==1.0": [f"lib @ {gone}"]}
    with pytest.raises(FileNotFoundError):
        resolve(packages, ["zapp"], "nab")  # type: ignore[operator]


def test_requires_python(resolve: object) -> None:
    # "Requires-Python:..." in the list is the release's Requires-Python.
    packages = {
        "app==1.0": ["Requires-Python:>=3", "lib"],
        "app==2.0": ["Requires-Python:<3", "lib"],
        "lib==1.0": ["Requires-Python:>=3"],
    }
    assert both(resolve, packages, ["app"]) == {"app": "1.0", "lib": "1.0"}


@pytest.mark.parametrize("solver", SOLVERS)
def test_no_prerelease_for_a_requirement_left_behind(
    resolve: object, solver: str
) -> None:
    # nab could reach p3 while only a parent version it later left admits
    # p3's pre-release: what it installs must be what pip offers beside the
    # answer's own requirements -- here, nothing, as for resolvelib.
    packages = {
        "p0==2.0": ["p1"],
        "p0==3.0": ["p1<=5.0", "p2!=1.0", "p3>=1.0", "p4", "p5!=1.0"],
        "p0==5.0b1": ["p4>2.0"],
        "p1==1.0": ["p3", "p4<=1.0", "p5==4.0"],
        "p1==2.0b1": ["p2", "p3", "p4<=1.0", "p5<2.0"],
        "p1==5.0": ["p2>2.0b1", "p3>=4.0b1", "p4", "p5<=6.0"],
        "p2==3.0b1": ["p4"],
        "p2==4.0": ["p3", "p4", "p5!=4.0b1"],
        "p2==5.0b1": ["p3==1.0b1", "p5>=2.0"],
        "p3==1.0b1": [],
        "p3==2.0b1": ["p4>6.0", "p5==4.0"],
        "p3==4.0": ["p4", "p5==3.0b1"],
        "p4==1.0": ["p5"],
        "p4==3.0": ["p5"],
        "p4==5.0": [],
        "p5==1.0b1": [],
        "p5==4.0": [],
        "p5==5.0b1": [],
    }
    with pytest.raises(DistributionNotFound):
        resolve(packages, ["p0"], solver)  # type: ignore[operator]
