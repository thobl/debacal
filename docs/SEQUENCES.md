# Event sequences: specification and correctness contract

This is the specification for the sequence language, its generated notation,
and the operations that edit a series. It also records the deliberate choices
where more than one behavior would be reasonable.

## One schedule, several representations

A series consists of a primary event and a canonical iCalendar schedule:
DTSTART, optionally RRULE and RDATE, EXDATE exclusions, and detached exceptions
identified by RECURRENCE-ID. That schedule determines what is displayed and
synchronized. Custom text is an editable, checked description of the same
occurrence starts, not a second independent source of event dates.
When verified, it also retains the user's relative instruction intent for later
moves and splits. Stale text is never used as that intent.

The primary event provides the date, clock, timezone, and default duration.
Sequence instructions add starts after that implicit primary. An empty string
means the primary alone; it never means zero occurrences. The primary need not
be the earliest event: an explicit full date may add an earlier event.

Duration, title, location, notes, alarms, and per-occurrence overrides are not
encoded in this language. They remain part of the canonical event. Retaining a
moved occurrence's actual start through a text edit retains its original
RECURRENCE-ID and complete override. A new timestamp entered in the text is an
addition, not an implicit request to guess which existing event was moved.

Standard recurrence is used whenever it can represent the schedule exactly.
Irregular sets use standard RDATE/EXDATE and detached exceptions. The custom
definition, version, and anchor travel as X-DEBACAL-PATTERN metadata alongside
those standard properties. Canonical data always wins if another client leaves
the annotation stale. Local mutations refresh or clear stale annotations before
they enter history and the sync queue.

## Syntax

```ebnf
sequence     = [ finite ], [ separator, tail ] | tail ;
finite       = item, { separator, item } ;
item         = atom | positive_integer, "*", atom
             | [ positive_integer, "*" ], "(", finite, ")" ;
atom         = relative | date | clock | clock, "-", date ;
relative     = "+", positive_integer, [ "d" | "w" | "m" | "y" ] ;
date         = day, ".", [ month, [ ".", four_digit_year ] ] ;
clock        = hour, ":", two_digit_minute ;
tail         = "every(", positive_integer, [ "d" | "w" | "m" | "y" ],
               [ ",", ( "count=", positive_integer | "until=", full_date ) ], ")" ;
full_date    = day, ".", month, ".", four_digit_year ;
separator    = whitespace | ",", [ whitespace ] ;
```

Whitespace around operators is accepted where the parser permits it. A tail is
terminal: it cannot occur inside a repeated group or be followed by another
instruction. A tail has at most one bound. `every` is case insensitive.

Accepted legacy spellings include `3.2.` and `3.2.2027.`; generated complete dates
have no final dot. Leading zeros are optional on dates and hours. Clock minutes
use two digits. Units default to weeks: `+3` is `+3w`, and `every(2)` is
`every(2w)`. Previously saved `h`/`min` relative steps remain readable, but are
not generated; new time changes use explicit clocks.

### Finite instruction semantics

Evaluation starts with `cursor = primary.start` and a set containing that start.
Each atom computes a new cursor and adds it. Instructions are evaluated in their
written order; the result is a set of unique starts. A duplicate updates the
cursor without creating a second occurrence. Multipliers repeat instructions,
not the primary event. Thus `2*+1d` has three occurrences in total.

| Instruction | Meaning |
| --- | --- |
| `+Nd`, `+Nw` | Advance the cursor by N local calendar days or weeks, keeping its clock. |
| `+Nm`, `+Ny` | Advance calendar months or years, keeping the cursor's clock; clamp an invalid month day to that month's last day. Each step uses the previous result. |
| `D.` | Next matching day of a month strictly after the cursor, at the primary clock. Skip months without that day. |
| `D.M` | Next matching day/month strictly after the cursor, at the primary clock. Skip invalid years, including non-leap years for 29 February. |
| `D.M.YYYY` | That exact date, even if earlier than the cursor, at the primary clock. |
| `HH:MM` | That clock on the cursor's day if strictly later; otherwise on the next day. |
| `HH:MM-D…` | Apply the corresponding date instruction using the given clock instead of the primary clock. |
| `K*atom`, `K*(…)` | Evaluate the atom/group K times consecutively. |

