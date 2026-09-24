"""A Version rebuilt from its wire record is the Version it was."""

from __future__ import annotations

import pytest
from kpip.core import versions
from kpip.core.versions import Version

TEXTS = (
    "1.0",
    "1.0.0",
    "2",
    "1!2.0.0",
    "1.0rc1",
    "1.0.post2",
    "1.0.dev3",
    "1.0a1.post2.dev3",
    "1.0+ubuntu.1",
    "10.20.30.0+local",
    "0.0.0",
)


@pytest.mark.parametrize("text", TEXTS)
def test_from_wire_rebuilds_the_version(text: str) -> None:
    original = Version(text)
    state = original.to_wire()
    versions._versions.clear()

    rebuilt = Version.from_wire(state)

    assert rebuilt is not original
    assert rebuilt == original
    assert hash(rebuilt) == hash(original)
    assert rebuilt.public == original.public
    assert rebuilt.release == original.release
    assert rebuilt.is_prerelease == original.is_prerelease
    assert rebuilt.base_version == original.base_version
    assert Version(text) is rebuilt


def test_a_missing_attribute_is_still_missing() -> None:
    versions._versions.clear()
    rebuilt = Version.from_wire(Version("1.0").to_wire())

    with pytest.raises(AttributeError):
        rebuilt.nonexistent  # noqa: B018
