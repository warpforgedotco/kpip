"""The resolver's reports of its own progress, passed on to the provider."""

from __future__ import annotations

from kpip._vendor.nab_resolver.resolver import ResolverObserver
from kpip.core.versions import Version

TYPE_CHECKING = False

if TYPE_CHECKING:
    from kpip.resolution.nab_provider import NabProvider


class DecisionObserver(ResolverObserver[str, Version]):
    """Tells a provider of each decision the resolver makes."""

    def __init__(self, provider: NabProvider) -> None:
        self._provider = provider

    def on_decision(self, package: str, version: Version, level: int) -> None:
        self._provider.note_decision(package, version, level)
