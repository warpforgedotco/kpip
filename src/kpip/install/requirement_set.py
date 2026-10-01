from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar


if TYPE_CHECKING:
    from kpip.resolution.models import RequirementInput

    RequirementT = TypeVar("RequirementT", bound="RequirementInput")

else:
    # Bounded only where the bound is read. A string bound is a lazily
    # evaluated annotation, and building one imports ``annotationlib`` --
    # and ``ast`` behind it -- on Python 3.14, for something no run-time
    # code ever looks at.
    RequirementT = TypeVar("RequirementT")


class RequirementSet(Generic[RequirementT]):
    __slots__ = ("named_internal", "unnamed")

    def __init__(
        self,
        named_internal: dict[str, RequirementT] | None = None,
        unnamed: list[RequirementT] | None = None,
    ) -> None:
        self.named_internal = named_internal if named_internal is not None else {}
        self.unnamed = unnamed if unnamed is not None else []

    @property
    def requirements(self) -> dict[str, RequirementT]:
        return dict(self.named_internal)

    @property
    def all_requirements(self) -> list[RequirementT]:
        return [*self.named_internal.values(), *self.unnamed]
