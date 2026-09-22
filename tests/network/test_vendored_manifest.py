from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


VENDOR_ROOT = Path(__file__).parents[2] / "src" / "kpip" / "_vendor"
LAUNCHER_ROOT = Path(__file__).parents[2] / "src" / "kpip" / "_launchers"

LICENSE_PATHS = (
    "certifi/LICENSE",
    "idna/LICENSE.md",
    "nab_resolver/LICENSE",
    "urllib3/LICENSE.txt",
    "tomli/LICENSE",
)

LAUNCHER_HASHES = {
    "t32.exe": "6b4195e640a85ac32eb6f9628822a622057df1e459df7c17a12f97aeabc9415b",
    "t64-arm.exe": "ebc4c06b7d95e74e315419ee7e88e1d0f71e9e9477538c00a93a9ff8c66a6cfc",
    "t64.exe": "81a618f21cb87db9076134e70388b6e9cb7c2106739011b6a51772d22cae06b7",
    "w32.exe": "47872cc77f8e18cf642f868f23340a468e537e64521d9a3a416c8b84384d064b",
    "w64-arm.exe": "c5dc9884a8f458371550e09bd396e5418bf375820a31b9899f6499bf391c7b2e",
    "w64.exe": "7a319ffaba23a017d7b1e18ba726ba6c54c53d6446db55f92af53c279894f8ad",
}


@pytest.mark.parametrize("relative_path", LICENSE_PATHS)
def test_vendored_license_is_present(relative_path: str) -> None:
    license_path = VENDOR_ROOT / relative_path

    assert license_path.is_file()
    assert license_path.stat().st_size > 0


def test_launcher_license_is_present() -> None:
    license_path = LAUNCHER_ROOT / "DISTLIB-LICENSE.txt"

    assert license_path.is_file()
    assert license_path.stat().st_size > 0


@pytest.mark.parametrize("filename, expected_hash", LAUNCHER_HASHES.items())
def test_vendored_launcher_matches_documented_hash(
    filename: str,
    expected_hash: str,
) -> None:
    launcher = LAUNCHER_ROOT / filename

    digest = hashlib.sha256()
    with launcher.open("rb") as launcher_file:
        while chunk := launcher_file.read(1024 * 1024):
            digest.update(chunk)

    assert digest.hexdigest() == expected_hash


def test_launcher_hashes_match_vendored_documentation() -> None:
    documentation = VENDOR_ROOT.joinpath("VENDORED.md").read_text(encoding="utf-8")

    for filename, expected_hash in LAUNCHER_HASHES.items():
        assert f"`{filename}` | `{expected_hash}`" in documentation
