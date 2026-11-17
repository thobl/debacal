"""Nominal daily/weekly stepping matches the independent dateutil path."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event
from debacal.prepared_recurrence import PreparedSeries, _simple_day_step
from debacal.recurrence import RecurrenceRangeError, expand_series, occurrence, recurrence_key


CASES = [
    ('UTC', (2026, 1, 1, 9, 15), 0),
    ('Europe/Berlin', (2026, 3, 27, 2, 30), 0),
    ('Europe/Berlin', (2026, 10, 25, 2, 30), 1),
    ('Australia/Lord_Howe', (2026, 10, 2, 2, 15), 0),
    ('Pacific/Apia', (2011, 12, 28, 0, 30), 0),
    ('America/Goose_Bay', (1988, 10, 28, 23, 30), 0),
]
RULES = ['FREQ=DAILY', 'FREQ=DAILY;INTERVAL=2', 'FREQ=WEEKLY',
         'FREQ=WEEKLY;INTERVAL=3;WKST=SU', 'FREQ=DAILY;COUNT=800', 'FREQ=WEEKLY;COUNT=300']


def source(zone_name, fields, fold=0, rule='FREQ=DAILY', duration=timedelta(minutes=15), **changes):
    start = datetime(*fields, tzinfo=ZoneInfo(zone_name), fold=fold)
    end = (start.astimezone(timezone.utc) + duration).astimezone(start.tzinfo)
    return Event('synthetic', 'local', 'Synthetic schedule', start, end,
                 recurrence_rule=rule, timezone_id=zone_name, **changes)


def snapshot(events):
    return [(event.to_dict(), event.start.isoformat(), event.start.fold, event.end.isoformat(), event.end.fold)
            for event in events]


@pytest.mark.parametrize('zone_name,fields,fold', CASES)
@pytest.mark.parametrize('rule', RULES)
def test_simple_rules_keep_seek_phase_count_instants_folds_and_gap_aliases(zone_name, fields, fold, rule):
    master = source(zone_name, fields, fold, rule)
    prepared = PreparedSeries(master)
    assert prepared._finite_values is None and _simple_day_step(prepared.rule) is not None
    for offset, width in ((-1, 12), (3, 35), (400, 14), (3000, 9)):
        begin = master.start + timedelta(days=offset, minutes=10)
        end = begin + timedelta(days=width)
        actual = snapshot(prepared.query(begin, end))
        with patch('debacal.prepared_recurrence._simple_day_step', return_value=None):
            expected = snapshot(prepared.query(begin, end))
        assert actual == expected
        if offset == -1:
            assert actual == snapshot(expand_series(master, begin, end))


@pytest.mark.parametrize('zone_name,fields,fold', CASES)
@pytest.mark.parametrize('frequency', ['DAILY', 'WEEKLY'])
def test_inclusive_until_and_explicit_single_weekday_match_dateutil(zone_name, fields, fold, frequency):
    master = source(zone_name, fields, fold)
    last = master.start + timedelta(days=28)
    until = last.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    rule = f'FREQ={frequency};UNTIL={until}'
    if frequency == 'WEEKLY':
        rule += ';BYDAY=' + ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')[master.start.weekday()]
    prepared = PreparedSeries(replace(master, recurrence_rule=rule))
    begin, end = master.start - timedelta(days=1), last + timedelta(days=14)
    assert _simple_day_step(prepared.rule) is not None
    actual = snapshot(prepared.query(begin, end))
    with patch('debacal.prepared_recurrence._simple_day_step', return_value=None):
        assert actual == snapshot(prepared.query(begin, end))


@pytest.mark.parametrize('all_day,floating', [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize('nominal_duration', [False, True])
def test_long_duration_exclusions_additions_and_detached_dates_keep_their_meaning(all_day, floating, nominal_duration):
    master = source('Europe/Berlin', (2026, 3, 20, 0 if all_day else 9), rule='FREQ=DAILY;COUNT=800',
                    duration=timedelta(days=3), all_day=all_day, floating_time=floating,
                    duration_spec='P3D' if nominal_duration else '')
    key = lambda value: recurrence_key(value, all_day, floating_time=floating)
    nominal = master.start + timedelta(days=8)
    detached = replace(occurrence(master, nominal), start=nominal + timedelta(days=5),
                       end=nominal + timedelta(days=8), title='Detached synthetic date')
    master = replace(master, excluded_dates=(key(master.start + timedelta(days=2)),),
                     recurrence_dates=(key(master.start + timedelta(days=5, hours=2 if not all_day else 0)),),
                     recurrence_overrides={key(nominal): detached.to_dict()})
    prepared = PreparedSeries(master)
    begin, end = master.start + timedelta(days=10), master.start + timedelta(days=25)
    actual = snapshot(prepared.query(begin, end))
    with patch('debacal.prepared_recurrence._simple_day_step', return_value=None):
        assert actual == snapshot(prepared.query(begin, end))
    assert actual == snapshot(expand_series(master, begin, end))


@pytest.mark.parametrize('year,month,day', [(1, 1, 1), (9999, 12, 27)])
@pytest.mark.parametrize('rule', ['FREQ=DAILY', 'FREQ=WEEKLY;INTERVAL=2'])
def test_supported_year_boundaries_terminate_without_overflow(year, month, day, rule):
    master = source('UTC', (year, month, day, 9), rule=rule, duration=timedelta())
    prepared = PreparedSeries(master)
    begin = master.start
    end = (datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc) if year == 9999 else
           begin + timedelta(days=30))
    actual = snapshot(prepared.query(begin, end))
    with patch('debacal.prepared_recurrence._simple_day_step', return_value=None):
        assert actual == snapshot(prepared.query(begin, end))


@pytest.mark.parametrize('rule', [
    'FREQ=DAILY;BYDAY=MO', 'FREQ=DAILY;BYMONTH=3', 'FREQ=DAILY;BYMONTHDAY=1',
    'FREQ=DAILY;BYHOUR=8,9', 'FREQ=DAILY;BYHOUR=8', 'FREQ=DAILY;BYMINUTE=0,30',
    'FREQ=DAILY;BYSECOND=0,30', 'FREQ=DAILY;BYSETPOS=1',
    'FREQ=WEEKLY;BYDAY=MO,WE', 'FREQ=WEEKLY;BYDAY=TU', 'FREQ=MONTHLY',
])
def test_extra_selectors_and_other_weekdays_use_the_original_engine(rule):
    master = source('UTC', (2026, 3, 2, 9, 15), rule=rule)
    prepared = PreparedSeries(master)
    assert _simple_day_step(prepared.rule) is None
    begin, end = master.start, master.start + timedelta(days=35)
    with patch('debacal.prepared_recurrence._nominal_days', side_effect=AssertionError('Complex rule must use dateutil')):
        assert snapshot(prepared.query(begin, end)) == snapshot(expand_series(master, begin, end))


def test_short_finite_rules_keep_the_predecoded_path_and_raw_candidate_budget_is_retained():
    short = PreparedSeries(source('UTC', (2026, 1, 1, 9), rule='FREQ=DAILY;COUNT=3'))
    with patch('debacal.prepared_recurrence._simple_day_step', side_effect=AssertionError('Keep finite starts')):
        assert len(short.query(short.master.start, short.master.start + timedelta(days=10))) == 3
    regular = PreparedSeries(source('UTC', (2026, 1, 1, 9)))
    with patch('debacal.recurrence_budget.MAX_CANDIDATE_WORK', 3):
        with pytest.raises(RecurrenceRangeError, match='computation budget'):
            list(regular._regular(None))
