"""Calendar gestures keep custom sequences compact, editable and lossless."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.models import Event
from debacal.patterns import expand_sequence
from debacal.preferences import Preferences
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    widgets, errors = [], []
    with patch('debacal.dialogs.local_zone', return_value=ZONE), patch('sys.excepthook', side_effect=lambda _type, value, _tb: errors.append(value)):
        def create(start=START, event=None, series=None, edit_scope='single'):
            reference = series or event
            if reference:
                start, end = reference.start, reference.end
            else:
                end = start + timedelta(minutes=90)
            dialog = EventDialog(store.list_calendars(), start, end, event=event, series=series,
                                 preferences=Preferences(0, 'european', '24h'), edit_scope=edit_scope)
            widgets.append(dialog)
            if event is None:
                dialog.title.setText('A clicked sequence')
                dialog.repeat.setCurrentIndex(dialog.repeat.findData('RDATE'))
            dialog.show()
            app.processEvents()
            return dialog

        def click(dialog, day, *, editing=True):
            dialog.preview.show_month(day)
            app.processEvents()
            assert dialog.preview.date_editing is editing
            rect = dialog.preview.grid.cell_rect(day)
            assert not rect.isEmpty()
            QTest.mouseClick(dialog.preview.grid, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
            app.processEvents()
            assert not errors
            return dialog.pattern.text()

        yield store, create, click
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def save(dialog):
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    return dialog.result_events[0]


def instants(starts):
    return {value.timestamp() for value in starts}


def actual(store, master):
    return store.query_occurrences(master.start - timedelta(days=100), master.start + timedelta(days=400))


@pytest.mark.parametrize('step', [timedelta(days=1), timedelta(weeks=2)])
def test_repeated_mouse_clicks_compress_regular_gaps_and_stay_editable(editors, step):
    store, create, click = editors
    dialog = create()
    for index in range(1, 5):
        click(dialog, (START + index * step).date())
        assert dialog.preview.date_editing
        assert len(dialog.sequence_starts()) == index + 1
    expression = dialog.pattern.text()
    assert '*' in expression
    if step == timedelta(weeks=2):
        assert expression == '4*+2'
    assert instants(expand_sequence(expression, START)) == instants([START + index * step for index in range(5)])
    master = save(dialog)
    assert master.recurrence_rule
    store.apply('Create clicked series', [master])
    assert len(actual(store, master)) == 5


def test_cross_year_clicks_omit_year_when_the_next_prefix_is_unambiguous(editors):
    _, create, click = editors
    anchor = datetime(2026, 12, 28, 9, tzinfo=ZONE)
    dialog = create(anchor)
    click(dialog, date(2027, 1, 3))
    click(dialog, date(2027, 1, 10))
    assert '2026' not in dialog.pattern.text() and '2027' not in dialog.pattern.text()
    assert [value.date() for value in dialog.sequence_starts()] == [anchor.date(), date(2027, 1, 3), date(2027, 1, 10)]
    assert dialog.preview.current_month == date(2027, 1, 1)


def test_before_anchor_click_keeps_initial_first_and_uses_exact_year(editors):
    _, create, click = editors
    dialog = create()
    click(dialog, date(2026, 10, 3))
    assert '2026' in dialog.pattern.text()
    click(dialog, date(2026, 11, 18))
    starts = dialog.sequence_starts()
    assert starts[0] == START
    assert instants(starts) == instants([START, datetime(2026, 10, 3, 9, tzinfo=ZONE), datetime(2026, 11, 18, 9, tzinfo=ZONE)])


def test_relative_text_typing_is_never_rewritten_and_mouse_clicks_still_work(editors):
    _, create, click = editors
    dialog = create()
    literal = '  2*(+2)  +3   '
    dialog.pattern.setFocus()
    QTest.keyClicks(dialog.pattern, literal)
    assert dialog.pattern.text() == literal
    assert dialog.preview.date_editing
    before = dialog.sequence_starts()
    new_day = date(2027, 1, 9)
    click(dialog, new_day)
    assert instants(dialog.sequence_starts()) == instants([*before, datetime(2027, 1, 9, 9, tzinfo=ZONE)])
    assert dialog.preview.date_editing


def test_reopen_compressed_series_click_remove_and_undo_keep_exact_duration(editors):
    store, create, click = editors
    dialog = create()
    for index in range(1, 5):
        click(dialog, (START + timedelta(weeks=2 * index)).date())
    master = save(dialog)
    store.apply('Create', [master])
    original = actual(store, master)
    edit = create(event=original[2], series=master, edit_scope='group')
    assert edit.pattern.text() == master.schedule_expression
    click(edit, original[3].start.date())
    assert edit.schedule_replacement
    store.edit_series(edit.edit_reference, save(edit), replace_schedule=edit.schedule_replacement)
    changed = store.get_event(master.id)
    current = actual(store, changed)
    assert instants(value.start for value in current) == instants(value.start for value in original if value.id != original[3].id)
    assert all(value.end - value.start == timedelta(minutes=90) for value in current)
    store.undo()
    assert store.get_event(master.id) == master
    assert actual(store, master) == original


def test_adding_and_removing_other_days_preserves_multiple_times_per_day(editors):
    _, create, click = editors
    dialog = create()
    dialog.pattern.setText('10:00 11:00 +1d 9:00')
    before = dialog.sequence_starts()
    click(dialog, date(2026, 11, 7))
    assert instants(dialog.sequence_starts()) == instants([*before, datetime(2026, 11, 7, 9, tzinfo=ZONE)])
    click(dialog, date(2026, 11, 5))
    assert [(value.hour, value.minute) for value in dialog.sequence_starts() if value.date() == START.date()] == [(9, 0), (10, 0), (11, 0)]
    assert not any(value.date() == date(2026, 11, 5) for value in dialog.sequence_starts())


def test_click_uses_current_stored_times_instead_of_stale_other_client_authoring(editors):
    store, create, click = editors
    first = START.replace(hour=11)
    second = (START + timedelta(days=1)).replace(hour=15)
    master = Event('external', 'local', 'External edit', START, START + timedelta(minutes=90),
                   recurrence_dates=(first.isoformat(), second.isoformat()), schedule_syntax=2,
                   schedule_expression='11:00 11:00-5.11.2026', expression_anchor=START.date().isoformat())
    store.apply('Import', [master])
    edit = create(event=actual(store, master)[1], series=master, edit_scope='group')
    assert instants(edit.sequence_starts()) == instants([START, first, second])
    click(edit, date(2026, 11, 6))
    assert instants(edit.sequence_starts()) == instants([START, first, second, START + timedelta(days=2)])
    store.edit_series(edit.edit_reference, save(edit), replace_schedule=edit.schedule_replacement)
    assert instants(value.start for value in actual(store, master)) == instants([START, first, second, START + timedelta(days=2)])


def test_removing_last_unrepresentable_imported_addition_does_not_restore_old_cache(editors):
    store, create, click = editors
    initial = START.replace(second=30, microsecond=123456)
    extra = (START + timedelta(days=1)).replace(second=59, microsecond=234567)
    master = Event('precise', 'local', 'Precise source', initial, initial + timedelta(minutes=90), recurrence_dates=(extra.isoformat(),))
    store.apply('Import', [master])
    edit = create(event=actual(store, master)[0], series=master, edit_scope='group')
    assert edit.pattern.text() == ''
    click(edit, extra.date())
    assert edit.pattern.text() == ''
    assert edit.sequence_starts() == [initial]
    assert edit.schedule_replacement
    store.edit_series(edit.edit_reference, save(edit), replace_schedule=edit.schedule_replacement)
    chosen = store.get_event(master.id)
    assert chosen.start == initial and chosen.end == initial + timedelta(minutes=90)
    assert chosen.recurrence_dates == ()
    store.undo()
    assert store.get_event(master.id) == master


def test_unrelated_click_preserves_exclusions_and_clicking_skipped_day_restores_it(editors):
    store, create, click = editors
    dialog = create()
    dialog.pattern.setText('3*+1')
    master = save(dialog)
    store.apply('Create', [master])
    instances = actual(store, master)
    skipped = instances[2]
    store.delete_occurrence(skipped)
    master = store.get_event(master.id)
    edit = create(event=actual(store, master)[0], series=master, edit_scope='group')
    assert edit.preview.events_on(skipped.start.date()) == []
    click(edit, date(2026, 11, 30))
    assert edit.preview.events_on(skipped.start.date()) == []
    assert edit.preview.masters[0].excluded_dates == master.excluded_dates
    click(edit, skipped.start.date())
    assert len(edit.preview.events_on(skipped.start.date())) == 1
    assert not edit.preview.masters[0].excluded_dates
    store.edit_series(edit.edit_reference, save(edit), replace_schedule=edit.schedule_replacement)
    assert len(actual(store, store.get_event(master.id))) == 5


def test_invalid_text_disables_date_edits_without_changing_what_was_typed(editors):
    _, create, click = editors
    dialog = create()
    dialog.pattern.setText('2*(+2')
    before = dialog.pattern.text()
    click(dialog, date(2026, 11, 12), editing=False)
    assert dialog.pattern.text() == before
    assert dialog.error.text()
