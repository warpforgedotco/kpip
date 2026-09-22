from __future__ import annotations

import base64
import configparser
import contextlib
import csv
import email.parser
import email.utils
import fnmatch
import hashlib
import importlib
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile

try:
    from tomllib import loads

except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    from kpip._vendor.tomli import loads

import zipfile
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from kpip.build.pep517_hooks import BuildBackendHookCaller, HookMissing
from kpip.core.errors import BuildError
from kpip.core.packaging import canonicalize_name, parse_requirement
from kpip.core.versions import InvalidVersion, Version
from kpip.core.subprocess import call_subprocess
from kpip.install.build_env.venv import create_isolated_venv


LEGACY_SETUPTOOLS_REQUIREMENT = "setuptools>=40.8.0,<82"
MINIMUM_PEP517_SETUPTOOLS = Version("40.8.0")
PKG_RESOURCES_REMOVAL_VERSION = Version("81")


class BuildHookMissing(BuildError):
    """A required build backend hook is unavailable."""


class BackendSpec:
    """The build backend and requirements declared by a project.

    Frozen and slotted, compared by value.
    """

    __slots__ = ("backend_path", "name", "requirements", "setup_py_present")

    name: str
    requirements: tuple[str, ...]
    backend_path: tuple[str, ...]
    setup_py_present: bool

    def __init__(
        self,
        name: str,
        requirements: tuple[str, ...],
        backend_path: tuple[str, ...],
        setup_py_present: bool = False,
    ) -> None:
        store = object.__setattr__
        store(self, "name", name)
        store(self, "requirements", requirements)
        store(self, "backend_path", backend_path)
        store(self, "setup_py_present", setup_py_present)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def _values(self) -> tuple[object, ...]:
        return (self.name, self.requirements, self.backend_path, self.setup_py_present)

    def __eq__(self, other: object) -> bool:
        return type(other) is BackendSpec and self._values() == other._values()

    def __hash__(self) -> int:
        return hash(self._values())

    def __repr__(self) -> str:
        return (
            f"BackendSpec(name={self.name!r}, requirements={self.requirements!r}, "
            f"backend_path={self.backend_path!r}, "
            f"setup_py_present={self.setup_py_present!r})"
        )

    @classmethod
    def from_project(cls, source_dir: str | os.PathLike[str]) -> BackendSpec | None:
        source_text = os.fspath(source_dir)

        pyproject = os.path.join(source_text, "pyproject.toml")

        setup_py = os.path.join(source_text, "setup.py")

        try:
            with open(pyproject, encoding="utf-8") as file:
                data = loads(file.read())

        except ValueError as exc:
            raise BuildError(
                f"Invalid PEP 518 build requirements in {pyproject}: {exc}",
            ) from exc

        except OSError:
            try:
                with open(setup_py, encoding="utf-8"):
                    pass

            except OSError:
                return None

            return cls(
                "setuptools.build_meta:__legacy__",
                (LEGACY_SETUPTOOLS_REQUIREMENT,),
                (),
                setup_py_present=True,
            )

        build_system = data.get("build-system")

        if not isinstance(build_system, dict):
            # A pyproject.toml carrying only tool configuration -- [tool.black]
            # and nothing else -- is the common shape for a project that still
            # builds through setup.py, and PEP 518 says the absent table means
            # the legacy setuptools backend, not "no backend". Returning None
            # here left such a project with no way to be built at all.
            try:
                with open(setup_py, encoding="utf-8"):
                    pass

            except OSError:
                return None

            return cls(
                "setuptools.build_meta:__legacy__",
                (LEGACY_SETUPTOOLS_REQUIREMENT,),
                (),
                setup_py_present=True,
            )

        if "requires" not in build_system:
            raise BuildError(
                f"Invalid PEP 518 [build-system] table in {pyproject}: "
                "mandatory `requires` key is missing",
            )

        requires = build_system.get("requires")

        if not isinstance(requires, list) or not all(
            isinstance(item, str) for item in requires
        ):
            raise BuildError(
                f"Invalid PEP 518 build requirements in {pyproject}: "
                "build-system.requires is not a list of strings",
            )

        parsed_requires = []

        for item in requires:
            try:
                parsed_requires.append(parse_requirement(item))
            except ValueError as exc:
                raise BuildError(
                    f"Invalid PEP 518 build requirement in {pyproject}: {item!r}",
                ) from exc

        for requirement in parsed_requires:
            if canonicalize_name(requirement.name) != "setuptools":
                continue

            _, upper_bound = requirement.specifier.bounds

            if upper_bound is not None and (
                upper_bound[0] < MINIMUM_PEP517_SETUPTOOLS
                or (upper_bound[0] == MINIMUM_PEP517_SETUPTOOLS and not upper_bound[1])
            ):
                raise BuildError(
                    f"Some build dependencies for {source_text} conflict with "
                    "PEP 517/518 supported requirements: setuptools==1.0 is "
                    "incompatible with setuptools>=40.8.0,<82.",
                )

        backend = build_system.get("build-backend", "setuptools.build_meta")

        if not isinstance(backend, str) or backend in {
            "kpip.build.build_backend",
            "uv_build",
        }:
            return None

        try:
            with open(setup_py, encoding="utf-8") as file:
                setup_contents = file.read()

        except OSError:
            setup_contents = None

        setup_uses_pkg_resources = (
            setup_contents is not None and "pkg_resources" in setup_contents
        )

        if (
            backend.startswith("setuptools.build_meta")
            and setup_contents is not None
            and setup_uses_pkg_resources
            and not any(
                canonicalize_name(requirement.name) == "setuptools"
                and not requirement.specifier.contains(
                    PKG_RESOURCES_REMOVAL_VERSION,
                    allow_prereleases=True,
                )
                for requirement in parsed_requires
            )
        ):
            requires.append("setuptools<82")

        backend_path = build_system.get("backend-path", [])

        if not isinstance(backend_path, list) or not all(
            isinstance(item, str) for item in backend_path
        ):
            raise BuildError(f"Invalid build-system.backend-path in {pyproject}")

        return cls(
            backend,
            tuple(requires),
            tuple(backend_path),
            setup_py_present=setup_contents is not None,
        )


