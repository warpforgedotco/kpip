# kpip architecture

A map for finding code and keeping package boundaries. The implementation
named in each section is the source of truth; this page only says where to
look and which rules to keep.

## Start here

| Question | Start at |
| --- | --- |
| What happens when `kpip` starts? | `cli/entrypoint.py:main` |
| Where is a command implemented? | `cli/registry.py` → the `CommandSpec.module_path` and runner named there |
| How does install choose a plan? | `cli/install.py:run_install` |
| How are dependencies resolved? | `resolution/api.py:ResolutionEngine` → `nab_provider.py` → `_vendor/nab_resolver/` |
| Where do index candidates come from? | `index/provider.py:CandidateProvider` |
| How does an artifact become local? | `index/candidate_materialization.py:CandidateMaterializer.ensure_local_text` → `index/artifacts.py:ArtifactLocator` for VCS and remote links |
| How are selected candidates prepared? | `install/output.py:prepare_install_candidates` |
| How are wheels installed? | `install/wheel_transaction.py:install_wheels_transactionally` |
| Where do persistent caches live? | the owner in the cache table below; `cli/cache.py` for `kpip cache` |
| How are build backends invoked? | `build/build_backend.py:ProjectBuilder`, `build/build.py:build_wheel_from_source` |

## Process entry and dispatch

The console script, `kpip.__init__:main` and `python -m kpip` all reach
`cli.entrypoint:main`.

```text
cli.entrypoint:main
  +--> handle_global_commands      help, --version, --require-virtualenv gate, unknown commands
  +--> cli.fast:run_before_startup  cheap argv recognizers, before any startup work
  |      +--> run_lock
  |      +--> cli.fast_install:run_cached_remote   missing target + exact remote pins, warm receipt
  |      +--> cli.fast_install:run_local_fallback  non-empty local target, --no-index wheelhouse
  |      +--> cli.fast_install:run                 empty target, --no-index wheelhouse
  |      +--> run_satisfied_install                 plain names, all already installed
  |      +--> run_list                             no index options; sys.path or --path
  |      `--> run_freeze                           no -r/--user; no editables unless excluded
  +--> execution context, logging, temp dir (per CommandSpec flags)
  +--> cli.fast:run_install_after_startup / run_lock_after_startup
  `--> run_command -> CommandSpec.load_runner
```

Rules:

- Fast paths are recognizers, not separate semantics: they return `None` for
  any argument, target state or feature they do not implement completely, and
  normal dispatch always remains available afterwards. Keep recognition rules
  in `cli/fast.py` and `cli/fast_install.py`; ordinary command dispatch stays
  registry-driven. `cli/entrypoint.py` names only its built-in `help` handling.
- The registry stores module paths and imports a command on first use. Startup
  gating belongs in `CommandSpec` flags (`needs_logging`, `needs_tempdir`,
  `needs_execution_context`), not in command-name tests.
- `cli/fast.py` and `cli/fast_install.py` are imported lazily and carry their
  own name normalization, requirement parsing, METADATA scanning and minimal
  wheelhouse resolver so a declined command pays only for the token tests. That
  duplication is deliberate; do not consolidate it into the shared
  implementations. The modules the fast install path does import keep their
  rarely-run dependencies (`email.parser`, `importlib.resources`,
  `concurrent.futures`, `resolution.models`) behind function-level imports;
  `tests/core/test_startup_imports.py` pins what that path may load.

Shared concerns inside `cli` have one owner each; extend the owner rather than
re-deriving locally:

| Concern | Owner |
| --- | --- |
| Config files, `KPIP_*` overrides, source selection | `cli/config.py` |
| Requirement collection, `--config-settings`, proxy environment | `cli/requirements.py` |
| `--group` and dependency-group files | `cli/dependency_groups.py` |
| Lock serialization (imports nothing; shared with fast paths) | `cli/lock_format.py` |
| Cache directory policy | `core/appdirs.py` |
| Resolver report → CLI diagnostic | `cli/resolution_errors.py` |

Known, deliberate divergence: `install` concatenates configured and
command-line find-links instead of using `resolve_sources`. Every caching
command takes its cache from `core/appdirs.py:command_cache_dir`, which is the
default cache unless `--no-cache-dir` or `KPIP_NO_CACHE_DIR` turns it off.

## Installation

```text
cli.install:run_install
  -> cli.requirements: roots, constraints, sources, policy
  -> plan: install.wheel_install_plan_cache:load_cached_install_plan (exact remote pins, warm)
           else ResolutionEngine.resolve
  -> install.output:prepare_install_candidates   materialize winners, prepare wheel archives
  -> cli.fast_install:install_resolved_pure_wheels (empty-target pure-wheel hybrid)
     else install.wheel_transaction:install_wheels_transactionally
  -> save_cached_install_plan after a fresh exact-pin install
  -> conflict warnings; editables go through build.build:build_editable_from_source
```

