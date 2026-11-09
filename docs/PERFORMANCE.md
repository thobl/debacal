# Performance checks

The target is the slowest individual operation, including the first visit to an
unrelated date. A fast median or repeated visit does not establish that target.

## Maintained representation

SQLite retains canonical calendar resources and a separate compact prepared
representation. Imports and event changes validate and prepare the changed
sources once, updating both representations in the same transaction. Undo and
redo follow the same path. Normal startup loads the prepared values without
parsing canonical event JSON, RRULE strings or timezone definitions. A format
and native timezone-data fingerprint identifies upgrades requiring a rebuild;
damaged derived rows can be repaired from their retained canonical resources.

The search trees themselves are rebuilt on every launch. Sorted interval entries
are assembled into balanced trees in linear time; sorting unordered inputs takes
`O(N log N)`. Startup performs no repeated AVL insertions or rotations. A
transactional change journal lets another connection update just the affected
entries. The sync worker maintains the durable data without constructing a
second in-memory view index. Visibility changes do not rebuild event data.

Display values share descriptions, timezone definitions and other unchanged
metadata. Original ICS transport text stays in the canonical records and is
loaded when an edit, export or sync needs it. A private provenance flag makes
edits of lightweight display values preserve that original transport metadata;
the flag is excluded from history, export and synchronization payloads.

Each calendar has AVL interval trees for fixed instants, civil dates and floating
wall clocks. Subtrees store their latest endpoint, including whether that endpoint
is an inclusive point event. Searches can discard both past and future subtrees;
individual insertions and deletions take logarithmic tree work. Empty calendar
partitions are removed, and an explicit calendar selection visits only its trees.

Ordinary events, explicit additional dates and detached exceptions have individual
interval entries, including exceptions outside a rule's nominal range. Finite
rules of up to 256 occurrences are unrolled during preparation. Longer rules have
conservative start/end envelopes; only overlapping rules are evaluated. Query
results therefore depend on nearby entries and relevant rules rather than the
total serialized size of all recurring resources. Producing and sorting many
visible occurrences still takes work proportional to the output.

Prepared rules retain decoded selectors, durations, exclusion sets and phase/rank
data. Recurrence evaluation seeks to the requested period without reparsing ICS
or RRULE strings, reconstructing every master, or learning from earlier windows.
Generated occurrences share already-validated descriptive data. DATE semantics
use civil-date coordinates; floating events use a conservative wall-time envelope
and exact current-timezone overlap checks, including repeated daylight-saving
hours.

Embedded timezone definitions also prepare their transition tables before date
lookup. The first distant-year lookup uses binary search, just like later ones;
there is no lazy history expansion or last-date cache. Existing safety budgets
bound preparation. If a definition exceeds them, its proven earlier coverage
remains usable and dates beyond that coverage report an explicit issue.

There are no main-view range-page, upcoming-visibility, search-match or decoded
search-model result caches. Current widget contents, selection and paint geometry
remain UI state. The maintained source/index representation is prepared regardless
of which date will be queried first and is not populated by viewing dates.

## Isolated range queries

```sh
.venv/bin/python scripts/benchmark_ranges.py --events 3000 --series 1200 --finite 120 --queries 12 --output /tmp/ranges.json
```

The benchmark creates a temporary SQLite file containing only generated data:
ordinary timed/all-day events, ended and future rules, ongoing weekly/monthly
rules, finite irregular sets, exclusions, detached occurrences, original ICS
text and embedded timezone definitions. It never opens an installed profile or
sync folder. Fixture creation and the independent dateutil/interval reference
calculation are outside the measured regions.

Two separate fresh worker processes measure source selection and the complete
`Store.query_occurrences` call. Each visits distinct, deterministic random
day/week/month windows in non-monotonic order and different calendar subsets.
There are no warm-up queries. The report keeps every request and latency,
maximum latency, numbers of decoded models/retained ICS bytes, and numbers of
expanded series. Reference checks happen after timing and require the complete
answer; truncating or returning fewer events cannot improve the score.

`preparation_ms` records opening the Store, including loading/preparing its
maintained data structures. It is separate from query latency so eager
preparation remains visible. Query-dependent memoization cannot help unique
requests. `--max-query-ms LIMIT` (also `--max-ms`) fails if **any** complete range
query exceeds the limit. `--max-source-ms` and `--max-prepare-ms` independently
bound source selection and Store preparation. Limits are explicit because
machine speed and output size vary. `--source-root OTHER_CHECKOUT` imports that checkout's implementation
while keeping the same fixture, workload and measurements.

