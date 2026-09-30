# kpip

## kpip ships as a compiled binary

kpip is distributed as a single Nuitka-compiled binary (`scripts/compile`,
`.github/workflows/compile.yml`). Running from source is how it is developed,
not how it is used. Every change must be correct in both, and the compiled one
is the one that matters.

Compiled, kpip has no Python of its own that it can use:

- `sys.executable` is a `python` beside the binary that does not exist. Never
  run it, open it or read facts from it. To start kpip again, use
  `core.interpreter.own_command()`; to run a build backend,
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
ABI, platform and markers. That holds for an interpreted kpip too, since
`--python` names another interpreter. Reach for `sys`, `site` or `sysconfig`
directly only for facts about the kpip process itself.

Branch on `core.interpreter.is_compiled()` only where the two builds must
genuinely differ; prefer code that asks the target interpreter and is then
the same for both. When adding a code path, ask what it does compiled, with
no Python on `PATH`, and with `--python` naming a different version.
