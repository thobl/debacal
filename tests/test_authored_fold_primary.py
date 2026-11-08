"""Explicit repeated-hour primaries use interoperable detached identities."""
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from debacal.event_clipboard import EventClipboard
from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.ics_fallback import folded, parse as parse_fallback, unfolded
from debacal.models import Calendar, Event
from debacal.patterns import compile_sequence
from debacal.recurrence import canonicalize_authored_primary, expand_event, occurrence, updated_series
from debacal.schedule_authoring import expression_matches


ZONE = ZoneInfo('Europe/Berlin')
BEGIN = datetime(2026, 10, 1, tzinfo=ZONE)
END = datetime(2027, 6, 1, tzinfo=ZONE)


def source():
    start = datetime(2026, 10, 24, 2, 30, tzinfo=ZONE)
    return Event('source', 'local', 'Weekly', start, start + timedelta(minutes=15),
                 recurrence_rule='FREQ=WEEKLY;COUNT=30')


def pasted_late_primary():
    original = source()
    clipboard = EventClipboard.capture([original], occurrence(original, original.start), 'group')
    target = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    pasted, = clipboard.instantiate('group', target, 'local')
    return pasted, target


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
def test_fold_primary_visible_ranges_survive_own_and_standard_ics_roundtrip(parser):
    pasted, target = pasted_late_primary()
    visible = expand_event(pasted, BEGIN, END)
    assert len(visible) == 30 and visible[0].start.timestamp() == target.timestamp()
    assert pasted.start.fold == 0 and len(pasted.recurrence_overrides) == 1
    assert any(item.start.astimezone(ZONE).month == 5 and item.start.astimezone(ZONE).hour == 2 for item in visible)
    raw = export_ics([pasted])
    assert b'DTSTART:20261025T013000Z' in raw  # Detached actual range is unambiguous.
    standard = folded(line for line in unfolded(raw) if not line.startswith('X-DEBACAL-'))
    expected = [(item.start.timestamp(), item.end.timestamp()) for item in visible]
    for payload in (raw, standard):
        restored, = parser(payload, 'local')
        actual = expand_event(restored, BEGIN, END)
        assert [(item.start.timestamp(), item.end.timestamp()) for item in actual] == expected
    assert expression_matches(pasted, pasted.schedule_expression)


def test_late_primary_keeps_its_phase_on_title_edit_and_can_be_copied_back_to_first_phase():
    pasted, target = pasted_late_primary()
    titled = updated_series(pasted, pasted, replace(pasted, title='Updated title'))
    assert expand_event(titled, BEGIN, END)[0].start.timestamp() == target.timestamp()
    reference = expand_event(titled, BEGIN, END)[0]
    clipboard = EventClipboard.capture([titled], reference, 'group')
    early, = clipboard.instantiate('group', target.replace(fold=0), 'local')
    actual = expand_event(early, BEGIN, END)
    assert len(actual) == 30
    assert actual[0].start.timestamp() == target.replace(fold=0).timestamp()
    assert actual[0].end.timestamp() - actual[0].start.timestamp() == 15 * 60


def test_omitted_late_reference_stays_omitted_after_standard_anchor_normalization():
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    text = '+1d every(1w, count=3)'
    authored = Event('custom', 'local', 'Following Mondays', start,
        datetime.fromtimestamp(start.timestamp() + 900, ZONE),
        schedule_expression=text, expression_anchor=start.date().isoformat(), schedule_syntax=2,
        **compile_sequence(text, start).fields)
    normalized = canonicalize_authored_primary(authored)
    actual = expand_event(normalized, BEGIN, END)
    assert len(actual) == 3 and all(item.start.weekday() == 0 for item in actual)
    assert actual[0].start.date().isoformat() == '2026-10-26'


def test_new_event_dialog_authors_a_late_repeated_hour_series_with_a_visible_primary():
    app = QApplication.instance() or QApplication([])
    start = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
    end = datetime.fromtimestamp(start.timestamp() + 900, ZONE)
    with patch('debacal.event_dialog.local_zone', return_value=ZONE):
        dialog = EventDialog([Calendar('local', 'Personal', '#587c67')], start, end)
        try:
            dialog.title.setText('Late weekly event')
            dialog.schedule_mode.setCurrentData('repeat')
            dialog.repeat.setCurrentData('WEEKLY')
            dialog.repeat_count.setValue(3)
            authored, = dialog.build_events()
            actual = expand_event(authored, BEGIN, END)
            assert len(actual) == 3 and actual[0].start.timestamp() == start.timestamp()
            restored, = parse_ics(export_ics([authored]), 'local')
            assert expand_event(restored, BEGIN, END)[0].start.timestamp() == start.timestamp()
        finally:
            dialog.close()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


@pytest.mark.parametrize('parser', [parse_ics, parse_fallback])
@pytest.mark.parametrize('start_minute,end_minute', [(10, 30), (50, 10)])
def test_late_master_endpoint_roundtrips_without_losing_its_selected_instant(parser, start_minute, end_minute):
    start = datetime(2026, 10, 25, 2, start_minute, tzinfo=ZONE, fold=0)
    end = datetime(2026, 10, 25, 2, end_minute, tzinfo=ZONE, fold=1)
    original = Event('endpoint', 'local', 'Late endpoint', start, end,
                     recurrence_rule='FREQ=DAILY;COUNT=3')
    raw = export_ics([original])
    assert b'DTSTART;TZID=Europe/Berlin:20261025T02' in raw
    assert f'DTEND:20261025T01{end_minute:02d}00Z'.encode() in raw
    standard = folded(line for line in unfolded(raw) if not line.startswith('X-DEBACAL-'))
    expected = [(item.start.timestamp(), item.end.timestamp()) for item in expand_event(original, BEGIN, END)]
    for payload in (raw, standard):
        restored, = parser(payload, 'local')
        assert restored.start.timestamp() == start.timestamp()
        assert restored.end.timestamp() == end.timestamp()
        assert restored.end.astimezone(ZONE).fold == 1
        # Preserve the existing recurrence-duration projection as well as the
        # exact authoritative endpoints; serialization only disambiguates it.
        assert [(item.start.timestamp(), item.end.timestamp())
                for item in expand_event(restored, BEGIN, END)] == expected
