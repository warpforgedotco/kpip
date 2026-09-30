from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.core.wheel import WheelCandidate
from kpip.index.candidate_materialization import (
    CandidateMaterializer,
    LazyWheelCandidate,
)
from kpip.index.links import Link
from kpip.index.source_models import CandidateRecord
from kpip.install.output import (
    _run_candidate_operation,
    installation_order,
    prepare_install_candidates,
)


def remote_candidate(name: str, version: str = "1.0") -> LazyWheelCandidate:
    requirement = parse_requirement(f"{name}=={version}")
    assert requirement is not None
    record = CandidateRecord(
        name=name,
        version=Version(version),
        link=Link.from_url(
            f"https://example.invalid/{name}-{version}-py3-none-any.whl",
            source_url=None,
        ),
    )
    return LazyWheelCandidate(record, requirement, CandidateMaterializer())


def test_run_candidate_operation_runs_remote_wheels_concurrently_in_order() -> None:
    candidates = [remote_candidate(f"demo-{index}") for index in range(3)]
    lock = threading.Lock()
    all_started = threading.Event()
    active = 0
    peak = 0
    indexes = {id(candidate): index for index, candidate in enumerate(candidates)}

    def finalize(candidate: WheelCandidate) -> WheelCandidate:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == len(candidates):
                all_started.set()
        assert all_started.wait(timeout=5)
        index = indexes[id(candidate)]
        time.sleep((len(candidates) - index) * 0.005)
        with lock:
            active -= 1
        return candidate

    result = _run_candidate_operation(candidates, finalize)

    assert all(result is candidate for result, candidate in zip(result, candidates))
    assert peak == len(candidates)


def test_run_candidate_operation_keeps_source_artifacts_on_calling_thread() -> None:
    requirement = parse_requirement("demo==1.0")
    assert requirement is not None
    source = LazyWheelCandidate(
        CandidateRecord(
            name="demo",
            version=Version("1.0"),
            link=Link.from_url(
                "https://example.invalid/demo-1.0.tar.gz",
                source_url=None,
            ),
        ),
        requirement,
        CandidateMaterializer(),
    )
    caller = threading.current_thread()
    worker: threading.Thread | None = None

    def finalize(candidate: WheelCandidate) -> WheelCandidate:
        nonlocal worker
        worker = threading.current_thread()
        return candidate

    result = _run_candidate_operation([source], finalize)

    assert len(result) == 1
    assert result[0] is source
    assert worker is caller


def test_prepare_install_candidates_pipelines_completed_downloads(
    tmp_path,
    monkeypatch,
) -> None:
    candidates = [remote_candidate(f"demo-{index}") for index in range(3)]
    archive_started = threading.Event()
    concrete_by_name = {
        candidate.name: WheelCandidate(
            name=candidate.name,
            version=candidate.version,
            path=str(tmp_path / f"{candidate.name}.whl"),
            dependencies=(),
            source_kind="wheel",
        )
        for candidate in candidates
    }

    def materialize(candidate: LazyWheelCandidate) -> WheelCandidate:
        if candidate.name == "demo-0":
            assert archive_started.wait(timeout=5)
        else:
            time.sleep(0.01)
        return concrete_by_name[candidate.name]

    def prepare(candidate: WheelCandidate, cache_dir: str) -> object:
        assert cache_dir == str(tmp_path / "cache")
        archive_started.set()
        return object()

    monkeypatch.setattr(LazyWheelCandidate, "materialize", materialize)
    result = prepare_install_candidates(
        candidates,
        str(tmp_path / "cache"),
        prepare,
    )

    assert [candidate.name for candidate in result] == [
        "demo-0",
        "demo-1",
        "demo-2",
    ]
    assert all(candidate.wheel_layout is not None for candidate in result)


def test_prepare_install_candidates_treats_cache_errors_as_fallback(
    tmp_path,
) -> None:
    candidate = WheelCandidate(
        name="demo",
        version=Version("1.0"),
        path=str(tmp_path / "demo.whl"),
        dependencies=(),
        source_kind="wheel",
    )

    def fail_cache(candidate: WheelCandidate, cache_dir: str) -> object:
        del candidate, cache_dir
        raise OSError("cache unavailable")

    result = prepare_install_candidates(
        [candidate],
        str(tmp_path / "cache"),
        fail_cache,
    )

    assert result == [candidate]
    assert result[0].wheel_layout is None


def _named(
    *names: str, requires_python: dict[str, str] | None = None
) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            canonical_name=name,
            requires_python=(requires_python or {}).get(name),
        )
        for name in names
    ]


