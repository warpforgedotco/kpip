"""Build kpip's optional compiled modules in place, for the running interpreter.

Each is a Cython pure-Python-mode source inside ``src/kpip`` whose importer
keeps a pure-Python implementation for when the extension is absent, so a
build only ever adds speed: PyPy, a plain source checkout and a platform
without a compiler run the same code uncompiled. The extension lands next to
its source, where the import finds it; ``.gitignore`` keeps it out of the
tree.
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from kpip_compile.vendor import REPO_ROOT

SOURCE_ROOT = REPO_ROOT / "src"

EXTENSIONS = {
    "kpip._vendor.nab_resolver._cranges": "kpip/_vendor/nab_resolver/_cranges.py",
    "kpip.index._page_catalog": "kpip/index/_page_catalog.py",
    "kpip.host._link_tree": "kpip/host/_link_tree.py",
}
"""Module name to source path under ``SOURCE_ROOT``.

The name is spelled out rather than derived: ``kpip._vendor`` has no
``__init__.py``, and Cython, deriving it, names the module
``nab_resolver._cranges``, which then fails to import and to unpickle.
"""

POSIX_ONLY = frozenset({"kpip.host._link_tree"})
"""Extensions built from POSIX calls, left out of a Windows build."""


def extensions_for(platform: str = sys.platform) -> dict[str, str]:
    """The entries of ``EXTENSIONS`` that build on ``platform``."""
    return {
        name: path
        for name, path in EXTENSIONS.items()
        if platform != "win32" or name not in POSIX_ONLY
    }


def optimization_args(compiler_type: str) -> list[str]:
    """The optimization flags, given here rather than left to ``CFLAGS``.

    setuptools builds with the interpreter's ``CFLAGS``, which carry ``-O3``
    and ``-DNDEBUG`` -- unless the environment sets ``CFLAGS``, which then
    replaces them. A shell exporting only include paths, as Homebrew setups
    often do, would build at ``-O0``, where the extension is slower than the
    pure Python it replaces.
    """
    if compiler_type == "msvc":
        return ["/O2"]
    return ["-O3"]


def build_extensions(
    source_root: Path = SOURCE_ROOT,
    *,
    force: bool = False,
) -> list[Path]:
    """Compile every entry of ``EXTENSIONS`` into ``source_root``; return the files."""
    from Cython.Build import cythonize
    from setuptools import Distribution, Extension
    from setuptools.command.build_ext import build_ext

    class OptimizedBuildExt(build_ext):
        def build_extension(self, ext: Extension) -> None:
            ext.extra_compile_args = [
                *ext.extra_compile_args,
                *optimization_args(self.compiler.compiler_type),
            ]
            super().build_extension(ext)

    source_root = source_root.resolve()
    selected = extensions_for()
    extensions = [
        Extension(name, [path], define_macros=[("NDEBUG", None)])
        for name, path in selected.items()
    ]

    # Every path handed on is relative. Cython lays the C it generates out
    # under build_dir by its source's path, and setuptools lays each object
    # file out under build_temp by its C file's path: absolute, each repeats
    # the whole of the one before, and an object file under a Windows
    # temporary directory runs past MAX_PATH, where MSVC cannot write it.
    with tempfile.TemporaryDirectory(prefix="kpip-extensions-") as temp:
        # The generated C goes to the temporary directory, not beside the
        # source; only the finished extension belongs in the tree.
        with _working_directory(source_root):
            modules = cythonize(
                extensions,
                build_dir=str(Path(temp) / "c"),
                compiler_directives={"language_level": 3},
                force=force,
                quiet=True,
            )
        for module in modules:
            module.sources = [
                os.path.relpath(source, temp) for source in module.sources
            ]
        with _working_directory(Path(temp)):
            command = OptimizedBuildExt(Distribution({"ext_modules": modules}))
            command.initialize_options()
            # build_lib is where a module's dotted name is laid out, so
            # pointing it at the source root builds each extension beside
            # its source.
            command.build_lib = str(source_root)
            command.build_temp = "build"
            command.force = force
            command.finalize_options()
            command.run()
            return [Path(command.get_ext_fullpath(name)) for name in selected]


@contextlib.contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    """``contextlib.chdir``, which arrived in 3.11."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
