"""Floating dates stay valid through midnight gaps and date-line changes."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.event_schedule import ScheduleDraft
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import compile_sequence, expand_sequence, reconstruct_sequence
from debacal.preview_navigation import PreviewNavigation
from debacal.recurrence import expand_event, occurrence, overlaps, recurrence_key, recurrence_set
from debacal.schedule_authoring import expression_event, expression_matches, reconstruct_authoring


@pytest.mark.parametrize(('zone_name', 'missing_midnight'), [
    ('Africa/Cairo', date(2026, 4, 24)),
    ('America/Sao_Paulo', date(2018, 11, 4)),
    ('Pacific/Apia', date(2011, 12, 30)),
])
@pytest.mark.parametrize('begin_on_transition', [False, True])
def test_all_day_language_graphical_edits_and_wire_use_calendar_dates(zone_name, missing_midnight, begin_on_transition):
    zone = ZoneInfo(zone_name)
    first = missing_midnight if begin_on_transition else missing_midnight - timedelta(days=1)
    anchor = datetime.combine(first, time.min, zone)
    expected = [first + timedelta(days=offset) for offset in range(3)]
    prototype = Event('floating', 'local', 'Floating dates', anchor,
                      datetime.combine(first + timedelta(days=1), time.min, zone), all_day=True)

    for text in ('+0d +1d +1d', 'every(1d, count=3)'):
        compiled = compile_sequence(text, anchor, all_day=True)
        assert compiled is not None
        assert [value.date() for value in expand_sequence(text, anchor, all_day=True)] == expected
        master = expression_event(prototype, text)
        assert [value.date() for value in recurrence_set(master)] == expected
        assert expression_matches(master, text)
        rebuilt = reconstruct_authoring(master)
        assert expression_matches(master, rebuilt.expression)

        navigation = PreviewNavigation([master])
        occurrences = navigation.finite_events()
        assert [item.start.date() for item in occurrences] == expected
        assert [navigation.position(item) for item in occurrences] == [(1, 3), (2, 3), (3, 3)]

        draft = ScheduleDraft.from_event(master)
        removed = draft.toggle_date(master, missing_midnight)
        actual = list(recurrence_set(removed))
        assert [value.date() for value in actual] == [day for day in expected if day != missing_midnight]
        restored = draft.toggle_date(removed, missing_midnight)
        assert [value.date() for value in recurrence_set(restored)] == expected
        loaded, = parse_ics(export_ics([restored]), 'local')
        begin = datetime.combine(first, time.min, timezone.utc)
        end = begin + timedelta(days=3)
        assert [item.start.date() for item in expand_event(loaded, begin, end)] == expected

    dates = [datetime.combine(day, time.min, zone) for day in expected]
    text = reconstruct_sequence(dates, anchor=anchor, all_day=True)
    assert [value.date() for value in expand_sequence(text, anchor, all_day=True)] == expected


@pytest.mark.parametrize('recurring', [False, True])
@pytest.mark.parametrize('all_day', [False, True])
def test_window_overlap_uses_floating_dates_only_for_all_day(recurring, all_day):
    origin, display = ZoneInfo('Pacific/Kiritimati'), ZoneInfo('Pacific/Honolulu')
    first = datetime(2026, 11, 9, tzinfo=origin)
    master = Event('event', 'local', 'Date-line boundary', first, first + timedelta(days=1),
                   all_day=all_day, recurrence_rule='FREQ=DAILY;COUNT=3' if recurring else '')
    begin, end = datetime(2026, 11, 9, tzinfo=display), datetime(2026, 11, 10, tzinfo=display)
    expected = [date(2026, 11, 9)] if all_day else ([date(2026, 11, 10)] if recurring else [])
    assert [item.start.date() for item in expand_event(master, begin, end)] == expected


def test_floating_overlap_keeps_exclusive_midnight_and_includes_partial_day_windows():
    zone = ZoneInfo('Pacific/Kiritimati')
    start = datetime(2026, 11, 9, tzinfo=zone)
    master = Event('event', 'local', 'Floating day', start, start + timedelta(days=1), all_day=True)
    display = ZoneInfo('Pacific/Honolulu')
    assert overlaps(master, datetime(2026, 11, 9, 9, tzinfo=display), datetime(2026, 11, 9, 10, tzinfo=display))
    assert overlaps(master, datetime(2026, 11, 8, 23, tzinfo=display), datetime(2026, 11, 9, 1, tzinfo=display))
    assert not overlaps(master, datetime(2026, 11, 8, tzinfo=display), datetime(2026, 11, 9, tzinfo=display))
    assert not overlaps(master, datetime(2026, 11, 10, tzinfo=display), datetime(2026, 11, 11, tzinfo=display))


@pytest.mark.parametrize('override_all_day', [False, True])
def test_date_line_projection_retains_detached_ranges_and_orders_truncation(override_all_day):
    origin, display = ZoneInfo('Pacific/Kiritimati'), ZoneInfo('Pacific/Honolulu')
    first = datetime(2026, 11, 8, tzinfo=origin)
    master = Event('series', 'local', 'Floating base', first, first + timedelta(days=1),
                   all_day=True, recurrence_rule='FREQ=DAILY;COUNT=3')
    nominal = first + timedelta(days=2)
    chosen = datetime(2026, 11, 9, 0 if override_all_day else 9, tzinfo=display)
    detached = replace(occurrence(master, nominal), title='Moved exception', start=chosen,
                       end=chosen + (timedelta(days=1) if override_all_day else timedelta(hours=1)),
                       all_day=override_all_day, timezone_id=display.key)
    master = replace(master, recurrence_overrides={recurrence_key(nominal, True): detached.to_dict()})
    begin, end = datetime(2026, 11, 9, tzinfo=display), datetime(2026, 11, 10, tzinfo=display)
    actual = expand_event(master, begin, end)
    assert {item.title for item in actual} == {'Floating base', 'Moved exception'}
    assert {item.start.date() for item in actual} == {date(2026, 11, 9)}
    if not override_all_day:
        assert [item.title for item in expand_event(master, begin, end, limit=1, truncate=True)] == ['Floating base']


def test_skipped_local_day_can_still_be_queried_as_a_floating_calendar_date():
    zone = ZoneInfo('Pacific/Apia')
    start, end = datetime(2011, 12, 30, tzinfo=zone), datetime(2011, 12, 31, tzinfo=zone)
    master = Event('skipped', 'local', 'Floating date', start, end, all_day=True,
                   recurrence_rule='FREQ=DAILY;COUNT=2')
    assert start.timestamp() == end.timestamp()
    assert [item.start.date() for item in expand_event(master, start, end)] == [date(2011, 12, 30)]


def test_mixed_timed_exceptions_in_a_floating_family_keep_repeated_hour_instant_order():
    zone = ZoneInfo('Europe/Berlin')
    first = datetime(2026, 10, 24, tzinfo=zone)
    master = Event('mixed', 'local', 'Floating family', first, first + timedelta(days=1), all_day=True,
                   recurrence_rule='FREQ=DAILY;COUNT=4', excluded_dates=('2026-10-24', '2026-10-25'))
    overrides = {}
    for key, minute, fold in [('2026-10-26', 45, 0), ('2026-10-27', 15, 1)]:
        nominal = datetime.fromisoformat(key).replace(tzinfo=zone)
        chosen = datetime(2026, 10, 25, 2, minute, tzinfo=zone, fold=fold)
        end = datetime.fromtimestamp(chosen.timestamp() + 300, zone)
        item = replace(occurrence(master, nominal), all_day=False, start=chosen, end=end)
        overrides[key] = item.to_dict()
    master = replace(master, recurrence_overrides=overrides)
    begin, end = datetime(2026, 10, 25, tzinfo=zone), datetime(2026, 10, 26, tzinfo=zone)
    actual = expand_event(master, begin, end)
    assert [(item.start.minute, item.start.fold) for item in actual] == [(45, 0), (15, 1)]
    assert expand_event(master, begin, end, limit=1, truncate=True)[0].start.timestamp() == actual[0].start.timestamp()


def test_timed_apia_tail_collisions_have_the_same_set_semantics_as_standard_rrule():
    zone = ZoneInfo('Pacific/Apia')
    start = datetime(2011, 12, 29, 9, tzinfo=zone)
    prototype = Event('apia', 'local', 'Timed series', start, start + timedelta(hours=1))
    text = 'every(1d, count=3)'
    master = expression_event(prototype, text)
    language = expand_sequence(text, start)
    canonical = expand_event(master, start - timedelta(days=1), start + timedelta(days=4))
    assert [(value.day, value.hour) for value in language] == [(29, 9), (31, 9)]
    assert [value.timestamp() for value in language] == [item.start.timestamp() for item in canonical]
    assert expression_matches(master, text)
