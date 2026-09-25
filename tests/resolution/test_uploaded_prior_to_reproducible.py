"""An upload cutoff resolves as the index stood at the cutoff.

``--uploaded-prior-to`` exists so that a resolve does not move when the
index does. Releases published after the cutoff used to take part anyway:
the resolver chose them, found them empty, and settled on an older release
by a search that skipped the forward check, so the result depended on what
had been published since. On airflow's graph, pinned to 2024-09-01, that
moved four pins between PyPI as recorded in 2026 and PyPI as it stood.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BENCHMARKS = Path(__file__).resolve().parents[1] / "benchmarks"
if str(_BENCHMARKS) not in sys.path:  # pragma: no cover - import side effect
    sys.path.insert(0, str(_BENCHMARKS))

import uv_graphs  # noqa: E402
from benchmark_support import reset_caches  # noqa: E402


def _pins(tmp_path: Path, **session_options: object) -> dict[str, str]:
    reset_caches()
    session = uv_graphs.ReplaySession(
        "airflow",
        cache=str(tmp_path / "http"),
        **session_options,
    )
    with uv_graphs.uv_environment():
        result = uv_graphs.resolve("airflow", session, str(tmp_path / "cache"))
    reset_caches()
    return {
        candidate.name.lower(): str(candidate.version)
        for candidate in result.candidates
    }


def test_releases_after_the_cutoff_do_not_move_the_resolve(tmp_path: Path) -> None:
    as_recorded = _pins(tmp_path / "recorded")
    as_it_stood = _pins(tmp_path / "as-of", as_of=uv_graphs.UPLOADED_PRIOR_TO)

    assert len(as_recorded) == 585
    assert as_recorded == as_it_stood
