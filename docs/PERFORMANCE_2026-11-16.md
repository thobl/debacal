# Extended performance review, 16 November 2026

This round measures three source versions on the same Arch Linux server. Earlier
results from other computers are not comparison inputs.

- **Before:** `8d8da9951b9e52d35e5672741ade53a22af978c8`, immediately before
  the previous range-index, startup and database-contention improvements.
- **Current:** `a03a60834f16e223ded2e870384d7de276a36b1f`, the source at the
  beginning of this round.
- **Optimized:** the completed implementation from this round, identified by
  `29a7751ad68aee3c559d928bd1634640639dd3b7` and the application fingerprint
  in the comparison report.

## Workload and measurement

The synthetic, file-backed fixture starts with 22,365 resources: 20,000 ordinary
events, 2,000 historical/future/active rules, 120 irregular finite series, 240
additional ongoing rules, four exception-heavy families and one 500-date
alternative-slot family. It has eight calendars, retained ICS payloads and
embedded timezone definitions. No real profile, calendar export or credentials
are read. Initial databases are 91.8 MiB before the previous round and 102.0 MiB
for the current and optimized versions, which retain the prepared representation.

Each version uses its own native database schema and a frozen copy of exactly
the same benchmark and generator. Seven fresh processes measure startup through
first paint. The usage run repeats 200 interaction types at least eight times,
in eight seeded, differently ordered rounds: 1,937 timed actions plus setup and
correctness checks. New/random date windows, earlier windows, selected series,
and growing imported data all occur during the run.

Coverage includes date/view navigation; calendar selection, visibility, order,
creation and editing; search, filtering, folded results and paging; preference
Apply/Save across categories; event-dialog fields, repeat modes, sequences and
date/time pickers; mouse creation, moves and Ctrl-drag copies; single/all/future
selection and edits; copy/paste placement; candidate confirmation; undo/redo;
imports, preview navigation, exports, issues, history and conflict dialogs.
Network services are isolated: sync scheduling/settings and conflict interfaces
are exercised without contacting accounts. Separate held-writer checks cover
database contention; this is not a benchmark of remote server/network latency.

The timed interval includes dispatch, queued database work, relevant timers,
event processing and painting. It also records the worst event-loop gap, dispatch
time and individual observations. Search debounce and dialog polling are real
parts of end-to-end latency, so their timer floors remain included. Correctness
oracles run outside the timing interval; returning fewer events is not a valid
speed improvement.

All scored GUI runs use native Qt X11 under Xvfb and Openbox, a 1600×1000 screen
at 96 DPI, a 1400×900 window, Fusion and DejaVu Sans. Python is 3.14.10, PySide6
6.12.0, icalendar 7.3.0 and python-dateutil 2.9.0.post0. Runs are sequential;
other test/profiling jobs are paused. Profiling results are diagnostic only and
are never used as scored latencies. OS filesystem caches are not flushed.

An observed maximum is not a real-time guarantee under arbitrary machine load
or arbitrarily large visible output. Means and maxima are reported separately;
there is no aggregate score that hides a slow operation.

## Results

All table entries are **mean / observed maximum**, in milliseconds. The complete
[201-operation table](benchmarks/extended-2026-11-16/all-operations.md),
[CSV](benchmarks/extended-2026-11-16/operations.csv) and
[comparison JSON](benchmarks/extended-2026-11-16/comparison.json) retain every
operation's sample count, mean, p95, maximum and worst event-loop gap. Raw reports
also retain every individual observation.