class BackendRunner:
    """Run hooks in an isolated environment for an external backend."""

    def __init__(
        self,
        source_dir: str | os.PathLike[str],
        spec: BackendSpec,
        *,
        build_constraints: list[str] | None = None,
        build_isolation: bool = True,
    ) -> None:
        self.source_dir = source_dir

        self.spec = spec

        self.build_constraints = build_constraints

        self.build_isolation = build_isolation

    @contextlib.contextmanager
    def caller(self) -> Iterator[tuple[BuildBackendHookCaller, str]]:
        if not self.build_isolation:
            with tempfile.TemporaryDirectory(
                prefix="pip-build-metadata-",
            ) as metadata_dir:
                caller = BuildBackendHookCaller(
                    os.fspath(self.source_dir),
                    self.spec.name,
                    backend_path=list(self.spec.backend_path) or None,
                    python_executable=sys.executable,
                )

                with backend_environment(self.source_dir):
                    yield caller, metadata_dir

            return

        with tempfile.TemporaryDirectory(prefix="pip-build-env-") as env_dir:
            env_path = env_dir

            python = create_isolated_venv(
                env_path,
                with_pip=bool(self.spec.requirements),
            ).python_executable

            if self.spec.requirements:
                constraint_args = [
                    argument
                    for constraint in self.build_constraints or ()
                    for argument in ("--constraint", constraint)
                ]

                environment = os.environ.copy()

                environment.pop("KPIP_CONSTRAINT", None)

                local_find_links = shlex.split(environment.get("KPIP_FIND_LINKS", ""))

                install_options = [
                    option
                    for link in local_find_links
                    if link
                    for option in ("--find-links", link)
                ]

                install_options.insert(0, "--ignore-installed")

                no_index = environment.get("KPIP_NO_INDEX", "").lower()

                if no_index in {"1", "true", "yes", "on"}:
                    install_options.insert(0, "--no-index")

                else:
                    environment.pop("KPIP_NO_INDEX", None)

                if any(
                    requirement.split("[", 1)[0].split(" ", 1)[0].lower()
                    == "setuptools"
                    for requirement in self.spec.requirements
                ):
                    install_options.extend(("--only-binary", "setuptools"))

                try:
                    subprocess.run(
                        [
                            python,
                            "-m",
                            "pip",
                            "install",
                            *install_options,
                            *constraint_args,
                            *self.spec.requirements,
                        ],
                        check=True,
                        cwd=self.source_dir,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )

                except subprocess.CalledProcessError as exc:
                    detail = "\n".join(
                        part for part in (exc.stdout, exc.stderr) if part
                    )

                    if (
                        not self.build_constraints
                        and self.spec.name.startswith("setuptools.build_meta")
                        and (
                            "Cannot import 'setuptools.build_meta'" in detail
                            or "No matching distribution found for setuptools" in detail
                            or "Could not find a version that satisfies the requirement setuptools"
                            in detail
                        )
                    ) and (
                        importlib.util.find_spec("setuptools.build_meta")  # type: ignore
                        is not None
                    ):
                        with tempfile.TemporaryDirectory(
                            prefix="pip-build-metadata-",
                        ) as metadata_dir:
                            caller = BuildBackendHookCaller(
                                os.fspath(self.source_dir),
                                self.spec.name,
                                backend_path=list(self.spec.backend_path) or None,
                                python_executable=sys.executable,
                            )

                            with backend_environment(self.source_dir):
                                yield caller, metadata_dir

                        return

                    raise RuntimeError(detail or str(exc)) from exc

            caller = BuildBackendHookCaller(
                os.fspath(self.source_dir),
                self.spec.name,
                backend_path=list(self.spec.backend_path) or None,
                python_executable=python,
            )

            with backend_environment(self.source_dir):
                yield caller, env_path


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return ProjectBuilder(os.getcwd()).build_wheel(
        wheel_directory,
        config_settings=config_settings,
    )


def get_requires_for_build_wheel(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return []


def get_requires_for_build_sdist(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return []


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    project = ProjectMetadataReader(os.getcwd()).read()

    dist_info = f"{wheel_distribution(project.name)}-{project.version}.dist-info"

    target = os.path.join(metadata_directory, dist_info)

    os.makedirs(target, exist_ok=True)

    with open(os.path.join(target, "METADATA"), "w", encoding="utf-8") as file:
        file.write(metadata_text(project))

    with open(os.path.join(target, "WHEEL"), "w", encoding="utf-8") as file:
        file.write(wheel_text_internal())

    for relative, contents in project_license_files(project, os.getcwd()):
        target_path = os.path.join(target, "licenses", *relative.split("/"))
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "wb") as file:
            file.write(contents)

    return dist_info


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    return ProjectBuilder(os.getcwd()).build_editable(
        wheel_directory,
        config_settings=config_settings,
    )


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    del config_settings

    source_dir = os.getcwd()

    project = ProjectMetadataReader(source_dir).read()

    sdist_name = f"{wheel_distribution(project.name)}-{project.version}.tar.gz"

    sdist_path = os.path.join(sdist_directory, sdist_name)

    os.makedirs(sdist_directory, exist_ok=True)

    root_name = sdist_name.removesuffix(".tar.gz")

    with tarfile.open(sdist_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        pkg_info = metadata_text(project, for_sdist=True).encode("utf-8")
        pkg_info_entry = tarfile.TarInfo(f"{root_name}/PKG-INFO")
        pkg_info_entry.mode = 0o644
        pkg_info_entry.size = len(pkg_info)
        archive.addfile(pkg_info_entry, io.BytesIO(pkg_info))

        excluded = sdist_exclude_patterns(source_dir)
        for child, relative in iter_sdist_files(source_dir, excluded=excluded):
            if relative == "PKG-INFO":
                continue
            archive.add(child, arcname=f"{root_name}/{relative}")

    return sdist_name


def sdist_exclude_patterns(source_dir: str) -> tuple[str, ...]:
    """Read project-specific source distribution exclusions."""
    try:
        with open(os.path.join(source_dir, "pyproject.toml"), encoding="utf-8") as file:
            data = loads(file.read())
    except OSError:
        return ()

    tool = data.get("tool", {})
    kpip = tool.get("kpip", {}) if isinstance(tool, dict) else {}
    backend = kpip.get("build-backend", {}) if isinstance(kpip, dict) else {}
    patterns = backend.get("sdist-exclude", []) if isinstance(backend, dict) else []
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) and pattern for pattern in patterns
    ):
        raise BuildError(
            "tool.kpip.build-backend.sdist-exclude must be a list of strings",
        )
    return tuple(patterns)


