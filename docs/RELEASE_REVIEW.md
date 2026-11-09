# Release review — 8 November 2026

This is a record of the release-preparation checks, not a guarantee of complete
correctness. An independent review subsequently found gaps in global sync
ownership checks, recurrence equivalence, duration projection, floating dates,
and repeated-hour gestures/layout. The follow-up tests cover those concrete
boundaries; the earlier broad preservation statements below describe only the
tested cases. See the [follow-up](REVIEW_FOLLOWUP.md) for the concrete fixes and
regression boundaries. Remaining performance and accessibility limits are listed below.

This review covers recurrence and storage integrity, selection operations,
startup/import delivery, sync recovery, dialogs, preferences, visual consistency,
and desktop performance. All benchmarks use synthetic calendars. Live provider
checks use the supplied test account and a uniquely identified temporary event;
its deletion was verified. Credentials and personal calendars are not included
in these artifacts.

## Correctness and recovery

* Mixed timed/all-day moves preserve floating dates, midnight boundaries, and
  companion durations. Explicit repeated-hour instants survive move/copy,
  editor projection, and date/time picking. A named-zone ambiguous timestamp
  denotes the first occurrence under
  [RFC 5545 §3.3.5](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.3.5);
  explicitly authored second occurrences use a UTC detached exception.
  Ambiguous second-occurrence end times also serialize in UTC.
  A shared duration model distinguishes an elapsed `24h` range from a nominal
  calendar-day `1d` range throughout expansion, editing, copy/move/split,
  overlap queries, and transport. Both ICS parsers retain lexical `PT24H` versus
  `P1D`; sync compares duration intent as well as the current endpoints.
  Month-contained Skip/Shift authoring checks clock transitions and selected
  bounds before promising that the full range fits. Unsafe choices offer a
  calendar-day duration or Cut rather than producing an overflowing occurrence.
  Year-1 recurrence lookback no longer
  underflows and quarantines valid events.
* Hidden or disabled sources lose their selected references before keyboard
  actions. A disabled paste destination retains the preview and offers recovery.
  Scope changes during editing still check the original group's membership.
* Imported updates retain identity by calendar and standard UID, even when
  another client removes native metadata. Duplicate native IDs cannot overwrite
  another UID/calendar. Detached exceptions follow their remapped master, and
  malformed resources remain isolated from healthy imports and uploads. Identity
  checks and the write share one transaction so background sync cannot race them.
* Repaired remote resources clear their stale quarantine entries. Mirror reads
  remain bounded, including duplicate-resource error paths.
* Point events remain zero-duration when edited. Date-limit key presses report
  errors instead of escaping through Qt callbacks; calendar spillover cells can
  paint outside Python's supported date range.

Regression tests cover undo, calendar transfers, timezone changes, DST folds,
coincident identities, missing metadata, exclusions, and damaged neighbors.
Performance tests assert bounded decoding and correct cache invalidation rather
than fragile machine-dependent timing limits.

## Interface and preferences

The normal week/month headlines and first grid lines remain aligned. Source
lists, event cards, and affected/primary selections retain their shared visual
styles. Closed editors, calendar/history dialogs, and year menus release their
widgets and callbacks.

Event/import previews adapt to compact windows and larger text, elide captions
with complete tooltips, and keep action buttons reachable. Conflict comparison
uses the selected date language, preserves all-day dates, and follows the theme
through its scrolling body and viewport. A rendered-pixel regression protects
dark-mode contrast. Duration and scheduling timezone appear in the comparison,
so equal current endpoints cannot hide different future recurrence behavior.
Fourteen representative offscreen captures were inspected,
including populated year styles and compact windows at 140% text size; the
reported defects were fixed and recaptured.

Preferences retain five functional categories:

| Category | Coverage |
| --- | --- |
| Calendar | Week start, date/time formats, visible hours, week numbers, default/preset durations. |
| Appearance | Colors and light/dark mode, typeface/size, controls/corners, year presentation, sidebar/agenda and weekend treatment. |
| Calendar sources | Accounts and discovered calendars, availability, aliases, colors and credentials. |
| Sync | Automatic sync, interval and conflict notification delivery. |
| Keyboard shortcuts | Grouped actions, multiple bindings, dialog acceptance and selection modes. |

The review found no missing essential setting for the implemented feature set.
Compact category navigation now keeps its title readable and avoids horizontal
scrollbars.

## Performance

`scripts/benchmark_workflows.py` exercises 60 complete workflows: views and
period navigation; source visibility; search typing/follow/filter;
selection/copy/paste placement; moving, editing, deleting, undo/redo;
event/sequence previews; date/time picking; preference changes; import preview
navigation/selection/acceptance/persistence; conflict comparison; and candidate confirmation.
It drives application commands, dialog controls, and representative Qt keyboard
and mouse input, including refresh/painting and asynchronous completion; this
is scripted coverage rather than a manual usability evaluation.
It checks completion and propagates callback/application errors. Sync is
disabled; actual network latency is tested separately, not mixed into UI timings.

