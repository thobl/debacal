"""Range-only iteration and fast Event copies preserve canonical semantics."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest

from debacal.models import Event, copy_event
from debacal.prepared_recurrence import PreparedSeries
from debacal.recurrence import (
    RecurrenceRangeError, expand_series, occurrence, occurrence_id,
    range_overlaps, recurrence_key,
)


UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')


def source(**changes):
    first = datetime(2026, 1, 1, 9, tzinfo=UTC)
    return replace(Event('source', 'local', 'Synthetic source', first, first + timedelta(hours=1),
                         description='Shared source notes', location='Room 1',
                         raw_ics='Synthetic retained transport', recurrence_rule='FREQ=DAILY;COUNT=400'),
                   **changes)


def snapshot(item):
    return item.to_dict() | {'start_fold': item.start.fold, 'end_fold': item.end.fold,
                            '_query_projection': item._query_projection}


def test_projection_copy_replaces_only_requested_fields_and_shares_remaining_values():
    original = source(_query_projection=True, recurrence_dates=('2026-03-01T09:00:00+00:00',),
                      recurrence_overrides={'2026-01-02T09:00:00+00:00': {'title': 'Detached'}})
    replacements = {'id': 'projected', 'start': original.start + timedelta(days=1),
                    'end': original.end + timedelta(days=1), 'recurrence_rule': '',
                    'recurrence_dates': (), 'recurrence_overrides': {}, 'raw_ics': ''}
    with patch.object(Event, '__post_init__', side_effect=AssertionError('Revalidated projection')), \
            patch.object(Event, '__getstate__', side_effect=AssertionError('Used pickle')):
        copied = copy_event(original, replacements)
        plain = copy_event(original)
        empty = copy_event(original, {})
    assert copied is not original and plain is not original and empty is not original
    for item in fields(Event):
        assert getattr(copied, item.name) is replacements.get(item.name, getattr(original, item.name))
        assert getattr(plain, item.name) is getattr(original, item.name)
        assert getattr(empty, item.name) is getattr(original, item.name)
    assert original.raw_ics == 'Synthetic retained transport'
    assert copied._query_projection
    with pytest.raises(AttributeError):
        copy_event(original, {'not_an_event_field': 'unexpected'})


def test_occurrence_key_escaping_matches_stdlib_for_ascii_unicode_and_bytes():
    class CustomString(str):
        def replace(self, *_args, **_kwargs):
            raise AssertionError('String subclasses must retain the general encoder path')
    keys = [chr(value) for value in range(128)]
    keys.extend([''.join(chr(value) for value in range(128)), '', '2026-01-01',
                 '2026-10-25T01:30:00+00:00', 'a/b %3A+ü😀', '日本語',
                 b'a/b+c', bytearray(b'2026-01-01'), CustomString('2026-01-01T09:00:00+00:00')])
    for key in keys:
        assert occurrence_id('source', key) == 'source::occurrence::' + quote(key, safe='')
    with pytest.raises(UnicodeEncodeError):
        occurrence_id('source', '\ud800')


@pytest.fixture(params=['elapsed', 'gap', 'fold', 'nominal', 'timed-cut', 'date-cut',
                       'floating', 'scoped', 'exceptions'])
def projection_case(request):
    kind, viewer = request.param, None
    first = datetime(2026, 1, 1, 9, tzinfo=UTC)
    changes = {}
    if kind == 'gap':
        first = datetime(2026, 3, 1, 2, 30, tzinfo=BERLIN)
        changes = {'recurrence_rule': 'FREQ=WEEKLY;COUNT=80'}
    elif kind == 'fold':
        first = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=1)
        changes = {'recurrence_rule': 'FREQ=DAILY;COUNT=8'}
    elif kind == 'nominal':
        first = datetime(2026, 3, 27, 12, tzinfo=BERLIN)
        changes = {'recurrence_rule': 'FREQ=DAILY;COUNT=12', 'duration_spec': 'P1D'}
    elif kind == 'timed-cut':
        first = datetime(2026, 1, 28, 9, tzinfo=BERLIN)
        changes = {'recurrence_rule': 'FREQ=MONTHLY;COUNT=12', 'duration_spec': 'P1DT3H',
                   'range_policy': 'cut'}
    elif kind == 'date-cut':
        first = datetime(2026, 1, 30, tzinfo=BERLIN)
        changes = {'recurrence_rule': 'FREQ=MONTHLY;COUNT=12', 'range_policy': 'cut', 'all_day': True}
    elif kind == 'floating':
        first = datetime(2026, 3, 27, 9, tzinfo=BERLIN)
        viewer = ZoneInfo('America/New_York')
        changes = {'floating_time': True, 'recurrence_rule': 'FREQ=DAILY;COUNT=15'}
    elif kind == 'scoped':
        identifier = 'Projection/Scoped'
        definition = (f'BEGIN:VTIMEZONE\nTZID:{identifier}\nBEGIN:STANDARD\n'
                      'DTSTART:19700101T000000\nTZOFFSETFROM:+0245\nTZOFFSETTO:+0245\n'
                      'END:STANDARD\nEND:VTIMEZONE\n')
        changes = {'timezone_id': identifier, 'timezone_definitions': {identifier: definition},
                   'recurrence_rule': 'FREQ=DAILY;COUNT=20'}
    elif kind == 'exceptions':
        detached = replace(occurrence(source(), first + timedelta(days=2)),
                           start=first + timedelta(days=30, hours=2),
                           end=first + timedelta(days=30, hours=4), title='Detached synthetic appointment')
        changes = {'recurrence_dates': ((first + timedelta(days=25)).isoformat(),),
                   'excluded_dates': ((first + timedelta(days=1)).isoformat(),),
                   'recurrence_overrides': {recurrence_key(first + timedelta(days=2)): detached.to_dict()}}
    duration = (timedelta(days=3) if kind == 'date-cut' else timedelta(days=1, hours=3)
                if kind == 'timed-cut' else timedelta(days=1) if kind == 'nominal' else timedelta(hours=1))
    original = source(start=first, end=first + duration, **changes)
    lower = first - timedelta(days=2)
    upper = first + timedelta(days=365)
    if viewer is not None:
        lower, upper = lower.astimezone(viewer), upper.astimezone(viewer)
    return original, lower, upper, viewer


def test_range_iteration_matches_materialized_values_and_independent_expansion(projection_case):
    master, start, end, viewer = projection_case
    prepared = PreparedSeries(master)
    ranges = list(prepared.iter_rule_ranges(start, end, floating_zone=viewer))
    projected = prepared.query_rule(start, end, floating_zone=viewer)
    def values(key, first, last, spec):
        return key, first.isoformat(), first.fold, last.isoformat(), last.fold, spec
    assert [values(*item) for item in ranges] == [
        values(item.recurrence_id, item.start, item.end, item.duration_spec) for item in projected]
    rebound = master if viewer is None else replace(master, start=master.start.replace(tzinfo=viewer),
                                                    end=master.end.replace(tzinfo=viewer))
    assert [snapshot(item) for item in prepared.query(start, end, floating_zone=viewer)] == [
        snapshot(item) for item in expand_series(rebound, start, end)]
    with patch.object(prepared, '_project_values', side_effect=AssertionError('Allocated Event')), \
            patch('debacal.prepared_recurrence.occurrence_id', side_effect=AssertionError('Encoded ID')):
        assert list(prepared.iter_rule_ranges(start, end, floating_zone=viewer)) == ranges


def test_native_ranges_deduplicate_keys_and_preserve_strict_and_truncated_budgets():
    master = source(recurrence_rule='FREQ=DAILY;COUNT=6')
    prepared = PreparedSeries(master)
    starts = [master.start + timedelta(days=index) for index in range(6)]
    repeated = [value for value in starts for _ in range(2)]
    with patch.object(prepared, '_regular', side_effect=lambda *_args: iter(repeated)):
        found = list(prepared.iter_rule_ranges(master.start, master.end + timedelta(days=10), limit=6))
        assert len(found) == 6 and len({item[0] for item in found}) == 6
        assert len(list(prepared.iter_rule_ranges(master.start, master.end + timedelta(days=10),
                                                  limit=3, truncate=True))) == 3
        with pytest.raises(RecurrenceRangeError, match='exceeds 3 occurrences'):
            list(prepared.iter_rule_ranges(master.start, master.end + timedelta(days=10), limit=3))
        assert list(prepared.iter_rule_ranges(master.start, master.end, limit=0, truncate=True)) == []


def test_native_and_explicit_streams_can_share_anchor_and_added_identities():
    master = source(recurrence_rule='FREQ=DAILY;COUNT=5',
                    recurrence_dates=('2026-01-02T09:00:00+00:00',))
    prepared = PreparedSeries(master)
    ranges = list(prepared.iter_rule_ranges(master.start, master.end + timedelta(days=10)))
    explicit = {item.recurrence_id for item in prepared.explicit_occurrences}
    assert {ranges[0][0], ranges[1][0]} <= explicit
    assert len(prepared.query(master.start, master.end + timedelta(days=10))) == 5


@pytest.mark.parametrize('fold,expected', [(0, False), (1, True)])
def test_range_overlap_uses_instants_for_repeated_hour_points(fold, expected):
    point = datetime(2026, 10, 25, 2, 30, tzinfo=BERLIN, fold=fold)
    first = datetime(2026, 10, 25, 2, tzinfo=BERLIN, fold=1)
    last = datetime(2026, 10, 25, 3, tzinfo=BERLIN)
    bounds = first.timestamp(), last.timestamp()
    assert range_overlaps(point, point, first, last) is expected
    assert range_overlaps(point, point, first, last, window_timestamps=bounds) is expected


def test_range_overlap_retains_civil_dates_and_exclusive_point_end():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    last = first + timedelta(days=1)
    assert range_overlaps(first, last, first, first + timedelta(hours=1), all_day=True)
    assert not range_overlaps(last, last + timedelta(days=1), first, last, all_day=True)
    assert range_overlaps(last, last + timedelta(days=1), first, last + timedelta(hours=1), all_day=True)
    assert range_overlaps(first, first, first, last)
    assert not range_overlaps(last, last, first, last)