def _sdist_path_is_excluded(relative: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        directory = pattern.rstrip("/")
        if (
            relative == directory
            or relative.startswith(directory + "/")
            or fnmatch.fnmatchcase(relative, pattern)
        ):
            return True
    return False


def iter_sdist_files(
    source_dir: str,
    *,
    excluded: tuple[str, ...] = (),
) -> Iterable[tuple[str, str]]:
    """Yield source files while honoring the repository's ignore rules."""

    try:
        tracked = subprocess.run(
            [
                "git",
                "-C",
                source_dir,
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        tracked = None

    if tracked is not None and tracked.returncode == 0:
        relative_paths = sorted(
            {os.fsdecode(path) for path in tracked.stdout.split(b"\0") if path},
        )
        for relative in relative_paths:
            relative = relative.replace(os.sep, "/")
            if _sdist_path_is_excluded(relative, excluded):
                continue
            child = os.path.join(source_dir, relative)
            if os.path.lexists(child):
                yield child, relative
        return

    excluded_directories = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "benchmark-runs",
        "build",
        "dist",
    }

    for current, directories, files in os.walk(
        source_dir,
        topdown=True,
        followlinks=False,
    ):
        directories[:] = sorted(
            name
            for name in directories
            if name not in excluded_directories and not name.endswith(".egg-info")
        )
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            child = os.path.join(current, name)
            relative = os.path.relpath(child, source_dir).replace(os.sep, "/")
            if _sdist_path_is_excluded(relative, excluded):
                continue
            yield child, relative


def get_requires_for_build_editable(
    config_settings: dict[str, Any] | None = None,
) -> list[str]:
    return []


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings: dict[str, Any] | None = None,
) -> str:
    return prepare_metadata_for_build_wheel(metadata_directory, config_settings)


class ProjectBuilder:
    """Build a project through its declared backend or pip's fallback backend."""

    def __init__(
        self,
        source_dir: str | os.PathLike[str],
        *,
        build_constraints: list[str] | None = None,
        build_isolation: bool = True,
    ) -> None:
        self.source_dir = os.fspath(source_dir)

        self.build_constraints = build_constraints

        self.build_isolation = build_isolation

        self.backend_spec = BackendSpec.from_project(source_dir)

    def build_wheel(
        self,
        wheel_directory: str | os.PathLike[str],
        *,
        config_settings: dict[str, Any] | None = None,
    ) -> str:
        if self.backend_spec is not None:
            return self.build_external(
                wheel_directory,
                config_settings=config_settings,
                validate_metadata_first=True,
            )

        return self.build_fallback_wheel(wheel_directory, editable=False)

    def build_editable(
        self,
        wheel_directory: str | os.PathLike[str],
        *,
        config_settings: dict[str, Any] | None = None,
    ) -> str:
        """Build an editable wheel through the backend's PEP 660 hook.

        Every declared backend gets ``build_editable``, setuptools included.
        A backend too old to implement it raises rather than falling back to
        ``build_wheel``, whose output installs as a copy: source edits would
        have no effect while the direct_url.json beside it still recorded
        ``"editable": true``.
        """
        if self.backend_spec is not None:
            return self.build_external(
                wheel_directory,
                config_settings=config_settings,
                editable=True,
            )

        return self.build_fallback_wheel(wheel_directory, editable=True)

    def build_external(
        self,
        wheel_directory: str | os.PathLike[str],
        *,
        config_settings: dict[str, Any] | None,
        editable: bool = False,
        validate_metadata_first: bool = False,
    ) -> str:
        assert self.backend_spec is not None

        os.makedirs(os.fspath(wheel_directory), exist_ok=True)

        backend_name = self.backend_spec.name

        try:
            with (
                BackendRunner(
                    self.source_dir,
                    self.backend_spec,
                    build_constraints=self.build_constraints,
                    build_isolation=self.build_isolation,
                ).caller() as (caller, env_path),
                caller.subprocess_runner(call_subprocess),
            ):
                if (
                    validate_metadata_first
                    and not editable
                    and read_legacy_metadata(self.source_dir) is None
                ):
                    preflight_path = os.path.join(env_path, "metadata-preflight")

                    os.mkdir(preflight_path)

                    try:
                        caller.prepare_metadata_for_build_wheel(preflight_path)

                    except HookMissing:
                        pass

                if editable:
                    wheel_name = caller.build_editable(
                        os.fspath(wheel_directory),
                        config_settings=config_settings,
                    )

                else:
                    wheel_name = caller.build_wheel(
                        os.fspath(wheel_directory),
                        config_settings=config_settings,
                    )

        except HookMissing as exc:
            if editable:
                raise BuildHookMissing(
                    "Cannot build editable "
                    f"{self.source_dir} because the build backend is missing "
                    "the 'build_editable' hook",
                ) from exc

            raise BuildError(
                f"Build backend {backend_name} is missing the 'build_wheel' hook",
            ) from exc

        except Exception as exc:
            raise BuildError(
                f"Failed to build {self.source_dir} with {backend_name}: {exc}",
            ) from exc

        if not isinstance(wheel_name, str):
            raise BuildError(
                f"Build backend {backend_name} did not return a wheel filename",
            )

        return wheel_name

    def build_fallback_wheel(
        self,
        wheel_directory: str | os.PathLike[str],
        *,
        editable: bool,
    ) -> str:
        project = ProjectMetadataReader(self.source_dir).read()

        os.makedirs(os.fspath(wheel_directory), exist_ok=True)

        distribution = wheel_distribution(project.name)

        wheel_name = f"{distribution}-{project.version}-py3-none-any.whl"

        wheel_path = os.path.join(os.fspath(wheel_directory), wheel_name)

        dist_info = f"{distribution}-{project.version}.dist-info"

        records: list[tuple[str, bytes]] = []

        def write_file(archive: zipfile.ZipFile, path: str, data: bytes | str) -> None:
            raw = data.encode("utf-8") if isinstance(data, str) else data

            archive.writestr(path, raw)

            records.append((path, raw))

        with zipfile.ZipFile(
            wheel_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            if editable:
                source_text = os.fspath(self.source_dir)

                src_root_text = os.path.join(source_text, "src")

                import_root = (
                    src_root_text if os.path.isdir(src_root_text) else source_text
                )

                write_file(
                    archive,
                    f"__editable__.{distribution}.pth",
                    os.path.realpath(import_root) + "\n",
                )

            else:
                project_files = list(iter_project_files(self.source_dir))

                version_path = version_module_path(project.name, project_files)

                if version_path is not None:
                    project_files = [
                        (path, data)
                        for path, data in project_files
                        if path != version_path
                    ]

                for path, data in project_files:
                    write_file(archive, path, data)

                if version_path is not None:
                    write_file(
                        archive,
                        version_path,
                        f"__version__ = {project.version!r}\n",
                    )

            write_file(archive, f"{dist_info}/METADATA", metadata_text(project))

            write_file(archive, f"{dist_info}/WHEEL", wheel_text_internal())

            for relative, contents in project_license_files(
                project,
                self.source_dir,
            ):
                write_file(
                    archive,
                    f"{dist_info}/licenses/{relative}",
                    contents,
                )

            entry_points = entry_points_text_internal(project)

            if entry_points:
                write_file(archive, f"{dist_info}/entry_points.txt", entry_points)

            archive.writestr(
                f"{dist_info}/RECORD",
                record_text_internal(records, dist_info),
            )

        return wheel_name

    def prepare_metadata(
        self,
        *,
        editable: bool = False,
        on_wheel_built: Callable[[str], None] | None = None,
    ) -> ProjectMetadata:
        """Read metadata through the project's declared build backend.

        A backend without the optional ``prepare_metadata_for_build_wheel``/
        ``prepare_metadata_for_build_editable`` hook forces the fallback
        below to build a full wheel just to read its METADATA file back out
        -- real, complete build output that would otherwise be thrown away
        the moment its temporary directory closes. ``on_wheel_built``, when
        given, is called with that wheel's path while it still exists, so a
        caller that already knows this candidate might get built for real
        later (resolution, not a one-off metadata read) can persist it
        somewhere a later build can find.
        """

        # A backend is available below, so only metadata PEP 643 guarantees
        # matches the wheel it would build may stand in for running it.
        static_metadata = read_legacy_metadata(self.source_dir, require_static=True)

        if static_metadata is not None and not editable:
            return static_metadata

        reader = ProjectMetadataReader(self.source_dir)

        if not editable:
            # A fully static [project] table says what the backend would say,
            # so reading it beats standing up a build environment, installing
            # the backend's requirements and calling a hook to be told the
            # same thing -- once per candidate, per backtrack.
            declared = reader.read_static()

            if declared is not None:
                return declared

        if self.backend_spec is None:
            return reader.read()

        backend_name = self.backend_spec.name

        try:
            with BackendRunner(
                self.source_dir,
                self.backend_spec,
                build_constraints=self.build_constraints,
                build_isolation=self.build_isolation,
            ).caller() as (
                caller,
                env_path,
            ):
                metadata_path = os.path.join(env_path, "metadata")

                os.mkdir(metadata_path)

                metadata = None

                with caller.subprocess_runner(call_subprocess):
                    if editable:
                        try:
                            dist_info = caller.prepare_metadata_for_build_editable(
                                metadata_path,
                            )

                        except HookMissing:
                            with tempfile.TemporaryDirectory(
                                prefix="kpip-metadata-editable-",
                            ) as wheel_directory:
                                wheel_name = caller.build_editable(wheel_directory)

                                assert wheel_name is not None

                                wheel_path = os.path.join(wheel_directory, wheel_name)

                                if on_wheel_built is not None:
                                    on_wheel_built(wheel_path)

                                with zipfile.ZipFile(wheel_path) as wheel:
                                    metadata_name = next(
                                        name
                                        for name in wheel.namelist()
                                        if name.endswith(".dist-info/METADATA")
                                    )

                                    metadata = email.parser.BytesParser().parsebytes(
                                        wheel.read(metadata_name),
                                    )

                            dist_info = None

                    else:
                        try:
                            dist_info = caller.prepare_metadata_for_build_wheel(
                                metadata_path,
                            )

                        except HookMissing:
                            with tempfile.TemporaryDirectory(
                                prefix="kpip-metadata-wheel-",
                            ) as wheel_directory:
                                wheel_name = caller.build_wheel(wheel_directory)

                                assert wheel_name is not None

                                wheel_path = os.path.join(wheel_directory, wheel_name)

                                if on_wheel_built is not None:
                                    on_wheel_built(wheel_path)

                                with zipfile.ZipFile(wheel_path) as wheel:
                                    metadata_name = next(
                                        name
                                        for name in wheel.namelist()
                                        if name.endswith(".dist-info/METADATA")
                                    )

                                    metadata = email.parser.BytesParser().parsebytes(
                                        wheel.read(metadata_name),
                                    )

                            dist_info = None

                if not editable and dist_info is None and metadata is None:
                    with tempfile.TemporaryDirectory(
                        prefix="kpip-metadata-wheel-",
                    ) as wheel_directory:
                        wheel_name = caller.build_wheel(wheel_directory)

                        assert wheel_name is not None

                        wheel_path = os.path.join(wheel_directory, wheel_name)

                        if on_wheel_built is not None:
                            on_wheel_built(wheel_path)

                        with zipfile.ZipFile(wheel_path) as wheel:
                            metadata_name = next(
                                name
                                for name in wheel.namelist()
                                if name.endswith(".dist-info/METADATA")
                            )

                            metadata = email.parser.BytesParser().parsebytes(
                                wheel.read(metadata_name),
                            )

                if dist_info is not None:
                    metadata_file = os.path.join(metadata_path, dist_info, "METADATA")

                    with open(metadata_file, encoding="utf-8") as file:
                        metadata = email.parser.Parser().parsestr(file.read())

                if metadata is None:
                    raise BuildError("Build backend returned no metadata")

        except Exception as exc:
            raise BuildError(
                f"Failed to prepare metadata for {self.source_dir} with {backend_name}: {exc}",
            ) from exc

        name = metadata.get("Name")

        version = metadata.get("Version")

        if not name or not version:
            raise BuildError(
                f"Build backend {backend_name} returned incomplete metadata",
            )

        return ProjectMetadata(
            name=name,
            version=version,
            summary=metadata.get("Summary"),
            requires_python=metadata.get("Requires-Python"),
            dependencies=tuple(metadata.get_all("Requires-Dist", [])),
            optional_dependencies={},
            scripts={},
            provided_extras=frozenset(metadata.get_all("Provides-Extra", [])),
        )


def prepare_project_metadata(
    source_dir: str | os.PathLike[str],
    *,
    editable: bool = False,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
    on_wheel_built: Callable[[str], None] | None = None,
) -> ProjectMetadata:
    """Read metadata through the project's declared PEP 517 backend.

    See ``ProjectBuilder.prepare_metadata`` for ``on_wheel_built``.
    """

    try:
        return ProjectBuilder(
            source_dir,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        ).prepare_metadata(editable=editable, on_wheel_built=on_wheel_built)

    except BuildError as exc:
        if build_isolation and "Cannot import 'setuptools.build_meta'" in str(exc):
            return ProjectBuilder(
                source_dir,
                build_constraints=build_constraints,
                build_isolation=False,
            ).prepare_metadata(editable=editable, on_wheel_built=on_wheel_built)

        raise


@contextlib.contextmanager
def backend_environment(source_dir: str | os.PathLike[str]) -> Iterator[None]:
    cwd = os.getcwd()

    source = os.fspath(source_dir)

    old_pythonpath = os.environ.get("PYTHONPATH")

    pythonpath = [source]

    if old_pythonpath:
        pythonpath.append(old_pythonpath)

    os.chdir(source)

    os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath)

    try:
        yield

    finally:
        os.chdir(cwd)

        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)

        else:
            os.environ["PYTHONPATH"] = old_pythonpath


