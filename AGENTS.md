# kpip

## kpip ships as a compiled binary

kpip is distributed as a single Nuitka-compiled binary (`scripts/compile`,
`.github/workflows/compile.yml`), and only as that: no user ever runs it
interpreted. Running from source exists for development and the test suite
alone. Design every change for the binary; the source run only has to keep
working well enough to develop and test with. Do not add behaviour, options
or pip parity for an interpreted install nobody has.

Compiled, kpip has no Python of its own that it can use:

- `sys.executable` is a `python` beside the binary that does not exist. Never
  run it, open it or read facts from it. To start kpip again, use
  `core.compiled.own_command()`; to run a build backend,
  `core.interpreter.build_interpreter()`.
- `sys.prefix`, `sys.path`, `site`, in-process `sysconfig` and
  `importlib.metadata.distributions()` describe the bundle, not an
  environment anyone installs into. `sys.version_info` and
  `sys.implementation` are the CPython kpip was built with, not the target's.
- `__file__` points inside the bundle. Do not build paths from it for
  anything that must exist on disk (scripts to run, data to read); package
  data goes through `importlib.resources`, and anything run as a script needs
  a compiled entry, as `install/_compile_worker.py` has.

Everything about the Python being installed for comes from
`host.interpreter_facts.target_interpreter()` (or `search_path()` for its
`sys.path`): prefix, scheme paths, scripts directory, user site, version,
ABI, platform and markers. Reach for `sys`, `site` or `sysconfig` directly
only for facts about the kpip process itself: the bundled CPython.

Prefer code that asks the target interpreter, so the source run used in tests
takes the same path as the binary; branch on `core.compiled.is_compiled()`
only where the two cannot. When adding a code path, ask what the binary does
with it: with an environment active, with only a `python3` on `PATH`, with no
Python at all, and with `--python` naming a version other than the bundled
one.
