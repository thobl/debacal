"""Explicit timezone lookup never grows history or caches queried dates."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from debacal.recurrence_budget import BoundedRule
from debacal.timezones import TimezoneError, resolve_timezone


def definition(identifier, *, frequency='YEARLY', reverse=False):
    standard = ('BEGIN:STANDARD\nDTSTART:16010101T030000\nTZOFFSETFROM:+0200\n'
                f'TZOFFSETTO:+0100\nRRULE:FREQ={frequency};BYDAY=-1SU;BYMONTH=10\nEND:STANDARD\n')
    daylight = ('BEGIN:DAYLIGHT\nDTSTART:16010101T020000\nTZOFFSETFROM:+0100\n'
                f'TZOFFSETTO:+0200\nRRULE:FREQ={frequency};BYDAY=-1SU;BYMONTH=3\nEND:DAYLIGHT\n')
    return (f'BEGIN:VTIMEZONE\nTZID:{identifier}\n'
            + (daylight + standard if reverse else standard + daylight) + 'END:VTIMEZONE\n')


def monthly_definition(identifier, *, reverse=False):
    standard = ('BEGIN:STANDARD\nDTSTART:16010101T030000\nTZOFFSETFROM:+0200\n'
                'TZOFFSETTO:+0100\nRRULE:FREQ=MONTHLY;BYMONTHDAY=1\nEND:STANDARD\n')
    daylight = ('BEGIN:DAYLIGHT\nDTSTART:16010115T020000\nTZOFFSETFROM:+0100\n'
                'TZOFFSETTO:+0200\nRRULE:FREQ=MONTHLY;BYMONTHDAY=15\nEND:DAYLIGHT\n')
    return (f'BEGIN:VTIMEZONE\nTZID:{identifier}\n'
            + (daylight + standard if reverse else standard + daylight) + 'END:VTIMEZONE\n')


def test_first_random_timezone_queries_only_search_frozen_transition_data():
    identifier = 'Performance/Prepared-annual'
    text = definition(identifier)
    zone = resolve_timezone(identifier, {identifier: text})
    indexes = [component.rrule for component in zone.backend._comps]
    states = [(index.values, index.failure_from, index.failure) for index in indexes]
    assert all(isinstance(index.values, tuple) for index in indexes)
    assert not hasattr(zone, '_wall_state')
    assert not any(hasattr(index, 'iterator') for index in indexes)
    with patch.object(BoundedRule, '__iter__', side_effect=AssertionError('Timezone history iterated during lookup')):
        for year, month, expected in [(9999, 12, 1), (2026, 7, 2), (1, 1, 1), (1601, 12, 1),
                                      (2077, 7, 2), (1995, 10, 2), (2400, 3, 1)]:
            assert datetime(year, month, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=expected)
        for fold, expected in [(0, 2), (1, 1)]:
            value = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=fold)
            assert value.utcoffset() == timedelta(hours=expected)
    assert [(index.values, index.failure_from, index.failure) for index in indexes] == states
    assert zone.backend._cachedate == []
    assert zone.backend._cachecomp == []


@pytest.mark.parametrize('reverse', [False, True])
def test_future_budget_exhaustion_keeps_present_lookup_valid_and_order_independent(reverse):
    identifier = f'Performance/Prepared-monthly-{reverse}'
    text = monthly_definition(identifier, reverse=reverse)
    zone = resolve_timezone(identifier, {identifier: text})
    assert all(component.rrule.failure is not None for component in zone.backend._comps)
    with patch.object(BoundedRule, '__iter__', side_effect=AssertionError('Timezone queried before preparation')):
        assert datetime(2026, 11, 4, 12, tzinfo=zone).utcoffset() == timedelta(hours=1)
        assert datetime(2026, 11, 20, 12, tzinfo=zone).utcoffset() == timedelta(hours=2)
        with pytest.raises(TimezoneError, match='computation limit'):
            datetime(9999, 1, 1, 12, tzinfo=zone).utcoffset()
        # Failure in a distant date does not poison proven earlier coverage.
        assert datetime(2026, 11, 4, 12, tzinfo=zone).utcoffset() == timedelta(hours=1)


def test_transition_preparation_quotas_prevent_component_order_starvation():
    offsets = []
    for reverse in (False, True):
        identifier = f'Performance/Fair-preparation-{reverse}'
        text = monthly_definition(identifier, reverse=reverse)
        zone = resolve_timezone(identifier, {identifier: text})
        lengths = sorted(len(component.rrule.values) for component in zone.backend._comps)
        assert lengths[0] > 5000
        assert lengths[-1] <= 10000
        offsets.append([datetime(2026, 11, day, 12, tzinfo=zone).utcoffset() for day in (4, 20)])
    assert offsets[0] == offsets[1]