_PROJECT_METADATA_FIELDS = (
    "name",
    "version",
    "summary",
    "requires_python",
    "dependencies",
    "optional_dependencies",
    "scripts",
    "provided_extras",
    "license_expression",
    "license_files",
    "long_description",
    "description_content_type",
    "authors",
    "classifiers",
    "project_urls",
)


class ProjectMetadata:
    """A project's core metadata as read from its source or built artifact.

    Frozen and slotted, compared by value; ``version`` is stored in its
    canonical spelling.
    """

    __slots__ = _PROJECT_METADATA_FIELDS

    name: str
    version: str
    summary: str | None
    requires_python: str | None
    dependencies: tuple[str, ...]
    optional_dependencies: dict[str, tuple[str, ...]]
    scripts: dict[str, str]
    provided_extras: frozenset[str]
    license_expression: str | None
    license_files: tuple[str, ...]
    long_description: str | None
    description_content_type: str | None
    authors: tuple[tuple[str | None, str | None], ...]
    classifiers: tuple[str, ...]
    project_urls: tuple[tuple[str, str], ...]

    def __init__(
        self,
        name: str,
        version: str,
        summary: str | None,
        requires_python: str | None,
        dependencies: tuple[str, ...],
        optional_dependencies: dict[str, tuple[str, ...]],
        scripts: dict[str, str],
        provided_extras: frozenset[str] = frozenset(),
        license_expression: str | None = None,
        license_files: tuple[str, ...] = (),
        long_description: str | None = None,
        description_content_type: str | None = None,
        authors: tuple[tuple[str | None, str | None], ...] = (),
        classifiers: tuple[str, ...] = (),
        project_urls: tuple[tuple[str, str], ...] = (),
    ) -> None:
        store = object.__setattr__
        store(self, "name", name)
        store(self, "version", str(Version(version)))
        store(self, "summary", summary)
        store(self, "requires_python", requires_python)
        store(self, "dependencies", dependencies)
        store(self, "optional_dependencies", optional_dependencies)
        store(self, "scripts", scripts)
        store(self, "provided_extras", provided_extras)
        store(self, "license_expression", license_expression)
        store(self, "license_files", license_files)
        store(self, "long_description", long_description)
        store(self, "description_content_type", description_content_type)
        store(self, "authors", authors)
        store(self, "classifiers", classifiers)
        store(self, "project_urls", project_urls)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def _values(self) -> tuple[object, ...]:
        return tuple(getattr(self, name) for name in _PROJECT_METADATA_FIELDS)

    def __eq__(self, other: object) -> bool:
        return type(other) is ProjectMetadata and self._values() == other._values()

    def __hash__(self) -> int:
        return hash(self._values())

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in _PROJECT_METADATA_FIELDS
        )
        return f"ProjectMetadata({fields})"


