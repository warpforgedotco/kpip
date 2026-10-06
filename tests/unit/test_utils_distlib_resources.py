"""distlib finds its launchers whatever loads it, as Nuitka does in the
compiled kpip."""

from __future__ import annotations

import pytest

from kpip._internal.utils import distlib_resources
from kpip._vendor import distlib
from kpip._vendor.distlib import resources


class CompiledLoader:
    """Stands in for Nuitka's loader, of a type distlib does not know."""


def test_launchers_are_found_with_a_loader_distlib_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(distlib, "__loader__", CompiledLoader())
    monkeypatch.setattr(resources, "_finder_cache", {})
    monkeypatch.setattr(resources, "_finder_registry", dict(resources._finder_registry))
    with pytest.raises(resources.DistlibException, match="Unable to locate finder"):
        resources.finder(distlib.__name__)

    distlib_resources.register()
    names = {
        resource.name
        for resource in resources.finder(distlib.__name__).iterator("")
        if resource.name.endswith(".exe")
    }
    assert {"t64.exe", "w64.exe", "t64-arm.exe"} <= names
