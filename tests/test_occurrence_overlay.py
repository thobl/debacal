"""Draft year presence stays compact and uses the viewer's civil dates."""
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from debacal.models import Event
from debacal.occurrence_picker import OccurrenceOverlay
from debacal.recurrence import expand_event, overlaps
from debacal.store import Store


def test_year_overlay_replaces_one_source_without_materializing_other_series():
    zone = ZoneInfo('Europe/Berlin')
    start, end = datetime(2026, 1, 1, tzinfo=zone), datetime(2027, 1, 1, tzinfo=zone)
    original = Event('edited', 'local', 'Edited', start + timedelta(hours=9),
                     start + timedelta(hours=10), recurrence_rule='FREQ=WEEKLY')
    other = replace(original, id='untouched', ical_uid='untouched', title='Other', recurrence_rule='FREQ=DAILY')
    draft = replace(original, id='draft', start=start + timedelta(hours=13),
                    end=start + timedelta(hours=14), recurrence_rule='FREQ=DAILY;COUNT=3')
    with Store(':memory:') as store:
        store.apply('Synthetic year', upserts=[original, other])
        with patch('debacal.timeutil.local_zone', return_value=zone):
            base = store.query_year_presence(start, end)
        extra = expand_event(draft, start, end)
        expected = store.query_occurrences(start, end)
        expected = [event for event in expected if event.series_id != original.id] + extra
        with patch.object(type(base), '__iter__', side_effect=AssertionError('Do not expand the stored year')):
            overlay = OccurrenceOverlay(base, extra, [original.id])
            assert len(overlay) == len(expected)
            assert overlay.event_by_id(extra[0].id) == extra[0]
            assert overlay.event_by_id(expand_event(original, start, end)[0].id) is None
            for offset, summary in enumerate(overlay.summaries):
                lower, upper = start + timedelta(days=offset), start + timedelta(days=offset + 1)
                values = [event for event in expected if overlaps(event, lower, upper)]
                assert summary == {key: (count, '') for key, count in Counter(
                    event.calendar_id for event in values).items()}
                assert overlay.events_on_day(lower.date()) == sorted(values, key=lambda event: (
                    event.start.timestamp(), event.end.timestamp(), event.id))
            other_instance = next(event for event in expected
                                  if event.series_id == other.id and event.start.date() == start.date())
            assert overlay.events_on_day(date(2026, 1, 1), limit=1) == [other_instance]
            assert overlay.events_on_day(date(2026, 1, 1), limit=0) == []
            assert other_instance in overlay.known_events


def test_overlay_day_details_use_projection_zone_not_machine_zone():
    zone = ZoneInfo('America/New_York')
    lower, upper = datetime(2026, 1, 1, tzinfo=zone), datetime(2027, 1, 1, tzinfo=zone)
    start = datetime(2026, 11, 4, 23, 30, tzinfo=zone)
    draft = Event('draft', 'local', 'Late appointment', start, start + timedelta(hours=1))
    with Store(':memory:') as store:
        with patch('debacal.timeutil.local_zone', return_value=zone):
            overlay = OccurrenceOverlay(store.query_year_presence(lower, upper), [draft])
        assert overlay.events_on_day(date(2026, 11, 4)) == [draft]
        assert overlay.events_on_day(date(2026, 11, 5)) == [draft]
        assert overlay.events_on_day(date(2026, 11, 6)) == []
