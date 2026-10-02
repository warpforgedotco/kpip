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
    monkeypatch.setattr(interpreter_pgo, "target_python", lambda: "/usr/bin/python3")

    # Every kpip step fails here, and the training still finishes.
    assert interpreter_pgo.train("/built/python") == 0

    first, first_env = calls[0]
    assert first == ["/built/python", *interpreter_pgo.CPYTHON_TASK]
    assert first_env is None
    for command, env in calls[1:]:
        assert command[:3] == ["/built/python", "-m", "kpip"]
        assert env["PYTHONPATH"] == str(interpreter_pgo.REPO_ROOT / "src")
        assert env["KPIP_PYTHON"] == "/usr/bin/python3"


def test_without_a_cpython_kpip_installs_for_the_built_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environments: list[dict] = []

    def run(command, **kwargs):
        if kwargs.get("env") is not None:
            environments.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(interpreter_pgo.subprocess, "run", run)
    monkeypatch.setattr(interpreter_pgo, "target_python", lambda: None)
    monkeypatch.delenv("KPIP_PYTHON", raising=False)

    assert interpreter_pgo.train("/built/python") == 0
    assert environments
    assert all("KPIP_PYTHON" not in env for env in environments)


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("cpython-314", "/bin/python3"), ("monolithpy-314", None), ("", None)],
)
def test_the_target_is_a_cpython_on_path(
    monkeypatch: pytest.MonkeyPatch, tag: str, expected: str | None
) -> None:
    monkeypatch.setattr(interpreter_pgo.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        interpreter_pgo.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, tag + "\n"),
    )

    assert interpreter_pgo.target_python() == expected


def test_the_launcher_imports_this_package(tmp_path: Path) -> None:
    script = interpreter_pgo.write_launcher(tmp_path / "task" / "train.py")

    text = script.read_text(encoding="utf-8")
    compile(text, str(script), "exec")
    source = Path(interpreter_pgo.__file__).resolve().parents[1]
    assert repr(str(source)) in text
    assert "from kpip_compile.interpreter_pgo import main" in text