`cli/install.py` is the adapter between the plan shapes; command code and
installers depend only on the shared candidate attributes, never on resolver
internals. Source candidates become wheels during candidate materialization,
not at install time.

`install_wheels_transactionally` dispatches to three routes, each preserving
batch rollback: `install_wheels_from_archive_cache` (clone cached immutable
trees into a stage, swap the target), `install_wheels_directly` (after a full
destination preflight), and the generic staged `WheelInstaller` /
`InstallTransaction` path.

The pure-wheel hybrid requires an empty explicit target. That is a safety
precondition: it validates members with the lexical
`cli/fast_install.py:is_safe_member`, which is sound only because every member
is written as a regular file into an empty tree. The staged routes write into
populated targets and therefore use `install/wheel_archive.py:validate_member_parts`
plus a resolved-parent containment check. Relaxing the emptiness rule means
adopting the resolving check.

## Resolution

```text
ResolutionEngine.resolve                     resolution/api.py
  -> inputs.coerce_requirements
  -> NabProvider(CandidateProvider)          resolution/nab_provider.py
  -> nab_resolver.Resolver.solve             _vendor/nab_resolver/ (vendored; the search itself)
  -> ResolutionResult                        resolution/models.py
```

`NabProvider` is the adapter between kpip and the search. It supplies version
choice, dependency and satisfiability queries; decision priority and readiness;
priority/dependency invalidations, partial-solution hints, pending clauses and
targeted-backtrack signals; decision widening; and conflict-display narrowing.
Everything the resolver learns about the index, installed state or policy
arrives through this provider surface. On failure `api.py` renders the
resolver's error with `format_error` and restores the user's original specifier
text.

`resolve_wheelhouse` is only a constructor that pins the engine to local
`find_links` with the index disabled; there is no second search. The separate
minimal resolver in `cli/fast_install.py` exists for startup cost (see above)
and must not grow an implementation arrow to this one.

## Index discovery and artifacts

```text
CandidateProvider.find_candidates            index/provider.py
  -> source locations, Simple API / find-links catalogs (catalog_cache.py)
  -> Link records -> CandidateEvaluator, candidate_filters
  -> CandidateRecord -> CandidateMaterializer.materialize
  -> CandidateStream[LazyWheelCandidate]      replayable, advances on demand
  -> selected LazyWheelCandidate.build_candidate
  -> CandidateMaterializer.iter_materialize -> WheelCandidate
```

`CandidateProvider` coordinates discovery; it owns neither backtracking nor
installation. `CandidateMaterializer` localizes artifacts only when metadata
or a selected winner needs them and builds sdists through
`build.build:build_wheel_from_source`.

`CandidateMaterializer.ensure_local_text` is the boundary from a candidate link
to local bytes. It returns file links directly and delegates VCS and remote
links to `ArtifactLocator.ensure_local_text`. The locator routes VCS links
through `materialize_vcs`; remote links try the artifact cache (URL receipt or
expected hash), then an HTTP-cache body, then a `NetworkSession` stream.
`network/download.py:Downloader` is currently standalone code covered by tests
and benchmarks; no production command imports it, so it is not on this path.

## Caches

Every persisted cache lives under `<cache root>/v<CACHE_VERSION>/`
(`core/appdirs.py:versioned_cache_dir`, currently `v1/`), which
`resolve_cache_dir`/`command_cache_dir` hand to every writer.

The cache is versioned at two levels, for two different jobs. The `v<N>` root
retires the whole tree at once; it is the escape hatch for a change that
crosses every store, and it is what `kpip cache purge` keys on, since that
removes every `v*` directory without knowing the stores. Each store then
carries its own version in its name (`core/utils.py:versioned_bucket`), so a
format change to one does not discard the others. Stores cost wildly
different amounts to refill -- re-parsing an index page is one request,
re-extracting every wheel is not -- and a shared version lets the cheapest
one decide when the most expensive is thrown away. There is no migration code
at either level: a store of another version is simply never read.

Bumping one store is the whole migration. The new name is a store this kpip
has never written; the old one is inert until a purge.

