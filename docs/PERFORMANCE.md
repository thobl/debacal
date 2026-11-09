# Performance checks

The target is the slowest individual operation, including the first visit to an
unrelated date. A fast median or repeated visit does not establish that target.

## Maintained representation

SQLite remains the durable source of truth. When a Store opens, it decodes each
resource once and builds an in-memory representation, before accepting date
queries. Writes update that representation inside the transaction; undo, redo
and imports follow the same path. A transactional, revision-indexed change table
lets another connection update just the affected resources. Visibility changes
do not rebuild event data.

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

## First-window measurements

The same twelve unrelated day/week/month requests ran against `8d8da99` and the
maintained-index implementation, without other test loads. The base fixture has
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
