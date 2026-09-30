"""Implementation of the ``kpip inspect`` subcommand."""

from __future__ import annotations

lazy import json
lazy import site

lazy from kpip.build.metadata import InstalledDistributionStore
lazy from kpip.cli.parsers.inspection import create_inspect_parser
lazy from kpip.core import kpip_version, packaging, urls
lazy from kpip.core.metadata import stdlib_pkgs


def run_inspect(args: list[str]) -> int:
    options = create_inspect_parser().parse_args(args)

    distributions = InstalledDistributionStore(
        paths=options.path or None,
        user_site=site.getusersitepackages(),
    ).iter(
        local_only=options.local,
        user_only=options.user,
        skip=set(stdlib_pkgs),
    )

    installed = []
    for dist in distributions:
        item: dict[str, object] = {
            "metadata": dist.metadata_dict,
            "metadata_location": dist.info_location,
        }

        direct_url = dist.direct_url
        if direct_url is not None:
            item["direct_url"] = direct_url.to_dict_compat()
        elif (location := dist.editable_project_location) is not None:
            item["direct_url"] = {
                "url": urls.path_to_url(location),
                "dir_info": {"editable": True},
            }

        if dist.installer:
            item["installer"] = dist.installer

        if dist.installed_with_dist_info:
            item["requested"] = dist.requested

        installed.append(item)

    print(
        json.dumps(
            {
                "version": "1",
                "kpip_version": kpip_version.get_kpip_version(),
                "installed": installed,
                "environment": packaging.default_environment(),
            },
        ),
    )

    return 0
