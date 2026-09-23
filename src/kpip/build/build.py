from __future__ import annotations

import atexit
import os

from kpip.core.errors import BuildError


def build_wheel_from_source(
    source: str,
    wheel_dir: str | None = None,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> str:
    from .build_backend import ProjectBuilder

    source_text = os.fspath(source)
    output_text = (
        os.fspath(wheel_dir) if wheel_dir is not None else default_wheel_dir_internal()
    )
    os.makedirs(output_text, exist_ok=True)
    source_is_dir = os.path.isdir(source_text)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="pip-build-") as temp_dir:
        project = (
            source_text
            if source_is_dir
            else unpack_source_internal(source_text, temp_dir)
        )
        if source_is_dir:
            os.makedirs(os.path.join(project, "build"), exist_ok=True)
        wheel_name = ProjectBuilder(
            project,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        ).build_wheel(output_text, config_settings=config_settings)
    wheel_path = os.path.join(output_text, wheel_name)
    if not os.path.isfile(wheel_path):
        raise BuildError(f"Build backend did not create expected wheel: {wheel_name}")
    return wheel_path


def build_editable_from_source(
    source: str,
    wheel_dir: str | None = None,
    config_settings: dict[str, object] | None = None,
    build_constraints: list[str] | None = None,
    build_isolation: bool = True,
) -> str:
    from .build_backend import BuildHookMissing, ProjectBuilder

    source_text = os.fspath(source)
    output_text = (
        os.fspath(wheel_dir) if wheel_dir is not None else default_wheel_dir_internal()
    )
    os.makedirs(output_text, exist_ok=True)
    source_is_dir = os.path.isdir(source_text)

    import tempfile

    with tempfile.TemporaryDirectory(prefix="pip-build-editable-") as temp_dir:
        project = (
            source_text
            if source_is_dir
            else unpack_source_internal(source_text, temp_dir)
        )
        builder = ProjectBuilder(
            project,
            build_constraints=build_constraints,
            build_isolation=build_isolation,
        )
        try:
            wheel_name = builder.build_editable(
                output_text,
                config_settings=config_settings,
            )
        except BuildHookMissing as exc:
            if os.path.isfile(os.path.join(source_text, "setup.py")) and os.path.isfile(
                os.path.join(source_text, "pyproject.toml"),
            ):
                return build_wheel_from_source(
                    source_text,
                    wheel_dir=output_text,
                    config_settings=config_settings,
                    build_constraints=build_constraints,
                    build_isolation=build_isolation,
                )
            raise BuildError(
                f"Build backend for {source_text} is missing the 'build_editable' hook",
            ) from exc
    wheel_path = os.path.join(output_text, wheel_name)
    if not os.path.isfile(wheel_path):
        raise BuildError(f"Build backend did not create expected wheel: {wheel_name}")
    return wheel_path


def default_wheel_dir() -> str:
    return default_wheel_dir_internal()


def default_wheel_dir_internal() -> str:
    import shutil
    import tempfile

    path = tempfile.mkdtemp(prefix="pip-build-wheelhouse-")
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def unpack_source(source: str, destination: str) -> str:
    return unpack_source_internal(source, destination)


def unpack_source_internal(source: str, destination: str) -> str:
    from kpip.core.errors import InstallationError
    from kpip.platform.unpacking import ArchiveExtractor

    source_text = os.fspath(source)
    destination_text = os.fspath(destination)
    try:
        ArchiveExtractor(source_text, destination_text, flatten=False).extract()
    except InstallationError as exc:
        raise BuildError(f"Cannot unpack source archive {source_text}: {exc}") from exc
    return single_project_root_internal(destination)


def single_project_root_internal(destination: str) -> str:
    destination_text = os.fspath(destination)
    with os.scandir(destination_text) as entries:
        children = [entry.path for entry in entries if entry.name != "__MACOSX"]
    if len(children) == 1 and os.path.isdir(children[0]):
        return children[0]
    project = os.path.join(destination_text, "project")
    os.mkdir(project)

    import shutil

    for child in children:
        shutil.move(child, os.path.join(project, os.path.basename(child)))
    return project
