"""The resolver's reports of its own progress, passed on to the provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kpip._vendor.nab_resolver.resolver import ResolverObserver
from kpip.core.versions import Version

if TYPE_CHECKING:
    from typing import Any

    from kpip.resolution.nab_provider import NabProvider


class DecisionObserver(ResolverObserver[str, Version]):
    """Tells a provider of each decision the resolver makes, and each conflict."""

    def __init__(self, provider: NabProvider) -> None:
        self._provider = provider

    def on_decision(self, package: str, version: Version, level: int) -> None:
        self._provider.note_decision(package, version, level)

    def on_conflict(self, incompatibility: Any) -> None:
        self._provider.note_conflict()