The identical harness ran against the pre-review checkout and the reviewed code
with 10,000 ordinary events, 200 ongoing weekly series anchored in 2018, and 500
alternative slots. The import fixture contains 4,201 masters. Measurements use
Python 3.14.9, Qt 6.12.0, Fusion style, and the offscreen backend. Three samples
are collected for repeated interactions; import and confirmation run once.
These are diagnostic samples on one machine, not statistical latency guarantees.

| Workflow | Before | After |
| --- | ---: | ---: |
| Move event | 343 ms | 83 ms |
| Edit and save | 385 ms | 129 ms |
| Hide calendar in year view | 378 ms | 95 ms |
| Hide calendar in week view | 45 ms | 26 ms |
| Apply warm theme | 116 ms | 78 ms |
| Apply 120% text size | 132 ms | 90 ms |
| Confirm alternative slot | 73 ms | 90 ms |
| Cold dense year | 564 ms | 601 ms |
| Open import and finish preparing preview, combined | 17.9 s | 2.3 s |
| Commit accepted import and refresh | 893 ms | 1,123 ms |

Moving/editing now uses resource-ID projections and snapshots of selected
families instead of decoding unrelated events. Calendar visibility can filter a
cached complete page without expanding the same series again. Unchanged
preferences and viewport fits retain geometry. A dense current page can live
alone in the cache even above its ordinary 50,000-occurrence budget; another
date window evicts it, so appearance changes do not repeatedly decode that same
year. Year summaries retain their indexes for unchanged input objects, source
order and timezone. A separate 40,000-event warm update check improved from
about 137 ms to 1.1 ms. Fold-changing replacements deliberately rebuild even
when Python's datetime equality considers their wall clocks equal.
The codec shares the system-zone cache, decodes common timezone definitions
once, and folds UTF-8 by encoded lines
instead of encoding every character. Calendar-transfer UID reservation uses a
SQL projection: a separate 10,000-event check improved from 137–144 ms to
2.77–3.48 ms.

The duration safety preflight checks the full supported recurrence horizon,
using mathematical fast paths where possible and a bounded cache. Separate
cold checks took about 115 ms for a safe near-boundary exact range and 190 ms
for a nominal daytime range; their cached checks took about 0.002 ms.
An unsafe daylight-saving spill rejected in about 0.05 ms. These first-time
checks apply to multi-day month-contained Skip/Shift authoring, not ordinary
viewing or every recurrence expansion.

Raw results: [before](benchmarks/review-before.json),
[after](benchmarks/review-after.json),
[50,000-event/500-series stress run](benchmarks/review-stress.json), and
[isolated X11 run](benchmarks/review-x11.json).
The stress run completed all 60 workflows. Peak process RSS, including the
accepted import and its undo history, was about 363 MiB before, 383 MiB after,
and 540 MiB in the larger stress run. The isolated X11 smoke run completed all
60 workflows with 1,000 events and 20 recurring series. This review does not
establish a memory reduction.

Reproduce without persistent data or network access:

```sh
.venv/bin/python scripts/benchmark_workflows.py --events 10000 --series 200 --iterations 3 --output /tmp/workflows.json
.venv/bin/python scripts/benchmark_workflows.py --events 50000 --series 500 --iterations 1 --output /tmp/stress.json
```

Use the same harness with `--source-root OTHER_CHECKOUT` for comparisons.
`--profile /tmp/workflows.pstats` records profiling data; profiled timings should
not be compared directly with unprofiled measurements.

## Validation and release limits

The complete test suite includes approved local-socket tests for startup,
single-instance forwarding, profiles, file arguments, and stdin import. Native
X11 checks use an isolated display. Packaging checks build the wheel without
network dependencies, install into a fresh temporary target, and verify its CLI
and an offscreen demo launch/screenshot from outside the checkout.

Baseline full-suite result during release preparation: **2,990 tests and 169 subtests passed**. The run
uses disposable profiles and offscreen rendering, with sandbox escalation only
for the local-socket integration tests. Python compilation and Git whitespace
checks also passed.

Live Nextcloud checks passed discovery, initial pull, recurring create/read,
series and occurrence edits, exclusion sync, deletion, and verified cleanup
through the application's default vdirsyncer bridge. This does not establish
complete account inventory coverage or correctness for every provider.

