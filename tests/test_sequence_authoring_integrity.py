"""The editor's custom text describes the schedule that is actually saved."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.event_dialog import EventDialog
from debacal.event_schedule import ScheduleDraft
from debacal.ics import export_ics, parse_ics
from debacal.models import Event
from debacal.patterns import compile_sequence
from debacal.recurrence import expand_event, occurrence, recurrence_key
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def source_for(kind='WEEKLY', *, start=START, all_day=False, **changes):
    if all_day:
        start = start.replace(hour=0, minute=0)
    values = dict(id='series', calendar_id='local', ical_uid='authoring-integrity',
                  title='A recurring appointment', start=start,
                  end=start + (timedelta(days=1) if all_day else timedelta(hours=1)),
                  all_day=all_day, recurrence_rule=f'FREQ={kind};INTERVAL=1', timezone_id=getattr(start.tzinfo, 'key', ''))
    return Event(**(values | changes))


def instances(event, *, years=12):
    return expand_event(event, event.start - timedelta(days=1), event.start + timedelta(days=366 * years), limit=10000)


def ranges(event, *, years=12):
    return [(value.start.astimezone(timezone.utc), value.end.astimezone(timezone.utc))
            for value in instances(event, years=years)]


def edited_master(dialog):
    # An existing editor submits an occurrence-shaped selection edit. Its
    # projected master is the same event that storage and preview will use.
    return dialog._project_preview(dialog.build_events()[0])


def assert_complete_expression(event, *, years=12):
    """Rebuild from text alone, so hidden EXDATE/RDATE cannot conceal a bad literal."""
    compiled = compile_sequence(event.schedule_expression, event.start, all_day=event.all_day)
    assert compiled is not None, 'The unbounded series needs an editable terminal every(...) instruction.'
    nominal = replace(event, recurrence_overrides={})
    from_text = replace(nominal, **compiled.fields)
    assert [value.start for value in instances(from_text, years=years)] == [value.start for value in instances(nominal, years=years)]


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    dialogs, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE), \
         patch('debacal.series_preview.local_zone', return_value=ZONE), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def create(source=None):
            selected = occurrence(source, source.start) if source else None
            dialog = EventDialog(store.list_calendars(), selected.start if selected else START,
                                 selected.end if selected else START + timedelta(hours=1),
                                 event=selected, series=source, edit_scope='group')
            dialogs.append(dialog)
            if source is None:
                dialog.title.setText('A recurring appointment')
            dialog.show()
            dialog.activateWindow()
            app.processEvents()
            return dialog
        yield create, store
        for dialog in reversed(dialogs):
            if dialog.range_picker is not None:
                dialog.range_picker.close()
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def test_weekly_indefinite_early_deletions_remain_an_editable_complete_sequence(editors):
    create, store = editors
    source = source_for()
    store.apply('Create source', [source])
    dialog = create(source)
    removed = {START + timedelta(weeks=1), START + timedelta(weeks=2)}
    for stamp in sorted(removed):
        dialog.toggle_date(stamp.date())
    edited = edited_master(dialog)
    expected = [(value.start, value.end) for value in instances(source) if value.start not in removed]
    assert [(value.start, value.end) for value in instances(edited)] == expected
    assert dialog.custom_input.currentIndex() == 0, 'Graphical edits must not strand a simple schedule in Imported custom recurrence.'
    assert_complete_expression(edited)
    dialog.pattern.setFocus()
    # Rereading valid authored text must not resurrect excluded occurrences.
    dialog.pattern.setText(' ' + dialog.pattern.text())
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    selected = occurrence(source, source.start)
    store.edit_selection((selected,), 'group', dialog.edit_reference, dialog.result_events[0],
                         replace_schedule=dialog.schedule_replacement)
    stored = store.get_event(source.id)
    assert [(value.start, value.end) for value in instances(stored)] == expected
    imported = parse_ics(export_ics([stored]), 'local')[0]
    assert ranges(imported) == ranges(stored)
    reopened = create(imported)
    assert_complete_expression(edited_master(reopened))
    assert ranges(edited_master(reopened)) == ranges(stored)


@pytest.mark.parametrize('kind', ['DAILY', 'WEEKLY', 'MONTHLY', 'YEARLY'])
@pytest.mark.parametrize('all_day', [False, True])
def test_graphical_deletions_and_restoration_match_text_for_all_regular_tails(editors, kind, all_day):
    create, _ = editors
    source = source_for(kind, all_day=all_day)
    original = instances(source)
    removed = [original[index] for index in (1, 2, 6)]
    removed_keys = {value.start for value in removed}
    dialog = create(source)
    for value in removed:
        dialog.toggle_date(value.start.date())
    edited = edited_master(dialog)
    assert [value.start for value in instances(edited)] == [value.start for value in original if value.start not in removed_keys]
    assert_complete_expression(edited)
    for value in reversed(removed):
        dialog.toggle_date(value.start.date())
    restored = edited_master(dialog)
    assert ranges(restored) == ranges(source)
    assert_complete_expression(restored)


@pytest.mark.parametrize('start,kind,years', [
    (START.replace(year=2027, month=1, day=31), 'MONTHLY', 3),
    (START.replace(year=2028, month=2, day=29), 'YEARLY', 32),
])
def test_missing_month_days_and_leap_days_keep_standard_tail_semantics(editors, start, kind, years):
    create, _ = editors
    source = source_for(kind, start=start)
    original = instances(source, years=years)
    dialog = create(source)
    dialog.toggle_date(original[1].start.date())
    dialog.toggle_date(original[2].start.date())
    edited = edited_master(dialog)
    assert [value.start for value in instances(edited, years=years)] == [value.start for index, value in enumerate(original) if index not in (1, 2)]
    assert_complete_expression(edited, years=years)


def test_unbounded_custom_draft_survives_clean_regular_modes_and_single_mode(editors):
    create, _ = editors
    dialog = create(source_for())
    dialog.toggle_date((START + timedelta(weeks=1)).date())
    dialog.toggle_date((START + timedelta(weeks=2)).date())
    custom = edited_master(dialog)
    text = dialog.pattern.text()
    dialog.repeat.setCurrentData('MONTHLY')
    dialog.no_end.setChecked(True)
    assert not edited_master(dialog).excluded_dates
    dialog.repeat.setCurrentData('RDATE')
    assert dialog.pattern.text() == text
    assert ranges(edited_master(dialog)) == ranges(custom)
    dialog.schedule_mode.setCurrentData('single')
    assert not dialog.build_events()[0].recurring
    dialog.schedule_mode.setCurrentData('repeat')
    assert ranges(edited_master(dialog)) == ranges(custom)
    assert_complete_expression(edited_master(dialog))


def test_extra_time_and_duration_are_not_erased_by_revalidating_the_sequence(editors):
    create, _ = editors
    dialog = create(source_for())
    added = START + timedelta(days=2, hours=3)
    dialog._add_range(added, added + timedelta(hours=2, minutes=30))
    before = edited_master(dialog)
    assert any(value.start == added and value.end - value.start == timedelta(hours=2, minutes=30)
               for value in instances(before))
    assert_complete_expression(before)
    dialog.pattern.setFocus()
    dialog.pattern.setText(' ' + dialog.pattern.text())
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    edited = dialog._project_preview(dialog.result_events[0])
    assert ranges(edited) == ranges(before)
    imported = parse_ics(export_ics([edited]), 'local')[0]
    assert ranges(imported) == ranges(before)


def test_sequence_text_edits_preserve_surviving_individual_range_and_content(editors):
    create, _ = editors
    source = source_for()
    nominal = START + timedelta(weeks=4)
    key = recurrence_key(nominal)
    individual = replace(occurrence(source, nominal), start=nominal + timedelta(hours=2),
                         end=nominal + timedelta(hours=4), title='An individual title',
                         description='Private occurrence note', location='Room B')
    source = replace(source, recurrence_overrides={key: individual.to_dict()})
    dialog = create(source)
    for week in (1, 2):
        dialog.toggle_date((START + timedelta(weeks=week)).date())
    before = edited_master(dialog)
    dialog.pattern.setFocus()
    dialog.pattern.setText(' ' + dialog.pattern.text())
    assert ranges(edited_master(dialog)) == ranges(before)
    # Add a different date without altering the weekly tail or this occurrence.
    dialog.pattern.setText('+1d +20d every(1w)')
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    edited = dialog._project_preview(dialog.result_events[0])
    actual = next(value for value in instances(edited) if value.recurrence_id == key)
    assert (actual.start, actual.end, actual.title, actual.description, actual.location) == (
        individual.start, individual.end, individual.title, individual.description, individual.location)
    imported = parse_ics(export_ics([edited]), 'local')[0]
    actual = next(value for value in instances(imported) if value.recurrence_id == key)
    assert (actual.start, actual.end, actual.title, actual.description, actual.location) == (
        individual.start, individual.end, individual.title, individual.description, individual.location)


def test_stale_synced_annotation_cannot_hide_or_replace_canonical_exclusions(editors):
    create, _ = editors
    source = source_for(schedule_expression='every(1d)', expression_anchor=START.date().isoformat(), schedule_syntax=2,
                        excluded_dates=(recurrence_key(START + timedelta(weeks=1)),))
    dialog = create(source)
    dialog.title.setText('A title-only change')
    assert ranges(edited_master(dialog)) == ranges(source)
    assert dialog._enter_custom(restore=False), dialog.error.text()
    edited = edited_master(dialog)
    assert ranges(edited) == ranges(source)
    assert_complete_expression(edited)


def test_valid_text_insertion_keeps_cursor_and_selection(editors):
    create, _ = editors
    dialog = create()
    dialog.pattern.setText('+2 +3 every(1w)')
    dialog.pattern.setFocus()
    dialog.pattern.setCursorPosition(2)
    QTest.keyClicks(dialog.pattern, 'w')
    assert dialog.pattern.text() == '+2w +3 every(1w)'
    assert dialog.pattern.cursorPosition() == 3
    dialog.pattern.setSelection(4, 2)
    QTest.keyClicks(dialog.pattern, '+4')
    assert dialog.pattern.text() == '+2w +4 every(1w)'
    assert dialog.pattern.cursorPosition() == 6
    assert not dialog.pattern.selectedText()
    QTest.keyClick(dialog.pattern, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert dialog.pattern.text() == '+2w +3 every(1w)'
    QTest.keyClick(dialog.pattern, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert dialog.pattern.text() == '+2w +4 every(1w)'


def test_invalid_pending_text_never_saves_a_previous_or_partially_converted_schedule(editors):
    create, _ = editors
    dialog = create(source_for())
    dialog.toggle_date((START + timedelta(weeks=1)).date())
    before = edited_master(dialog)
    previous_text = dialog.pattern.text()
    dialog.pattern.setFocus()
    dialog.pattern.setText('every(1w')
    for _ in range(3):
        dialog._update_preview()
        with pytest.raises(ValueError):
            dialog.build_events()
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Rejected and not dialog.result_events
    dialog.pattern.setText(previous_text)
    assert ranges(edited_master(dialog)) == ranges(before)
    assert_complete_expression(edited_master(dialog))


def test_all_day_custom_authoring_across_the_dateline_keeps_calendar_dates(editors):
    create, _ = editors
    zone = ZoneInfo('Pacific/Kiritimati')
    source = source_for(start=datetime(2026, 11, 4, tzinfo=zone), all_day=True)
    removed = {(source.start + timedelta(weeks=value)).date() for value in (1, 2)}
    expected = [(value.start.date(), value.end.date()) for value in instances(source)
                if value.start.date() not in removed]
    display = ZoneInfo('Etc/GMT+12')
    with patch('debacal.event_dialog.local_zone', return_value=display), \
         patch('debacal.series_preview.local_zone', return_value=display):
        dialog = create(source)
        for day in sorted(removed):
            dialog.toggle_date(day)
        edited = edited_master(dialog)
        assert [(value.start.date(), value.end.date()) for value in instances(edited)] == expected
        assert_complete_expression(edited)
        dialog.pattern.setFocus()
        dialog.pattern.setText(' ' + dialog.pattern.text())
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
        stored = dialog._project_preview(dialog.result_events[0])
        imported = parse_ics(export_ics([stored]), 'local')[0]
        assert [(value.start.date(), value.end.date()) for value in instances(imported)] == expected


@pytest.mark.parametrize('through_dialog', [False, True])
def test_moving_a_source_with_stale_tail_text_preserves_its_canonical_weekly_schedule(editors, through_dialog):
    create, _ = editors
    source = source_for(schedule_expression='every(1d)', expression_anchor=START.date().isoformat(), schedule_syntax=2,
                        excluded_dates=(recurrence_key(START + timedelta(weeks=1)),))
    delta = timedelta(days=2)
    if through_dialog:
        dialog = create(source)
        dialog.start.set_value(START + delta)
        edited = edited_master(dialog)
        dialog.save()
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    else:
        draft = ScheduleDraft.from_event(source, authoring_zone=ZONE)
        fields = draft.fields(START + delta, source.end + delta)
        edited = replace(source, start=START + delta, end=source.end + delta, **fields)
    assert 'FREQ=WEEKLY' in edited.recurrence_rule
    assert [value.start for value in instances(edited)] == [value.start + delta for value in instances(source)]
    assert recurrence_key(START + timedelta(weeks=1) + delta) in edited.excluded_dates
    assert ranges(parse_ics(export_ics([edited]), 'local')[0]) == ranges(edited)
