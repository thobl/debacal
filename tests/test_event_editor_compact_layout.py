"""The editor keeps its controls aligned without crowding the month preview."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel

from debacal.event_dialog import EventDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 8, 9, tzinfo=ZONE)


@pytest.fixture
def editor_factory():
    app = QApplication.instance() or QApplication([])
    owned = []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE):
        def create(scale=100):
            source = Event('one', 'work', 'Appointment', START, START + timedelta(hours=1))
            dialog = EventDialog(
                [Calendar('work', 'Work', '#587c67'), Calendar('personal', 'Personal', '#a66a46')],
                START, event=source,
                preferences=Preferences(date_format='european', font_scale=scale),
                scope_bindings={'scope_single': 'F5', 'scope_all': 'F6', 'scope_following': 'F7'})
            owned.append(dialog)
            dialog.resize(1080, 900)
            dialog.show()
            for _ in range(5):
                app.processEvents()
            return dialog
        yield create
        for dialog in owned:
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_edit_scope_shares_the_heading_row_without_a_separate_section(editor_factory, scale):
    dialog = editor_factory(scale)
    assert dialog.scope_controls.parentWidget() is dialog.header
    assert abs(dialog.scope_controls.geometry().center().y() - dialog.heading.geometry().center().y()) <= 1
    assert dialog.scope_controls.geometry().left() > dialog.heading.geometry().right()
    assert not any(label.text() == 'Apply to' for label in dialog.findChildren(QLabel))
    for value, key in (('single', 'F5'), ('group', 'F6'), ('future', 'F7')):
        button = dialog.scope_buttons[value]
        assert not button.text() and not button.icon().isNull()
        assert key in button.toolTip()
    assert dialog.calendar.isEnabled()
    assert dialog.calendar.findData('personal') >= 0


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_endpoint_and_duration_labels_stay_inline_without_horizontal_overflow(editor_factory, scale):
    dialog = editor_factory(scale)
    for all_day in (False, True):
        dialog.all_day.setChecked(all_day)
        for _ in range(3):
            QApplication.processEvents()
        assert [label.text() for label in dialog.range_fields.labels] == ['Start', 'End']
        for label, field in zip(dialog.range_fields.labels, dialog.range_fields.fields):
            assert abs(label.geometry().center().y() - field.geometry().center().y()) <= 1
            assert label.geometry().right() < field.geometry().left()
            assert field.geometry().right() < dialog.range_fields.width()
        duration = dialog.duration_fields
        assert abs(duration.label.geometry().center().y() - dialog.duration.geometry().center().y()) <= 1
        assert duration.label.geometry().right() < dialog.duration.geometry().left()
        assert dialog.controls_scroll.widget().width() <= dialog.controls_scroll.viewport().width()
    assert not hasattr(dialog, 'duration_preferences')


def test_add_occurrence_stays_beside_schedule_mode_and_preview_heading_follows_navigation(editor_factory):
    dialog = editor_factory()
    add, modes = dialog.add_occurrence_button, dialog.schedule_mode
    assert abs(add.geometry().center().y() - modes.geometry().center().y()) <= 1
    assert add.geometry().left() > modes.geometry().right()
    assert dialog.frequency_row.indexOf(add) == -1
    label, next_button = dialog.preview.month_label, dialog.preview.next_button
    assert label.alignment() & Qt.AlignmentFlag.AlignLeft
    assert label.geometry().left() > next_button.geometry().right()