def read_project_readme(
    source_dir: str | os.PathLike[str],
    configured: object,
) -> tuple[str | None, str | None]:
    """Read the PEP 621 project readme and determine its content type."""
    if configured is None:
        return None, None

    readme_path: str | None = None
    readme_text: str | None = None
    content_type: str | None = None

    if isinstance(configured, str):
        readme_path = configured
    elif isinstance(configured, dict):
        file_value = configured.get("file")
        text_value = configured.get("text")
        content_value = configured.get("content-type")
        if (
            (file_value is None) == (text_value is None)
            or (file_value is not None and not isinstance(file_value, str))
            or (text_value is not None and not isinstance(text_value, str))
            or (content_value is not None and not isinstance(content_value, str))
        ):
            raise BuildError("project.readme must specify exactly one of file or text")
        readme_path = file_value
        readme_text = text_value
        content_type = content_value
    else:
        raise BuildError("project.readme must be a path or a table")

    if readme_path is not None:
        source = os.path.realpath(os.fspath(source_dir))
        target = os.path.realpath(os.path.join(source, readme_path))
        try:
            contained = os.path.commonpath((source, target)) == source
        except ValueError:
            contained = False
        if not contained or not os.path.isfile(target):
            raise BuildError(
                f"Project readme is missing or outside the source tree: {readme_path!r}",
            )
        try:
            with open(target, encoding="utf-8") as file:
                readme_text = file.read()
        except OSError as exc:
            raise BuildError(
                f"Cannot read project readme {readme_path!r}: {exc}",
            ) from exc

        if content_type is None:
            extension = os.path.splitext(readme_path)[1].lower()
            content_type = {".md": "text/markdown", ".rst": "text/x-rst"}.get(
                extension,
            )

    if content_type is None:
        raise BuildError("project.readme requires a known extension or content-type")

    return readme_text, content_type


