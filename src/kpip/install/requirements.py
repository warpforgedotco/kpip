"""Installation orchestration for prepared requirements."""

from __future__ import annotations

import logging

from kpip.core.names import canonicalize_name
from kpip.install.target import InstallTarget
from kpip.install.uninstall import DistributionUninstaller
from kpip.install.wheel_transaction import (
    WheelInstaller,
)
from kpip.resolution.req_install import InstallRequirement

logger = logging.getLogger(__name__)


class RequirementInstaller:
    """Install and remove prepared requirements in one target configuration."""

    def __init__(
        self,
        *,
        root: str | None = None,
        home: str | None = None,
        prefix: str | None = None,
        use_user_site: bool = False,
        pycompile: bool = True,
        script_executable: str | None = None,
    ) -> None:
        self.root = root
        self.home = home
        self.prefix = prefix
        self.use_user_site = use_user_site
        self.pycompile = pycompile
        self.script_executable = script_executable
        self.uninstaller_internal = DistributionUninstaller()

    def uninstall(self, name: str, *, paths: list[str] | None = None) -> bool:
        """Uninstall an installed distribution using its recorded files."""
        uninstaller = (
            self.uninstaller_internal
            if paths is None
            else DistributionUninstaller(paths)
        )
        return uninstaller.uninstall(name)

    def install(self, requirement: InstallRequirement) -> None:
        req = requirement.req
        local_file_path = requirement.local_file_path
        if req is None or not requirement.is_wheel or local_file_path is None:
            raise RuntimeError(f"Cannot install unprepared requirement {requirement}")
        target = InstallTarget.from_options(
            canonicalize_name(req.name),
            target=None,
            user=self.use_user_site,
            home=self.home,
            root=self.root,
            prefix=self.prefix,
            isolated=requirement.isolated,
        )
        WheelInstaller(
            target,
            pycompile=self.pycompile,
            force=requirement.should_reinstall,
            script_executable=self.script_executable,
        ).install(
            local_file_path,
            requested=requirement.user_supplied,
            direct_url=requirement.download_info if requirement.is_direct else None,
        )
        requirement.install_succeeded = True
