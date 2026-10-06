# kpip benchmarks

CodSpeed benchmarks of kpip's own code paths. They live here rather than in
`tests/`, which is generated from upstream pip by `scripts/vendor_pip.py`.

```console
uv run --all-groups pytest benchmarks --codspeed -m "not slow"   # what CI runs
uv run --all-groups pytest benchmarks -m slow                    # the airflow resolve
```

Without `--codspeed`, pytest-benchmark times them locally. End-to-end
comparisons against uv, from the command line, are `scripts/benchmark`'s.

| File | Measures |
| --- | --- |
| `test_uv_graphs.py` | uv-bench's `resolve_warm_jupyter` and `resolve_warm_airflow`: a warm `kpip install --dry-run` of each graph |
| `test_install.py` | uv-bench's many-files cases: unzipping and installing a 10,000-file wheel, untarring a 10,000-file sdist |
| `test_parsing.py` | Parsing uv's compiled requirements files and the largest project pages of the airflow graph |

## The uv graphs

uv's `crates/uv-bench/benches/uv.rs` resolves `jupyter==1.0.0` and
`apache-airflow[all]==2.9.3` (with the Apache Beam provider) from a warm
cache, for CPython 3.11 on an arm64 Mac, with uploads after 2024-09-01
excluded. `uv_graphs.py` runs the same inputs through `kpip install
--dry-run --report`: `--uploaded-prior-to`, `--python-version`,
`--platform`, `--implementation` and `--abi` for the cutoff and tags, and
uv-bench's marker environment in place of the running interpreter's. Each
iteration drops kpip's in-memory caches, so it measures a new process
finding its HTTP cache on disk. Jupyter resolves to 99 packages, airflow to
584.

The airflow resolve takes seconds natively, and many times that under
CodSpeed's simulation, so it is marked `slow` and run locally only.

Nothing is fetched while benchmarking. `corpus/uv_graphs/<name>/` holds every
response the resolves receive (project pages in `pages.zip`, `.metadata`
files and sdists in `files.zip`), served from below kpip's HTTP cache so
caching runs as it does against PyPI. A request the corpus lacks fails,
naming it.

Each sdist in the corpus is a static one: its only member is the `PKG-INFO`
its build produced, and the pages are served with that file's digest and
size. While replaying, kpip reads that `PKG-INFO` instead of running a build
backend, so the benchmark measures kpip, not a build in a subprocess.

### Filling the corpus

A kpip change can make a resolve read something the corpus lacks. Fill it
from PyPI, under Python 3.11 so that sdists are built for the interpreter
uv-bench targets:

```console
PYTHONPATH=src "$(uv python find 3.11)" -I -c \
    "import sys; sys.path[:0] = ['src', 'benchmarks']; import uv_graphs; uv_graphs.fill(['airflow'])"
```

This answers from the corpus where it can and adds only what it fetched.
For a new sdist, `PKG-INFO` is used as is when PEP 643 makes it reliable;
otherwise the sdist is built once, and its metadata stored. When the build
fails, its `PKG-INFO` stands in and the sdist is reported as approximate;
`mysqlclient-2.2.4.tar.gz` is (it needs MySQL's headers to build, and has
no dependencies to get wrong).