class ProjectMetadataReader:
    """Read project metadata from a source tree and its legacy fallbacks."""

    def __init__(self, source_dir: str | os.PathLike[str]) -> None:
        self.source_dir = os.fspath(source_dir)

    def _pyproject(self) -> dict | None:
        try:
            with open(
                os.path.join(self.source_dir, "pyproject.toml"),
                encoding="utf-8",
            ) as file:
                return loads(file.read())

        except OSError:
            return None

    def _read_project_table(
        self,
        data: dict,
        *,
        require_static: bool,
    ) -> ProjectMetadata | None:
        """The ``[project]`` table, or ``None`` if it cannot answer.

        ``require_static`` additionally rejects a table that declares any of
        the fields read here in ``project.dynamic``: the backend computes
        those at build time, so what is written here is not what the wheel
        will say. Callers that can run the backend pass it.
        """
        source_dir = self.source_dir

        project = data.get("project")

        if not isinstance(project, dict):
            return None

        name = project.get("name")

        version = project.get("version")

        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
        ):
            return None

        if require_static:
            declared = project.get("dynamic", [])

            if not isinstance(declared, list):
                return None

            if _DYNAMIC_BLOCKS_STATIC.intersection(
                value.lower() for value in declared if isinstance(value, str)
            ):
                return None

            try:
                Version(version)

            # A version this cannot parse is one the backend may still
            # normalize, so under require_static it means "cannot answer" and
            # the backend decides. Raising here would escape prepare_metadata,
            # where callers are waiting for BuildError to fall back on.
            except InvalidVersion:
                return None

        else:
            Version(version)

        dependencies = project.get("dependencies", [])

        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise BuildError(
                f"Cannot build {source_dir}: project.dependencies is invalid",
            )

        scripts = project.get("scripts", {})

        if not isinstance(scripts, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in scripts.items()
        ):
            raise BuildError(
                f"Cannot build {source_dir}: project.scripts is invalid",
            )

        summary = (
            project.get("description")
            if isinstance(project.get("description"), str)
            else None
        )

        requires_python = (
            project.get("requires-python")
            if isinstance(project.get("requires-python"), str)
            else None
        )

        license_expression = (
            project.get("license") if isinstance(project.get("license"), str) else None
        )

        license_files_raw = project.get("license-files", [])

        if not isinstance(license_files_raw, list) or not all(
            isinstance(item, str) for item in license_files_raw
        ):
            raise BuildError(
                f"Cannot build {source_dir}: project.license-files is invalid",
            )

        long_description, description_content_type = read_project_readme(
            source_dir,
            project.get("readme"),
        )

        authors_raw = project.get("authors", [])
        if not isinstance(authors_raw, list):
            raise BuildError(f"Cannot build {source_dir}: project.authors is invalid")
        authors: list[tuple[str | None, str | None]] = []
        for author in authors_raw:
            if not isinstance(author, dict):
                raise BuildError(
                    f"Cannot build {source_dir}: project.authors is invalid",
                )
            author_name = author.get("name")
            author_email = author.get("email")
            if (
                (author_name is not None and not isinstance(author_name, str))
                or (author_email is not None and not isinstance(author_email, str))
                or (author_name is None and author_email is None)
            ):
                raise BuildError(
                    f"Cannot build {source_dir}: project.authors is invalid",
                )
            authors.append((author_name, author_email))

        classifiers_raw = project.get("classifiers", [])
        if not isinstance(classifiers_raw, list) or not all(
            isinstance(classifier, str) for classifier in classifiers_raw
        ):
            raise BuildError(
                f"Cannot build {source_dir}: project.classifiers is invalid",
            )

        urls_raw = project.get("urls", {})
        if not isinstance(urls_raw, dict) or not all(
            isinstance(label, str) and isinstance(url, str)
            for label, url in urls_raw.items()
        ):
            raise BuildError(f"Cannot build {source_dir}: project.urls is invalid")

        optional_dependencies_raw = project.get("optional-dependencies", {})

        if not isinstance(optional_dependencies_raw, dict):
            raise BuildError(
                f"Cannot build {source_dir}: project.optional-dependencies is invalid",
            )

        optional_dependencies: dict[str, tuple[str, ...]] = {}

        for extra, values in optional_dependencies_raw.items():
            if (
                not isinstance(extra, str)
                or not isinstance(values, list)
                or not all(isinstance(item, str) for item in values)
            ):
                raise BuildError(
                    f"Cannot build {source_dir}: "
                    "project.optional-dependencies is invalid",
                )

            optional_dependencies[extra] = tuple(values)

        return ProjectMetadata(
            name=name,
            version=version,
            summary=summary,
            requires_python=requires_python,
            dependencies=tuple(dependencies),
            optional_dependencies=optional_dependencies,
            scripts=scripts,
            provided_extras=frozenset(optional_dependencies),
            license_expression=license_expression,
            license_files=tuple(license_files_raw),
            long_description=long_description,
            description_content_type=description_content_type,
            authors=tuple(authors),
            classifiers=tuple(classifiers_raw),
            project_urls=tuple(urls_raw.items()),
        )

    def read_static(self) -> ProjectMetadata | None:
        """``[project]`` metadata a backend cannot disagree with, if any."""
        data = self._pyproject()

        return (
            None
            if data is None
            else self._read_project_table(
                data,
                require_static=True,
            )
        )

    def read(self) -> ProjectMetadata:
        source_dir = self.source_dir

        source_text = os.fspath(source_dir)

        data = self._pyproject()

        if data is not None:
            static = self._read_project_table(data, require_static=False)

            if static is not None:
                return static

            setup_py_path = os.path.join(source_text, "setup.py")

            if not os.path.isfile(setup_py_path):
                metadata = read_legacy_metadata(source_dir)

                if metadata is not None:
                    return metadata

                metadata = read_setup_cfg_metadata(source_dir)

                if metadata is not None:
                    return metadata

                metadata = infer_metadata_from_package_dir(source_dir)

                if metadata is not None:
                    return metadata

                project = data.get("project")

                if isinstance(project, dict):
                    if not isinstance(project.get("name"), str) or not project.get(
                        "name",
                    ):
                        raise BuildError(
                            f"Cannot build {source_dir}: missing project.name",
                        )

                    raise BuildError(
                        f"Cannot build {source_dir}: missing project.version",
                    )

                raise BuildError(
                    f"Cannot build {source_dir}: missing [project] metadata",
                )

            setup_cfg_metadata = read_setup_cfg_metadata(source_dir)

            if setup_cfg_metadata is not None:
                return setup_cfg_metadata

            raise BuildError(
                f"Cannot read metadata for {source_dir}: use the project's build backend",
            )

        setup_py_path = os.path.join(source_text, "setup.py")

        if not os.path.isfile(setup_py_path):
            metadata = read_legacy_metadata(source_dir)

            if metadata is not None:
                return metadata

            metadata = read_setup_cfg_metadata(source_dir)

            if metadata is not None:
                return metadata

            metadata = infer_metadata_from_package_dir(source_dir)

            if metadata is not None:
                return metadata

            raise BuildError(f"Cannot build {source_dir}: missing pyproject.toml")

        setup_cfg_metadata = read_setup_cfg_metadata(source_dir)

        if setup_cfg_metadata is not None:
            return setup_cfg_metadata

        raise BuildError(
            f"Cannot read metadata for {source_dir}: use the project's build backend",
        )


def read_setup_cfg_metadata(
    source_dir: str | os.PathLike[str],
) -> ProjectMetadata | None:
    setup_cfg = os.path.join(os.fspath(source_dir), "setup.cfg")

    parser = configparser.ConfigParser()

    try:
        with open(setup_cfg, encoding="utf-8") as file:
            parser.read_file(file)

    except (OSError, configparser.Error):
        return None

    if not parser.has_section("metadata"):
        return None

    name = parser.get("metadata", "name", fallback="").strip()

    version = parser.get("metadata", "version", fallback="").strip()

    if not name or not version:
        return None

    try:
        Version(version)

    except InvalidVersion:
        return None

    summary = parser.get("metadata", "description", fallback="").strip() or None

    requires_python = (
        parser.get("options", "python_requires", fallback="").strip() or None
    )

    dependencies = setup_cfg_install_requires(parser)

    return ProjectMetadata(
        name=name,
        version=version,
        summary=summary,
        requires_python=requires_python,
        dependencies=dependencies,
        optional_dependencies={},
        scripts={},
    )


def setup_cfg_install_requires(
    parser: configparser.ConfigParser,
) -> tuple[str, ...]:
    if not parser.has_option("options", "install_requires"):
        return ()

    raw = parser.get("options", "install_requires")

    dependencies: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        dependencies.append(stripped)

    return tuple(dependencies)


