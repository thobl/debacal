# Independent review follow-up

The November 2026 review found failures at boundaries that the existing tests
did not cover. These fixes retain the calendar's workflow and add independent
expected results, actual editor entry, mouse gestures, database transactions,
and structural checks with another ICS parser.

| Finding | Change and regression coverage |
| --- | --- |
| Incoming native IDs overwrite another calendar | Validate global ID and calendar/UID ownership inside the write transaction, including tombstones, conflict acceptance and deletions. Quarantine the resource and continue healthy sync. Tests include another connection writing after inventory. |
| Rule simplification invents dates | Remove a selector only when its frequency and the other selectors prove it redundant. Otherwise preserve the complete schedule document. Tests specify expected dates independently and perform repeated whole-series moves. |
| Document duration lost in the editor | Treat duration as part of the staged schedule state; commit text, controls and prototype together. Save/reopen tests cover adding/removing nominal days across both DST transitions. |
| All-day sequences reject midnight gaps | Evaluate floating dates with neutral calendar cursors, independently of nonexistent local clocks. Typed and graphical authoring, navigation and ICS tests include Cairo, São Paulo and Apia. |
| All-day dates vanish across the date line | SQL candidates, recurrence bounds and overlap checks use floating dates. Import/persist/query tests change between Kiritimati and Honolulu in both directions. |
| Repeated-hour dragging loses its phase | Preview and commit share a validated gesture range. Real Qt drags assert timestamp movement from the second repeated hour. Invalid target clocks reject the operation. |
| Repeated-hour events cover each other | Allocate columns using the same clipped wall-coordinate ranges that are painted. Both phases remain selectable and show timezone labels; tooltips include endpoints and elapsed duration. |
| Later search occurrences cannot be reached | At the paging cap, an explicit “Show dates near current view” action rebases that series. Ordinary following remains stable. Mouse/keyboard tests reach dates beyond the original cap. |
| Action date has no visible feedback | Week/month use a quiet selected-header background. Day/week/month mark today with a bold TODAY label; the day view omits redundant selection shading. Adjacent-month content is muted rather than filled, and the year view uses a bold underlined today number. Shared paint helpers keep the roles consistent; no selected column or whole month cell is filled. |
| Historical recurrence work is unbounded | Seek static hourly/daily/weekly periods while retaining selectors, phase and COUNT. Work-limit failures omit a whole series with an actionable warning while retaining healthy events. Tests count invisible historical work. |
| Combined export duplicates TZIDs | Generate shared timezone coverage before serialization and consolidate retained generated definitions and equivalent observances. Conflicting imported definitions produce an actionable error rather than choosing an offset silently. Independent structural parsing verifies unique identifiers. |

The Apia skipped-date tail regression also checks that compact-language
expansion and canonical recurrence expansion agree on coincident starts.

The approved UI refinements rename the toolbar group to **Actions**, use
**Single / Series / Choices**, and keep compact recurrence
icons with tooltips. No additional sequence-help panel was added. Search and
agenda rows expose complete date/time, calendar and location information to
Qt accessibility alongside their visible titles. The main calendar's today
indicators refresh at midnight without navigating the user's view. A compact
screen uses scrolling around the existing workspace; normal-size windows keep
their existing geometry and theme.

The bulk sync fixture is generated and distributable; example people and
accounts are deliberately fictional. No private calendar fixture is required.

## Scope and remaining limits

Conservative equivalence and timezone checks can retain a schedule document or
reject combining uncertain timezone definitions. They do not guess at calendar
semantics. Export conflicting imported calendars separately when advised.

The native agenda/search accessibility information is an improvement, not a
complete semantic interface for the painted calendar grids. Screen-reader
operation still needs manual validation. Moving all projection and import
commits to cancellable background operations remains separate architectural
work; dense cold views and accepted bulk imports can still pause the UI. The
bounded dateutil adapter depends on a capability-checked internal calendar
helper interface within the supported 2.x dependency. An incompatible engine
reports a view issue rather than running an unguarded calendar scan.

