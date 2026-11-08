# Event sequences: specification and correctness contract

## The model

A series has a **reference range**, a timezone, and a canonical iCalendar
schedule: DTSTART, RRULE, RDATE, EXDATE, and detached exceptions identified by
RECURRENCE-ID. The reference supplies the default clock and duration. It is a
coordinate origin, **not a compulsory occurrence**. Removing or moving its
occurrence is an ordinary single-occurrence operation; the reference can remain.

Every schedule has an editable string. Short programs describe actual
occurrences, including several at the same start;
the complete `schedule{…}` form describes canonical scheduling fields and
identities directly. Generation tries the short form first and always falls
back to the complete form. Neither infinity, an excluded reference, coincident
occurrences, nor a size limit can produce a blank or uneditable state.

Canonical data controls display and synchronization. Valid matching text also
records relative intent. Stale or obsolete annotations are reconstructed from
canonical data, never interpreted as trusted instructions. Title, location,
notes, alarms and detached content remain in the event model.

### Duration semantics

Timed ranges with an explicit end retain their **elapsed** duration, including
through daylight-saving changes. A nominal calendar-day duration is distinct:
`1d` means the same local clock on the next day, whereas `24h` means exactly
24 elapsed hours. The duration field accepts both; default durations and preset
buttons are elapsed minutes. All-day ranges always count floating calendar
dates. Moving Start retains the selected duration semantics; explicitly changing
End or stretching an endpoint chooses an elapsed range.

The model stores nominal timed intent in `duration_spec`, using RFC 5545
`DURATION` spelling, such as `P1D` or `P1DT2H`. Days advance first in the event's
timezone and clock units then advance elapsed time. An empty value means an
exact duration derived from the endpoints. Pure-clock specifications such as
`PT24H` normalize to that exact mode, never to `P1D`. Recurrence expansion,
copy/move/split, overlap queries, editors, and ICS transport use this shared
duration model. Detached exceptions retain their explicit chosen endpoints.

## Short syntax

```ebnf
sequence = "none" | finite | [ finite, separator ], tail ;
finite   = item, { separator, item } ;
item     = atom | positive_integer, "*", atom
         | [ positive_integer, "*" ], "(", finite, ")" ;
atom     = relative | date | clock | clock, "-", date ;
relative = "+", nonnegative_integer, unit, [ "!" ] ;
unit     = "d" | "w" | "m" | "y" ;
date     = day, "." | day, ".", month, "."
         | day, ".", month, ".", four_digit_year ;
clock    = hour, ":", two_digit_minute ;
tail     = "every(", positive_integer, unit, [ "!" ],
           { ",", option }, ")" ;
option   = "on=", selector, { "+", selector }
         | "day=", day | "week_start=", weekday
         | "overflow=", ( "skip" | "shift" | "cut" ) | "span=", positive_integer
         | "count=", positive_integer | "until=", full_date ;
selector = [ ordinal ], weekday ;
ordinal  = [ "-" ], ( "1" | "2" | "3" | "4" | "5" ) ;
weekday  = "MO" | "TU" | "WE" | "TH" | "FR" | "SA" | "SU" ;
full_date = day, ".", month, ".", four_digit_year ;
separator = whitespace | ",", [ whitespace ] ;
```

Units are mandatory: `+2`, `every(2)`, `h`, and `min` are invalid. Clamping
`!` applies only to months/years. Partial month dates **end with a dot**:
`3.2.`; full dates **do not**: `3.2.2027`. Leading zeros are accepted.
Clock minutes have two digits. Retired spellings have no compatibility parser.
Blank input is an incomplete edit; `none` denotes an empty schedule.

### Evaluation

Start with an empty occurrence list and a cursor at the reference. Each finite
instruction moves the cursor and emits **one separate occurrence**, even when
another occurrence already has that start. `+0d` emits the current cursor
without moving it: `2*+0d` means two distinct occurrences at the reference.
This is a multiset of starts, not a deduplicated set. Multipliers repeat
instructions, never an implicit reference. `2*+1d` emits two events;
`+0d 2*+1d` emits three. An explicit full date returning to a previously emitted
instant also creates another occurrence there.

