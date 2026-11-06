"""Import inclusion keeps only chosen occurrences using standard recurrence."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from debacal.ics import export_ics, parse_ics_report
from debacal.ics_resources import ParseReport, ResourceIssue, RejectedResource
from debacal.import_preparation import remap_calendar
from debacal.import_selection import ImportSelection, display_date
from debacal.models import Event
from debacal.recurrence import expand_events


UTC = timezone.utc
START = datetime(2026, 11, 2, 9, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)


def event(identifier='one', *, start=START, **values):
    return Event(identifier, 'preview', identifier.title(), start, start + timedelta(hours=1), **values)


def instances(report, start=START - timedelta(days=1), end=END):
    return expand_events(report.events, start, end)


def effect(events):
    return sorted(((item.start.date().isoformat() if item.all_day else item.start.isoformat()),
                   (item.end.date().isoformat() if item.all_day else item.end.isoformat()),
                   item.title, item.location, item.all_day)
                  for item in events)


def serialized(report):
    return parse_ics_report(export_ics(report.events), 'imported')


def recurrence_report():
    raw = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:weekly-test\r\n"
           "DTSTART:20261102T090000Z\r\nDTEND:20261102T100000Z\r\n"
           "RRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Weekly meeting\r\nX-TEST:Preserve metadata\r\n"
           "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nDESCRIPTION:Reminder\r\nEND:VALARM\r\nEND:VEVENT\r\n"
           "BEGIN:VEVENT\r\nUID:weekly-test\r\nRECURRENCE-ID:20261109T090000Z\r\n"
           "DTSTART:20261111T140000Z\r\nDTEND:20261111T160000Z\r\nSUMMARY:Moved meeting\r\n"
           "LOCATION:Other room\r\nX-TEST-OVERRIDE:Preserve exception metadata\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    return parse_ics_report(raw.encode(), 'preview')


def test_default_includes_everything_and_empty_selection_never_imports_events():
    report = ParseReport((event(), event('two', start=START + timedelta(days=1))))
    selection = ImportSelection()
    assert all(selection.is_selected(value) for value in report.events)
    assert selection.snapshot().filter_report(report) == report
    selection.select_none()
    assert not selection.has_selection and not selection.snapshot().has_selection
    assert selection.snapshot().filter_report(report).events == ()
    selection.select_all()
    assert selection.snapshot().filter_report(report) == report


def test_ordinary_toggles_and_snapshots_are_independent_of_later_selection_changes():
    first, second = event(), event('two')
    report = ParseReport((first, second))
    selection = ImportSelection()
    selection.toggle(first)
    frozen = selection.snapshot()
    assert frozen.filter_report(report).events == (second,)
    selection.toggle(first)
    assert selection.snapshot().filter_report(report) == report
    assert frozen.filter_report(report).events == (second,)
    selection.select_none()
    selection.toggle(first)
    assert selection.snapshot().filter_report(report).events == (first,)


def test_excluding_occurrences_preserves_rrule_and_cannot_export_unselected_overrides():
    report = recurrence_report()
    original = report.events[0]
    shown = instances(report)
    moved = next(item for item in shown if item.title == 'Moved meeting')
    last = max(shown, key=lambda item: item.start)
    selection = ImportSelection()
    selection.set_selected((moved, last), False)
    chosen = selection.snapshot().filter_report(report)
    master = chosen.events[0]
    assert master.recurrence_rule == original.recurrence_rule
    assert (master.id, master.ical_uid) == (original.id, original.ical_uid)
    assert {moved.recurrence_id, last.recurrence_id} <= set(master.excluded_dates)
    assert moved.recurrence_id not in master.recurrence_overrides
    expected = [item for item in shown if item.id not in {moved.id, last.id}]
    assert effect(instances(chosen)) == effect(expected)
    assert effect(instances(serialized(chosen))) == effect(expected)
    raw = export_ics(chosen.events).decode()
    assert 'X-TEST:Preserve metadata' in raw and 'BEGIN:VALARM' in raw
    assert 'X-TEST-OVERRIDE' not in raw
    assert report.events[0] == original and len(original.recurrence_overrides) == 1


def test_deselecting_every_occurrence_of_a_finite_series_drops_its_empty_master():
    report = recurrence_report()
    selection = ImportSelection()
    selection.set_selected(instances(report), False)
    assert not selection.snapshot().filter_report(report).events


def test_unbounded_series_keeps_future_dates_when_only_specific_occurrences_are_excluded():
    source = event(recurrence_rule='FREQ=WEEKLY')
    report = ParseReport((source,))
    shown = instances(report)
    selection = ImportSelection()
    selection.set_selected(shown, False)
    result = selection.snapshot().filter_report(report)
    assert result.events[0].recurrence_rule == source.recurrence_rule
    future = instances(result, END, END + timedelta(days=30))
    assert future


def test_explicit_subset_uses_finite_rdates_and_preserves_selected_moved_exceptions():
    report = recurrence_report()
    shown = instances(report)
    moved = next(item for item in shown if item.title == 'Moved meeting')
    selected = (shown[0], moved, shown[-1])
    selection = ImportSelection(False)
    selection.set_selected(selected, True)
    result = selection.snapshot().filter_report(report)
    assert len(result.events) == 1
    master = result.events[0]
    assert not master.recurrence_rule and len(master.recurrence_dates) == 2
    assert master.ical_uid != report.events[0].ical_uid
    assert effect(instances(result)) == effect(selected)
    assert effect(instances(serialized(result))) == effect(selected)
    assert not instances(result, END, END + timedelta(days=3650))
    raw = export_ics(result.events).decode()
    assert 'BEGIN:VALARM' in raw and 'X-TEST-OVERRIDE:Preserve exception metadata' in raw
    # Stable partial identity permits retrying the same explicit selection.
    assert selection.snapshot().filter_report(report).events[0].ical_uid == master.ical_uid


def test_one_selected_override_is_detached_with_effective_fields_alarms_and_new_identity():
    report = recurrence_report()
    moved = next(item for item in instances(report) if item.title == 'Moved meeting')
    selection = ImportSelection(False)
    selection.toggle(moved)
    result = selection.snapshot().filter_report(report)
    assert len(result.events) == 1 and not result.events[0].recurring
    assert result.events[0].ical_uid != report.events[0].ical_uid
    assert effect(result.events) == effect((moved,))
    assert effect(instances(serialized(result))) == effect((moved,))
    raw = export_ics(result.events).decode()
    assert 'BEGIN:VALARM' in raw and 'X-TEST-OVERRIDE' in raw
    assert 'RECURRENCE-ID' not in raw and 'RRULE' not in raw and 'RDATE' not in raw


def test_unbounded_rule_can_be_imported_as_only_two_selected_dates():
    report = ParseReport((event(recurrence_rule='FREQ=WEEKLY'),))
    chosen = instances(report)[::2]
    selection = ImportSelection(False)
    selection.set_selected(chosen, True)
    result = selection.snapshot().filter_report(report)
    assert len(result.events) == 1 and not result.events[0].recurrence_rule
    assert effect(instances(result)) == effect(chosen)
    assert not instances(result, END, END + timedelta(days=36500))


def test_all_day_point_and_multiday_occurrences_are_whole_import_choices():
    day = START.replace(hour=0)
    all_day = replace(event('trip', start=day, all_day=True, recurrence_rule='FREQ=WEEKLY;COUNT=2'),
                      end=day + timedelta(days=3))
    point = replace(event('point'), end=START)
    report = ParseReport((all_day, point))
    shown = instances(report)
    trip = next(item for item in shown if item.all_day)
    selection = ImportSelection()
    selection.toggle(trip)
    assert not selection.is_selected(trip)
    assert selection.is_selected(point)
    result = selection.snapshot().filter_report(report)
    assert len(instances(result)) == 2
    remaining = next(item for item in instances(result) if item.all_day)
    assert remaining.end - remaining.start == timedelta(days=3)
    assert effect(instances(serialized(result))) == effect(instances(result))


def test_day_toggle_uses_display_start_date_and_reincludes_mixed_day_in_one_click():
    zone = ZoneInfo('Europe/Berlin')
    first = event(start=START.replace(hour=23, minute=30))
    second = event('two', start=START + timedelta(days=1, hours=4))
    all_day = event('day', start=START.replace(hour=0), all_day=True)
    with patch('debacal.import_selection.local_zone', return_value=zone):
        assert display_date(first) == date(2026, 11, 3)
        assert display_date(all_day) == date(2026, 11, 2)
        selection = ImportSelection()
        selection.toggle(first)
        selection.toggle_date(date(2026, 11, 3), (first, second, all_day))
        assert selection.is_selected(first) and selection.is_selected(second)
        selection.toggle_date(date(2026, 11, 3), (first, second, all_day))
        assert not selection.is_selected(first) and not selection.is_selected(second)
        assert selection.is_selected(all_day)


def test_subset_clears_authoring_text_that_would_describe_unselected_dates():
    source = event(recurrence_dates=((START + timedelta(days=7)).isoformat(),),
                   schedule_expression='+1', expression_anchor='2026-11-02', schedule_syntax=2)
    report = ParseReport((source,))
    selection = ImportSelection(False)
    selection.toggle(instances(report)[-1])
    result = selection.snapshot().filter_report(report).events[0]
    assert result.schedule_expression == result.expression_anchor == ''


def test_final_duplicate_definition_cannot_resurrect_from_an_earlier_file_after_deselection():
    old = event()
    final = replace(old, title='Final version')
    selection = ImportSelection()
    selection.toggle(final)
    snapshot = selection.snapshot(masters=(final,))
    assert not snapshot.filter_report(ParseReport((old,))).events
    assert not snapshot.filter_report(ParseReport((final,))).events
    selection.select_all()
    snapshot = selection.snapshot((final,))
    assert not snapshot.filter_report(ParseReport((old,))).events
    assert snapshot.filter_report(ParseReport((final,))).events == (final,)


def test_issues_are_preserved_for_review_without_creating_empty_events():
    issue = ResourceIssue('bad', 'Malformed input')
    rejected = RejectedResource('bad', 'synthetic raw resource', 'bad', issue.reason)
    report = ParseReport((event(),), (issue,), (rejected,))
    selection = ImportSelection(False)
    result = selection.snapshot().filter_report(report)
    assert not result.events and result.issues == report.issues and result.rejected == report.rejected


def test_partial_native_metadata_cannot_reuse_the_original_full_series_identity():
    source = event('native-event', recurrence_rule='FREQ=WEEKLY;COUNT=4')
    report = parse_ics_report(export_ics((source,)), 'preview')
    selection = ImportSelection(False)
    selection.set_selected(instances(report)[::2], True)
    selected = selection.snapshot().filter_report(report)
    moved = remap_calendar(selected, 'preview')
    assert moved.events[0].id != source.id
    assert effect(instances(serialized(moved))) == effect(instances(selected))
