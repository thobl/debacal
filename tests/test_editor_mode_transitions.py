"""Recurrence controls stay coherent through mixed graphical and text edits."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.recurrence import expand_event, occurrence


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 8, 9, tzinfo=ZONE)


def starts(event):
    return [value.start for value in expand_event(event, START - timedelta(days=60), START + timedelta(days=2500))]


@pytest.fixture
def editor_factory():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _type, value, _trace: errors.append(value)):
        def create(source=None, scale=100):
            selected = occurrence(source, source.start) if source else None
            dialog = EventDialog([Calendar('local', 'Personal', '#587c67')], START,
                                 START + timedelta(hours=1), event=selected, series=source,
                                 edit_scope='group', preferences=Preferences(date_format='european', font_scale=scale))
            dialog.resize(1080, 900)
            dialog.title.setText('An appointment')
            owned.append(dialog)
            dialog.show()
            for _ in range(5):
                app.processEvents()
            return dialog
        yield create
        for dialog in reversed(owned):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_frequency_switching_keeps_notes_position_and_sidebar_width(editor_factory, scale):
    dialog = editor_factory(scale=scale)
    dialog.schedule_mode.setCurrentData('repeat')
    for _ in range(5):
        QApplication.processEvents()
    top = dialog.notes_section.geometry().top()
    for kind in ('DAILY', 'MONTHLY', 'WEEKLY', 'YEARLY', 'RDATE', 'MONTHLY', 'RDATE', 'DAILY'):
        QTest.mouseClick(dialog.repeat.buttons[kind], Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        assert dialog.repeat.currentData() == kind
        assert dialog.notes_section.geometry().top() == top
        assert dialog.controls_scroll.widget().width() <= dialog.controls_scroll.viewport().width()
        assert not dialog._loading
        assert not dialog.error.text()


def test_custom_regular_and_alternative_modes_save_the_visible_schedule(editor_factory):
    dialog = editor_factory()
    dialog.pattern.setText('+1d +3d +2w')
    assert dialog.repeat.currentData() == 'RDATE'
    for kind in ('DAILY', 'MONTHLY', 'WEEKLY'):
        dialog.repeat.setCurrentData(kind)
        dialog.interval.setValue(2)
        dialog.repeat_count.setValue(4)
        assert dialog._draft.kind == kind
        event = dialog.build_events()[0]
        assert 'FREQ=' + kind in event.recurrence_rule
        assert event.schedule_expression == ''
        expected = starts(event)
        dialog.repeat.setCurrentData('RDATE')
        assert starts(dialog.build_events()[0]) == expected
        dialog.schedule_mode.setCurrentData('alternatives')
        assert dialog.build_events()[0].candidate
        dialog.schedule_mode.setCurrentData('single')
        assert not dialog.build_events()[0].recurring
        dialog.schedule_mode.setCurrentData('repeat')
        assert starts(dialog.build_events()[0]) == expected
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    event = dialog.result_events[0]
    assert starts(parse_ics(export_ics([event]), 'local')[0]) == starts(event)


def test_invalid_custom_text_can_be_replaced_by_a_regular_pattern(editor_factory):
    dialog = editor_factory()
    dialog.pattern.setText('2*(+2')
    dialog.repeat.setCurrentData('DAILY')
    dialog.repeat_count.setValue(3)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert starts(dialog.result_events[0]) == [START + timedelta(days=value) for value in range(3)]


def test_invalid_finite_bound_blocks_save_and_a_valid_choice_recovers(editor_factory):
    dialog = editor_factory()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('YEARLY')
    dialog.repeat_count.setValue(100000)
    assert dialog.error.text()
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert not dialog.result_events
    dialog.repeat_count.setValue(3)
    assert not dialog.error.text()
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_imported_advanced_rules_are_preserved_without_a_rule_editor(editor_factory):
    source = Event('monthly', 'local', 'Last Friday', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=MONTHLY;BYDAY=-1FR;COUNT=4', timezone_id=ZONE.key)
    dialog = editor_factory(source)
    assert not hasattr(dialog, 'custom_rule') and not hasattr(dialog, 'rule_button')
    assert dialog.custom_input.currentWidget() is dialog.advanced_page
    dialog.title.setText('Renamed')
    assert dialog.build_events()[0].recurrence_rule == source.recurrence_rule
    before = starts(dialog.build_events()[0])
    dialog.convert_sequence_button.click()
    assert dialog._draft.kind == 'custom'
    assert starts(dialog.build_events()[0]) == before
    assert dialog.custom_input.currentIndex() == 0


def test_unbounded_import_stays_valid_after_refused_sequence_conversion(editor_factory):
    source = Event('monthly', 'local', 'Last Friday', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=MONTHLY;BYDAY=-1FR', timezone_id=ZONE.key)
    dialog = editor_factory(source)
    dialog.convert_sequence_button.click()
    assert dialog._draft.kind == 'advanced'
    assert dialog.custom_input.currentWidget() is dialog.advanced_page
    assert dialog.build_events()[0].recurrence_rule == source.recurrence_rule
    assert dialog.error.text()
    dialog.repeat.setCurrentData('DAILY')
    dialog.no_end.setChecked(False)
    assert not dialog.error.text()
    assert dialog._draft.kind == 'DAILY'


def test_scope_switching_preserves_invalid_sequence_text_without_poisoning_other_scope(editor_factory):
    source = Event('series', 'local', 'A series', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY;COUNT=4', timezone_id=ZONE.key)
    dialog = editor_factory(source)
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('2*(+2')
    dialog.scope.setCurrentData('single')
    assert not dialog.error.text()
    assert not dialog.build_events()[0].recurring
    dialog.scope.setCurrentData('group')
    assert dialog.pattern.text() == '2*(+2'
    with pytest.raises(ValueError):
        dialog.build_events()
    dialog.repeat.setCurrentData('DAILY')
    assert not dialog.error.text()
    assert dialog.build_events()[0].recurrence_rule