| Instruction | Meaning |
| --- | --- |
| `+Nd`, `+Nw` | Advance N local days/weeks, keeping the cursor clock. |
| `+Nm`, `+Ny` | Advance on an N-month/year grid, skipping invalid dates as RRULE does. |
| `+Nm!`, `+Ny!` | Advance N months/years and clamp to the last available day, retaining the preferred day for later steps. |
| `D.` | Next matching day strictly after the cursor, at the reference clock; skip invalid months. |
| `D.M.` | Next matching day/month strictly after the cursor, at the reference clock; skip invalid years. |
| `D.M.YYYY` | That exact date at the reference clock, including earlier dates. |
| `HH:MM` | That clock later on the cursor day, otherwise on the next day. |
| `HH:MM-D…` | The corresponding date instruction using this clock. |
| `K*atom`, `K*(…)` | Execute the atom/group K times. |

The cursor remembers a preferred day, initially the reference day. Month/year
steps retain it after clamping. Day/week and absolute instructions reset it to
their resulting day. Clock-only instructions also reset it at the resulting date.

Starting on **31 October 2026**:

* `+0d 4*+1m` emits 31 October, 31 December, 31 January, 31 March, 31 May.
* `+0d 4*+1m!` emits 31 October, 30 November, 31 December, 31 January, 28 February.
* The next `+1m!` restores 31 March.
* `+2m` skips invalid candidates by another **two** months, retaining its
  interval grid. The same rule applies to leap-day year strides.

Both skipping and clamping use standard RRULEs when representable. A clamped
monthly day 31 uses `BYMONTHDAY=28,29,30,31;BYSETPOS=-1`; day 30 uses
`28,29,30`. A clamped leap-day yearly rule also specifies `BYMONTH=2`.
Other calendar clients therefore see the same clamping without proprietary
copies. The editor offers its month-range policy choices only when the
interval and event range can encounter a shorter month.

### Ranges at short month boundaries

Calendar-day monthly/yearly tails can specify
`overflow=skip|shift|cut, span=N`. Span is the number of **occupied calendar
dates** used to fit the range; an exclusive midnight endpoint does not occupy
the following date. The editor derives it from Start and End.

For an all-day reference covering **28–29 January**, span is two:

| Definition | February in a non-leap year |
| --- | --- |
| `every(1m, overflow=skip, span=2)` | No occurrence: the whole range does not fit. |
| `every(1m, overflow=shift, span=2)` | 27–28 February, retaining the full range. |
| `every(1m, overflow=cut, span=2)` | 28 February only. |

Skip/Shift compile their start constraints into standard RRULEs. Cut retains
the preferred starting date and omits months where that date does not exist;
it truncates only the native end at the next month's midnight. For example,
a 30–31 January range has no February occurrence, becomes 30 April only in
April, and returns to 30–31 May in May. For timed events this is a month-boundary cut,
**not** independent clamping of the end's day and clock. It never moves an end
before its start. Later months recover the reference's original duration.

Shift is unavailable if the occupied span is longer than a selected month;
choose Skip or Cut instead. A genuine cross-month reference keeps its ordinary
duration and does not acquire month-contained range handling implicitly.
Explicit additions and detached exceptions keep their own chosen ranges.
Single-day Skip/Shift normalize to the existing ordinary/clamped notation;
`!` remains the concise single-day clamp spelling. Bounds, `day=N`, moves and
splits preserve the range policy and span where relevant.

A timed elapsed range can occupy an extra local date when a clock changes.
Before authoring a month-contained Skip/Shift rule, the implementation verifies
that its native start constraints keep the actual range within every selected
month, respecting Count/Until. Unsafe choices are unavailable rather than
silently overflowing; choose a calendar-day duration (for example `2d`), Cut,
or an earlier endpoint. Plain imported RRULEs keep their standard meaning and
are not subject to this authoring constraint.