| Interaction | Before previous round | Current control | Optimized |
| --- | ---: | ---: | ---: |
| Fresh process → first paint | 1905.1 / 1933.4 | 1745.5 / 1759.5 | 1739.8 / 1877.8 |
| Switch to week | 1016.3 / 1633.6 | 192.3 / 313.8 | 155.1 / 160.3 |
| Switch to month | 1396.8 / 1874.9 | 257.9 / 266.0 | 219.0 / 227.9 |
| Switch to year | 3505.7 / 3980.6 | 1385.8 / 1431.1 | 386.2 / 406.5 |
| Hide calendar, week | 127.1 / 627.4 | 164.3 / 201.5 | 149.3 / 186.8 |
| Show calendar, month | 44.5 / 46.9 | 258.4 / 279.6 | 218.0 / 221.4 |
| Confirm alternative slot | 1455.5 / 1575.9 | 205.6 / 314.9 | 172.6 / 188.1 |
| Apply theme | 686.3 / 713.4 | 872.1 / 930.8 | 676.9 / 711.5 |
| Apply text size | 742.9 / 792.3 | 905.3 / 977.8 | 734.7 / 763.1 |
| Type search matching many series | 192.1 / 196.3 | 198.6 / 223.7 | 207.2 / 296.7 |
| Commit 300-resource import | 1804.6 / 2014.1 | 613.2 / 768.5 | 519.7 / 656.8 |
| Export one full source | 859.8 / 876.2 | 876.7 / 894.6 | 874.2 / 897.6 |

The largest usage interaction was 4,120.7 ms before the previous round,
1,559.3 ms in the current control and 897.6 ms after optimization. Startup is
listed separately. Year-entry maximum fell by 72% relative to current, week
entry by 49%, and candidate confirmation by 40%.

The earlier version can win warmed visibility toggles because it retained
completed range results. It is much slower on first/unrelated windows. The
current and optimized versions deliberately make fresh queries on navigation
and visibility, so these warmed numbers are included alongside the cold cases.
The isolated query checks use unique windows in fresh processes.

There are limits and regressions to keep visible. Startup mean barely changed;
its observed maximum increased from 1,760 to 1,878 ms. Search did not improve:
one round reached 297 ms, including a 206 ms event-loop gap, versus 224/131 ms
in the control. The observation is retained. Export remains about 0.9 seconds,
and theme/font application still spends substantial time updating Qt controls.
The result is not a claim that every interaction became faster.

| Memory during the same growing usage workload, MiB | Before | Current | Optimized |
| --- | ---: | ---: | ---: |
| Resident after round 1 | 297.5 | 347.2 | 296.8 |
| Resident after round 8 | 333.8 | 369.9 | 318.7 |
| Process peak | 441.6 | 474.7 | 493.8 |

All versions finish with 24,829 resources after real imports and creations.
Optimized settled residency is about 14% lower than current at round 8, while
process peak is higher. The benchmark therefore does not establish a peak-memory
improvement. Worker threads stay at two; final open-file counts are also retained
in the comparison report.

## Isolated data-processing checks

The annual benchmark uses the same 22,365-resource fixture, three fresh workers
and seven unique, randomly ordered years per worker: 21 first-visit queries per
version. Every timed query finishes before the full-event correctness oracles
run. Counts, daily/calendar counters and per-source issues must match exactly.
The Berlin case changes the viewer timezone and civil query boundaries; fixture
events remain UTC. It exercises the general path without the UTC daily shortcut.

| Annual query and counters, mean / max ms | Before | Current | Optimized |
| --- | ---: | ---: | ---: |
| UTC viewer | 1957.6 / 2483.1 | 779.9 / 1066.7 | 243.7 / 298.5 |
| Europe/Berlin viewer | 1965.5 / 2513.1 | 810.4 / 1087.8 | 459.7 / 599.2 |

Relative to current, the observed annual-query maximum falls by 72% in UTC and
45% in Berlin. The general range representation therefore improves the non-UTC
case too. Raw annual reports are linked in the [artifact manifest](benchmarks/extended-2026-11-16/manifest.json).

The separate day/week/month range benchmark has 20,000 ordinary events, 2,000
rules and 120 finite series across eight calendars. Source selection and full
queries each use a fresh worker and 48 distinct, non-monotonic windows. Neither
phase has a warm-up. Full answers are checked against the independent reference.

| Isolated phase, mean / max ms | Before | Current | Optimized |
| --- | ---: | ---: | ---: |
| Select relevant sources | 47.090 / 82.144 | 0.141 / 0.217 | 0.146 / 0.230 |
| Produce full occurrences | 393.448 / 482.213 | 14.980 / 19.714 | 14.536 / 18.864 |

