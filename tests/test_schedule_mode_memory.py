"""Choosing a clean regular pattern is reversible and never revives old edits."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.recurrence import (
    ScheduleReplacement, expand_event, occurrence, recurrence_key, updated_series,
)
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 8, 9, tzinfo=ZONE)
BOUNDS = START - timedelta(days=5), START + timedelta(days=100)
SCHEDULE_FIELDS = ('recurrence_rule', 'recurrence_dates', 'excluded_dates',
                   'recurrence_overrides', 'schedule_expression', 'expression_anchor', 'schedule_syntax')


def ranges(event):
    return [(value.start, value.end) for value in expand_event(event, *BOUNDS)]


def schedule(event):
    return {name: getattr(event, name) for name in SCHEDULE_FIELDS}


def irregular_source():
    source = Event('series', 'local', 'A series', START, START + timedelta(hours=1),
                   recurrence_rule='FREQ=DAILY;COUNT=10', timezone_id=ZONE.key)
    key = recurrence_key(START + timedelta(days=7))
    changed = replace(occurrence(source, START + timedelta(days=7)),
                      end=START + timedelta(days=7, hours=3), title='An individual exception')
    return replace(source, recurrence_overrides={key: changed.to_dict()},
                   excluded_dates=(recurrence_key(START + timedelta(days=2)),),
                   recurrence_dates=(recurrence_key(START + timedelta(days=20, hours=2)),))


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    owned, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _type, value, _trace: errors.append(value)):
        def create(source=None, *, selected=None, scope='group', all_day=False):
            selected = selected or (occurrence(source, source.start) if source else None)
            dialog = EventDialog(store.list_calendars(), selected.start if selected else START,
                                 selected.end if selected else START + timedelta(hours=1),
                                 event=selected, series=source, edit_scope=scope)
            owned.append(dialog)
            if source is None:
                dialog.title.setText('A draft')
                dialog.all_day.setChecked(all_day)
            return dialog
        yield create, store
        for dialog in reversed(owned):
            if dialog.range_picker is not None:
                dialog.range_picker.close()
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


@pytest.mark.parametrize('all_day', [False, True])
def test_deleted_regular_dates_are_remembered_separately_from_clean_indefinite_rule(editors, all_day):
    create, _ = editors
    dialog = create(all_day=all_day)
    anchor = dialog.value(dialog.start)
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('DAILY')
    dialog.repeat_count.setValue(6)
    dialog.toggle_date((anchor + timedelta(days=2)).date())
    assert dialog.repeat.currentData() == 'RDATE'
    custom = dialog.build_events()[0]
    text = dialog.pattern.text()
    dialog.repeat.setCurrentData('WEEKLY')
    dialog.no_end.setChecked(True)
    regular = dialog.build_events()[0]
    assert 'FREQ=WEEKLY' in regular.recurrence_rule
    assert 'COUNT=' not in regular.recurrence_rule and 'UNTIL=' not in regular.recurrence_rule
    assert not regular.recurrence_dates and not regular.excluded_dates and not regular.recurrence_overrides
    assert not regular.schedule_expression
    assert not dialog.repeat_count.isEnabled() and not dialog.repeat_until.isEnabled()
    dialog.repeat.setCurrentData('RDATE')
    assert dialog.pattern.text() == text
    assert schedule(dialog.build_events()[0]) == schedule(custom)
    dialog.schedule_mode.setCurrentData('alternatives')
    assert dialog.build_events()[0].candidate
    assert ranges(dialog.build_events()[0]) == ranges(custom)
    dialog.schedule_mode.setCurrentData('single')
    assert not dialog.build_events()[0].recurring
    dialog.schedule_mode.setCurrentData('repeat')
    assert schedule(dialog.build_events()[0]) == schedule(custom)


def test_custom_range_and_literal_return_after_visiting_regular_modes(editors):
    create, _ = editors
    dialog = create()
    dialog.pattern.setText('  +1d  +4d ')
    added = START + timedelta(days=8, hours=2)
    dialog._add_range(added, added + timedelta(hours=2))
    before = dialog.build_events()[0]
    text = dialog.pattern.text()
    assert any(end - start == timedelta(hours=2) for start, end in ranges(before))
    for kind in ('DAILY', 'MONTHLY', 'WEEKLY'):
        dialog.repeat.setCurrentData(kind)
        dialog.no_end.setChecked(False)
        dialog.repeat_count.setValue(3)
        regular = dialog.build_events()[0]
        assert regular.recurrence_rule.startswith('FREQ=' + kind)
        assert not regular.recurrence_dates and not regular.excluded_dates and not regular.recurrence_overrides
    dialog.repeat.setCurrentData('RDATE')
    assert dialog.pattern.text() == text
    restored = dialog.build_events()[0]
    assert schedule(restored) == schedule(before)
    assert ranges(parse_ics(export_ics([restored]), 'local')[0]) == ranges(before)


def test_title_only_edit_preserves_imported_exceptions_until_a_pattern_is_chosen(editors):
    create, _ = editors
    source = irregular_source()
    dialog = create(source)
    dialog.title.setText('Only the shared title changed')
    edited = dialog.build_events()[0]
    assert schedule(edited) == schedule(source)
    projected = dialog.preview.masters[0]
    assert ranges(projected) == ranges(source)
    dialog.repeat.setCurrentData('RDATE')
    before = dialog.build_events()[0]
    dialog.repeat.setCurrentData('WEEKLY')
    regular = dialog.build_events()[0]
    assert not regular.recurrence_dates and not regular.excluded_dates and not regular.recurrence_overrides
    assert not dialog.preview.masters[0].recurrence_overrides
    dialog.repeat.setCurrentData('RDATE')
    assert schedule(dialog.build_events()[0]) == schedule(before)


def test_scope_changes_keep_independent_custom_memories_and_reset_flags(editors):
    create, _ = editors
    source = irregular_source()
    selected = occurrence(source, START + timedelta(days=3))
    dialog = create(source, selected=selected)
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('+2d +3d +9d')
    group_custom = schedule(dialog.build_events()[0])
    dialog.repeat.setCurrentData('WEEKLY')
    dialog.repeat_count.setValue(3)
    assert dialog.schedule_replacement is ScheduleReplacement.RESET

    dialog.scope.setCurrentData('single')
    assert not dialog.build_events()[0].recurring
    dialog.scope.setCurrentData('future')
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('+4d +2d +11d')
    future_custom = schedule(dialog.build_events()[0])
    dialog.repeat.setCurrentData('DAILY')
    dialog.no_end.setChecked(True)

    dialog.scope.setCurrentData('group')
    assert dialog.repeat.currentData() == 'WEEKLY'
    assert dialog.repeat_count.value() == 3
    assert dialog.schedule_replacement is ScheduleReplacement.RESET
    dialog.repeat.setCurrentData('RDATE')
    assert schedule(dialog.build_events()[0]) == group_custom
    dialog.scope.setCurrentData('future')
    assert dialog.repeat.currentData() == 'DAILY' and dialog.no_end.isChecked()
    assert dialog.schedule_replacement is ScheduleReplacement.RESET
    dialog.repeat.setCurrentData('RDATE')
    assert schedule(dialog.build_events()[0]) == future_custom


@pytest.mark.parametrize('scope', ['group', 'future'])
def test_clean_regular_selection_does_not_revive_a_matching_old_exception_and_is_undoable(editors, scope):
    create, store = editors
    source = irregular_source()
    store.apply('Create source', [source])
    selected = occurrence(source, START + timedelta(days=7) if scope == 'future' else START)
    dialog = create(source, selected=selected, scope=scope)
    dialog.repeat.setCurrentData('RDATE')
    dialog.repeat.setCurrentData('WEEKLY')
    dialog.repeat_count.setValue(3)
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert dialog.schedule_replacement is ScheduleReplacement.RESET
    edited = dialog.result_events[0]
    assert not dialog.preview.masters[0].recurrence_overrides
    store.edit_selection((selected,), scope, dialog.edit_reference, edited,
                         replace_schedule=dialog.schedule_replacement)
    stored = (store.get_event(source.id) if scope == 'group' else
              next(value for value in store.list_events() if value.id != source.id))
    assert stored.recurrence_overrides == {}
    assert stored.recurrence_dates == () and stored.excluded_dates == ()
    expected = [(selected.start + timedelta(weeks=index), selected.end + timedelta(weeks=index)) for index in range(3)]
    assert ranges(stored) == expected
    assert ranges(parse_ics(export_ics([stored]), 'local')[0]) == expected
    store.undo()
    assert schedule(store.get_event(source.id)) == schedule(source)


def test_edit_selection_keeps_a_new_range_for_an_existing_detached_occurrence(editors):
    create, store = editors
    source = irregular_source()
    store.apply('Create source', [source])
    selected = occurrence(source, START)
    dialog = create(source, selected=selected)
    target = next(value for value in expand_event(source, *BOUNDS)
                  if value.recurrence_id == recurrence_key(START + timedelta(days=7)))
    moved = target.start + timedelta(hours=2)
    dialog.edit_occurrence(target)
    picker = dialog.range_picker
    picker.set_range(moved, moved + timedelta(hours=4))
    picker.accept()
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    store.edit_selection((selected,), 'group', dialog.edit_reference, dialog.result_events[0],
                         replace_schedule=dialog.schedule_replacement)
    stored = store.get_event(source.id)
    changed = next(value for value in expand_event(stored, *BOUNDS) if value.recurrence_id == target.recurrence_id)
    assert changed.start == moved and changed.end == moved + timedelta(hours=4)
    assert changed.title == target.title
    assert ranges(parse_ics(export_ics([stored]), 'local')[0]) == ranges(stored)
    store.undo()
    assert schedule(store.get_event(source.id)) == schedule(source)


def test_reset_projection_is_authoritative_while_ordinary_replacements_keep_their_merge_behavior():
    source = irregular_source()
    edited = replace(source, recurrence_rule='FREQ=WEEKLY;COUNT=3', recurrence_dates=(),
                     excluded_dates=(), recurrence_overrides={})
    assert updated_series(source, source, edited, True).recurrence_overrides
    clean = updated_series(source, source, edited, ScheduleReplacement.RESET)
    assert clean.recurrence_overrides == {}
    assert clean.excluded_dates == () and clean.recurrence_dates == ()
