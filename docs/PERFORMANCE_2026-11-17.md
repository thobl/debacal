# Background syncing and preferences, 17 November 2026

The earlier offline workflow benchmark did not exercise Python-heavy syncing
concurrently with preferences. This round adds that workload and measures the
whole sync cycle, including publication after the dialog has closed.

## Cause and changes

A background thread keeps network waits away from Qt, but its Python parsing and
calendar preparation still share the desktop interpreter's GIL. Building,
painting and destroying Qt controls repeatedly crosses the Python/C++ boundary;
a busy parser can delay those handoffs. Separate parser-only and CPU-only
controls reproduced the lag without database writes, locks or sync notifications.

The changes move subscription parsing, CalDAV bridge processing and large
Undo/Redo availability checks into disposable child interpreters. Canonical
SQLite transactions, conflict checks and foreground-write priority remain in
force. Ordinary history stays immediate; large history keeps the toolbar actions
disabled until a result for the current revision and permissions is available.

Preferences build their larger categories when first opened. Unchanged remote
resources and sync-state payloads avoid unnecessary writes. Idle polling leaves
the UI alone, and unchanged sync completion avoids a full calendar refresh.
Detailed footer information is built when requested. Calendar query results are
not cached between visited date windows.

Publication profiling also identified avoidable work in year aggregation,
interval-tree payload replacement and bounded upcoming-event queries. Year
aggregation uses sparse boundary changes and arithmetic for eligible regular
schedules; repeated UTC geometry is shared only within the current query. The
interval trees replace values when their keys are unchanged. Upcoming queries
tighten their bounds using already validated earliest candidates. Complex rules
and timezone edges retain their checked fallbacks. The final comparison includes
these follow-up changes.

## Measurement

The same local-only script runs against immutable source snapshots, each building
its native database from the same synthetic input. The fixture includes 20,000
ordinary events, 2,000 series, 120 irregular finite series, 240 additional active
series, exception-heavy families and alternative slots. A separate loopback HTTP
process serves a fixed 1,500-event, roughly 4 MiB ICS subscription. It uses the
real application SyncEngine, parser and Store; no backend is replaced. No real
profile, account, credentials or external server is used.

Each version opens four preferences categories for the first time in every fresh
dialog, in week and year views, and closes with both Cancel and Save. Each
combination runs three times while idle, during unchanged syncing, and during a
sync changing all 1,500 resources: 144 dialog interactions. Six additional refresh
samples follow an undoable 1,500-event import and independent unrelated remote
revisions, preventing a warm history result from hiding revalidation cost.

Opening is timed through first dialog paint. Closing includes the real dialog
callback, native deferred deletion, following calendar paint and heartbeat.
Separate cycle observers remain active through sync publication, so a late
calendar pause is retained even if the dialog has already closed. Completion
waits run native Qt event loops; tight QtTest `qWait` loops were rejected after
showing that they can starve a background SQLite worker. Profiling is diagnostic
only and its timings are excluded from the results.

The main scored runs are sequential on the same 12-CPU Linux machine, using
Python 3.14.10, PySide6 6.12.0, SQLite 3.54.1, icalendar 7.3.0 and
python-dateutil 2.9.0.post0. Both use Qt offscreen, Fusion, DejaVu Sans, UTC and a
1400×900 application window. The control is commit `18f0eac`; the optimized
production tree, committed as `78e843c`, is pinned by SHA-256
`38d9fc2ddea666012dae4af6dfec38465d152953f0f2d710e35d67e0b76c8c99`.
The reports retain the full fixture, dependency and harness fingerprints.

## Results

All entries are **mean / worst observed milliseconds**. The dialog rows each
contain 48 observations per version; history contains six. These are observed
maxima, not a hard real-time bound under arbitrary machine load.