Source finding is already fast in current: the optimized result is essentially
unchanged. Both decode **zero models and zero retained ICS bytes** during every
query. Before the previous round, the worst request decoded 1,759 models and
3.66 MB of retained ICS text. The remaining annual cost came from generating and
processing the many relevant occurrences, rather than finding their sources.

Store opening is measured separately. In the annual check, current/optimized
openings average 948/937 ms, versus less than 1 ms before the previous round.
The earlier Store deferred its decoding to queries; current loads prepared
source structures at startup. Whole-app first-paint measurements include this
cost, rather than hiding it outside the comparison.

Serialization uses three fresh workers and 100 calls per operation per worker.
These small timings explain bulk-write savings; they are not GUI latency claims.

| Payload snapshot, mean / max ms | Before | Current | Optimized |
| --- | ---: | ---: | ---: |
| Ordinary event | 0.0107 / 0.0228 | 0.0105 / 0.0303 | 0.0056 / 0.0104 |
| 4,096 additional dates | 0.3394 / 0.4387 | 0.3378 / 0.4730 | 0.0179 / 0.0220 |
| 400 detached overrides | 2.7512 / 3.5099 | 2.7825 / 6.3588 | 1.8600 / 2.0380 |
| 400-override model round trip | 6.3261 / 7.0726 | 6.4287 / 7.6306 | 4.4521 / 4.8629 |

An additional native-X11 run made 80 jumps and 160 visibility changes against
20,000 ordinary events, 2,000 rules and 120 finite series. A background writer
held its canonical transaction for **1,000 ms** every fourth iteration. Explicitly
contended hides averaged 86 ms / peaked at 112 ms; contended shows averaged
74 ms / peaked at 87 ms. The maximum visibility action across the entire run was
159 ms. This is separate from the three-version score. Other dialog/workspace
operations under held writers remain covered by the correctness suite.

## Changes

- **Compact year data:** interval trees select the same relevant sources, then
  prepared rules yield ranges directly into per-day/calendar counters. Drawing
  the overview no longer creates a full Event and encoded ID for every native
  occurrence. Details, selected families and keyboard navigation resolve only
  the needed sources/days. Overlap and start-day indexes serve different jobs;
  arrow ordering still respects actual instants, including mixed DATE/timed
  events. Budgets omit an entire problematic source and retain issue reporting.
- **Arithmetic daily spans:** a proven subset of fixed-UTC daily rules uses
  COUNT/UNTIL and day arithmetic directly, without visiting every occurrence.
  Overlapping start-day spans merge before constructing navigation metadata.
  This path rejects exceptions, extra dates/selectors, floating time, nominal
  durations, month policies and non-UTC source/viewer zones. Those continue
  through the general prepared-range path; their correctness is not traded for
  a faster UTC benchmark.
- **Cheaper projection:** validated event slots are assigned once; native ISO
  occurrence IDs have a fast encoding path with the general URL encoder as
  fallback. Range and full-event projections share duration, Cut, floating,
  DST, exclusion, identity and work-limit logic.
- **Cheaper writes:** snapshots copy mutable containers while sharing immutable
  fields. A transaction hands its privately normalized model to derived-index
  maintenance only while the canonical JSON still matches exactly. Savepoint
  rollback, direct SQL changes and canonical undo conflict checks retain their
  normal safeguards. The handoff disappears at commit; caller-owned objects
  are never reused as canonical models.
- **Selection and controls:** one highlight pass borrows only the selected
  source metadata, avoiding repeated canonical model decoding. Unchanged
  calendar rows avoid stylesheet/icon updates and layout reinsertion. Hidden
  calendar surfaces update when activated.
