# Calendar interoperability

Debacal needs three separate compatibility checks: interpreting imported data,
preserving its meaning through persistence and editing, and producing resources
a CalDAV server accepts. A successful parse alone does not establish any of
these later properties. The normal test suite includes synthetic regressions;
the optional corpus tools exercise independently produced public data.

## What is tested

`scripts/check_calendar_interop.py` checks each file through:

1. Import, including partial acceptance and retained rejected resources.
2. SQLite persistence, comparing event semantics before and after storage.
3. A title edit, followed by another persistence check.
4. Individual file export, CalDAV upload normalization, and combined export.
5. Re-import and comparison of endpoints, recurrence, editable content, and
   retained properties, including alarms and attendees.
6. Optionally, independent Sabre VObject `PROFILE_CALDAV` validation of uploads.

The audit runs offline, limits input size and time per file, and reports relative
source paths, hashes, counts and the failing stage. It omits event titles, UIDs,
calendar contents and exception messages, and records dependency versions and
application source hashes. Source filenames can themselves be identifying.
Downloaded samples stay outside Git;
committed regressions contain synthetic data. An optional downloader fetches only
immutable GitHub blobs, checking both their Git object identity and SHA-256.

`--report-only` permits an exploratory corpus to contain unclassified rejected,
partial, non-event or incomplete test inputs. It still fails on data loss,
unexpected exceptions, timeouts or server rejection. It is not a pass rate for
all input files. Strict runs require explicit classifications for known
unsupported inputs; unexpected changes to their outcomes fail too.

## Findings from the 2026-11-10 audit

The lifecycle audit considered **181 public files** and independently validated
**1,250 accepted event resources**, with no remaining lifecycle mismatches or
CalDAV validation errors in those accepted resources. Unsupported resources are
excluded from that success count, and partial files remain explicitly partial.

| Source | Files | Complete passes | Partial files | Other outcomes | Accepted resources |
| --- | ---: | ---: | ---: | --- | ---: |
| Python `icalendar` 7.3.0 fixtures | 163 | 43 | 4 | 42 rejected, 18 non-event, 56 incomplete | 56 |
| ICAL.js and KDE KCalendarCore samples | 16 | 12 | 2 | 2 unsupported recurrence files | 32 |
| FOSDEM 2026 schedule and GOV.UK bank holidays | 2 | 2 | 0 | None | 1,162 |

Library suites intentionally include malformed calendars and standalone property
or component fragments. Those are not all intended to be importable events.
Producer samples include Google, Mozilla, Evolution, KOrganizer, eGroupware and
old Apple iCal data. The two public feeds add a large conference export and an
all-day holiday calendar. This is a concrete compatibility sample, not a claim
that every product or version has been tested.

