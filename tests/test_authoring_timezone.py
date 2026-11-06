"""Saved custom text keeps its authoring clock across display timezones."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import expand_sequence
from debacal.recurrence import base_schedule_starts, expand_series, schedule_fields
from debacal.store import Store


BERLIN = ZoneInfo('Europe/Berlin')
UTC = timezone.utc
START = datetime(2027, 3, 20, 9, tzinfo=BERLIN)
BOUNDS = datetime(2025, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)


def authored(text='10:00 +1d', anchor=START, *, all_day=False):
    starts = expand_sequence(text, anchor)
    return Event('source', 'local', 'Authored series', anchor,
                 anchor + (timedelta(days=1) if all_day else timedelta(minutes=90)),
                 ical_uid='synthetic-authoring@example.invalid', timezone_id=BERLIN.key,
                 **schedule_fields(starts, all_day=all_day), all_day=all_day,
                 schedule_expression=text, expression_anchor=anchor.date().isoformat(), schedule_syntax=2)


def instants(values):
    return {value.astimezone(UTC) for value in values}


def occurrences(master):
    return expand_series(master, *BOUNDS)


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    widgets = []
    store = Store(':memory:')
    with patch('debacal.dialogs.local_zone', return_value=UTC), \
         patch('debacal.series_preview.local_zone', return_value=UTC), \
         patch('debacal.calendar_widgets.local_zone', return_value=UTC):
        def create(master=None, *, selected=None, scope='group', start=START):
            selected = selected or (occurrences(master)[0] if master else None)
            dialog = EventDialog(store.list_calendars(), selected.start if selected else start,
                                 selected.end if selected else start + timedelta(minutes=90),
                                 event=selected, series=master, edit_scope=scope)
            widgets.append(dialog)
            if master is None:
                dialog.title.setText('New series')
            return dialog

        def click(dialog, day):
            dialog.show()
            dialog.preview.show_month(day)
            app.processEvents()
            assert dialog.preview.date_editing
            rect = dialog.preview.grid.cell_rect(day)
            QTest.mouseClick(dialog.preview.grid, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
            app.processEvents()

        yield store, create, click
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    store.close()


def save(dialog):
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    return dialog.result_events[0]


@pytest.mark.parametrize('text', ['  4*(+2)  ', '10:00 +1d', '+5d 11:00-2.4.2027 +3d'])
def test_title_only_edit_keeps_literal_timezone_anchor_and_dst_instants(editors, text):
    store, create, _ = editors
    master = parse_ics(export_ics([authored(text)]), 'local')[0]
    store.apply('Import', [master])
    original = instants(base_schedule_starts(master))
    dialog = create(master, selected=occurrences(master)[-1])
    assert dialog.pattern.text() == text
    assert not dialog._sequence_reencode and not dialog.schedule_replacement
    assert dialog.value(dialog.start) == master.start.astimezone(UTC)
    assert dialog.start.dateTime().toPython().hour == 8
    assert 'Sequence dates and times use Europe/Berlin' in dialog.pattern_help.text()
    assert 'calendar show UTC' in dialog.pattern_help.text()
    dialog.title.setText('Renamed')
    store.edit_series(dialog.edit_reference, save(dialog), replace_schedule=dialog.schedule_replacement)
    changed = store.get_event(master.id)
    assert (changed.schedule_expression, changed.timezone_id, changed.expression_anchor) == (
        text, BERLIN.key, START.date().isoformat())
    assert instants(base_schedule_starts(changed)) == original


def test_retyping_configuration_uses_original_clock_and_preserves_duration(editors):
    store, create, _ = editors
    master = authored()
    store.apply('Import', [master])
    dialog = create(master)
    literal = '11:00 +2d 9:00'
    dialog.pattern.setText(literal)
    expected = expand_sequence(literal, START)
    assert instants(dialog.sequence_starts()) == instants(expected)
    edited = save(dialog)
    store.edit_series(dialog.edit_reference, edited, replace_schedule=dialog.schedule_replacement)
    changed = store.get_event(master.id)
    assert changed.timezone_id == BERLIN.key
    assert changed.schedule_expression == literal
    assert instants(base_schedule_starts(changed)) == instants(expected)
    assert all(value.end - value.start == timedelta(minutes=90) for value in occurrences(changed))
    reopened = create(parse_ics(export_ics([changed]), 'local')[0])
    assert reopened.pattern.text() == literal
    assert instants(reopened.sequence_starts()) == instants(expected)


def test_previous_display_day_anchor_and_clicks_keep_source_clock(editors):
    store, create, click = editors
    initial = datetime(2026, 11, 7, 0, 30, tzinfo=BERLIN)
    master = authored('1:30 +1d', initial)
    store.apply('Import', [master])
    dialog = create(master)
    assert dialog.start.date().toPython() == date(2026, 11, 6)
    assert dialog.pattern.text() == master.schedule_expression
    assert dialog.preview.anchor_dates == {date(2026, 11, 6)}
    click(dialog, date(2026, 11, 6))
    assert 'initial event is fixed' in dialog.error.text()
    assert dialog.pattern.text() == master.schedule_expression
    # The original 00:30 authoring clock appears on the preceding UTC date.
    click(dialog, date(2026, 11, 10))
    added = datetime(2026, 11, 11, 0, 30, tzinfo=BERLIN)
    expected = [*base_schedule_starts(master), added]
    assert instants(dialog.sequence_starts()) == instants(expected)
    assert len(dialog.preview.events_on(date(2026, 11, 10))) == 1
    # Removing a displayed day removes its matching slots, keeping the anchor.
    click(dialog, date(2026, 11, 7))
    expected = [value for value in expected if value.astimezone(UTC).date() != date(2026, 11, 7)]
    assert instants(dialog.sequence_starts()) == instants(expected)
    store.edit_series(dialog.edit_reference, save(dialog), replace_schedule=dialog.schedule_replacement)
    changed = store.get_event(master.id)
    assert changed.expression_anchor == '2026-11-07'
    assert changed.timezone_id == BERLIN.key
    reopened = create(parse_ics(export_ics([changed]), 'local')[0])
    assert instants(reopened.sequence_starts()) == instants(expected)


def test_calendar_click_accounts_for_dst_date_offset_and_before_anchor_dates(editors):
    _, create, click = editors
    initial = datetime(2027, 3, 25, 1, 30, tzinfo=BERLIN)
    master = authored('+1d', initial)
    dialog = create(master)
    click(dialog, date(2027, 3, 29))
    # Summer time moves 01:30 Berlin to the previous UTC day.
    added = datetime(2027, 3, 30, 1, 30, tzinfo=BERLIN)
    assert added.astimezone(UTC).date() == date(2027, 3, 29)
    assert added.astimezone(UTC) in instants(dialog.sequence_starts())
    click(dialog, date(2026, 12, 10))
    previous = datetime(2026, 12, 10, 1, 30, tzinfo=BERLIN)
    assert previous.astimezone(UTC) in instants(dialog.sequence_starts())
    assert dialog.sequence_starts()[0] == initial
    assert instants(expand_sequence(dialog.pattern.text(), initial)) == instants(dialog.sequence_starts())


def test_calendar_click_rejects_nonexistent_original_clock_without_rewriting_text(editors):
    _, create, click = editors
    initial = datetime(2027, 3, 25, 2, 30, tzinfo=BERLIN)
    master = authored('+1d', initial)
    dialog = create(master)
    before = dialog.pattern.text()
    click(dialog, date(2027, 3, 28))
    assert 'daylight-saving clock change' in dialog.error.text()
    assert dialog.pattern.text() == before
    assert instants(dialog.sequence_starts()) == instants(base_schedule_starts(master))


def test_scope_drafts_keep_origin_zone_and_each_literal(editors):
    _, create, _ = editors
    master = authored('  4*+2  ')
    selected = occurrences(master)[2]
    dialog = create(master, selected=selected, scope='single')
    assert not dialog.pattern.isEnabled()
    dialog.scope.setCurrentIndex(dialog.scope.findData('group'))
    dialog.pattern.setText('10:00 +2d')
    expected = instants(expand_sequence('10:00 +2d', START))
    dialog.scope.setCurrentIndex(dialog.scope.findData('future'))
    assert dialog.sequence_zone() == BERLIN
    future_text = dialog.pattern.text()
    dialog.pattern.setText('11:00 +3d')
    dialog.scope.setCurrentIndex(dialog.scope.findData('group'))
    assert dialog.sequence_zone() == BERLIN
    assert dialog.pattern.text() == '10:00 +2d'
    assert instants(dialog.sequence_starts()) == expected
    dialog.scope.setCurrentIndex(dialog.scope.findData('single'))
    assert dialog.pattern.text() == master.schedule_expression
    dialog.scope.setCurrentIndex(dialog.scope.findData('future'))
    assert dialog.sequence_zone() == BERLIN
    assert dialog.pattern.text() == '11:00 +3d' and dialog.pattern.text() != future_text


def test_all_day_imported_dates_do_not_follow_timezone_date_conversion(editors):
    _, create, click = editors
    initial = datetime(2026, 11, 7, tzinfo=BERLIN)
    source = authored('2*+1d', initial, all_day=True)
    with patch('debacal.ics.local_timezone', return_value=UTC):
        master = parse_ics(export_ics([source]), 'local')[0]
    dialog = create(master)
    assert dialog.start.date().toPython() == initial.date()
    assert dialog.pattern.text() == source.schedule_expression
    assert 'Sequence dates and times use' not in dialog.pattern_help.text()
    click(dialog, date(2026, 11, 5))
    changed = save(dialog)
    assert changed.all_day
    assert {value.date() for value in base_schedule_starts(changed)} == {
        date(2026, 11, 5), date(2026, 11, 7), date(2026, 11, 8), date(2026, 11, 9)}
    assert changed.expression_anchor == '2026-11-07'


def test_all_day_foreign_zone_controls_and_scope_drafts_keep_calendar_dates(editors):
    _, create, _ = editors
    initial = datetime(2026, 11, 7, tzinfo=BERLIN)
    master = authored('2*+1d', initial, all_day=True)
    selected = occurrences(master)[1]
    dialog = create(master, selected=selected)
    assert dialog.start.date().toPython() == initial.date()
    assert dialog.value(dialog.start) == initial
    unchanged = dialog.build_events()[0]
    assert unchanged.start.astimezone(UTC) == master.start.astimezone(UTC)
    assert unchanged.end.astimezone(UTC) == master.end.astimezone(UTC)
    assert unchanged.expression_anchor == master.expression_anchor
    for scope, expected in [('single', selected.start), ('future', selected.start), ('group', initial)]:
        dialog.scope.setCurrentIndex(dialog.scope.findData(scope))
        assert dialog.start.date().toPython() == expected.date()
        assert dialog.value(dialog.start).astimezone(UTC) == expected.astimezone(UTC)


def test_new_custom_and_imported_unannotated_dates_use_local_display_zone(editors):
    _, create, _ = editors
    master = replace(authored(), schedule_expression='', expression_anchor='', schedule_syntax=1)
    for source in (None, master):
        dialog = create(source)
        dialog.pattern.setText('10:00 +1d')
        assert dialog.sequence_zone() == UTC
        assert dialog.sequence_starts()[1].hour == 10
        assert dialog.sequence_starts()[1].tzinfo == UTC
        assert 'Sequence dates and times use' not in dialog.pattern_help.text()


def test_saved_500_event_bound_and_retyped_overflow_still_apply(editors):
    _, create, _ = editors
    master = authored('499*+1d')
    dialog = create(master)
    assert dialog.pattern.text() == '499*+1d'
    assert len(dialog.sequence_starts()) == 500
    assert dialog.build_events()[0].timezone_id == BERLIN.key
    dialog.pattern.setText('500*+1d')
    dialog.save()
    assert not dialog.result_events
    assert 'limit of 500' in dialog.error.text()