For primary 7 November 2026 at 09:00, `13. +1 18. 2. 3.2 +2` adds
13 November, 20 November, 18 December, 2 January 2027, 3 February, and 17 February.
For a 09:00 primary, `10:00 11:00 9:00 10:00 11:00` describes six events over
two days including the primary.

### Regular tails and bounds

`every(1d)` includes the current cursor and continues daily without an end.
Weekly, monthly, and yearly tails use the corresponding standard RRULE.
Missing calendar dates are skipped: monthly recurrence on the 31st skips months
without a 31st. This deliberately differs from clipped finite `+1m` arithmetic.
The two operations express different intentions and must not be interchanged
unless their complete occurrence sets are equivalent.

`every(1d, count=3)` includes the cursor plus two later rule occurrences. Without
a prefix, that is three events in total, including the primary.
`+3 every(1w, count=4)` has the primary plus four weekly occurrences beginning
three weeks later: five events in total. Prefix dates before that cursor are
separate; they do not consume the tail's count.

`every(1d, until=13.11.2026)` includes rule occurrences on or before the given
local calendar date. The bound is a full date, so it cannot silently roll into a
different year. The final date must not precede the tail cursor. Prefix events
are not retrospectively restricted by a tail bound. COUNT and UNTIL are mutually
exclusive. A bound is part of a regular recurrence, not repeated expansion of a
finite instruction body.

The tail must be representable as a single standard rule anchored at DTSTART,
with finite additions/exclusions. Its interval phase must align with the
primary. For example, `+3 every(2w)` is rejected rather than silently changing
the interval. The compiler adjusts the rule's count for the pre-tail nominal
instances and excludes those not requested by the finite prefix.

### Timezone, identity, and limits

Timed instruction comparisons use exact UTC instants. Arithmetic is performed
in the series' authoring timezone, independent of the display timezone. All-day
events use floating calendar dates; any clock entries must stay at midnight.
Ambiguous relative/time-only clocks retain the preceding fold; explicit date
instructions retain the primary fold. Explicit nonexistent local times are
rejected with an actionable error; they are never silently rewritten.

