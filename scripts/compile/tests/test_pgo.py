from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest
from kpip_compile import build as build_module
from kpip_compile import pgo
from kpip_compile.build import BuildOptions, build


def test_windows_is_not_supported() -> None:
    pgo.check_supported("darwin")
    pgo.check_supported("linux")
    with pytest.raises(pgo.PgoError):
        pgo.check_supported("win32")


def test_training_resolves_every_set_and_exercises_other_commands(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    steps = pgo.training_steps(tmp_path, cache)
    arguments = [step.arguments for step in steps]

    for name, python_version in pgo.TRAINING_RESOLVES:
        locks = [
            a
            for a in arguments
            if a[0] == "lock" and str(pgo.REQUIREMENTS_DIR / name) in a
        ]
        assert len(locks) == 3, name
        assert all(("--python-version" in a) == bool(python_version) for a in locks), (
            name
        )
    commands = {a[0] for a in arguments}
    assert commands >= {
        "--version",
        "lock",
        "download",
        "install",
        "list",
        "freeze",
        "inspect",
        "wheel",
        "cache",
    }
    assert all(str(cache) in a for a in arguments if "--cache-dir" in a)


def test_every_training_set_exists() -> None:
    """The sets are the benchmark's; renaming one there must not break a PGO build."""
    for name, _ in pgo.TRAINING_RESOLVES:
        assert (pgo.REQUIREMENTS_DIR / name).is_file(), name


def test_only_steps_that_may_fail_can_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    steps = [pgo.TrainingStep(["lock"], may_fail=True), pgo.TrainingStep(["--version"])]
    monkeypatch.setattr(pgo, "training_steps", lambda work, cache: steps)
    monkeypatch.setattr(
        pgo.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1),
    )

    with pytest.raises(pgo.PgoError, match="--version"):
        pgo.train(tmp_path / "kpip")


def test_nuitka_runs_the_training_through_a_script(tmp_path: Path) -> None:
    binary = tmp_path / "out dir" / "kpip.dist" / "kpip.bin"
    result = tmp_path / "result"
    result.write_text("stale", encoding="utf-8")
    script = tmp_path / "pgo-train"

    options = pgo.nuitka_options(script, binary, result)

    assert options[:2] == ["--pgo-c", f"--pgo-executable={script}"]
    assert shlex.split(options[2].removeprefix("--pgo-args=")) == [
        str(binary),
        str(result),
    ]
    assert script.stat().st_mode & 0o111
    assert "-m kpip_compile.pgo" in script.read_text(encoding="utf-8")
    assert not result.exists()


@pytest.mark.parametrize(
    ("outcome", "message"),
    [(None, "never ran"), ("training step failed: kpip lock", "kpip lock")],
)
def test_a_build_whose_training_did_not_finish_fails(
    tmp_path: Path, outcome: str | None, message: str
) -> None:
    result = tmp_path / "result"
    if outcome is not None:
        result.write_text(outcome, encoding="utf-8")

    with pytest.raises(pgo.PgoError, match=message):
        pgo.check_training(result)


def test_the_training_records_its_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = tmp_path / "result"
    monkeypatch.setattr(pgo, "train", lambda binary: None)
    assert pgo.main([str(tmp_path / "kpip.bin"), str(result)]) == 0
    pgo.check_training(result)

    def fails(binary: Path) -> None:
        raise pgo.PgoError("training step failed: kpip --version")

    monkeypatch.setattr(pgo, "train", fails)
    assert pgo.main([str(tmp_path / "kpip.bin"), str(result)]) == 1
    with pytest.raises(pgo.PgoError, match="--version"):
        pgo.check_training(result)


def test_a_pgo_build_is_one_nuitka_run_that_trains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs: list[BuildOptions] = []

    def run_nuitka(
        options: BuildOptions,
        nuitka_dir: Path,
        environ: dict[str, str],
        interpreter: str,
    ) -> int:
        runs.append(options)
        (options.output_dir / "pgo-training.result").write_text(
            pgo.RESULT_OK, encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(build_module, "_run_nuitka", run_nuitka)

    status = build(
        BuildOptions(
            output_dir=tmp_path, platform="darwin", pgo=True, extra_args=("--lto=yes",)
        ),
        tmp_path,
    )

    assert status == 0
    (options,) = runs
    assert options.mode == "onefile"
    assert options.extra_args[0] == "--pgo-c"
    assert options.extra_args[-1] == "--lto=yes"
    pgo_args = next(a for a in options.extra_args if a.startswith("--pgo-args="))
    assert shlex.split(pgo_args.removeprefix("--pgo-args="))[0] == str(
        tmp_path / "kpip.dist" / "kpip.bin"
    )


def test_a_pgo_build_fails_when_its_training_did(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(build_module, "_run_nuitka", lambda *args: 0)

    with pytest.raises(pgo.PgoError, match="never ran"):
        build(BuildOptions(output_dir=tmp_path, platform="darwin", pgo=True), tmp_path)


def test_a_failed_pgo_build_is_not_checked_for_training(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(build_module, "_run_nuitka", lambda *args: 1)

    assert (
        build(BuildOptions(output_dir=tmp_path, platform="darwin", pgo=True), tmp_path)
        == 1
    )