The instrumentation uses direct wrappers around model decoding and recurrence
expansion, rather than a profiler. Their overhead is included equally in before
and after measurements. Full query timing excludes answer serialization and
reference checks. The source-selection measurement is diagnostic; source
selection and complete queries are timed independently, not added together.

SQLite is file-backed, but the benchmark does not flush kernel filesystem
caches or promise physical-disk cold reads. Store preparation, application
process state, first-query latency and revisits are different costs. A finite
sample's maximum is an observed worst case, not a real-time guarantee under
arbitrary machine load or an unlimited number of visible occurrences.

## Desktop operations

```sh
.venv/bin/python scripts/benchmark_ui.py --events 6000 --series 1200 --queries 24 --output /tmp/navigation.json
.venv/bin/python scripts/benchmark_workflows.py --events 10000 --series 200 --iterations 3 --output /tmp/workflows.json
```

The navigation runner visits unique, unrelated date windows and changes calendar
visibility, including Qt layout and paint completion. The broader runner covers
editing, copy/paste, selection, undo/redo, search, preferences, import previews,
conflict comparison and candidate confirmation. Every iteration of the broader
runner starts a new process with a separate SQLite file. Its first visits are
therefore not diluted by repeated warm iterations. Both report raw samples and
maxima and support a per-operation `--max-ms` failure threshold. Network sync is
disabled; generated files and databases are removed when the run finishes.

The historical-rule runner also uses distinct, non-monotonic dates and reports
maximum iterator work and latency:

```sh
.venv/bin/python scripts/benchmark_recurrence_history.py --queries 5
```

Older reports in `docs/benchmarks` describe the previous warm/synthetic-in-memory
methodology. They remain historical observations and are not evidence of the
first-visit latency target.

## Range-index baseline before startup optimization

The same twelve unrelated day/week/month requests ran against `8d8da99` and the
initial maintained-index implementation, before the startup work below and
without other test loads. The base fixture has
3,000 ordinary resources, 1,200 rule series and 120 finite irregular series,
retaining approximately 2 KiB of synthetic ICS text per resource. Queries choose
four of six calendars. All answers matched the independent reference.

| Maximum observed cost | Before | Indexed | Indexed + 10,800 irrelevant rules |
| --- | ---: | ---: | ---: |
| Source selection | 40.358 ms | 0.191 ms | 0.206 ms |
| Complete range query | 228.023 ms | 10.520 ms | 10.746 ms |
| Models decoded during one query | 975 | 0 | 0 |
| Expansion calls during one query | 895 | 80 | 80 |
| Store opening and data preparation | 0.777 ms | 638.384 ms | 3,776.987 ms |

Adding `--irrelevant-series 10800` increases the number of rule resources tenfold
while preserving every answer and the number of relevant rules. This matters:
increasing `--series` alone would also increase active series and result sizes.
The unchanged expansion count demonstrates that unrelated series are pruned
before query-time expansion. The original selection returned all 880 recurring
resources in the selected calendars; the indexed source query returns at most
160 relevant parents, including finite sets resolved through individual points.
The full query expands only the 80 remaining long rules. The old expansion
counter includes ordinary events passed through its common expansion function.

Preparation is a real cost, not omitted work: canonical resources and index
entries are constructed eagerly before querying. The base fixture took
1,071 ms and the larger fixture 5,562 ms to import as a single bulk write.
Querying does not build or grow a date-window cache; arbitrary requests use the
maintained structure. Startup and
large import batches therefore remain distinct performance targets.

The navigation runner used the base fixture and twenty-four unrelated windows.
Its observed maxima were 33.506 ms for day, 38.624 ms for week, 40.905 ms for
month, 40.373 ms for hiding a calendar and 52.936 ms for showing one. Store
preparation took 692.849 ms and opening/painting the window another 213.942 ms.

The broader runner used 10,000 ordinary resources, 200 rules, 120 finite sets
and 500 candidate dates in three fresh processes. It found remaining larger
operations: opening a 4,201-resource import dialog reached 1,695.012 ms,
applying the import 1,045.112 ms, export 405.788 ms, preferences opening
196.697 ms and entering year view 147.396 ms. Store preparation took at most
616.285 ms and creating the fixture in one bulk write 1,671.014 ms.
Fast range retrieval does not establish that every complete desktop workflow meets an interaction
latency budget.

