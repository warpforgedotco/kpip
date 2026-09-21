"""A catalog rescan for a chosen release must keep the requested extras.

``choose_version`` rescans the catalog when the chosen release materializes
no candidate, and again with yanked releases admitted. Both used to scan a
bare package name, so the candidates they handed back carried dependencies
for no extras, and a release chosen that way settled into the solution with
the extra's dependencies never resolved. The stored requirement cannot be
used as-is either: it is not undone on backtrack, so its specifier can be
stale against the live range. The scan keeps the extras and drops the rest.
"""

from __future__ import annotations

from typing import Any

from kpip.core.packaging import parse_requirement
from kpip.core.versions import Version
from kpip.index.provider import CandidateProvider
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider


def _adapter(monkeypatch: Any) -> tuple[NabProvider, list[Any]]:
    adapter = NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True),
    )
    scanned: list[Any] = []

    class Fallback:
        def find_candidates(self, requirement: Any, **kwargs: Any) -> list[Any]:
            scanned.append(requirement)
            return []

    monkeypatch.setattr(adapter.provider, "with_yanked_policy", lambda _: Fallback())
    return adapter, scanned


def test_the_yanked_retry_scans_with_the_extras_and_no_specifier(
    monkeypatch: Any,
) -> None:
    adapter, scanned = _adapter(monkeypatch)
    requirement = parse_requirement("demo[fast]==1.0")
    adapter.requirements["demo"] = requirement

    adapter._retry_including_yanked(
        "demo",
        Version("1.0"),
        requirement=adapter._unpinned("demo", requirement),
        matching=[Version("1.0")],
        constraints=(),
        version_range=None,  # type: ignore[arg-type]
    )

    assert scanned, "the retry never scanned the catalog"
    assert all(req.extras == frozenset({"fast"}) for req in scanned)
    assert all(not req.specifier.text for req in scanned)


def test_unpinned_is_memoized_per_requirement(monkeypatch: Any) -> None:
    adapter, _ = _adapter(monkeypatch)
    requirement = parse_requirement("demo[fast]>=1")

    first = adapter._unpinned("demo", requirement)

    assert adapter._unpinned("demo", requirement) is first
    assert first.extras == frozenset({"fast"})
    assert not first.specifier.text
    assert adapter._unpinned("demo", parse_requirement("demo[slow]")) is not first