## Performance and validation

Historical-rule measurements use the same Python 3.14.9 environment and
independently specified complete query results, with each case in a separate
process. Three timings are sampled; the benchmark process has a five-second
timeout.

| One-day query of a rule beginning in 2000 | Before | After |
| --- | ---: | ---: |
| Hourly, 24 results | 401.211 ms | 0.536 ms |
| Daily with all hours/minutes, 1,440 results | Benchmark timed out | 21.514 ms |

The hourly query generates 27 iterator candidates instead of 235,312. The
dense daily query generates 3,781 candidates. Deterministic tests enforce these
work bounds independently of timings. Other rule shapes have explicit limits
of 10,000 calendar periods and 50,000 candidate/clock work units per rule.
Full-horizon Cut export deliberately permits 120,000 work units and retains
its separate 100,000-exception limit. These are computation bounds, not strict
wall-clock deadlines.

Raw historical measurements: [before](benchmarks/followup-history-before.json),
[after](benchmarks/followup-history-after.json). Reproduce with
`scripts/benchmark_recurrence_history.py --source-root CHECKOUT`.
See [the release review](RELEASE_REVIEW.md) for the complete desktop-workflow
benchmark and its original scope.

The follow-up also ran all 60 desktop workflows sequentially against the
pre-review checkout and the final code, with identical 1400×900 windows,
10,000 ordinary events, 200 weekly series, 500 candidate dates and 4,201 import
resources. Repeated actions have three samples; confirmation/import commits
have one. These are diagnostic measurements on one machine, not latency
guarantees or a claim that every workflow became faster.

| Workflow | Before | After |
| --- | ---: | ---: |
| Cold year view | 580 ms | 555 ms |
| Month view | 95 ms | 100 ms |
| Search typing | 2.8 ms | 4.3 ms |
| Hide calendar | 27 ms | 29 ms |
| Confirm alternative | 69 ms | 80 ms |
| Open/prepare import preview, combined | 2.07 s | 2.19 s |
| Commit import and refresh | 938 ms | 944 ms |

The new identity and recurrence validation has some overhead. The SQL range
projection now avoids extracting unused JSON date fields for timed/unlimited
queries. Historical scans are the substantial performance improvement; cold
dense views and synchronous import commits remain limitations. Memory results
vary between runs, so no memory reduction is claimed.

Raw workflow measurements: [before](benchmarks/followup-workflows-before.json),
[after](benchmarks/followup-workflows-after.json). The harness now accepts
`--width` and `--height` and records the actual initial window dimensions.

The complete regression run includes the approved disposable local-socket
tests. A separate four-test UI run exposed a pre-existing native teardown
crash, also reproduced in the unchanged release: its synthetic clipboard MIME
data survived until Qt's static destruction after Python shutdown. A native
backtrace localized the failure to Shiboken object destruction. Clearing that
test-owned clipboard in fixture cleanup gives a clean isolated exit; production
clipboard behavior is unchanged.

The final complete run passed **3,160 tests and 169 subtests**. The subsequent
header-highlighting refinement passed **113 focused calendar, theme, weekend
and status tests**. Wheel construction, Python compilation and Git whitespace
checks passed. This local run uses Python 3.14.9/Qt 6.12.0; it does not substitute
for rerunning the supported-version CI matrix on these new commits.

## Deliberate scope choices

- Existing compact recurrence icons and tooltips remain, as requested; no help
  panel or selected-option text was added.
- The month subtitle still describes the visible grid range. It was left
  unchanged rather than introducing another explanatory label.
- The existing synthetic bulk fixture and Python/dependency CI matrix already
  address those review suggestions. No private calendar fixture is required.
- Refactoring follows the concrete boundaries: schedule/duration staging,
  validated gesture ranges, timezone consolidation, and bounded recurrence
  work. Splitting the entire workspace/editor and adding a complete accessible
  calendar interface remain separate work.