Generated standard RRULE times follow iCalendar's timezone interpretation,
including [verified RFC 5545 erratum 4271](https://www.rfc-editor.org/errata/eid4271).
A generated spring-gap time is interpreted using the pre-gap offset and shown
at its corresponding real local time. It is not dropped after counting it.
Month/year dates that do not exist are skipped. Exclusions and exception
identities refer to the original occurrence instant, including moved events.

Finite instruction authoring is bounded to 500 steps including the primary,
with an 8,192-character input limit and eight nesting levels. Larger simple
bounded rules remain symbolic as `every(..., count=...)`; they are not expanded
into thousands of date tokens. Regular intervals are 1–999, bounded tail counts
are 1–100,000, and compilation visits at most 10,000 pre-tail nominal instances.
An unbounded schedule is never inferred from a
finite sample. Unsupported imported rules remain canonical calendar recurrences
with a preserved-recurrence editor state.

Some canonical calendars cannot be represented by this set language: two
distinct events with the same actual start, an excluded primary, or a detached
primary whose original start no longer occurs. Those retain their canonical
events rather than collapsing identities or inventing a primary. A simple
standard rule can still expose its regular controls at the nominal anchor.

## Generated normal form

Authored text and generated text have different preservation rules. Valid user
text keeps its exact spelling, spaces, grouping, cursor position, and undo
history through opening, title/location edits, and operations for which it
remains equivalent. Normalization happens when a graphical schedule edit or a
move actually requires a replacement definition. There is no live rewriting of
valid typed text on every keystroke.

For generation, the primary is first, then all other unique actual starts sorted
by UTC instant (calendar date for all-day events). This makes the result depend
on the schedule, not the order in which dates were clicked or RDATEs imported.
The intent priority is: preserve a verified authored program, retain a known
standard calendar rule, then infer a readable program from a bare finite set.
Dates alone cannot uniquely recover intent: the same two dates may be two months
apart, a monthly rule skipping February, or an explicit pair of appointments.
The normalizer chooses deterministically when that information is absent; it
does not claim to infer the user's original intention from those dates.

The finite normalizer considers:

1. Date instructions using day only, then day/month, then full year, each tested
   against the cursor. A year is included only when shorter date forms choose
   another instant. Complete dates have no trailing dot.
2. Clock-only or clock/date instructions when needed to reproduce a different
   start time. No clock is written when the primary clock is sufficient.
3. Verified relative steps and repeated strides/groups. Unrelated offsets such
   as `+8d +3d` lose to readable dates; real repetition such as `4*(+8d +3d)`
   remains useful.
4. Stable tie-breaking: fewer unpatterned offsets, fewer characters, fewer
   explicit years, shallower nesting, fewer groups, then lexical order.

The search is bounded, deterministic dynamic programming, not a claim to find
the globally shortest possible expression. Beyond its optimization budget it
uses a linear verified date/clock spelling with adjacent run compression. Correctness takes precedence over
compression. Regular or repeating structure is retained whenever a certified
candidate describes it; a singleton produces empty text.

For simple bounded standard rules, a bounded `every` is available when a finite
instruction list would be large or fail to express calendar skipping correctly.
For unbounded schedules, generation identifies a standard rule phase, emits
the finite affected prefix through one unaffected rule occurrence, then appends
`every(...)`. Skipping the second and third dates of an endless weekly series
therefore gives `+3 every(1w)`.

## Algorithm and correctness argument

The authoring layer first constructs a temporary actual-start view of the
canonical recurrence. Each moved override removes its nominal start and adds
its actual start. The projection is never persisted: standard exception IDs,
content, and durations remain on the original master.

For finite sets, each candidate instruction is evaluated with the same primary
and cursor semantics as the parser and retained only if it produces exactly its
target instant. The normalizer combines only valid transitions and repeated
blocks. It then evaluates the entire output and compares its unique instants
with the complete target set. The atomic transition property gives an induction
over the instruction list; the final equality check covers combination,
deduplication, grouping, and serialization mistakes.

For a standard tail, the compiler retains DTSTART, creates one RRULE, and visits
only the finite region preceding the tail cursor. Requested prefix dates missing
from the rule become RDATE; unwanted pre-tail rule dates become EXDATE. From the
tail cursor onward the generated rule has the specified phase, frequency, and
bound. These finite set differences plus the matching rule prove the complete
schedule, including its infinite suffix; a preview of a few months is not proof.

Two canonical schedules are certified equivalent by one of these routes:

- Same primary, timezone, and normalized rule: compare membership only at their
  differing finite addition/exclusion keys.
- Same simple regular phase and exact finite count, including COUNT versus an
  instant-precise UNTIL: the rule model establishes equal base sets, then the
  same finite-difference check applies.
- Otherwise, compare the complete bounded finite sets within the authoring
  budget. Different infinite rules are never certified by sampling.

Only a certified definition is adopted. A failed conversion leaves canonical
data intact and explains why the schedule remains a preserved recurrence.

## User operations

The same selection plan and pure recurrence projection are used by dragging,
the editor, keyboard operations, copy/paste, and calendar transfer. The operation
acts on the union of selected resources, without processing overlapping series
selections twice. An immutable reviewed source prevents stale edits from being
partially applied. Each committed batch is one undoable action.

| Scope | Edit/move/resize | Delete | Copy/paste |
| --- | --- | --- | --- |
| One | One ordinary event, or a standard detached exception for one recurrence. | Remove the ordinary event, or add EXDATE for its recurrence ID. | Copy the selected actual occurrence as an independent event. |
| All | Change the whole series, preserving its identity and recurrence structure. | Delete the complete series. | Copy the full canonical series with independent resource/UID identities. |
| Following | Split at the selected original recurrence ID and change the suffix. | Keep only the prefix before that recurrence ID. | Copy the suffix as an independent series; the original is unchanged. |

The split boundary is inclusive in the suffix and exclusive in the prefix.
Moved exceptions are partitioned by original recurrence ID, not by the date to
which they were dragged. This prevents an earlier nominal occurrence moved into
next month from migrating into an unrelated later suffix. If the boundary is
the first occurrence, there is no empty prefix resource. Standard rules remain
standard rules after splitting. A truncated unbounded prefix becomes bounded.
Splitting a relative finite program slices its instructions instead of inferring
new units from the dates: splitting `4*+1m` before its third event leaves a
`+1m` prefix and a `2*+1m` suffix. Both retain month clipping when moved later.
At an unsupported identity boundary, or a generated timezone-gap range whose
new primary would change the rule's phase/duration, the split fails atomically
with an explanation instead of silently changing its meaning.

For an endless daily series split before its fourth event, the prefix has three
occurrences. The editor's count, final included date, custom equivalent
`2*+1d`, and preview must all agree. An exact UNTIL one second before the fourth
event must not be rounded up to include that event's whole day.

### Moving and preserving definitions

An authored finite program made entirely of relative instructions retains those
instructions and is evaluated from the new primary. This includes calendar
clipping: `2*+1m` from 31 January gives 31 January, 28 February, 28 March; moving
the primary to 30 January gives 30 January, 28 February, 28 March. It does not
turn into 30 January, 27 February, 27 March. This is the chosen behavior for
relative definitions, including grouped instructions.

An explicitly dated finite set is translated by the same local wall-time delta.
A standard RRULE retains its calendar pattern while its phase and bounds move.
Individual override ranges and nominal IDs follow the corresponding logical
occurrence; moving alone keeps their distinct durations. The displayed and
persisted result must be the same whether the operation comes from the mouse,
dialog, or paste. Calendar-dependent relative programs remap occurrence IDs by
instruction position, rather than assuming every month step moved equally.

After projecting the canonical move, evaluate the existing text at the new
primary. If it still matches, retain it byte for byte. This applies to mixed
clock/date text as well as purely relative instructions. In particular, moving
`10:00 11:00 9:00 10:00 11:00` to another day need not change its definition.
If the old text no longer matches, reconstruct a certified concise definition.
Moving first-of-month events by two days should yield `4*3.`, not a list of
year-qualified dates and not the unchanged, now incorrect `4*1.`.

For date constraints this chooses moving the explicit dates over retaining
now-incorrect calendar constraints. For relative instructions it chooses
preserving the program over translating its old expansion. These are deliberate
differences between relative and date instructions, independent of whether the
canonical finite data happens to fit RRULE or RDATE.

Resizing changes duration without regenerating a valid start sequence. Changes
to title, location, notes, calendar, or alternative-slot status also leave valid
authoring text unchanged. Moving one occurrence updates that occurrence's
clock/date in the sequence while retaining its original exception identity.

### Editor transitions and pending input

Regular controls and Custom are views of one schedule. A regular pattern is
shown only when it can describe the complete edited schedule. Graphical
additions, deletions, and time changes may turn it into Custom; an exact regular
set may be recognized again. Regular count includes its first occurrence.
Until represents the last included date in the controls and is computed from
the exact rule cutoff before synchronizing count.
Relative calendar programs with possible month/year clipping remain Custom
even if two current dates happen to look like a regular day interval. Safe
calendar cycles can use regular controls; finite sample coincidence alone must
not change the meaning of extending a program's count.

An explicit Custom-to-regular change replaces custom additions/exclusions with
the chosen regular pattern. The per-scope custom draft is remembered so that an
immediate switch back restores it. This replaces the old behavior of retaining
hidden exclusions while showing apparently clean regular controls. Single,
Repeat, and Alternatives retain independent valid state; alternative slots use
the same scheduling semantics until one slot is confirmed.

Invalid pending text cannot save an older valid schedule in its place. Parsing
and conversion are transactional. Selection, insertion position, and text undo
remain intact while valid text is being edited. Scope shortcuts use the same
configured keys inside and outside the dialog.

## Verification

The regression suites cover syntax, normalization and complete-set proofs;
count/date limits and exact split cutoffs; month/year skipping versus clipping;
timezones and DST; actual moved exceptions and their content; and real GUI
operations on week, month, and all-day surfaces. Combined scenarios exercise
creation → drag → split → editor → ICS → undo/redo, plus clipboard transfer,
calendar changes, and Custom/regular mode memory. Unsupported conversions must
preserve data or fail atomically, never silently substitute a daily recurrence.

The implementation entry points are:

- `patterns.py`: grammar, evaluator, tail compiler, finite normalizer.
- `regular_schedule.py`: shared regular rule/count/exact-boundary model.
- `schedule_authoring.py`: actual-start projection and equivalence certificates.
- `event_schedule.py`: transactional editor draft and mode transitions.
- `recurrence.py`, `series_operations.py`: move and split projections.
- `store.py`: reviewed atomic selection operations, annotation reconciliation,
  history, and independently queued calendar resources.
