# Calendar format support

Debacal reads and writes iCalendar VEVENT resources using the `icalendar` and
`python-dateutil` libraries. It keeps recurring masters and exceptions together,
expanding occurrences only for requested views. CalDAV transport uses vdirsyncer.
This documents tested behavior, rather than claiming complete RFC compliance.

## Dates, times and timezones

- UTC date-times, named IANA zones, floating local date-times, and all-day DATE
  values retain their respective meanings through import, storage and export.
- A matching embedded VTIMEZONE supplies the event's actual timezone rules.
  Its TZID need not be an IANA name: Windows identifiers such as
  `W. Europe Standard Time` work with their supplied STANDARD/DAYLIGHT rules.
  Explicit rules take priority even when the identifier resembles a database
  name. Another resource reusing the same name cannot replace these rules.
- Custom definitions accompany edits, copied/split series and synchronized
  resources. Unknown names without usable definitions produce an issue instead
  of silently guessing an offset or alias.
- Without an embedded definition, documented libical, Citadel and Mozilla
  namespace prefixes resolve to their IANA database location. For example,
  `/freeassociation.sourceforge.net/Europe/Berlin` uses `Europe/Berlin`, as does
  the older `/freeassociation.sourceforge.net/Tzfile/Europe/Berlin` form.
  Matching embedded definitions still take priority; arbitrary vendor prefixes
  are not guessed. Database aliases are normalized consistently through storage,
  edits and export without rebinding unrelated resource-local definitions.
  Global forms such as `/Europe/Stockholm` also resolve when the entire name
  after the leading slash is an available IANA database identifier.
- Floating times retain their wall clocks when the viewing system's zone changes.
  All-day events retain calendar dates, including across daylight-saving changes.
  Floating durations use wall-clock arithmetic, independent of the viewing zone.
- Explicit ambiguous clocks use the first occurrence; nonexistent clocks use
  the pre-transition offset, as specified by RFC 5545. Export uses UTC where
  necessary to distinguish an explicitly selected second repeated-hour endpoint.
- DURATION preserves the difference between nominal days (`P1D`) and elapsed
  hours (`PT24H`) across daylight-saving transitions. DTEND remains exclusive.
- Leap-second values use second 59 as recommended for implementations without
  leap-second clocks. Invalid DATE/DATE-TIME endpoint combinations are reported
  instead of being silently reinterpreted.

## Recurrence and retained data

RRULE, RDATE date/date-time and PERIOD values, EXDATE, cancelled occurrences and
detached RECURRENCE-ID edits are preserved. PERIOD values retain each occurrence's
individual end or duration; export represents them with standard RDATE starts
and detached event ranges. Floating rules retain floating UNTIL values;
zoned rules use UTC UNTIL values. The editor's custom sequence metadata augments
the standard recurrence rather than replacing it for other clients.

Parsing handles case-insensitive property/component names, quoted parameters,
escaped text and folded UTF-8 lines, including folds inside a multi-byte
character. Explicit legacy quoted-printable display text is decoded with its
declared character set. Unrecognized event properties and nested
alarms survive edits; retaining an alarm does not imply delivering notifications
for it. Blank or absent SUMMARY uses an untitled display name.

Multiple VCALENDAR documents can be imported without mixing their timezone
contexts. Independently established explicit definitions that share a TZID can
be exported as separate envelopes in a standard calendar stream. Competing
versions of the same UID across documents are retained for review.

Safe compatibility repairs include equal redundant DTEND/DURATION declarations
and date-only RDATE/EXDATE values with a meaningless `Z` in an all-day series.
Conflicting declarations and recurring nominal durations that could diverge
across DST remain reviewable. CalDAV uploads normalize required envelope
headers, omit scheduling METHOD, and collapse exact duplicate singleton
metadata. Conflicting organizers or statuses stay queued with a specific issue.

## Limits and recovery

These cases currently require review instead of being silently approximated:

- Detached-only UID resources without a master; multiple RRULE properties;
  legacy EXRULE; RECURRENCE-ID with RANGE=THISANDFUTURE.
- RDATE periods mixing floating and fixed time modes within the same UID.
  Fixed periods may use a different timezone from the master.
- Sub-minute recurrence frequencies and rules exceeding the bounded expansion
  budget. Dense or oversized timezone definitions also have computation/size
  limits; ordinary historic yearly transition rules are supported.
- A native event whose retained raw template contradicts its modeled timezone.
  Separate export/review is required when the source does not establish which
  definition owns its timestamps. Non-Gregorian CALSCALE values are unsupported.

A bad UID does not block healthy neighbors. The source is retained privately,
with event-level diagnostics and inspection/export/retry actions under **Issues**
in the sidebar and **Review issues** in the import dialog. Retained local-file
failures can be retried through the reviewed import dialog. A successful later
sync or corrected import of the same UID clears its retained issue. VTODO and
other non-event resources remain in the sync mirror without appearing as events.

Tests use synthetic calendars for timezone transitions, historic rules, scoped
name collisions, floating clocks, SQLite persistence, editing, copies, splits,
recurrence exceptions, sync comparisons and import/export round trips. Reference:
[RFC 5545](https://www.rfc-editor.org/rfc/rfc5545.html), especially sections
3.3.5, 3.3.6, 3.3.10, 3.6.1 and 3.6.5.

The [interoperability audit](INTEROPERABILITY.md) records public producer
samples, independent server validation, reproducible corpus tools and known gaps.
