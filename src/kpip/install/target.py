"""Canonical installation destinations.



All filesystem installation code should consume :class:`InstallTarget` rather

than calculating individual scheme paths.

"""

from __future__ import annotations

import os

from kpip.host.interpreter_facts import target_interpreter
from kpip.host.locations.sysconfig_scheme import get_scheme
from kpip.host.scheme import Scheme


class InstallTarget:
    """The complete destination scheme for one installation transaction."""

    __slots__ = (
        "data",
        "headers",
        "headers_root",
        "platlib",
        "purelib",
        "resolved_roots_internal",
        "scripts",
    )

    def __init__(
        self,
        purelib: str,
        platlib: str,
        headers: str,
        scripts: str,
        data: str,
    ) -> None:
        self.purelib = purelib

        self.platlib = platlib

        self.headers = headers

        self.scripts = scripts

        self.data = data

        # The directory each distribution's own headers directory is named
        # in, when the scheme has one per distribution (see
        # :meth:`for_distribution`).
        self.headers_root: str | None = None

        self.resolved_roots_internal: dict[str, str] = {}

    @classmethod
    def from_scheme(cls, scheme: Scheme) -> InstallTarget:
        resolved: dict[str, str] = {}

        def resolve(path: str) -> str:
            cached = resolved.get(path)

            if cached is None:
                cached = os.path.realpath(path)

                resolved[path] = cached

            return cached

        return cls(
            purelib=resolve(scheme.purelib),
            platlib=resolve(scheme.platlib),
            headers=resolve(scheme.headers),
            scripts=resolve(scheme.scripts),
            data=resolve(scheme.data),
        )

    @classmethod
    def from_options(
        cls,
        name: str,
        *,
        target: str | None = None,
        user: bool = False,
        home: str | None = None,
        prefix: str | None = None,
        root: str | None = None,
        isolated: bool = False,
    ) -> InstallTarget:
        if target is not None:
            target_text = os.fspath(target)

            scheme = Scheme(
                platlib=target_text,
                purelib=target_text,
                headers=target_text,
                scripts=os.path.join(
                    target_text,
                    "Scripts" if os.name == "nt" else "bin",
                ),
                data=target_text,
            )

            if root is not None:
                scheme = apply_root(scheme, root)

            return cls.from_scheme(scheme)

        scheme = get_scheme(
            name,
            interpreter=target_interpreter(),
            user=user,
            home=home,
            root=root,
            isolated=isolated,
            prefix=prefix,
        )

        install_target = cls.from_scheme(scheme)

        install_target.headers_root = os.path.realpath(
            os.path.dirname(scheme.headers),
        )

        return install_target

    def for_distribution(self, name: str) -> InstallTarget:
        """This target, with the headers directory of distribution ``name``.

        A scheme's headers path is per distribution, ``include/<name>``: one
        target for a whole batch would put every wheel's ``.data/headers`` in
        the first wheel's directory. pip names it after the requirement,
        which for a wheel it resolved is the normalized name its filename
        parses to, and so does ``name`` here. A ``--target`` directory has
        one headers path for all, and is returned as it is.
        """

        if self.headers_root is None:
            return self

        headers = os.path.realpath(os.path.join(self.headers_root, name))

        if headers == self.headers:
            return self

        install_target = InstallTarget(
            purelib=self.purelib,
            platlib=self.platlib,
            headers=headers,
            scripts=self.scripts,
            data=self.data,
        )

        install_target.headers_root = self.headers_root

        install_target.resolved_roots_internal = self.resolved_roots_internal

        return install_target

    @property
    def library_roots(self) -> tuple[str, str]:
        return self.purelib, self.platlib

    def destination(self, relative: str, *, base: str = "purelib") -> str:
        """Return a validated destination for a wheel-relative path."""

        root = getattr(self, base)

        root_text = os.fspath(root)

        destination_text = os.path.realpath(os.path.join(root_text, relative))

        resolved_root = self.resolved_roots_internal.get(root_text)

        if resolved_root is None:
            resolved_root = os.path.realpath(root_text)

            self.resolved_roots_internal[root_text] = resolved_root

        try:
            if os.path.commonpath((destination_text, resolved_root)) != resolved_root:
                raise ValueError

        except (OSError, ValueError) as exc:
            raise ValueError(f"path escapes installation target: {relative!r}") from exc

        return destination_text


def apply_root(scheme: Scheme, root: str) -> Scheme:
    def relocate(path: str) -> str:
        value = os.fspath(path)

        drive, tail = os.path.splitdrive(value)

        if (
            drive
            or tail.startswith(os.sep)
            or (os.altsep is not None and tail.startswith(os.altsep))
        ):
            value = tail.lstrip("/\\")

        return os.path.join(os.fspath(root), value)

    return Scheme(
        platlib=relocate(scheme.platlib),
        purelib=relocate(scheme.purelib),
        headers=relocate(scheme.headers),
        scripts=relocate(scheme.scripts),
        data=relocate(scheme.data),
    )
