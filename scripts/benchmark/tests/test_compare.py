from __future__ import annotations

import json
from pathlib import Path

import pytest
from kpip_benchmark.compare import compare


def write_export(
    directory: Path, name: str, *, kpip_mean: float, uv_mean: float
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "results": [
                    {"command": f"kpip ({name})", "mean": kpip_mean, "stddev": 0.001},
                    {"command": f"uv ({name})", "mean": uv_mean, "stddev": 0.001},
                ],
            },
        ),
        encoding="utf-8",
    )


def write_meta(directory: Path, *, python_version: str) -> None:
    (directory / "meta.json").write_text(
        json.dumps(
            {
                "kpip_python_version": python_version,
                "uv_version": "uv 0.12.1",
                "git_commit": "deadbeef",
            },
        ),
        encoding="utf-8",
    )


def test_compare_reports_delta_per_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "lock-cold", kpip_mean=0.100, uv_mean=0.050)
    write_export(after, "lock-cold", kpip_mean=0.080, uv_mean=0.050)
    write_meta(before, python_version="Python 3.10.20")
    write_meta(after, python_version="Python 3.10.20")

    assert compare(before, after) == 0

    out = capsys.readouterr().out
    assert "lock-cold" in out
    assert "-20.0%" in out
    assert "+0.0%" in out


def test_compare_warns_on_mismatched_interpreter(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "startup-help", kpip_mean=0.050, uv_mean=0.010)
    write_export(after, "startup-help", kpip_mean=0.020, uv_mean=0.010)
    write_meta(before, python_version="Python 3.14.6")
    write_meta(after, python_version="Python 3.10.20")

    assert compare(before, after) == 0

    err = capsys.readouterr().err
    assert "kpip_python_version differs" in err
    assert "3.14.6" in err
    assert "3.10.20" in err


def test_compare_warns_when_metadata_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "startup-help", kpip_mean=0.050, uv_mean=0.010)
    write_export(after, "startup-help", kpip_mean=0.020, uv_mean=0.010)

    assert compare(before, after) == 0

    err = capsys.readouterr().err
    assert "no meta.json" in err


def test_compare_rejects_no_overlap(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "lock-cold", kpip_mean=0.100, uv_mean=0.050)
    write_export(after, "install-cold", kpip_mean=0.080, uv_mean=0.050)

    assert compare(before, after) == 1

    err = capsys.readouterr().err
    assert "No matching benchmark names" in err


def test_compare_reports_names_present_in_only_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    write_export(before, "lock-cold", kpip_mean=0.100, uv_mean=0.050)
    write_export(before, "install-cold", kpip_mean=0.100, uv_mean=0.050)
    write_export(after, "lock-cold", kpip_mean=0.080, uv_mean=0.050)
    write_meta(before, python_version="Python 3.10.20")
    write_meta(after, python_version="Python 3.10.20")

    assert compare(before, after) == 0

    out = capsys.readouterr().out
    assert "install-cold" in out.rsplit("Skipped", 1)[-1]


def test_compare_labels_a_compiled_kpip_and_checks_its_build(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    before, after = tmp_path / "before", tmp_path / "after"
    for directory, mean, build in (
        (before, 0.100, "kpip 0.0.1 (python 3.14)"),
        (after, 0.090, "kpip 0.0.1 (python 3.13)"),
    ):
        directory.mkdir()
        (directory / "lock-warm.json").write_text(
            json.dumps(
                {
                    "results": [
                        {
                            "command": "kpip-compiled (offline/lock-warm)",
                            "mean": mean,
                            "stddev": 0.001,
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )
        write_meta(directory, python_version="Python 3.14.7")
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        meta["kpip_compiled_version"] = build
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    assert compare(before, after) == 0

    captured = capsys.readouterr()
    assert "kpip-compiled" in captured.out
    assert "-10.0%" in captured.out
    assert "kpip_compiled_version differs" in captured.err


@pytest.mark.parametrize(
    ("before_binary", "after_binary", "expected"),
    [
        ("a" * 64, "b" * 64, "sha256 aaaaaaaaaaaa -> bbbbbbbbbbbb"),
        ("a" * 64, "a" * 64, "same compiled kpip"),
    ],
)
def test_compare_tells_which_compiled_binaries_ran(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    before_binary: str,
    after_binary: str,
    expected: str,
) -> None:
    """Same version, different binaries: the fingerprint tells them apart."""
    before, after = tmp_path / "before", tmp_path / "after"
    for directory, binary in ((before, before_binary), (after, after_binary)):
        write_export(directory, "lock-warm", kpip_mean=0.1, uv_mean=0.05)
        write_meta(directory, python_version="Python 3.14.7")
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        meta["kpip_compiled_version"] = "kpip 0.0.1 (python 3.14)"
        meta["kpip_compiled_sha256"] = binary
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    assert compare(before, after) == 0

    err = capsys.readouterr().err
    assert expected in err
    assert "kpip_compiled_version differs" not in err
