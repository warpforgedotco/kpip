"""An isolated build environment is filled from the command's indexes."""

from __future__ import annotations

import textwrap
from pathlib import Path

from kpip_test_support import KpipTestEnvironment
from tests.wheel_helpers import make_wheel

BACKEND = textwrap.dedent(
    """\
    import base64
    import hashlib
    import os
    import zipfile

    import kpip_private_build_helper

    DIST_INFO = "index_built-1.0.dist-info"
    FILES = {
        DIST_INFO + "/METADATA": "Metadata-Version: 2.1\\nName: index-built\\nVersion: 1.0\\n",
        DIST_INFO + "/WHEEL": (
            "Wheel-Version: 1.0\\nGenerator: test\\nRoot-Is-Purelib: true\\n"
            "Tag: py3-none-any\\n"
        ),
        "index_built.py": "HELPER = " + repr(kpip_private_build_helper.NAME) + "\\n",
    }


    def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
        os.makedirs(os.path.join(metadata_directory, DIST_INFO))
        for name in ("METADATA", "WHEEL"):
            with open(os.path.join(metadata_directory, DIST_INFO, name), "w") as file:
                file.write(FILES[DIST_INFO + "/" + name])
        return DIST_INFO


    def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
        name = "index_built-1.0-py3-none-any.whl"
        rows = []
        with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as wheel:
            for path, text in FILES.items():
                data = text.encode()
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                rows.append(f"{path},sha256={digest.rstrip(b'=').decode()},{len(data)}")
                wheel.writestr(path, data)
            rows.append(DIST_INFO + "/RECORD,,")
            wheel.writestr(DIST_INFO + "/RECORD", "\\n".join(rows) + "\\n")
        return name
    """
)


def test_build_requirements_come_from_the_commands_index(
    script: KpipTestEnvironment, tmp_path: Path
) -> None:
    """A build requirement only a private index has is found there: the kpip
    filling the build environment is given --index-url, as pip's is, rather
    than going to pypi.org for it."""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel = make_wheel(
        wheels, "kpip-private-build-helper", "kpip_private_build_helper", "1.0"
    )
    project_page = tmp_path / "index" / "kpip-private-build-helper"
    project_page.mkdir(parents=True)
    project_page.joinpath("index.html").write_text(
        f'<!DOCTYPE html><html><body><a href="{wheel.as_uri()}">{wheel.name}</a>'
        "</body></html>\n",
        encoding="utf-8",
    )

    project = tmp_path / "index-built"
    (project / "backend").mkdir(parents=True)
    project.joinpath("pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["kpip-private-build-helper"]\n'
        'build-backend = "index_backend"\n'
        'backend-path = ["backend"]\n',
        encoding="utf-8",
    )
    project.joinpath("backend", "index_backend.py").write_text(
        BACKEND, encoding="utf-8"
    )

    script.kpip(
        "install",
        "--no-cache-dir",
        "--index-url",
        (tmp_path / "index").as_uri(),
        str(project),
    )

    installed = script.site_packages_path / "index_built.py"
    assert "kpip-private-build-helper" in installed.read_text(encoding="utf-8")
