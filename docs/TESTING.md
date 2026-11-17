# Tests and release checks

Install the development environment with `uv sync --extra dev`. Tests use
synthetic calendars and temporary databases; no personal profile or live account
is needed. Qt's offscreen platform supports the automated GUI checks without a
desktop or X11 server. Tests using local IPC need permission to create local
sockets.

The development extra includes `pytz` for the alternate timezone-provider tests.
Installing it system-wide alone does not make it available in uv's isolated
project environment; refresh that environment with `uv sync --extra dev`.

## GitHub release gate

The workflow runs correctness checks on Python 3.11 and 3.14:

```sh
QT_QPA_PLATFORM=offscreen TZ=UTC PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest -q -m "not local_performance" \
  --strict-markers --tb=short --maxfail=5 --durations=20
```

Both versions retain recurrence/timezone/import/export matrices, Store and
history tests, synchronization and held-writer concurrency checks, dialogs,
selection and navigation, rendering/layout assertions, launch/IPC behavior,
dependency compatibility, and release packaging tests. The matrix remains useful
because the Python versions have different Qt binding compatibility constraints.

Performance-related filenames do not imply exclusion. A check that forbids
unrelated JSON decoding, asserts bounded model reads, prevents event iteration
while painting, or verifies that the UI progresses while a writer holds a lock
is a correctness/algorithmic guard and remains in CI. Small tests of benchmark
oracles, fresh-process isolation and report statistics also remain in CI.

Large-capacity variants are marked `local_performance`. Their ordinary CI
counterparts retain the same behavioral assertions on smaller inputs that still
exercise paging, batching, summary construction or indexed navigation. The
original large workloads remain available locally. This avoids using shared
GitHub runner timing as evidence of desktop latency while preserving functional
release coverage.

| Contract | CI input | Additional local input |
| --- | --- | --- |
| Search batching and stable rows | 2,000 matching sources | 50,000 matching sources |
| Year summaries and painting without scanning | 1,000 overlapping events, both styles | 50,000 overlapping events, both styles |
| Indexed import arrows | 2,500 ordinary events | 50,000 ordinary events |
| Appearance changes without rebuilding event geometry | 500 events | 5,000 events |
| Compact sequence generation | Small regular/mixed-clock sets | 500 selections per pattern |
| Bulk import and independent candidate upload | 600 resources | 1,442 resources |
| Unbounded cut export, year-9999 edges and compact re-import | A source near year 9999; generated children exceeding an explicit ordinary-component limit | Full 2027–9999 expansion, including more than 30,000 generated components |

The workflow prints the slowest checks. Review those durations before moving
further tests out of CI; do not exclude a whole module merely because its name
contains `performance` or `benchmark`.

## Local capacity and performance runs

Run the entire suite, including the marked capacity variants:

```sh
QT_QPA_PLATFORM=offscreen TZ=UTC PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest -q --strict-markers --durations=30
```

Or run only the larger variants:

```sh
QT_QPA_PLATFORM=offscreen TZ=UTC PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  .venv/bin/python -m pytest -q -m local_performance --strict-markers
```

Extended usage and startup measurements are separate local CLI workloads, not
pytest collection or GitHub release steps. See [performance checks](PERFORMANCE.md)
for their data sizes, operation inventory, correctness oracles and reproducible
commands. Run them sequentially on an otherwise quiet machine; concurrent test
or profiling processes distort latency comparisons. Keep raw timings, sample
counts and observed maxima rather than reporting only averages.

`scripts/benchmark_extended.py` runs the large mixed GUI workload; `--quick`
provides a small development smoke run. `scripts/benchmark_year_presence.py`
isolates annual range/count processing without painting, and
`scripts/benchmark_serialization.py` measures payload copying in fresh workers.
`scripts/compare_performance.py` checks that three extended reports used the
same harness, workload, environment and complete repeated interaction inventory.
These CLI workloads are all local only; their small oracle/report tests remain
in the release gate.

The optional public calendar audit and independent CalDAV validation are
documented in [calendar interoperability](INTEROPERABILITY.md). They are not
required runtime dependencies or network-dependent release tests.