Cut is symbolic locally (`range_policy="cut"`). At ICS export its changed
ranges become standard detached exceptions through year 9999, rather than a
rolling date window. Verified derived exceptions collapse back to the small
master on import; edits by other clients remain authoritative. See the
[transport protocol](SYNC.md#month-range-cut-policy) for provenance checks and
the 100,000-derived-position budget. Exceeding that budget fails before export
with an action to shorten the recurrence or choose Skip/Shift; it never
silently truncates the series.

### Regular tails and bounds

`every(1w)` starts at the cursor and continues weekly. A selector instead starts
at its first matching instant on or after the cursor. `count=N` counts **tail
occurrences**, not periods: `every(2w, on=MO+WE, count=6)` emits six appointments
across active fortnightly weeks. `until=D.M.YYYY` includes starts on or before
that local date. Bounds are mutually exclusive. A tail is terminal and cannot
appear inside a repeated group.

When the tail starts exactly at the **last emitted prefix occurrence**, it
continues that occurrence rather than creating a second copy. Thus
`+0d every(1d, count=3)` has three occurrences, while
`2*+0d every(1d, count=3)` has four: two at the reference and one on each of the
next two days. Only this final cursor overlap is shared. An earlier prefix
occurrence coinciding with a later tail date is an additional copy. Prefix
occurrences otherwise neither consume the tail count nor inherit its bound.

`on=MO+WE` selects Mondays and Wednesdays. Weekly selectors use plain weekday
names; monthly and yearly selectors may also use ordinals: `on=2MO` means the
second Monday and `on=-1FR` the last Friday. Multiple selectors choose their
union; selecting one date twice inside `on` does not multiply a standard rule
occurrence. Yearly selectors apply within the tail cursor's month each year.
`week_start=SU` chooses the weekly interval's week boundary; its default is MO.
Options are case insensitive, may appear in any order, and cannot be repeated.

`day=31` explicitly supplies the preferred month day, independently of the
reference day. It is available for monthly/yearly tails and combines with `!`:
`every(1m!, day=31)` starting on 30 November emits 30 November, 31 December,
31 January, 28 February, 31 March. This preserves a clipped series suffix's
original preference. Without `!`, missing preferred dates are skipped.
`on` cannot combine with `day` or `!`; `week_start` applies only to weekly rules.

An ordinary tail establishes its standard recurrence at its actual cursor.
A clamped tail retains the preferred day. Thus after `+1m!` from 31 October,
`every(1m)` repeats on the 30th, while `every(1m!)` continues on the
preferred 31st with shorter-month clamping.

```text
+0d 13. +1w 18. 2. 3.2. +2w
+0d 4*(+2w) +3w 3*+2w
+0d 10:00 11:00 9:00 10:00 11:00
every(1m!, count=13)
every(1w, on=MO+WE)
every(1m, on=-1FR, count=6)
every(1m!, day=31)
2*+0d
+0d +3w every(1w)
every(1d, until=13.11.2026)
none
```

The first example, at 7 November 2026 09:00, emits 7, 13, 20 November;
18 December; 2 January, 3 February, 17 February 2027. The clock example emits
09:00, 10:00, 11:00 on two consecutive days. The weekly prefix retains the
reference while skipping the next two occurrences.

Compilation preserves the tail phase using one standard RRULE and finite
additions/exclusions. An incompatible phase is rejected before adopting new
short input. Existing canonical calendars of any phase still have their
complete `schedule{…}` representation.

### Timezones and limits

Timed comparisons use UTC instants; arithmetic uses the authoring timezone.
All-day events use floating dates and permit only midnight clock entries.
Explicit nonexistent clocks are errors. Ambiguous absolute dates retain the
reference fold; relative/clock-only entries retain the preceding fold.
Generated RRULE times follow iCalendar's timezone interpretation, including
[RFC 5545 erratum 4271](https://www.rfc-editor.org/errata/eid4271), rather than
dropping counted spring-gap occurrences.

Compact parsing limits text to 8,192 characters, nesting to eight levels and
ordinary expansion to 500 steps. Internal reconstruction can use 10,000 steps.
Regular intervals are 1–999 and counts are 1–100,000. Tail compilation visits
at most 10,000 pre-tail instances. Exceeding a short-input budget is an editing
error; it cannot remove an existing calendar's complete text representation.
Finite samples never imply infinity.

## Complete identity-aware form

```text
schedule{"rule": "FREQ=WEEKLY;COUNT=3", "omit": ["2026-11-04T09:00:00+00:00"], "move": {"2026-11-11T09:00:00+00:00": {"start": "2026-11-18T09:00:00+00:00", "end": "2026-11-18T10:00:00+00:00", "all_day": false}}}
```

After `schedule` comes strict JSON, with these optional fields:

| Field | Meaning |
| --- | --- |
| `rule` | Exact canonical RRULE; absent means no rule. |
| `add` | RDATE keys as ISO dates or timezone-aware datetimes. |
| `omit` | EXDATE keys, including an excluded reference. |
| `move` | Map of nominal recurrence keys to ranges containing `start`, `end`, and optional `all_day` and IANA `zone`. |
| `range_policy` | Optional `"cut"` for derived month-boundary ends; absent/empty keeps ordinary durations. |
| `duration_spec` | Optional nominal timed duration, e.g. `"P1D"`; absent/empty retains exact elapsed duration. Also allowed on a moved range. |

Entering a document in the editor commits its duration together with the
schedule: a nominal duration adjusts the reference end, and removing it keeps
the current endpoints as an exact elapsed range. The duration field immediately
shows that choice. Invalid text leaves the last valid schedule and range intact.

Missing fields mean empty fields. `schedule{}` follows the standard DTSTART
default and means the reference alone; `none` excludes it. Generation uses
canonical key/list ordering. Matching user text retains its whitespace.
Omitted range attributes inherit the nominal occurrence's all-day setting and
timezone; date-only and local datetime range inputs use that timezone.
Unknown/duplicate fields, wrong types, invalid dates and duplicate normalized
move identities are errors. Parsing evaluates data, never code.

Move keys identify **original nominal occurrences**, not their actual starts.
Two keys can have identical ranges while remaining two events. Editing a move
retains its complete other content at that key. Removing the move restores the
nominal occurrence; adding its key to `omit` removes it instead. New moves
inherit reference content. The document contains scheduling data, not titles,
credentials or unrelated fields.

Arbitrary imported RRULE clauses are represented directly without forcing them
through four regular controls. Document serialization and equality checking
do not expand the rule and have no compact-language event/character limit.

**Totality argument:** every stored event has a finite canonical tuple: rule
string, RDATE list, EXDATE list, and exception map. The document writes this
tuple's scheduling projection. Each exception keeps its nominal key and range;
parsing merges its unchanged content at that same key. Unchanged text therefore
round-trips scheduling fields and full exception payloads, including coincident
events and infinite rules. This guarantees a string when compact occurrence
notation is unsuitable. Mixed timed/all-day occurrences, unsupported rule
combinations and huge irregular calendars can still use this complete form;
coincident starts alone no longer require it. An existing fallback is offered
as compact text when the extended language can now represent it exactly.

## Generation and normal form

Valid matching user text retains its spelling, grouping, cursor position and
undo history. Generate text only when it is absent or a schedule edit invalidates it.

1. Preserve a certified expression and its relative intent.
2. Express exact standard regular rules symbolically with `every` and a bound.
3. For manageable finite occurrence lists or finite edits to supported ongoing rules,
   generate a compact program and certify equivalence.
4. Otherwise serialize the complete document and certify canonical equality.

The finite normalizer takes an explicit reference and preserves every supplied
occurrence, including repeated actual starts. Canonical reconstruction supplies
a stable order: occurrences at the reference first, then the remaining starts
by instant (floating date for all-day), retaining separate nominal identities
for ties. Consecutive equal starts use `+0d`; repeated zero steps may use a
multiplier. It emits `none` for an empty occurrence list.
For each next start it considers
day-only, day/month, full date, clock/date and relative atoms. Every candidate
must evaluate to exactly that start. Repeated strides/blocks are compressed;
isolated irregular gaps prefer dates over arbitrary `+8d +3d` offsets. Years
are omitted only when the shorter instruction is exact. Month dates end with
a dot, full-year dates do not, and units are always explicit.

Bounded dynamic programming uses deterministic tie-breaks on irregular offsets,
length, explicit years, nesting, groups and lexical order. Beyond the optimization
budget, verified linear date/clock notation is sufficient. Global shortestness
is not claimed. Re-expansion certifies finite output **with multiplicity**;
supported ongoing rules require rule equivalence and exhaustive comparison of
finite date differences and extra-occurrence counts. A finite sample cannot
prove equivalence between different infinite rules.
Uncertain proofs select documents, never guessed short programs.

Finite compilation first recognizes uniform relative programs beginning with
`+0d`, preserving their exact day/week/month/year interval and clamp choice.
It also checks date/clock programs for an equivalent bounded standard RRULE,
including hourly or minutely rules. Such standard rules do not reintroduce
relative hour/minute syntax. Every inferred rule is checked against all
emitted starts. A finite program omitting its reference uses explicit RDATEs
and EXDATE(reference), so inference cannot invent a new DTSTART occurrence.

### Standard identities for coincident occurrences

iCalendar RRULE/RDATE describe a set and cannot encode an extra copy by repeating
the same RDATE. Compact compilation therefore separates ordinary recurrence
membership from finite additional occurrence counts. Existing detached
RECURRENCE-IDs and their complete payloads are retained when text is re-read.
New copies receive unused valid nominal RDATE slots and detached VEVENTs whose
actual ranges point to the requested shared start. Nominal slots are whole
seconds for timed events and dates for all-day events; allocation never
overwrites a normal occurrence, exclusion, or retained identity. It searches
unused slots near the reference within a bounded budget and rejects an edit
atomically if no valid slot can be allocated.

The correctness contract compares the actual-start **multiset**, then retains
the identity/content mapping separately. Two nominal IDs at one instant must
remain two events through text re-entry, move, split, history, and ICS sync.
An unchanged text edit preserves their different durations, titles, notes,
locations and alarms; these remain detached content rather than syntax fields.

## Operations and editor state

Every entry point uses **one**, **all**, or **following** scope. Selected
occurrences are identified by nominal RECURRENCE-ID even after detachment.
Opening a dialog is not a mutation.

* **One:** edit/update a detached exception; deletion adds its nominal EXDATE.
  This applies equally to an occurrence at the reference.
* **All:** content/duration edits preserve the schedule. Moving a certified
  relative program reevaluates its instructions at the moved reference and
  retains its instruction intent. Named weekday selectors rotate with
  a day move; weekly interval boundaries rotate with them to keep their phase.
  An ordinal move is rejected before mutation if the requested new date would
  no longer satisfy that ordinal (for example, moving a last Friday on 30 April
  one day later does not make 1 May the last Saturday of May).
  A fixed day combined with a date prefix is likewise rejected atomically when
  the requested move cannot preserve its tail cursor and full range. The error
  offers changing the fixed day or moving only the selected event.
  Detached payloads/identities follow corresponding logical
  occurrences. Explicit-date/document schedules translate canonically and
  reconstruct text when needed. Excluded references stay excluded.
* **Following:** partition at the selected occurrence's logical position. A
  rule-defined occurrence retains its nominal position after a move; an
  independently added detached RDATE uses its actual start. This prevents a
  coincident copy's allocated identity from placing it before earlier events.
  Copies at the same logical position stay on the same side of the boundary;
  selection highlights use this same ordering. The prefix stops before that
  position; the suffix receives a new UID/reference. Counts, UNTIL, additions,
  exclusions and detached payloads are partitioned together. Then apply the
  all-series operation to the suffix. Both sides receive exact text; a clipped
  suffix retains its former preferred calendar day with `day=N` when necessary.
* **Copy/paste:** one copies the actual occurrence; all/following share the same
  projection/partition and placement logic with new identities. Originals remain.
* **Confirm alternative:** retain the chosen actual event and remove all other
  choices in one undoable operation.

Start/End controls edit the reference. Preview edits modify individual
occurrences, including the one at the reference, without redefining that origin.
Removing its chip/day is allowed. Adding/removing days or changing a time updates
the string. Weekly basic controls preserve all selected weekdays. Ordinal
selectors, a different preferred day and nondefault week boundaries stay in
Custom when the basic controls cannot retain those clauses. Regular controls
appear only when exact. Explicitly switching from
custom to regular replaces the schedule while remembering the custom draft.

Draft changes are staged and adopted atomically. Invalid pending text cannot
save an earlier valid schedule instead. Local mutations refresh authoring
metadata before history/outbox writes. Undo/redo restore canonical fields and
text together. ICS carries standard recurrence plus X-DEBACAL-PATTERN, version
and reference date. The development grammar is version 2; retired notation
has no interpreter. Unannotated calendars are reconstructed canonically.

## Verification

Tests cover retired spelling rejection; normalization round-trips and budgets;
skip/clamp cycles, strides and leap years; weekday/ordinal selectors, interval
phases and bounds; rule/text/control agreement; excluded
and moved references; coincident identities; huge/ongoing/advanced documents;
typed and graphical edits; all/following moves/splits; detached payload transport;
clipboard projection; history/outbox and ICS round-trip. Seeded combinations
certify document reconstruction without recurrence expansion. UI tests use Qt's
offscreen platform.