Independent validation used Sabre VObject **5.0.0**, pinned to revision
`5e2f338811916c03927398bb24e527989de63194`, under PHP 8.5.12. SabreDAV uses
these [CalDAV validation rules](https://github.com/sabre-io/vobject/blob/5e2f338811916c03927398bb24e527989de63194/lib/Component/VCalendar.php).
These checks do not make live changes to Nextcloud or test authentication,
authorization, quota limits or scheduling-message delivery.

The audit found and fixed:

- Global IANA spellings such as `/Europe/Stockholm`, without overriding explicit
  timezone definitions.
- A two-hour shift when persisting an ambiguous time from a custom timezone with
  negative daylight saving time. UTC conversion now uses prepared transition
  indexes, including the original offsets and both repeated-hour phases.
- Generated timezone definitions must match the native timezone around each
  transition, not just on ordinary dates. The audit checks forward changes,
  negative daylight time and historical Moroccan Ramadan changes independently
  of the maintained library's timezone generator.
- UTF-8 sequences split across folded physical lines. Bytes are unfolded before
  decoding, as prescribed by [RFC 5545 section 3.1](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.1).
- Explicit legacy quoted-printable display text, including charset handling and
  the ordering of transfer decoding versus iCalendar escaping.
- Redundant `DTEND` and `DURATION` declarations when their meanings agree.
  Nominal-day recurrences that could diverge across DST remain retained instead
  of choosing one interpretation.
- Date-only `RDATE`/`EXDATE` values with a spurious `Z`, only in an all-day
  series whose date context is already known.
- Multiple VCALENDAR documents in one stream, preserving each envelope's
  timezone scope. Competing versions of one UID are retained for review.
  Combined export uses separate envelopes when independently established
  explicit timezone definitions cannot safely share one calendar.
- Missing, duplicated or obsolete storage-envelope headers and malformed header
  token spacing. CalDAV output is normalized without discarding unrelated event
  metadata. Exact duplicate singleton metadata can be collapsed; conflicting
  organizers, statuses or similar fields remain queued with a specific issue.

## Safe repairs and remaining gaps

Repairs need a defensible interpretation. Debacal must not guess an unknown
timezone, discard recurrence exceptions, choose between conflicting organizers,
or relabel a non-Gregorian calendar and silently change its dates. Rejected
resources remain inspectable and recoverable; healthy peers can continue.

The public corpus still exposes these limitations:

- `RECURRENCE-ID;RANGE=THISANDFUTURE` needs range exceptions throughout the model,
  indexed projections, operations and export. Removing `RANGE` or expanding a
  finite horizon would lose its meaning. Mozilla implements it using ordered
  range exceptions and lookup of the preceding change.
- Detached occurrences without their master need identity reconciliation with
  the original series. Importing them as ordinary masters can create duplicates.
- Old Apple iCal data contains underspecified VTIMEZONEs with offsets but no
  onset dates or transition rules. Repair requires an explicit timezone choice;
  an apparently familiar name does not justify replacing an explicit definition.
- Legacy EXRULE exclusions, multiple RRULEs, mixed/contradictory date types and
  non-Gregorian CALSCALE values remain explicit unsupported cases.

The next substantial compatibility work should address range exceptions and
detached-instance reconciliation. A user-approved timezone repair workflow is
appropriate for the underspecified definitions; permissive parsing alone cannot
recover missing scheduling information.

The timezone fixes prevent subsequent conversions from shifting dates. They do
not guess repairs to already edited or previously corrupted dates; those need a
reviewed re-import or synchronization with their original source.

## Repeating the checks

The normal automated suite covers synthetic reproductions without network or
PHP dependencies. To audit the installed library's intentionally mixed fixtures:

```sh
uv run python scripts/check_calendar_interop.py --installed-icalendar \
  --report-only --output /tmp/debacal-library-audit.json
```

To reproduce the pinned client corpus, downloading public data explicitly:

```sh
uv run python scripts/fetch_calendar_corpus.py /tmp/debacal-public-corpus
uv run python scripts/check_calendar_interop.py /tmp/debacal-public-corpus \
  --expectations tests/interop_expectations.json \
  --output /tmp/debacal-client-audit.json
```

The checked-in expectations use that corpus directory name. With another name,
update the relative source labels in a copy of the expectations file.

For independent validation, install PHP and Composer separately, prepare a
temporary validator, and pass its autoloader:

```sh
mkdir -p /tmp/debacal-sabre
composer --working-dir=/tmp/debacal-sabre require sabre/vobject:5.0.0
uv run python scripts/check_calendar_interop.py /tmp/debacal-public-corpus \
  --expectations tests/interop_expectations.json \
  --sabre-autoload /tmp/debacal-sabre/vendor/autoload.php \
  --output /tmp/debacal-server-audit.json
```

Run both the synthetic tests and corpus audit before releasing parser, timezone
or upload-normalization changes. Add a minimal synthetic regression whenever a
new public or privately reported case exposes a defect. Keep unsupported cases
classified, and require review when a future implementation changes an outcome.

## Sources and comparison implementations

Immutable fixture URLs, content hashes, upstream licensing references and public
feed snapshots are recorded in [interop_sources.json](../tests/interop_sources.json).
Feed hashes describe the downloaded snapshot; public feeds can change.

- [Python icalendar](https://github.com/collective/icalendar), BSD-2-Clause:
  producer regressions and error-tolerant parsing examples.
- [Mozilla ICAL.js](https://github.com/kewisch/ical.js), MPL-2.0:
  [calendar streams](https://github.com/kewisch/ical.js/blob/main/lib/ical/parse.js)
  and [range exceptions](https://github.com/kewisch/ical.js/blob/main/lib/ical/event.js).
- [KDE KCalendarCore compatibility data](https://github.com/KDE/kcalendarcore/tree/master/autotests/data/Compat)
  and its separate [legacy vCalendar parser](https://github.com/KDE/kcalendarcore/blob/master/src/vcalformat.cpp).
- [libical parser](https://github.com/libical/libical/blob/master/src/libical/icalparser.c):
  tolerant handling is useful precedent, but removing an invalid critical
  property would be unsafe for Debacal's editable, synchronized model.
- [Home Assistant's ical compatibility guide](https://allenporter.github.io/ical/ical/guides/compatibility.html):
  concrete producer errors and the tradeoffs in interpreting them.
- [FOSDEM schedule](https://archive.fosdem.org/2026/schedule/) and
  [GOV.UK bank holidays](https://www.gov.uk/bank-holidays): public feed publishers.
