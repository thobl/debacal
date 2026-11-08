"""Standard generated clocks normalize through DST gaps without losing events."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import islice
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.event_navigation import adjacent_event_day
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.preferences import Preferences
from debacal.preview_navigation import PreviewNavigation
from debacal.recurrence import (
    RecurrenceError, expand_event, normalize_recurrence_stream,
    normalize_recurrence_time, recurrence_key, recurrence_set,
)
from debacal.search_panel import SearchPanel
from debacal.series_operations import partition_series
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2027, 3, 26, 2, 30, tzinfo=ZONE)
GAP = datetime(2027, 3, 28, 2, 30, tzinfo=ZONE)
ACTUAL = datetime(2027, 3, 28, 3, 30, tzinfo=ZONE)
BOUNDS = START - timedelta(days=1), START + timedelta(days=8)


def source(rule='FREQ=DAILY;COUNT=4', *, duration=timedelta(hours=1)):
    return Event('series', 'local', 'Daily meeting', START, START + duration,
                 recurrence_rule=rule, timezone_id=ZONE.key)


@pytest.mark.parametrize('rule', ['FREQ=DAILY;COUNT=4', 'FREQ=DAILY;UNTIL=20270329T003000Z'])
def test_daily_standard_rule_keeps_gap_occurrence_and_duration(rule):
    master = source(rule)
    values = expand_event(master, *BOUNDS)
    assert [(item.start.day, item.start.hour, item.start.minute) for item in values] == [
        (26, 2, 30), (27, 2, 30), (28, 3, 30), (29, 2, 30)]
    assert values[2].recurrence_id == recurrence_key(GAP) == recurrence_key(ACTUAL)
    assert values[2].end == ACTUAL + timedelta(hours=1)
    assert all(item.end.timestamp() > item.start.timestamp() for item in values)
    loaded, = parse_ics(export_ics([master]), master.calendar_id)
    assert [(item.start, item.end) for item in expand_event(loaded, *BOUNDS)] == [(item.start, item.end) for item in values]


def test_point_occurrence_in_a_window_starting_inside_gap_normalization_is_not_missed():
    master = source(duration=timedelta(0))
    values = expand_event(master, ACTUAL - timedelta(minutes=10), ACTUAL + timedelta(minutes=10))
    assert len(values) == 1 and values[0].start == values[0].end == ACTUAL


def test_half_hour_timezone_gap_uses_the_pre_gap_offset_and_excludes_by_instant():
    zone = ZoneInfo('Australia/Lord_Howe')
    initial = datetime(2026, 10, 2, 2, 15, tzinfo=zone)
    master = Event('half-hour', 'local', 'Meeting', initial, initial + timedelta(minutes=30),
                   recurrence_rule='FREQ=DAILY;COUNT=4', timezone_id=zone.key)
    begin, end = initial - timedelta(days=1), initial + timedelta(days=5)
    values = expand_event(master, begin, end)
    assert [(item.start.day, item.start.hour, item.start.minute) for item in values] == [
        (2, 2, 15), (3, 2, 15), (4, 2, 45), (5, 2, 15)]
    excluded = replace(master, excluded_dates=(values[2].recurrence_id,))
    assert len(list(recurrence_set(excluded))) == 3
    assert [item.start.day for item in expand_event(excluded, begin, end)] == [2, 3, 5]


def test_generated_end_clock_in_gap_normalizes_without_changing_the_rule_or_losing_occurrences():
    initial = START.replace(hour=1)
    master = replace(source(), start=initial, end=initial + timedelta(hours=1))
    values = expand_event(master, *BOUNDS)
    assert len(values) == 4
    gap = values[2]
    assert gap.start == GAP.replace(hour=1)
    assert gap.end == ACTUAL
    assert gap.end.timestamp() - gap.start.timestamp() == 3600
    # A new prototype with a normalized endpoint would encode a different
    # wall duration for all future events, so the unsupported split is atomic.
    with pytest.raises(RecurrenceError, match='clock gap'):
        partition_series(master, gap)


def test_gap_exclusion_uses_one_instant_in_canonical_set_and_calendar_preview():
    master = replace(source(), excluded_dates=(recurrence_key(ACTUAL),))
    assert len(list(recurrence_set(master))) == 3
    assert [item.start.day for item in expand_event(master, *BOUNDS)] == [26, 27, 29]
    navigation = PreviewNavigation([master])
    assert [item.start.day for item in navigation.finite_events()] == [26, 27, 29]


def test_gap_generated_occurrence_can_be_edited_deleted_undone_and_found_by_navigation():
    with Store(':memory:') as store:
        master = source()
        store.apply('Create', [master])
        values = store.query_occurrences(*BOUNDS)
        assert len(values) == 4
        selected = values[2]
        assert store.get_occurrence(selected.id) == selected
        edited = replace(selected, start=ACTUAL + timedelta(hours=1), end=ACTUAL + timedelta(hours=2), title='Changed')
        store.edit_occurrence(selected, edited)
        assert store.get_occurrence(selected.id).start == edited.start
        assert adjacent_event_day([store.get_event(master.id)], values[1].start.date(), 1, ZONE) == selected.start.date()
        store.delete_occurrence(store.get_occurrence(selected.id))
        assert store.get_occurrence(selected.id) is None
        store.undo()
        assert store.get_occurrence(selected.id).title == 'Changed'
        store.undo()
        assert store.get_occurrence(selected.id) == selected
        upcoming = store.upcoming_occurrences(ACTUAL - timedelta(minutes=10), ACTUAL + timedelta(days=1), limit=1)
        assert [item.id for item in upcoming] == [selected.id]
        navigation = PreviewNavigation([master])
        assert len(navigation.finite_events()) == 4
        assert navigation.position(selected) == (3, 4)
        assert adjacent_event_day([master], values[1].start.date(), 1, ZONE) == selected.start.date()


@pytest.mark.parametrize('pivot', [0, 1, 3])
def test_splitting_before_or_after_gap_keeps_complete_schedule(pivot):
    master = source()
    values = expand_event(master, *BOUNDS)
    before, after = partition_series(master, values[pivot])
    starts = [item.start for part in (before, after) if part for item in expand_event(part, *BOUNDS)]
    assert starts == [item.start for item in values]


@pytest.mark.parametrize('operation', ['move', 'delete', 'copy'])
def test_split_exactly_at_gap_is_rejected_atomically_in_all_following_operations(operation):
    master = source()
    with Store(':memory:') as store:
        store.apply('Create', [master])
        selected = store.query_occurrences(*BOUNDS)[2]
        history, pending = store.history(), store.pending_changes()
        with pytest.raises(RecurrenceError, match='clock gap'):
            if operation == 'move':
                store.move_selection([selected], 'future', selected,
                                     selected.start + timedelta(hours=1), selected.end + timedelta(hours=1))
            elif operation == 'delete':
                store.delete_selection([selected], 'future')
            else:
                store.future_snapshot(selected)
        assert store.list_events() == [master]
        assert store.history() == history and store.pending_changes() == pending


def test_gap_stream_sorts_and_deduplicates_only_the_local_displacement_window():
    values = [GAP.replace(minute=minute) for minute in (0, 15, 30, 45)]
    values += [ACTUAL.replace(minute=minute) for minute in (0, 15, 30, 45)]
    values += [ACTUAL.replace(hour=4, minute=0)]
    normalized = list(normalize_recurrence_stream(values))
    assert [(value.hour, value.minute) for value in normalized] == [(3, 0), (3, 15), (3, 30), (3, 45), (4, 0)]
    # Normal recurrence streams stay lazy and do not inspect future history.
    with patch('debacal.recurrence.normalize_recurrence_time', wraps=normalize_recurrence_time) as normalize:
        assert list(islice(normalize_recurrence_stream(iter([START] * 1000)), 3)) == [START] * 3
    assert normalize.call_count == 3


def test_repeated_annual_gap_windows_yield_lazily_instead_of_buffering_history():
    consumed = []
    def annual_gaps():
        for year in range(2027, 2030):
            consumed.append(year)
            assert len(consumed) <= 2, 'An unbounded annual series must not scan its future history.'
            last = datetime(year, 3, 31, 2, 30, tzinfo=ZONE)
            yield last - timedelta(days=(last.weekday() - 6) % 7)
    first = next(normalize_recurrence_stream(annual_gaps()))
    assert first == ACTUAL
    assert consumed == [2027, 2028]


def test_search_occurrences_include_the_same_normalized_gap_range_as_the_calendar():
    app = QApplication.instance() or QApplication([])
    with Store(':memory:') as store:
        master = source()
        panel = SearchPanel(store, Preferences())
        try:
            values = list(panel._occurrences(master, floor=START))
            assert [(item.start, item.end) for item in values] == [(item.start, item.end) for item in expand_event(master, *BOUNDS)]
        finally:
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