def test_installation_order_is_pips_leaves_first_requested_last() -> None:
    """pip's weights: leaves pruned round by round, the first round
    heaviest, installed heaviest first, ties by name descending. The
    requested ``jupyter`` goes last, so its ``jupyter.py`` is the one left
    over ``jupyter-core``'s."""
    graph = {
        "jupyter": {"notebook", "jupyter-console"},
        "notebook": {"jupyter-core"},
        "jupyter-console": {"jupyter-core"},
        "jupyter-core": set(),
    }
    candidates = _named("jupyter", "jupyter-console", "jupyter-core", "notebook")

    ordered = installation_order(candidates, graph, {"jupyter"})

    assert [candidate.canonical_name for candidate in ordered] == [
        "jupyter-core",
        "notebook",
        "jupyter-console",
        "jupyter",
    ]


def test_installation_order_weighs_a_cycle_by_its_longest_path() -> None:
    graph = {"root": {"a"}, "a": {"b"}, "b": {"a"}}

    ordered = installation_order(_named("a", "b", "root"), graph, {"root"})

    assert [candidate.canonical_name for candidate in ordered] == ["b", "a", "root"]


def test_installation_order_counts_the_interpreter_as_pip_does() -> None:
    """``pip install --no-deps jupyter==1.0.0 jupyter-core==5.7.1`` installs
    jupyter, then jupyter-core: jupyter-core's Requires-Python makes the
    interpreter its child in pip's graph, so it is not a first-round leaf
    and weighs less (pip 26.2 weighs them 3 and 1)."""
    candidates = _named(
        "jupyter", "jupyter-core", requires_python={"jupyter-core": ">=3.8"}
    )

    ordered = installation_order(candidates, {}, {"jupyter", "jupyter-core"})

    assert [candidate.canonical_name for candidate in ordered] == [
        "jupyter",
        "jupyter-core",
    ]


def test_installation_order_leaves_the_interpreter_out_when_ignored() -> None:
    candidates = _named(
        "jupyter", "jupyter-core", requires_python={"jupyter-core": ">=3.8"}
    )

    ordered = installation_order(
        candidates, {}, {"jupyter", "jupyter-core"}, ignore_requires_python=True
    )

    assert [candidate.canonical_name for candidate in ordered] == [
        "jupyter-core",
        "jupyter",
    ]


def _prefetch_with(monkeypatch, fetch) -> tuple:
    """A WheelPrefetch whose candidates are all remote wheels named by
    ``url``, fetched by ``fetch``."""
    from kpip.install import output

    monkeypatch.setattr(
        output, "_remote_wheel_url", lambda candidate: getattr(candidate, "url", None)
    )
    monkeypatch.setattr(output, "materialize_candidate", fetch)
    prepared: list[str] = []

    def prepare_archive(candidate, cache_dir):
        prepared.append(candidate.url)
        return f"archive:{candidate.url}"

    return output.WheelPrefetch("cache", prepare_archive), prepared


class _Wheel(SimpleNamespace):
    def copy_with(self, **changes):
        return _Wheel(**{**vars(self), **changes})


def test_a_decided_wheel_is_fetched_once_before_the_install_prepares_it(
    monkeypatch,
) -> None:
    """The prefetch only warms the caches: the install waits for it, then
    prepares the candidate as it would have, finding them warm."""
    import threading

    events: list[str] = []
    release = threading.Event()

    def fetch(candidate):
        if threading.current_thread().name == "kpip-prefetch":
            release.wait(5)
            events.append("prefetched")
        else:
            events.append("installed")
        return candidate

    prefetch, prepared = _prefetch_with(monkeypatch, fetch)
    wheel = _Wheel(url="https://x/a-1.0-py3-none-any.whl", canonical_name="a")
    prefetch(wheel)
    prefetch(wheel)  # decided again after a backtrack
    threading.Timer(0.05, release.set).start()

    result = prepare_install_candidates(
        [wheel], "cache", lambda c, d: "archive", prefetch
    )

    assert events == ["prefetched", "installed"]
    assert prepared == ["https://x/a-1.0-py3-none-any.whl"]
    assert result[0].wheel_layout == "archive"
    prefetch.close()


def test_a_failed_prefetch_leaves_the_install_to_report_its_own_error(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fetch(candidate):
        calls.append(candidate.url)
        raise OSError("connection reset")

    prefetch, _ = _prefetch_with(monkeypatch, fetch)
    wheel = _Wheel(url="https://x/b-1.0-py3-none-any.whl", canonical_name="b")
    prefetch(wheel)

    with pytest.raises(OSError, match="connection reset"):
        prepare_install_candidates([wheel], "cache", lambda c, d: None, prefetch)

    assert calls == [wheel.url, wheel.url]
    prefetch.close()


def test_nothing_is_fetched_after_close(monkeypatch) -> None:
    fetched: list[str] = []
    prefetch, _ = _prefetch_with(monkeypatch, lambda c: fetched.append(c.url) or c)
    prefetch.close()

    prefetch(_Wheel(url="https://x/c-1.0-py3-none-any.whl", canonical_name="c"))
    time.sleep(0.05)

    assert fetched == []
