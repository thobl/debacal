"""Floating recurrence identities survive the surrounding calendar workflows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.coincident_occurrences import apply_occurrence_counts, multiplicities
from debacal.event_clipboard import EventClipboard, calendar_day_delta
from debacal.event_navigation import adjacent_event_day, event_day
from debacal.ics import export_ics, parse_ics_report
from debacal.ics_resources import ParseReport
from debacal.import_preparation import PreparedImport, PreparedSource, remap_calendar
from debacal.import_selection import ImportSelection, display_date
from debacal.models import Event
from debacal.preferences import Preferences
from debacal.preview_navigation import PreviewNavigation
from debacal.recurrence import expand_series, occurrence
from debacal.search_panel import SearchPanel
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 3, 28, 2, 30, tzinfo=ZONE)
BOUNDS = START - timedelta(days=1), START + timedelta(days=10)


def floating(**changes):
    return replace(Event('floating', 'local', 'Floating meeting', START,
                         START + timedelta(minutes=45), floating_time=True,
                         recurrence_rule='FREQ=DAILY;COUNT=3'), **changes)


@pytest.fixture(autouse=True)
def viewer_zone(monkeypatch):
    monkeypatch.setattr('debacal.timeutil.local_zone', lambda: ZONE)
    monkeypatch.setattr('debacal.search_panel.local_zone', lambda: ZONE)
    monkeypatch.setattr('debacal.import_preparation.local_zone', lambda: ZONE)
    monkeypatch.setattr('debacal.import_selection.local_zone', lambda: ZONE)


def signature(events):
    return [(item.start.replace(tzinfo=None), item.end.replace(tzinfo=None), item.title)
            for item in events]


def test_preview_positions_and_floating_until_keep_gap_wall_time():
    source = floating(recurrence_rule='FREQ=DAILY;UNTIL=20260330T023000')
    navigation = PreviewNavigation([source])
    first = navigation.first()
    second = navigation.adjacent(first, 1)
    last = navigation.last()
    assert first.recurrence_id == '2026-03-28T02:30:00'
    assert second.recurrence_id == '2026-03-29T02:30:00'
    assert second.start.hour == 2 and second.start.minute == 30
    assert navigation.position(second) == (2, 3)
    assert last.recurrence_id == '2026-03-30T02:30:00'
    assert navigation.position(last) == (3, 3)
    explicit = replace(source, recurrence_rule='', recurrence_dates=('2026-03-29T02:30:00',))
    assert PreviewNavigation([explicit]).explicit_count() == 2


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_copy_paste_keeps_floating_identity_and_wall_range(scope):
    source = floating()
    selected = expand_series(source, *BOUNDS)[1]
    clipboard = EventClipboard.capture([source], selected, scope)
    restored = EventClipboard.from_bytes(clipboard.to_bytes())
    target = datetime(2026, 4, 5, 2, 30, tzinfo=ZONE)
    copied, = restored.instantiate(scope, target, 'destination')
    result = expand_series(copied, target - timedelta(days=5), target + timedelta(days=5))
    assert copied.floating_time
    assert all(item.floating_time and item.start.hour == 2 and item.start.minute == 30 for item in result)
    assert any(item.start == target for item in result)
    assert all('+' not in item.recurrence_id and 'Z' not in item.recurrence_id for item in result)
    assert len(result) == {'single': 1, 'group': 3, 'future': 2}[scope]


def test_copy_can_place_floating_event_in_a_clock_gap():
    source = floating(recurrence_rule='')
    clipboard = EventClipboard.capture([source], source)
    target = START + timedelta(days=1)
    copied, = clipboard.instantiate('single', target, 'destination')
    assert copied.start.replace(tzinfo=None) == datetime(2026, 3, 29, 2, 30)
    assert copied.end.replace(tzinfo=None) == datetime(2026, 3, 29, 3, 15)


def test_import_occurrence_choices_remain_floating_after_remapping_and_ics_roundtrip():
    source = floating(recurrence_rule='FREQ=DAILY;COUNT=5')
    report = ParseReport((source,))
    shown = expand_series(source, *BOUNDS)
    selection = ImportSelection(False)
    selection.set_selected((shown[1], shown[3]), True)
    selected = selection.snapshot().filter_report(report)
    mapped = remap_calendar(selected, 'destination')
    parsed = parse_ics_report(export_ics(mapped.events), 'other')
    assert not parsed.issues
    assert all(item.floating_time for item in mapped.events + parsed.events)
    assert signature(expand_series(mapped.events[0], *BOUNDS)) == signature((shown[1], shown[3]))
    assert signature(expand_series(parsed.events[0], *BOUNDS)) == signature((shown[1], shown[3]))
    exclusion = ImportSelection()
    exclusion.toggle(shown[1])
    filtered = exclusion.snapshot().filter_report(report)
    assert filtered.events[0].excluded_dates == ('2026-03-29T02:30:00',)


def test_floating_day_helpers_ignore_attached_offset_and_keep_civil_day():
    source = floating(start=START.replace(hour=23, minute=30), end=START.replace(hour=23, minute=45),
                      recurrence_rule='')
    distant = ZoneInfo('Pacific/Kiritimati')
    assert event_day(source, distant) == date(2026, 3, 28)
    with patch('debacal.import_selection.local_zone', return_value=distant):
        assert display_date(source) == date(2026, 3, 28)
    assert calendar_day_delta(source, datetime(2026, 3, 29, 23, 30, tzinfo=distant)) == 1


def test_floating_import_preview_and_neighbour_navigation_use_naive_keys():
    source = floating(excluded_dates=('2026-03-29T02:30:00',))
    prepared = PreparedImport((PreparedSource(0, 'Floating.ics', ParseReport((source,))),))
    page = prepared.month_page(date(2026, 3, 1), Preferences())
    assert not page.warnings
    assert [item.recurrence_id for item in page.events] == ['2026-03-28T02:30:00', '2026-03-30T02:30:00']
    assert prepared.initial_month(Preferences(), today=date(2026, 3, 29)) == date(2026, 3, 1)
    assert adjacent_event_day([source], date(2026, 3, 28), 1, ZONE) == date(2026, 3, 30)
    assert adjacent_event_day([source], date(2026, 3, 30), -1, ZONE) == date(2026, 3, 28)


def test_coincident_floating_occurrences_preserve_naive_distinct_identities():
    source = floating(recurrence_rule='')
    candidate = apply_occurrence_counts(source, source, (source.start,))
    result = expand_series(candidate, *BOUNDS)
    assert len(result) == 2 and len({item.id for item in result}) == 2
    assert len({item.start.replace(tzinfo=None) for item in result}) == 1
    assert all('+' not in item.recurrence_id for item in result)
    assert multiplicities(candidate) == {'2026-03-28T02:30:00': 1}


def test_search_uses_floating_keys_and_displays_moved_occurrence_only_once():
    app = QApplication.instance() or QApplication([])
    errors = []
    with Store(':memory:') as store, patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        source = floating()
        key = '2026-03-29T02:30:00'
        original = occurrence(source, START + timedelta(days=1), key)
        moved = replace(original, title='Moved floating meeting',
                        start=original.start.replace(hour=15), end=original.end.replace(hour=16))
        source = replace(source, recurrence_overrides={key: moved.to_dict()})
        store.apply('Create', [source])
        panel = SearchPanel(store, Preferences())
        try:
            panel.set_range(*BOUNDS)
            panel.populate('meeting')
            root = panel.topLevelItem(0)
            root.setExpanded(True)
            children = [root.child(index) for index in range(root.childCount()) if panel.event_id(root.child(index))]
            events = [store.get_occurrence(panel.event_id(child)) for child in children]
            assert [item.recurrence_id for item in events] == ['2026-03-28T02:30:00', key, '2026-03-30T02:30:00']
            assert events[1].title == moved.title and events[1].start.hour == 15
            assert all(item.floating_time for item in events)
        finally:
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        assert not errors
