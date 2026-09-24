"""A relock's previous pins, as the resolver adapter sees them."""

from __future__ import annotations

from typing import Any

from kpip.core.versions import Version
from kpip.index.provider import CandidateProvider
from kpip.resolution.models import ResolutionConfig
from kpip.resolution.nab_provider import NabProvider


def adapter(preferences: dict[str, str]) -> NabProvider:
    return NabProvider(
        CandidateProvider.from_options(no_index=True),
        ResolutionConfig(ignore_installed=True, preferences=preferences),
    )


def test_a_preference_that_is_not_a_version_is_dropped() -> None:
    assert adapter({"demo": "1.0", "odd": "not a version"})._preferences == {
        "demo": Version("1.0"),
    }


def test_every_preferred_page_starts_in_one_wave(monkeypatch: Any) -> None:
    subject = adapter({"demo": "1.0", "other": "2.0"})
    started: list[tuple[tuple[str, ...], bool]] = []
    monkeypatch.setattr(
        subject.provider,
        "prefetch_available_versions",
        lambda requirements, *, lookahead=False: started.append(
            (
                tuple(requirement.canonical_name for requirement in requirements),
                lookahead,
            )
        ),
    )

    subject._prefetch_preferred_catalogs()

    assert started == [(("demo", "other"), True)]
    assert subject.provider.preferred_versions == {
        "demo": Version("1.0"),
        "other": Version("2.0"),
    }


def test_no_previous_lock_starts_nothing(monkeypatch: Any) -> None:
    subject = adapter({})
    monkeypatch.setattr(
        subject.provider,
        "prefetch_available_versions",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError(args)),
    )

    subject._prefetch_preferred_catalogs()

    assert subject.provider.preferred_versions == {}
