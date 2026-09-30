"""An extra asked for only by a release the solve rejected is not in the lock.

The provider merges every extra a release asks of a dependency into that
dependency's requirement, and the merge outlives the backtrack that undoes
the release. Locking airflow on Python 3.15 walked back through provider
releases asking for ``google-cloud-aiplatform[evaluation]`` and
``apache-airflow-providers-common-sql[polars]`` to ones that ask for neither,
and the lock still carried litellm, scikit-learn, polars and nine more
packages nothing in it required.
"""

from __future__ import annotations

from pathlib import Path

from kpip.resolution.api import ResolutionEngine
from tests.wheel_helpers import make_wheel


def _rejected_after_asking_for_the_extra(wheelhouse: Path) -> None:
    """``app`` 2.0 asks for ``lib[fast]`` and cannot be installed.

    Its conflict is two steps away, past the forward check, so the solve
    decides on 2.0 and reads its dependencies before backing out to 1.0.
    """
    make_wheel(wheelhouse, "app", "app", "2.0", requires=["lib[fast]", "broken"])
    make_wheel(wheelhouse, "broken", "broken", "1.0", requires=["missing>=2"])
    make_wheel(wheelhouse, "missing", "missing", "1.0")
    make_wheel(wheelhouse, "speedup", "speedup", "1.0", requires=["speedup-core"])
    make_wheel(wheelhouse, "speedup-core", "speedup_core", "1.0")


def _resolve(wheelhouse: Path, *requirements: str) -> dict[str, frozenset[str]]:
    result = ResolutionEngine.resolve_wheelhouse([str(wheelhouse)], list(requirements))
    assert result is not None
    assert {candidate.canonical_name for candidate in result.candidates} == set(
        result.graph
    )
    return dict(result.graph)


def test_an_extra_only_a_rejected_release_asked_for_is_dropped(
    tmp_path: Path,
) -> None:
    _rejected_after_asking_for_the_extra(tmp_path)
    make_wheel(tmp_path, "app", "app", "1.0", requires=["lib"])
    make_wheel(tmp_path, "lib", "lib", "1.0", requires=['speedup; extra == "fast"'])

    assert _resolve(tmp_path, "app") == {
        "app": frozenset({"lib"}),
        "lib": frozenset(),
    }


def test_an_extra_the_chosen_release_asks_for_is_kept(tmp_path: Path) -> None:
    _rejected_after_asking_for_the_extra(tmp_path)
    make_wheel(tmp_path, "app", "app", "1.0", requires=["lib[fast]"])
    make_wheel(tmp_path, "lib", "lib", "1.0", requires=['speedup; extra == "fast"'])

    assert _resolve(tmp_path, "app") == {
        "app": frozenset({"lib"}),
        "lib": frozenset({"speedup"}),
        "speedup": frozenset({"speedup-core"}),
        "speedup-core": frozenset(),
    }


def test_an_extra_a_root_asks_for_is_kept(tmp_path: Path) -> None:
    _rejected_after_asking_for_the_extra(tmp_path)
    make_wheel(tmp_path, "app", "app", "1.0", requires=["lib"])
    make_wheel(tmp_path, "lib", "lib", "1.0", requires=['speedup; extra == "fast"'])

    assert _resolve(tmp_path, "app", "lib[fast]")["lib"] == frozenset({"speedup"})


def test_a_package_something_else_requires_survives_the_dropped_extra(
    tmp_path: Path,
) -> None:
    _rejected_after_asking_for_the_extra(tmp_path)
    make_wheel(tmp_path, "app", "app", "1.0", requires=["lib", "speedup-core"])
    make_wheel(tmp_path, "lib", "lib", "1.0", requires=['speedup; extra == "fast"'])

    assert _resolve(tmp_path, "app") == {
        "app": frozenset({"lib", "speedup-core"}),
        "lib": frozenset(),
        "speedup-core": frozenset(),
    }


def test_an_extra_spelled_through_another_extra_is_kept(tmp_path: Path) -> None:
    _rejected_after_asking_for_the_extra(tmp_path)
    make_wheel(tmp_path, "app", "app", "1.0", requires=["lib[all]"])
    make_wheel(
        tmp_path,
        "lib",
        "lib",
        "1.0",
        requires=[
            'lib[fast]; extra == "all"',
            'speedup; extra == "fast"',
            'missing; extra == "docs"',
        ],
    )

    graph = _resolve(tmp_path, "app")

    assert "speedup" in graph["lib"]
    assert "missing" not in graph


def test_an_extra_asked_for_through_a_dropped_extra_is_dropped(
    tmp_path: Path,
) -> None:
    _rejected_after_asking_for_the_extra(tmp_path)
    make_wheel(tmp_path, "app", "app", "1.0", requires=["lib", "speedup"])
    make_wheel(
        tmp_path, "lib", "lib", "1.0", requires=['speedup[turbo]; extra == "fast"']
    )
    make_wheel(
        tmp_path,
        "speedup",
        "speedup",
        "2.0",
        requires=['speedup-core; extra == "turbo"'],
    )

    assert _resolve(tmp_path, "app") == {
        "app": frozenset({"lib", "speedup"}),
        "lib": frozenset(),
        "speedup": frozenset(),
    }