Raw requests, maxima and work counters are retained in
[before](benchmarks/ranges-before.json), [indexed](benchmarks/ranges-after.json),
[scaled](benchmarks/ranges-scaled.json), [navigation](benchmarks/ui-after.json)
and [complete workflows](benchmarks/workflows-after.json).

## Startup stages and memory

```sh
.venv/bin/python scripts/benchmark_startup.py --samples 3 --output /tmp/startup.json
.venv/bin/python scripts/benchmark_startup.py --samples 3 --irrelevant-series 10800 --output /tmp/startup-large.json
.venv/bin/python scripts/benchmark_startup.py --samples 3 --ancient-timezone --output /tmp/startup-timezone.json
```

The startup runner uses the same disposable file-backed calendars and unrelated
first-query windows as the range benchmark. It records every fresh-process Store
opening and query, verifies complete answers against the independent reference,
and checks `--max-startup-ms` and `--max-query-ms` against the slowest sample.
Module import time and fixture creation are separate from opening the Store.
It also measures a standalone sync/writer Store and the cost of opening one
beside the application's reader Store. The baseline had no separate writer
role, so its second Store necessarily prepared another complete view index.
The startup maximum includes every uninstrumented Store opening, including the
reader in that duplicate-role probe and optional desktop samples; diagnostics
cannot hide a slower opening by placing it outside the main sample array.

CPU profiling and allocation tracing run in separate fresh processes. Their
overhead is absent from the latency score. The profile reports exclusive function
time in non-overlapping groups, plus explicitly non-additive inclusive timings
for named construction/loading stages. The `other` group includes standard
library and built-in operations; time spent iterating SQLite rows can be charged
to the Python caller rather than `sqlite3.execute`. A profiled millisecond must
not be presented as an uninstrumented startup millisecond.

The memory run reports retained and peak traced Python allocations. Normal
latency runs independently record resident memory before and after loading, and
before/after opening the writer. Tracemalloc's own bookkeeping increases RSS,
so its resident totals are not compared with uninstrumented RSS. Physical disk
caches are not flushed. The optional ancient-timezone fixture replaces the
fixed embedded zone with two annual DST observances starting in 1601, exposing
transition preparation that a fixed-offset fixture cannot measure.

`--desktop` additionally measures Qt/workspace imports, QApplication creation,
Store loading and the first offscreen workspace paint in fresh processes, with
network sync disabled. These distinguish data preparation from the fixed Python
and Qt costs of launching the complete application. `ready_ms` is the sum of
those measured phases, not an external measurement from process creation.
`--max-ready-ms` bounds the slowest such desktop startup when `--desktop` is used.

`--fixture-source-root PATH_TO_OLD_CHECKOUT` builds the canonical fixture using
that implementation, then opens it with `--source-root` to measure one-time
upgrade preparation. Each worker receives a fresh copy of the original database;
an earlier worker cannot migrate the next worker's input. Copies are outside
timing. The report labels upgrade runs separately and records database size
before/after closing, making persistent-index storage growth visible. Ordinary
restart measurements use data already maintained by the new implementation.

### Startup results and design choice

The following maxima use synthetic data, three fresh reader samples and the
additional uninstrumented writer/desktop probes. Base means 3,000 ordinary
resources, 1,200 rule series and 120 irregular finite series. The larger case
adds 10,800 irrelevant rules without changing any queried answer. Annual zone
uses the base data with the ancient two-observance timezone.

| Maximum Store opening | Full reconstruction | Bulk trees only | Persisted preparation + bulk trees |
| --- | ---: | ---: | ---: |
| Base | 799.632 ms | 635.718 ms | 228.561 ms |
| Larger | 4,587.147 ms | 3,421.552 ms | 1,046.650 ms |
| Annual zone | 2,278.918 ms | — | 227.301 ms |

The base reconstruction maximum includes a 799.632 ms second full Store; the
largest reader-only opening was 747.715 ms. The final standalone writer took at
most 7.841 ms across these cases, and opening it beside the reader took about
1 ms. It constructs no independent view index.