| Owner | Under `v1/` | Contents |
| --- | --- | --- |
| `network/cache.py` | `http-v2/` | HTTP metadata and bodies, fresh for as long as the response allowed (`network/freshness.py`, RFC 9111) and revalidated after; normal responses use one atomically replaced combined file, while raw artifact bodies use a split metadata/`.body` layout for hard-linking; incomplete split entries are misses |
| `index/catalog_cache.py` | entries in `http-v2/` | parsed Simple API catalogs, release summaries (`Version.to_wire()`), target choices; key prefixes and payload headers carry their own versions; checksum-validated, recompiled from the catalog on any failure |
| `index/artifact_cache.py` | `artifacts-v1/` | bodies by SHA-256 plus URL receipts |
| `index/candidate_cache.py` | `wheels-v2/` | validated wheels built from content-identified sdists or immutable VCS sources, keyed by source, build settings, interpreter and target, then published as completed entry directories |
| `index/metadata_cache.py` | `metadata-v1.sqlite` | parsed headers of local wheel files and of installed `METADATA` files, and SHA-256 of local wheels, by path, size, mtime |
| `index/candidate_metadata_cache.py` | `candidate-metadata-v1.sqlite` | dependency metadata reused during resolution |
| `index/release_facts_cache.py` | `release-facts-v1-<interp>.marshal` | deterministic release rejection reasons |
| `cli/fast.py` | `fast-lock-plan-v2/` | rendered lock output, keyed on `core/code_identity.py` so an upgraded kpip never replays it |
| `cli/lock_replay.py` | `lock-replay-v1/` | an index lock of hashed wheels, keyed on its inputs, interpreter and `code_identity`, with the ETag/Last-Modified of every project page it read; replayed while each is unchanged -- by the fast path when all are fresh, else after `cli/lock.py:replay_after_revalidation` revalidates the stale ones in one concurrent wave |
| `cli/fast_install.py` | `fast-install-v1-<interp>.marshal`, `fast-install-trees-v1-<interp>/` | fast-path plans, metadata, cloneable completed targets |
| `install/wheel_archive_cache.py` | `archive-v1-<interp>/` | validated unpacked wheel trees by digest, and their byte-compiled `pyc/` sibling |
| `install/wheel_install_plan_cache.py` | `resolution-v1-<interp>/` | short-lived exact-pin receipts over archive entries |

`<interp>` is `core/utils.py:CACHE_INTERPRETER_TAG`, applied by
`versioned_bucket(..., interpreter=True)`. `marshal` payloads are not portable
across interpreters, and neither are installed trees or the bytecode in them,
so those stores are scoped to the interpreter that wrote them as well as
versioned.

Invariants:

1. A cache is optional. Missing, corrupt, inaccessible or ineligible entries
   are misses, never correctness failures; every load validates its store's
   shape and identity contract.
2. A key and its eligibility checks cover every input that the store permits to
   affect reuse: requirement, source, interpreter/target, policy, hashes or
   filesystem identity as applicable. If a stable identity cannot be
   established, the result is not persisted.
3. Validation precedes publication. Self-contained entries are atomically
   replaced; multi-file and tree stores publish a final marker or rename a
   completed staging directory, so readers never accept a partial write.
4. Caches stay independent. No cross-cache transactions or shared mutable
   database.

## Package ownership and dependency direction

| Package | Owns | Must not own |
| --- | --- | --- |
| `core` | value types, packaging rules, hashes, URLs, wheels, archive reading, cache primitives | command policy |
| `platform` | config locations, install schemes, cloning, secure archive extraction, host behavior | archive reading, selection policy |
| `build` | backend hooks, metadata generation, build isolation | resolver decisions |
| `index` | sources, links, catalogs, discovery, artifact localization, materialization | backtracking, installation |
| `network` | sessions, auth, HTTP transport, HTTP cache | resolver state |
| `vcs` | VCS URLs, revisions, source retrieval | wheel selection |
| `resolution` | requirements, constraints, search adapter, result assembly | filesystem installation |
| `install` | targets, inventories, wheel plans, transactions | index parsing |
| `cli` | argument parsing, dispatch, presentation, fast paths | reusable mechanics |

Allowed first-party dependency edges (enforced by
`tests/core/test_architecture_imports.py`):

| Domain | May import |
| --- | --- |
| `core` | nothing first-party |
| `platform` | `core` |
| `build` | `core`, `platform` |
| `index` | `core`, `build`, `platform` |
| `network` | `core`, `platform`, `build`, `index` |
| `vcs` | `core` |
| `resolution` | `core`, `index`, `network`, `vcs` |
| `install` | everything but `cli` |
| `cli` | everything |

Four edges cross the table and are known debt, not precedent:
`build/build_backend.py` → `install.build_env.venv`,
`resolution/inputs.py` → `install.requirement_set`,
`resolution/api.py` → install-only typing contracts,
`resolution/req_install.py` → `build.pep517_hooks`. `TYPE_CHECKING` imports
count as edges too; move the shared shape down instead of guarding the import.
Vendored code is outside these rules.

The test walks every non-vendored Python file recursively and parses nested and
`TYPE_CHECKING` imports too; it also rejects stale debt exceptions once an edge
is removed. Extend the exception set only while documenting an existing edge
here; new shared shapes should move down instead.

## Performance boundaries

1. Fast paths recognize only what they implement completely and decline before
   committing to unsupported behavior.
2. Discovery, metadata parsing, artifact localization and source builds stay
   demand-driven; replaying a `CandidateStream` reuses work.
3. Winner materialization and archive preparation may run concurrently, but
   candidate and install order stay deterministic.
4. Caches accelerate; they are never the sole source of correctness.
5. A faster install route preserves batch atomicity or declines to one that does.
6. Optimize a semantic workload class at an existing boundary, never a
   benchmark fixture or package name.