def infer_metadata_from_package_dir(
    source_dir: str | os.PathLike[str],
) -> ProjectMetadata | None:
    source_text = os.fspath(source_dir)

    roots: list[str] = []

    src_root = os.path.join(source_text, "src")

    if os.path.isdir(src_root):
        roots.append(src_root)

    roots.append(source_text)

    for root in roots:
        with os.scandir(root) as entries:
            package_entries = sorted(entries, key=lambda entry: entry.name)

        for entry in package_entries:
            if not entry.is_dir():
                continue

            init_py = os.path.join(entry.path, "__init__.py")

            try:
                with open(init_py, encoding="utf-8", errors="replace") as file:
                    text = file.read()

            except OSError:
                continue

            match = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", text)

            if match is None:
                continue

            try:
                Version(match.group(1))

            except InvalidVersion:
                continue

            return ProjectMetadata(
                name=entry.name,
                version=match.group(1),
                summary=None,
                requires_python=None,
                dependencies=(),
                optional_dependencies={},
                scripts={},
            )

    return None


_STATIC_GUARANTEED_FROM = (2, 2)
"""First core metadata version whose fields are guaranteed static.

PEP 643. Before it, a source distribution's ``PKG-INFO`` recorded whatever
the author's machine produced and a backend was free to compute something
else at build time, so it cannot be used to answer what a wheel built from
that sdist will require.
"""

_MAY_NOT_BE_DYNAMIC = ("requires-dist", "provides-extra", "requires-python")
"""Fields this reader reports, and so must find declared static."""

_DYNAMIC_BLOCKS_STATIC = frozenset(
    {"version", "dependencies", "optional-dependencies", "requires-python"},
)
"""``project.dynamic`` entries that make the ``[project]`` table unusable.

Declaring one of these hands the field to the build backend, so what the
table says about it -- including saying nothing -- is not what the built
wheel will say."""


def _metadata_version(fields: dict[str, list[str]]) -> tuple[int, ...]:
    raw = fields.get("Metadata-Version", [""])[0]

    try:
        return tuple(int(part) for part in raw.strip().split("."))

    except ValueError:
        return ()


def _fields_are_static(fields: dict[str, list[str]]) -> bool:
    """Whether PEP 643 guarantees this metadata matches the built wheel."""
    if _metadata_version(fields) < _STATIC_GUARANTEED_FROM:
        return False

    dynamic = {value.strip().lower() for value in fields.get("Dynamic", [])}

    return not dynamic.intersection(_MAY_NOT_BE_DYNAMIC)


def read_legacy_metadata(
    source_dir: str | os.PathLike[str],
    *,
    require_static: bool = False,
) -> ProjectMetadata | None:
    """Metadata already written into a source tree, without building it.

    ``require_static`` rejects anything PEP 643 does not guarantee: metadata
    older than 2.2, or declaring one of the fields read here in ``Dynamic``.
    Callers that can fall back to running the build backend pass it, because
    a wrong dependency set is worse than a slow one. Callers with no backend
    to fall back to leave it off and take the best answer available.

    Metadata inside a ``.dist-info`` is exempt: that is a built wheel's own
    metadata, which describes something already built rather than predicting
    it.
    """
    source_text = os.fspath(source_dir)

    with os.scandir(source_text) as entries:
        egg_info_candidates = []

        dist_info_candidates = []

        for entry in entries:
            if not entry.is_dir():
                continue

            if entry.name.endswith(".egg-info"):
                egg_info_candidates.append(os.path.join(entry.path, "PKG-INFO"))

            elif entry.name.endswith(".dist-info"):
                dist_info_candidates.append(os.path.join(entry.path, "METADATA"))

    candidates = (
        sorted(egg_info_candidates)
        + [
            os.path.join(source_text, "METADATA"),
            os.path.join(source_text, "PKG-INFO"),
        ]
        + sorted(dist_info_candidates)
    )

    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8", errors="replace") as file:
                lines = file.read().splitlines()

        except OSError:
            continue

        fields: dict[str, list[str]] = {}

        current_key: str | None = None

        for line in lines:
            if not line.strip():
                current_key = None

                continue

            if line[:1].isspace() and current_key is not None:
                fields[current_key][-1] += " " + line.strip()

                continue

            if ":" not in line:
                continue

            current_key, value = line.split(":", 1)

            values = fields.get(current_key)
            if values is None:
                values = []
                fields[current_key] = values
            values.append(value.strip())

        name = fields.get("Name", [None])[0]

        version = fields.get("Version", [None])[0]

        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
        ):
            continue

        Version(version)

        if (
            require_static
            and not os.path.basename(os.path.dirname(candidate)).endswith(".dist-info")
            and not _fields_are_static(fields)
        ):
            continue

        summary = fields.get("Summary", [None])[0]

        requires_python = fields.get("Requires-Python", [None])[0]

        dependencies = fields.get("Requires-Dist", [])

        if not dependencies and os.path.basename(os.path.dirname(candidate)).endswith(
            ".egg-info",
        ):
            requires_path = os.path.join(os.path.dirname(candidate), "requires.txt")

            try:
                dependencies = _read_legacy_requirements(requires_path)

            except OSError:
                pass

        return ProjectMetadata(
            name=name,
            version=version,
            summary=summary if isinstance(summary, str) else None,
            requires_python=(
                requires_python if isinstance(requires_python, str) else None
            ),
            dependencies=tuple(dependencies),
            optional_dependencies={},
            scripts={},
            provided_extras=frozenset(
                value for value in fields.get("Provides-Extra", []) if value
            ),
        )

    return None


def _read_legacy_requirements(path: str) -> list[str]:
    """Read setuptools' legacy ``requires.txt`` format."""

    dependencies: list[str] = []

    extra: str | None = None

    with open(path, encoding="utf-8", errors="replace") as file:
        lines = file.read().splitlines()

    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("[") and line.endswith("]"):
            extra = line[1:-1].strip() or None

            continue

        if line.startswith("-"):
            continue

        dependencies.append(
            f'{line}; extra == "{extra}"' if extra is not None else line,
        )

    return dependencies


def iter_project_files(
    source_dir: str | os.PathLike[str],
) -> Iterable[tuple[str, bytes]]:
    source_text = os.fspath(source_dir)

    src_root = os.path.join(source_text, "src")

    if os.path.isdir(src_root):
        yield from iter_package_files(src_root)

        return

    with os.scandir(source_text) as entries:
        children = sorted(entries, key=lambda entry: entry.name)

    for entry in children:
        if entry.name.startswith("."):
            continue

        if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "__init__.py")):
            yield from iter_package_files(source_text, root=entry.path)

        elif entry.name.endswith(".py") and entry.name != "setup.py":
            try:
                with open(entry.path, "rb") as file:
                    contents = file.read()

            except OSError:
                continue

            yield entry.name, contents