Profiling guided the split. In the base profile, individual AVL insertion
accounted for 418 ms of exclusive instrumented work; bulk construction reduced
that to 36 ms. Actual startup improved less because recurrence preparation,
model reconstruction and timezone calculations remained. One annual embedded
timezone added roughly 1.5 seconds of uninstrumented startup work; its transition
preparation accounted for 3.08 seconds inside a 5.37-second instrumented run.
Persisting those prepared transitions removes that repeated iteration.

After persistence, dataclass copy/pickle-state machinery was the largest
avoidable reconstruction cost: base event restoration took 481 ms inclusive in
the profile. A direct slot copy and numeric datetime restoration reduced that
to 268 ms. These are nested profiler diagnostics, not additional wall times.

The implementation therefore persists versioned preparation records, numeric
interval entries and shared timezone data. It **rebuilds the actual balanced
trees at each launch**: entries are sorted and nodes constructed in a bulk pass.
It does not serialize Python AVL nodes or introduce a memory-mapped tree format.
Measured bulk construction is small enough that the extra persistent-tree
format and update complexity were not justified. Updates still maintain only
changed sources and their interval entries.

| Retained Python allocations (separate traced run) | Before | Final |
| --- | ---: | ---: |
| Base | 38.06 MiB | 21.40 MiB |
| Larger | 151.48 MiB | 76.97 MiB |

Normal-process RSS with both reader and sync-role Stores fell from 113.67 to
59.34 MiB for the base data, and from 355.02 to 119.66 MiB for the larger data.
These are Store benchmark processes, excluding a full Qt desktop. A separate
close probe retained the closed Store object and collected unreachable cycles:
traced Python allocations fell from 21.40 to 2.12 MiB. RSS under tracing remained
high because tracing and allocator memory are separate from live application
objects; it is not evidence that all those resident pages were returned to the
operating system.

### Upgrade cost and first-query guards

Prepared records cost one initial migration for an existing canonical-only
database. Each upgrade worker started from a fresh copy of the old database.
The base upgrade used three reader samples; the larger and annual cases used
one, with writer/duplicate-role probes recorded separately.

| One-time upgrade | Maximum opening | Additional allocated database space |
| --- | ---: | ---: |
| Base | 988.939 ms | 2.21 MiB |
| Larger | 4,605.674 ms | 8.54 MiB |
| Annual zone | 2,578.267 ms | 2.33 MiB |

The larger migration remains approximately as expensive as its old repeated
startup, but subsequent openings use the maintained data. The base database
grew from about 18.02 to 20.23 MiB; the larger database from 62.64 to 71.18 MiB.
The same transaction maintains canonical changes and their prepared records.

The startup runner's final maximum unique-window query times were 9.576 ms for
base, 11.613 ms for larger and 11.544 ms for annual data, with complete answers
verified independently. A separate range guard observed 13.025 ms, decoded no
canonical models and expanded at most 80 relevant rules. The desktop navigation
guard observed 34.932/37.031/42.107 ms for day/week/month, and 39.266/51.147 ms
for hiding/showing a calendar. Preparation does not depend on visiting a date.

Desktop phase sums, including support imports, Qt imports, QApplication, Store
opening and first offscreen paint, reached 1,348.408 ms before and 735.934 ms
after. Network sync was disabled. These totals include fixed Python/Qt costs
and must not be confused with the 228.561 ms Store-opening result.

Raw reports: [before base](benchmarks/startup-before-base.json),
[before larger](benchmarks/startup-before-scaled.json),
[before annual](benchmarks/startup-before-annual.json),
[bulk base](benchmarks/startup-bulk-base.json),
[bulk larger](benchmarks/startup-bulk-scaled.json),
[initial persistence base](benchmarks/startup-persist-base.json),
[initial persistence larger](benchmarks/startup-persist-scaled.json),
[final base](benchmarks/startup-final-base.json),
[final larger](benchmarks/startup-final-scaled.json),
[final annual](benchmarks/startup-final-annual.json),
[upgrade base](benchmarks/startup-upgrade-base.json),
[upgrade larger](benchmarks/startup-upgrade-scaled.json),
[upgrade annual](benchmarks/startup-upgrade-annual.json),
[desktop before](benchmarks/startup-desktop-before.json),
[range guard](benchmarks/startup-final-ranges.json),
[navigation guard](benchmarks/startup-final-ui.json),
[close-memory probe](benchmarks/startup-close-memory.json).