| Operation | Before | Optimized |
| --- | ---: | ---: |
| First preferences paint, idle | 434 / 514 | 108 / 210 |
| First preferences paint, unchanged sync | 1,832 / 1,957 | 109 / 198 |
| First preferences paint, changed sync | 1,780 / 1,943 | 108 / 206 |
| Cancel/Save through following paint, idle | 96 / 128 | 54 / 80 |
| Cancel/Save through following paint, unchanged sync | 125 / 275 | 56 / 89 |
| Cancel/Save through following paint, changed sync | 117 / 163 | 56 / 146 |
| Largest heartbeat gap over unchanged sync | 1,870 / 1,968 | 112 / 196 |
| Largest heartbeat gap over changed sync | 1,841 / 1,950 | 257 / 432 |
| Calendar refresh publishing 1,500 changed resources | 407 / 582 | 215 / 382 |
| Refresh through following paint with large history | 451 / 585 | 169 / 247 |

Unchanged sync now performs **zero calendar refreshes**, compared with one per
cycle before. Idle first-paint means by category were Calendar 429→61 ms,
Appearance 429→104 ms, Calendar sources 422→79 ms and Keyboard shortcuts
456→188 ms. The remaining highest preferences-open result belongs to the
shortcuts editor, rather than a background parser delay.

The changes remove the roughly two-second sync-related preferences stalls in
this workload. They do not remove all GUI work after a real bulk update:
publishing 1,500 changed resources still produced a 382 ms refresh and a 432 ms
heartbeat gap in the worst UTC observation. Large history refreshes also retain
calendar query and paint costs after history validation moves to the child.

A second matched run checked the actual X11 Qt backend under Xvfb and Openbox,
with a 1600×1000 screen and the **Europe/Berlin** viewer timezone. This smaller
run covers Calendar and Appearance, week and year views, both closing actions
and all three sync states: 26 observations per version, only one per
combination. The optimized version ran first, followed by the control.

| X11 operation | Before | Optimized |
| --- | ---: | ---: |
| First preferences paint, changed sync | 1,533 / 1,945 | 98 / 135 |
| Cancel/Save through following paint, changed sync | 154 / 183 | 65 / 84 |
| Largest heartbeat gap over changed sync | 1,796 / 1,966 | 251 / 342 |
| Calendar refresh publishing 1,500 changed resources | 554 / 857 | 220 / 296 |
| Refresh through following paint with large history | 587 / 877 | 178 / 254 |

Native window-management and offscreen costs differ; these runs do not measure
remote network latency or authentication. The full correctness run passed
**4,720 tests and 169 subtests**; 15 sustained capacity variants remain local-only
and were excluded from that run.

Raw observations: [UTC control](benchmarks/sync-2026-11-17/before-offscreen-utc.json),
[UTC optimized](benchmarks/sync-2026-11-17/optimized-offscreen-utc.json),
[X11 control](benchmarks/sync-2026-11-17/before-x11-berlin.json),
[X11 optimized](benchmarks/sync-2026-11-17/optimized-x11-berlin.json).
[Per-operation comparisons](benchmarks/sync-2026-11-17/operations.csv) include
sample counts, mean and maximum timings and speedups;
[summary statistics](benchmarks/sync-2026-11-17/summary.json) group the main
workloads. All four reports use the same harness and fixed ICS payload.

## Reproduce

These sustained workloads are opt-in local commands and are not release CI jobs:

```sh
uv run python scripts/benchmark_sync_responsiveness.py \
  --source-root /path/to/source --revision source-label --output /tmp/result.json
```

Use identical options for both source versions. `--samples`, `--views`,
`--categories`, `--workloads`, `--subscription-events` and `--history-events`
control the workload. `--timezone Europe/Berlin` changes the viewer timezone;
UTC is the default and was used for the main comparison.
`--progress /tmp/progress.jsonl` retains progress and raw observations during a
long run. A failed run retains its complete diagnostic stderr beside the
requested output. To run the smaller native comparison, use an available X11
display (or Xvfb with a window manager) and set `QT_QPA_PLATFORM=xcb`,
`--timezone Europe/Berlin --samples 1 --categories Calendar,Appearance`.