Dense-year loading remains the main measured UI limitation: about
0.60 seconds for the normal benchmark and 1.99 seconds in the stress run;
the latter's cold month view took 297 ms. Cold year navigation in that larger
workspace also takes roughly 1.6–2.1 seconds. Its cached appearance switches
now take 42–50 ms, and calendar visibility changes take 283–495 ms. Cached
visibility and presentation changes are faster in the ordinary benchmark.
Large imports still take time to prepare and can briefly contend with painting,
despite running parsing on a worker. The accepted import's database commit
remains synchronous: 1.12 seconds for 4,201 masters in the ordinary benchmark,
and 1.09 seconds in the larger workspace. Progressively
handling that commit remains a performance improvement for future work.

System-keyring locked/unlocked behavior and screen-reader operation still need
manual validation on target desktops. X11 smoke coverage does not establish
Wayland or every window manager's behavior. OAuth, unsupported advanced calendar
features, and additional providers remain outside the verified scope. Floating
all-day regressions now include midnight gaps and a wholly skipped civil date;
timed values still require real local clocks. A Linux
desktop launcher, Arch build recipe, release wheel/source packages, and 0BSD
license were added during release preparation after this review. See the
[installation guide](INSTALL.md) for current packaging.

## Publication preparation

The README is now a short introduction with synthetic screenshots; the detailed
reference lives in the [user guide](USER_GUIDE.md). The project uses 0BSD.

The Arch recipe was built with makepkg without installing it system-wide.
A validation run against the extracted release archive passed **2,942 tests
and 169 subtests; 5 maintainer-tool tests were skipped** because those tools
are intentionally outside the source archive. Local-IPC tests were excluded
from that validation run; the complete workspace suite covers them.
Ordinary package installation now runs a short offline smoke check, including
CLI startup, desktop-file validation, and rendering the demo. The complete
suite remains available with `DEBACAL_FULL_CHECK=1` and runs in release CI.
CI also exposed test fixtures that assumed the host used Berlin time. The
fixtures now state their display zone, and all-day roundtrips compare floating
calendar dates while timed events still compare exact instants. A separate
export assertion now checks stable UIDs and event content instead of requiring
an export-time DTSTAMP to remain unchanged across a clock-second boundary.
The corrected tests were checked in UTC and Berlin. CI uses short tracebacks
and stops after five failures to keep diagnostics manageable.
The compatibility matrix also caught an actual runtime dependency defect:
PySide6 6.12.0 over-releases singleton references on Python 3.11 and can abort
ordinary GUI calls ([PYSIDE-3474](https://qt-project.atlassian.net/browse/PYSIDE-3474)).
Conditional dependency metadata and the refreshed lockfile exclude that version
on Python 3.11, while preserving the normal Qt range on newer Python. Small
metadata regression tests protect this compatibility rule.
Package contents, Python launcher, desktop entry, icon and license were checked,
and the packaged application launched with a disposable demo profile.

The release source, wheel and Arch recipe bundle reproduced byte-for-byte in
separate staging trees with the documented toolchain. The wheel also installed
and launched outside the checkout. Source archives use an explicit input list,
normalized timestamps/ownership, and omit local data and development galleries.

Before the initial GitHub upload, all reachable history was audited. Private
author/committer metadata was replaced with a public GitHub no-reply identity;
personal export-filename references were generalized. The bulk-calendar test
now generates its own synthetic data. No calendar exports, profiles, databases,
credentials, personal notes, or private server/account details are tracked.
Historical image metadata and known-identifying-text checks were reviewed
alongside their synthetic fixture generators. This is an audit of known
identifiers and content, not a mathematical proof of the absence of all PII.

### Release refresh — 9 November 2026

The complete workspace suite passed **3,268 tests and 169 subtests**, including
the review follow-ups, current date styling, focus handling and release-update
checks. The rebuilt wheel launched from a temporary directory outside the
checkout using a disposable demo profile. The source, wheel and Arch recipe
bundle were rebuilt with refreshed checksums; that refresh used Arch package
release `0.1.0-2`. The version tag remains `v0.1.0`.

The subsequent packaging update adds `PKGBUILD.local` for building checkout
edits through the shared package steps. Eighteen packaging/publication checks
passed, along with isolated checkout and standalone release package builds.
Arch package release `0.1.0-3`, its source, wheel and recipe bundle
reproduced byte-for-byte, and the wheel launched outside the checkout.

The installed Arch CalDAV library also exposed a missing transitive dependency
during lazy client loading. Discovery now uses the existing read-only DAV
fallback in that case, including account color refresh. Package release
`0.1.0-4` adds an offline discovery smoke check with the package interpreter;
it passes both with the missing system dependency and with complete project
dependencies. The repaired Arch package built and passed its normal checks.
The full suite passed **3,284 tests and 169 subtests** with the installed Python,
using offscreen Fusion and disabling desktop theme/shortcut overrides for the
test baseline. The system CalDAV dependency remained missing throughout.
