"""Nuitka user plugin: compile kpip's C modules into the binary.

kpip-compile passes this file to Nuitka with ``--user-plugin``; Nuitka loads
it, not kpip-compile, so it imports nothing of kpip_compile's.

Each source in ``NATIVE_SOURCES`` is handed to Nuitka as an extra code file,
which it compiles and links into the binary with its own compiler, LTO and
profile, on every platform. With ``KPIP_LINK_TREE_BUILTIN`` defined, a
source registers its module as a built-in before the interpreter starts (see
``src/kpip/_acceleration/_kpip_link_tree.c``), so kpip imports it by name and
no extension file is shipped or loaded.
"""

from __future__ import annotations

import sys
from pathlib import Path

from nuitka.plugins.PluginBase import NuitkaPluginBase

REPO_ROOT = Path(__file__).resolve().parents[4]

NATIVE_SOURCES = {
    "_kpip_link_tree.c": (
        REPO_ROOT / "src" / "kpip" / "_acceleration" / "_kpip_link_tree.c",
        "KPIP_LINK_TREE_BUILTIN",
    ),
    "_kpip_tls.c": (
        REPO_ROOT / "src" / "kpip" / "_acceleration" / "_kpip_tls.c",
        "KPIP_TLS_BUILTIN",
    ),
    "_kpip_unzip.c": (
        REPO_ROOT / "src" / "kpip" / "_acceleration" / "_kpip_unzip.c",
        "KPIP_UNZIP_BUILTIN",
    ),
    "_kpip_catalog.c": (
        REPO_ROOT / "src" / "kpip" / "_acceleration" / "_kpip_catalog.c",
        "KPIP_CATALOG_BUILTIN",
    ),
}
"""File name in the build to its source and the define that builds it in."""

DEPENDENCY_INCLUDE = Path(sys.prefix) / "dependency_libs" / "base" / "include"
"""Where MonolithPy installs the headers of the libraries it links, zlib's
among them: _kpip_unzip.c uses zlib, linked already for the zlib module."""


class KpipNativeModules(NuitkaPluginBase):
    plugin_name = "kpip-native"
    plugin_desc = "Compile kpip's C modules into the binary as built-ins."

    def getExtraIncludeDirectories(self) -> list[str] | None:
        return [str(DEPENDENCY_INCLUDE)] if DEPENDENCY_INCLUDE.is_dir() else None

    def getExtraCodeFiles(self) -> dict[str, str]:
        # MonolithPy links zlib in statically; a Python that loads it as a
        # shared module gives the binary no zlib to call.
        zlib = "#define KPIP_UNZIP_ZLIB 1\n" if DEPENDENCY_INCLUDE.is_dir() else ""
        return {
            name: f"#define {define} 1\n{zlib}" + source.read_text(encoding="utf-8")
            for name, (source, define) in NATIVE_SOURCES.items()
        }
