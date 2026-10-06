"""Unpacking and installing many files.

Ports of uv-bench's many-files suite (``crates/uv-bench/benches/uv.rs``) at
the same 10,000-file scale, where per-member archive overhead dominates
rather than fixed per-call cost.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from kpip._internal.locations import get_scheme
from kpip._internal.operations.install.wheel import install_wheel
from kpip._internal.utils.unpacking import untar_file, unzip_file
from pytest_codspeed import BenchmarkFixture


def test_unzip_wheel_many_files(
    benchmark: BenchmarkFixture, many_files_wheel: Path, tmp_path: Path
) -> None:
    counter = itertools.count()

    def unzip() -> None:
        unzip_file(str(many_files_wheel), str(tmp_path / str(next(counter))), False)

    benchmark(unzip)


def test_unpack_sdist_many_files(
    benchmark: BenchmarkFixture, many_files_sdist: Path, tmp_path: Path
) -> None:
    counter = itertools.count()

    def untar() -> None:
        untar_file(str(many_files_sdist), str(tmp_path / str(next(counter))))

    benchmark(untar)


def test_install_wheel_many_files(
    benchmark: BenchmarkFixture, many_files_wheel: Path, tmp_path: Path
) -> None:
    """``kpip install --target``'s install step, without compiling bytecode."""
    counter = itertools.count()

    def install() -> None:
        scheme = get_scheme("many-files-pkg", home=str(tmp_path / str(next(counter))))
        install_wheel(
            "many-files-pkg",
            str(many_files_wheel),
            scheme,
            "many-files-pkg==1.0.0",
            pycompile=False,
        )

    benchmark(install)