def version_module_path(
    project_name: str,
    project_files: list[tuple[str, bytes]],
) -> str | None:
    package_names = sorted(
        {
            path.split("/", 1)[0]
            for path, _ in project_files
            if "/" in path and path.endswith("/__init__.py")
        },
    )

    if not package_names:
        return None

    expected_name = project_name.replace("-", "_")

    package_name = expected_name if expected_name in package_names else package_names[0]

    return f"{package_name}/_version.py"


def iter_package_files(
    base: str,
    *,
    root: str | None = None,
) -> Iterable[tuple[str, bytes]]:
    base_text = os.fspath(base)

    search_root_text = os.fspath(root) if root is not None else base_text

    normalized_base = os.path.normpath(base_text)

    project_root_text = (
        os.path.dirname(normalized_base)
        if os.path.basename(normalized_base) == "src"
        else normalized_base
    )

    project_root_real = os.path.realpath(project_root_text)

    for current, directories, files in os.walk(
        search_root_text,
        topdown=True,
        followlinks=False,
    ):
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".") and name != "__pycache__"
        )

        for name in sorted(files):
            path = os.path.join(current, name)

            if not _is_package_payload_text(path, project_root_text, project_root_real):
                continue

            relative = os.path.relpath(path, base_text)

            try:
                with open(path, "rb") as file:
                    contents = file.read()

            except OSError:
                continue

            yield relative.replace(os.sep, "/"), contents


def _is_package_payload_text(
    path: str,
    project_root: str,
    project_root_real: str | None = None,
) -> bool:
    relative_path = os.path.relpath(path, project_root)

    relative_parts = relative_path.split(os.sep)

    if any(part.startswith(".") for part in relative_parts):
        return False

    if "__pycache__" in relative_parts or os.path.splitext(path)[1] in {
        ".pyc",
        ".pyo",
    }:
        return False

    if not os.path.islink(path):
        return True

    try:
        resolved = os.path.realpath(path)

        if project_root_real is None:
            project_root_real = os.path.realpath(project_root)

        return (
            os.path.exists(resolved)
            and os.path.commonpath(
                (resolved, project_root_real),
            )
            == project_root_real
        )

    except (OSError, ValueError):
        return False


def metadata_text(project: ProjectMetadata, *, for_sdist: bool = False) -> str:
    lines = [
        "Metadata-Version: 2.4"
        if project.license_expression or project.license_files
        else f"Metadata-Version: {'2.2' if for_sdist else '2.1'}",
        f"Name: {project.name}",
        f"Version: {project.version}",
    ]

    if project.license_expression:
        lines.append(f"License-Expression: {project.license_expression}")

    lines.extend(f"License-File: {path}" for path in project.license_files)

    if project.summary:
        lines.append(f"Summary: {project.summary}")

    author_names = [
        name for name, author_email in project.authors if name and not author_email
    ]
    if author_names:
        lines.append(f"Author: {', '.join(author_names)}")

    author_emails = [
        email.utils.formataddr((name or "", address))
        for name, address in project.authors
        if address
    ]
    if author_emails:
        lines.append(f"Author-email: {', '.join(author_emails)}")

    lines.extend(f"Classifier: {classifier}" for classifier in project.classifiers)
    lines.extend(f"Project-URL: {label}, {url}" for label, url in project.project_urls)

    if project.description_content_type:
        lines.append(f"Description-Content-Type: {project.description_content_type}")

    if project.requires_python:
        lines.append(f"Requires-Python: {project.requires_python}")

    for dependency in project.dependencies:
        lines.append(f"Requires-Dist: {dependency}")

    for extra, dependencies in sorted(project.optional_dependencies.items()):
        lines.append(f"Provides-Extra: {extra}")

        for dependency in dependencies:
            if "; " in dependency:
                requirement, marker = dependency.split("; ", 1)

                lines.append(
                    f'Requires-Dist: {requirement}; ({marker}) and extra == "{extra}"',
                )

            else:
                lines.append(f'Requires-Dist: {dependency}; extra == "{extra}"')

    headers = "\n".join(lines) + "\n\n"
    if project.long_description is None:
        return headers
    return headers + project.long_description.rstrip("\n") + "\n"


def project_license_files(
    project: ProjectMetadata,
    source_dir: str | os.PathLike[str],
) -> tuple[tuple[str, bytes], ...]:
    """Read validated PEP 639 license files from a project source tree."""
    source = os.path.realpath(os.fspath(source_dir))
    result: list[tuple[str, bytes]] = []
    for configured_path in project.license_files:
        normalized = configured_path.replace("\\", "/")
        parts = normalized.split("/")
        if (
            not normalized
            or os.path.isabs(configured_path)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise BuildError(f"Invalid project license file path: {configured_path!r}")
        path = os.path.realpath(os.path.join(source, *parts))
        try:
            contained = os.path.commonpath((source, path)) == source
        except ValueError:
            contained = False
        if not contained or not os.path.isfile(path):
            raise BuildError(
                f"Project license file is missing or outside the source tree: "
                f"{configured_path!r}",
            )
        try:
            with open(path, "rb") as file:
                contents = file.read()
        except OSError as exc:
            raise BuildError(
                f"Cannot read project license file {configured_path!r}: {exc}",
            ) from exc
        result.append((normalized, contents))
    return tuple(result)


def wheel_text_internal() -> str:
    return "Wheel-Version: 1.0\nGenerator: pip-core\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def entry_points_text_internal(project: ProjectMetadata) -> str:
    scripts = dict(project.scripts)

    if project.name == "pip" and not scripts:
        scripts = {"pip": "kpip.cli.main:main"}

    if not scripts:
        return ""

    lines = ["[console_scripts]"]

    lines.extend(f"{name} = {target}" for name, target in sorted(scripts.items()))

    return "\n".join(lines) + "\n"


def record_text_internal(records: list[tuple[str, bytes]], dist_info: str) -> str:
    rows: list[tuple[str, str, str]] = []

    for path, data in records:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")

        rows.append((path, f"sha256={digest.decode('ascii')}", str(len(data))))

    rows.append((f"{dist_info}/RECORD", "", ""))

    output = io.StringIO()

    csv.writer(output, lineterminator="\n").writerows(rows)

    return output.getvalue()


wheel_pattern = re.compile(r"[^A-Za-z0-9_.]+")


def wheel_distribution(name: str) -> str:
    normalized = canonicalize_name(name).replace("-", "_")
    return re.sub(wheel_pattern, "_", normalized)