- **Long-session stability:** the first large optimized run exposed a native
  layout-wrapper lifetime failure and was discarded. A reduced reorder/removal
  reproducer failed with borrowed layout items; widget-based lookups completed
  1,000 cycles. Calendar refresh and drag ordering now avoid those wrappers,
  with a regression check across repeated creation, reordering and deletion.
  Qt documents a related stale-layout-pointer class of failures in its
  [6.12 release notes](https://www.qt.io/blog/qt-for-python-release-6.12-is-here).
  The application fix does not suppress event-filter exceptions.
- **Preference application:** pure presentation changes update the model already
  on screen. A changed revision, calendar, window or timezone forces a fresh
  query; navigation and visibility always query. Geometry depends on relevant
  metrics rather than every preference. Synchronous preference saves avoid
  disabling and re-enabling the entire control tree; real pending writes still
  lock it.

No historical date-window/result cache was introduced. These are maintained
source structures, transaction-local handoffs and current widget state.

## Validation and CI scope

Final local validation uses Python 3.14.10:

- The final CI-selected suite passes **4,467 tests and 169 subtests** in 271.58 s.
  Two optional provider tests skip because `pytz` is not installed.
- All **15 large capacity variants** pass locally in 58.28 s, including the
  50,000-event views and long-range export cases.
- After the arithmetic change, 401 focused range, persistence, recurrence,
  summary, navigation and UI checks pass. Tests cover DST/folds, nominal
  durations, floating/DATE values, Cut, duplicates, whole-source limits, source
  snapshots, rollback/ownership, year-1 boundaries and allocation avoidance.
- All three scored native-X11 runs complete the same 1,937 actions, with no
  missing repeated interaction types. The final held-writer run also passes.

GitHub retains the Python 3.11/3.14 correctness matrix, including database
contention and algorithmic guards. Fifteen large-capacity variants now have
smaller CI counterparts and remain available under `local_performance`; the
long usage/startup/counter benchmarks are standalone local CLIs. In the eight
affected modules, the same frozen current application measured 63.904 s of
testcase work with the original large inputs versus 7.943 s with the CI inputs
(196 cases in either selection). This saves about 56 s of local testcase work
per matrix job, without claiming an exact GitHub wall-time reduction.
See the [scope evidence](benchmarks/extended-2026-11-16/ci-scope.json) and
[validation summary](benchmarks/extended-2026-11-16/validation.json).

## Reproducing the checks

Install development dependencies with `uv sync --extra dev`. For offscreen
development smoke tests:

```sh
QT_QPA_PLATFORM=offscreen TZ=UTC .venv/bin/python scripts/benchmark_extended.py \
  --quick --output /tmp/debacal-quick.json
```

For native Linux measurements, install Xvfb, an X11 window manager and Qt's XCB
runtime dependencies. Start Openbox inside the Xvfb session, then run:

```sh
QT_QPA_PLATFORM=xcb TZ=UTC LC_ALL=C.UTF-8 PYTHONHASHSEED=0 \
  .venv/bin/python scripts/benchmark_extended.py \
  --source-root /path/to/frozen/checkout --revision COMMIT \
  --startup-runs 7 --output /tmp/extended-COMMIT.json
```

Keep the harness/generator, dimensions, dependencies and configuration identical
for all three versions. `--source-root` is verified against imported modules and
an application-source fingerprint. The comparison refuses mismatched workloads
or incomplete coverage:

```sh
.venv/bin/python scripts/compare_performance.py \
  /tmp/extended-before.json /tmp/extended-current.json /tmp/extended-optimized.json \
  --output-dir /tmp/debacal-comparison --markdown
```

Additional isolated tools are `benchmark_ranges.py` (unique source/window
queries), `benchmark_serialization.py` (fresh-worker payload copies),
`benchmark_startup.py` (startup stages) and `benchmark_toggle_latency.py`
(visibility with a held background writer). See [performance checks](PERFORMANCE.md)
for their options and [testing](TESTING.md) for CI versus local capacity checks.

The annual counter comparison uses:

```sh
.venv/bin/python scripts/benchmark_year_presence.py \
  --source-root /path/to/frozen/checkout --revision COMMIT --samples 3 \
  --viewer-timezone Europe/Berlin --output /tmp/year-berlin.json
```

Use `UTC` for the other viewer variant. Keep the benchmark tools and their
configuration fixed across versions. All published raw reports and checksums
are listed in the [manifest](benchmarks/extended-2026-11-16/manifest.json).
