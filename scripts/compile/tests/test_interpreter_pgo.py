from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from kpip_compile import interpreter_pgo, pgo


def test_the_profile_resolves_are_training_sets() -> None:
    assert set(interpreter_pgo.PROFILE_RESOLVES) <= set(pgo.TRAINING_RESOLVES)
    for name, _ in interpreter_pgo.PROFILE_RESOLVES:
        assert (pgo.REQUIREMENTS_DIR / name).is_file(), name


def test_only_the_profile_resolves_are_locked(tmp_path: Path) -> None:
    steps = pgo.training_steps(tmp_path, tmp_path / "cache", (("black.in", None),))
    requirement_files = {
        step.arguments[step.arguments.index("-r") + 1]
        for step in steps
        if step.arguments[0] == "lock" and "-r" in step.arguments
    }

    assert requirement_files == {
        str(pgo.REQUIREMENTS_DIR / "black.in"),
        str(tmp_path / "web.txt"),
    }


def test_the_training_runs_cpythons_slice_then_kpip_from_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict | None]] = []

    def run(command, **kwargs):
        calls.append((command, kwargs.get("env")))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(interpreter_pgo.subprocess, "run", run)

    # Every kpip step fails here, and the training still finishes.
    assert interpreter_pgo.train("/built/python") == 0

    first, first_env = calls[0]
    assert first == ["/built/python", *interpreter_pgo.CPYTHON_TASK]
    assert first_env is None
    for command, env in calls[1:]:
        assert command[:3] == ["/built/python", "-m", "kpip"]
        assert env["PYTHONPATH"] == str(interpreter_pgo.REPO_ROOT / "src")


def test_the_launcher_imports_this_package(tmp_path: Path) -> None:
    script = interpreter_pgo.write_launcher(tmp_path / "task" / "train.py")

    text = script.read_text(encoding="utf-8")
    compile(text, str(script), "exec")
    source = Path(interpreter_pgo.__file__).resolve().parents[1]
    assert repr(str(source)) in text
    assert "from kpip_compile.interpreter_pgo import main" in text
