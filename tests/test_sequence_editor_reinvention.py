"""The editor exposes the same explicit-reference and clamp model as transport."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Calendar, Event
from debacal.patterns import compile_sequence
from debacal.recurrence import expand_event, recurrence_key
from debacal.schedule_authoring import expression_matches


UTC = timezone.utc
START = datetime(2026, 10, 31, 9, tzinfo=UTC)


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    owned = []
    with patch('debacal.event_dialog.local_zone', return_value=UTC):
        def create(start=START, source=None):
            dialog = EventDialog([Calendar('local', 'Personal', '#587c67')], start,
                                 start + timedelta(hours=1), event=source,
                                 series=source, edit_scope='group')
            owned.append(dialog)
            dialog.title.setText('Appointment')
            return dialog
        yield create
        for dialog in owned:
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def starts(source):
    return [item.start for item in expand_event(source, START - timedelta(days=5), START + timedelta(days=400))]


def test_month_clamp_control_changes_standard_rule_and_keeps_preferred_day(editors):
    dialog = editors()
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('MONTHLY')
    dialog.repeat_count.setValue(6)
    assert not dialog.clamp.isHidden()
    assert [item.day for item in starts(dialog.build_events()[0])] == [31] * 6
    dialog.clamp.setChecked(True)
    source = dialog.build_events()[0]
    assert 'BYSETPOS=-1' in source.recurrence_rule
    assert [item.day for item in starts(source)] == [31, 30, 31, 31, 28, 31]
    assert expression_matches(source, source.schedule_expression)
    loaded = parse_ics(export_ics([source]), 'local')[0]
    assert starts(loaded) == starts(source)
    reopened = editors(source=loaded)
    assert reopened.clamp.isChecked()
    reopened.clamp.setChecked(False)
    assert [item.day for item in starts(reopened.build_events()[0])] == [31] * 6


@pytest.mark.parametrize('kind, day', [('DAILY', 31), ('WEEKLY', 31), ('MONTHLY', 28), ('YEARLY', 31)])
def test_irrelevant_clamp_option_is_hidden(editors, kind, day):
    dialog = editors(START.replace(day=day))
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData(kind)
    assert dialog.clamp.isHidden()


def test_custom_calendar_clamp_toggle_retains_other_text(editors):
    dialog = editors()
    text = '+0d  2*(+1m) +2w'
    dialog.pattern.setText(text)
    assert not dialog.clamp.isHidden()
    dialog.clamp.setChecked(True)
    assert dialog.pattern.text() == '+0d  2*(+1m!) +2w'
    assert expression_matches(dialog.build_events()[0], dialog.pattern.text())


def test_clamped_monthly_draft_can_switch_to_daily_then_back_to_custom(editors):
    dialog = editors()
    dialog.pattern.setText('+0d 3*+1m!')
    original = starts(dialog.build_events()[0])
    assert dialog.clamp.isChecked()
    dialog.repeat.setCurrentData('DAILY')
    assert not dialog.error.text()
    assert dialog._draft.kind == 'DAILY'
    assert not dialog._draft.clamp
    dialog.repeat.setCurrentData('RDATE')
    assert starts(dialog.build_events()[0]) == original
    assert dialog.clamp.isChecked()


def test_reference_occurrence_can_be_removed_in_preview_and_stay_removed_on_move(editors):
    dialog = editors()
    dialog.pattern.setText('every(1d, count=4)')
    dialog.toggle_date(START.date())
    source = dialog.build_events()[0]
    assert START not in starts(source)
    assert len(starts(source)) == 3
    assert dialog.pattern.text() and expression_matches(source, dialog.pattern.text())
    dialog.start.set_value(START + timedelta(days=2))
    moved = dialog.build_events()[0]
    assert moved.start not in starts(moved)
    assert expression_matches(moved, moved.schedule_expression)


def test_none_saves_and_roundtrips_without_restoring_reference(editors):
    dialog = editors()
    dialog.pattern.setText('none')
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    source = dialog.result_events[0]
    assert starts(source) == []
    assert source.excluded_dates == (recurrence_key(START),)
    assert starts(parse_ics(export_ics([source]), 'local')[0]) == []
    reopened = editors(source=source)
    assert reopened.pattern.text() == 'none'


def test_document_larger_than_qlineedit_default_is_fully_editable(editors):
    additions = tuple(recurrence_key(START + timedelta(days=index)) for index in range(1, 1600))
    source = Event('large', 'local', 'Large date set', START, START + timedelta(hours=1),
                   recurrence_dates=additions)
    dialog = editors(source=source)
    assert len(dialog.pattern.text()) > 32767
    assert dialog.pattern.text().startswith('schedule{')
    assert expression_matches(source, dialog.pattern.text())
    assert dialog.build_events()[0].recurrence_dates == source.recurrence_dates


def test_coincident_reference_moves_have_compact_text_and_keep_exception_content(editors):
    source = Event('same', 'local', 'Same time', START, START + timedelta(hours=1),
                   **compile_sequence('every(1d, count=2)', START).fields)
    moved = replace(source, start=START + timedelta(days=1), end=START + timedelta(days=1, hours=1),
                    recurrence_rule='', description='Retain the first occurrence notes')
    source = replace(source, recurrence_overrides={recurrence_key(START): moved.to_dict()})
    dialog = editors(source=source)
    assert not dialog.pattern.text().startswith('schedule{')
    assert '+0d' in dialog.pattern.text()
    assert expression_matches(source, dialog.pattern.text())
    assert dialog.build_events()[0].recurrence_overrides == source.recurrence_overrides


@pytest.mark.parametrize('obsolete', ['+2', '+1h', '+30min', '3.2', '3.2.2027.'])
def test_obsolete_input_cannot_save_an_old_valid_draft(editors, obsolete):
    dialog = editors()
    dialog.pattern.setText('+0d +1w')
    dialog.pattern.setText(obsolete)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.error.text()
    assert not dialog.result_events
