"""The event editor explains short-month choices without changing imports."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.event_dialog import EventDialog
from debacal.models import Calendar, Event
from debacal.patterns import compile_sequence
from debacal.recurrence import expand_event


UTC = timezone.utc
JANUARY = datetime(2027, 1, 28, tzinfo=UTC)


@pytest.fixture
def editors():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('debacal.event_dialog.local_zone', return_value=UTC), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def create(start=JANUARY, end=None, *, all_day=True, source=None):
            dialog = EventDialog([Calendar('local', 'Personal', '#587c67')],
                source.start if source else start, source.end if source else end or start + timedelta(days=2),
                event=source, series=source, edit_scope='group')
            owned.append(dialog)
            dialog.title.setText('Appointment')
            if source is None:
                dialog.all_day.setChecked(all_day)
            return dialog
        yield create
        for dialog in reversed(owned):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def monthly(dialog, count=3):
    dialog.schedule_mode.setCurrentData('repeat')
    dialog.repeat.setCurrentData('MONTHLY')
    dialog.repeat_count.setValue(count)


def test_editor_policy_change_is_saved_through_selection_and_restored_by_undo(editors):
    from dataclasses import replace
    from debacal.schedule_authoring import expression_event
    from debacal.store import Store
    from debacal.ics import export_ics, parse_ics
    template = Event('policy-event', 'local', 'Two days', JANUARY, JANUARY + timedelta(days=2), all_day=True)
    text = 'every(1m, overflow=skip, span=2, count=3)'
    master = replace(expression_event(template, text), schedule_expression=text,
                     expression_anchor=JANUARY.date().isoformat(), schedule_syntax=2)
    with Store(':memory:') as store:
        store.apply('Create', [master])
        dialog = editors(source=master)
        dialog.overflow_policy.setCurrentData('cut')
        edited = dialog.build_events()[0]
        assert edited.range_policy == 'cut'
        operation = store.edit_selection([master], 'group', dialog.edit_reference, edited,
                                         replace_schedule=dialog.schedule_replacement)
        saved = store.get_event(master.id)
        assert saved.range_policy == 'cut'
        loaded, = parse_ics(export_ics([saved]), 'local')
        february = expand_event(loaded, datetime(2027, 2, 1, tzinfo=UTC), datetime(2027, 3, 1, tzinfo=UTC))
        assert len(february) == 1 and (february[0].end.date() - february[0].start.date()).days == 1
        store.undo(operation)
        assert store.get_event(master.id) == master
        store.redo(operation)
        assert store.get_event(master.id).range_policy == 'cut'


def ranges(dialog):
    source = dialog.build_events()[0]
    return [(item.start.date().isoformat(), item.end.date().isoformat()) for item in
            expand_event(source, datetime(2027, 1, 1, tzinfo=UTC), datetime(2027, 8, 1, tzinfo=UTC))]


def test_multi_day_range_offers_three_icon_choices_and_defaults_to_skip(editors):
    dialog = editors()
    monthly(dialog)
    choices = dialog.overflow_policy
    assert not choices.isHidden()
    assert choices.visibleCount() == 3
    assert choices.currentData() == 'skip'
    assert choices.width() == choices.height() * 3
    assert all(not button.text() and not button.icon().isNull() for button in choices.buttons.values())
    assert all(button.toolTip().startswith(caption) for caption, button in
               [('Skip', choices.buttons['skip']), ('Shift', choices.buttons['shift']), ('Cut', choices.buttons['cut'])])
    assert ranges(dialog) == [('2027-01-28', '2027-01-30'), ('2027-03-28', '2027-03-30'), ('2027-04-28', '2027-04-30')]
    assert dialog.repeat_until.value().date().isoformat() == '2027-04-28'


@pytest.mark.parametrize('kind,start,count', [
    ('MONTHLY', datetime(2024, 1, 30, tzinfo=ZoneInfo('Europe/Berlin')), 3),
    ('YEARLY', datetime(2023, 3, 30, tzinfo=ZoneInfo('Europe/Berlin')), 2),
])
def test_elapsed_range_disables_unsafe_policies_but_keeps_cut_available(editors, kind, start, count):
    with patch('debacal.event_dialog.local_zone', return_value=start.tzinfo):
        dialog = editors(start, start + timedelta(hours=48), all_day=False)
        monthly(dialog, count)
        dialog.repeat.setCurrentData(kind)
        choices = dialog.overflow_policy
        assert not choices.isHidden()
        for policy in ('skip', 'shift'):
            button = choices.buttons[policy]
            assert not button.isEnabled()
            assert 'month' in button.toolTip() and '2d' in button.toolTip() and 'Cut' in button.toolTip()
            previous = choices.currentData()
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert choices.currentData() == previous
        assert choices.buttons['cut'].isEnabled()
        if kind == 'YEARLY':
            assert 'Clock changes' in choices.buttons['cut'].toolTip()
        choices.buttons['cut'].click()
        assert not dialog.error.text()
        assert dialog.build_events()[0].range_policy == 'cut'


def test_calendar_day_duration_reenables_safe_policies_and_restores_their_tooltips(editors):
    start = datetime(2024, 1, 30, tzinfo=ZoneInfo('Europe/Berlin'))
    with patch('debacal.event_dialog.local_zone', return_value=start.tzinfo):
        dialog = editors(start, start + timedelta(hours=48), all_day=False)
        monthly(dialog)
        assert not dialog.overflow_policy.buttons['skip'].isEnabled()
        dialog.duration.setText('2d')
        dialog._duration_changed()
        for policy in ('skip', 'shift'):
            button = dialog.overflow_policy.buttons[policy]
            assert button.isEnabled()
            assert 'Use a calendar-day duration' not in button.toolTip()
        saved, = dialog.build_events()
        assert saved.duration_spec == 'P2D'
        dialog.duration.setText('48h')
        dialog._duration_changed()
        assert not dialog.overflow_policy.buttons['skip'].isEnabled()
        assert not dialog.overflow_policy.buttons['shift'].isEnabled()


def test_policy_safety_tracks_each_choices_count_and_the_active_until_bound(editors):
    start = datetime(2024, 1, 30, tzinfo=ZoneInfo('Europe/Berlin'))
    with patch('debacal.event_dialog.local_zone', return_value=start.tzinfo):
        dialog = editors(start, start + timedelta(hours=48), all_day=False)
        monthly(dialog, 1)
        skip, shift = (dialog.overflow_policy.buttons[policy] for policy in ('skip', 'shift'))
        assert skip.isEnabled() and shift.isEnabled()
        dialog.repeat_count.setValue(2)
        # Skip reaches March as its second occurrence. Shift's second is still
        # in February, before the March clock change.
        assert not skip.isEnabled() and shift.isEnabled()
        dialog.repeat_count.setValue(3)
        assert not skip.isEnabled() and not shift.isEnabled()
        dialog.repeat_until.set_value(datetime(2024, 3, 29, tzinfo=start.tzinfo))
        assert skip.isEnabled() and shift.isEnabled()
        dialog.no_end.setChecked(True)
        assert not skip.isEnabled() and not shift.isEnabled()


@pytest.mark.parametrize('policy, february', [('shift', ('2027-02-27', '2027-03-01')),
                                           ('cut', ('2027-02-28', '2027-03-01'))])
def test_28_to_29_policy_choices_preview_shifted_or_cut_february(editors, policy, february):
    dialog = editors()
    monthly(dialog)
    dialog.overflow_policy.setCurrentData(policy)
    assert not dialog.error.text()
    assert ranges(dialog) == [('2027-01-28', '2027-01-30'), february, ('2027-03-28', '2027-03-30')]
    assert dialog.repeat_until.value().date().isoformat() == '2027-03-28'
    assert dialog.build_events()[0].range_policy == ('cut' if policy == 'cut' else '')


def test_single_day_shows_only_skip_and_shift_without_a_blank_cut_segment(editors):
    start = datetime(2027, 1, 31, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=1))
    monthly(dialog)
    choices = dialog.overflow_policy
    assert not choices.isHidden()
    assert choices.visibleCount() == 2 and choices.buttons['cut'].isHidden()
    assert choices.width() == choices.height() * 2
    choices.setCurrentData('shift')
    assert ranges(dialog)[1] == ('2027-02-28', '2027-03-01')


def test_tooltips_follow_changes_between_single_day_and_full_range_policies(editors):
    start = datetime(2027, 1, 29, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=1))
    monthly(dialog)
    choices = dialog.overflow_policy

    def assert_one_day_tooltips():
        assert 'Not every month' in choices.buttons['skip'].toolTip()
        shift = choices.buttons['shift'].toolTip()
        assert 'last day' in shift
        assert 'duration' not in shift
        assert 'full date range' not in shift

    assert_one_day_tooltips()
    dialog.duration.setText('2d')
    dialog._duration_changed()
    assert 'full date range' in choices.buttons['skip'].toolTip()
    assert 'keeping its duration' in choices.buttons['shift'].toolTip()
    assert 'Skip months without the starting date' in choices.buttons['cut'].toolTip()
    dialog.duration.setText('1d')
    dialog._duration_changed()
    assert_one_day_tooltips()


def test_leap_day_tooltips_explain_missing_yearly_date(editors):
    start = datetime(2028, 2, 29, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=1))
    monthly(dialog)
    dialog.repeat.setCurrentData('YEARLY')
    choices = dialog.overflow_policy
    assert not choices.isHidden()
    for policy in ('skip', 'shift'):
        tooltip = choices.buttons[policy].toolTip()
        assert 'date does not exist every year' in tooltip
        assert 'duration' not in tooltip


def test_intentional_cross_month_tooltips_describe_only_missing_start_dates(editors):
    start = datetime(2027, 1, 31, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=2))
    monthly(dialog)
    choices = dialog.overflow_policy
    assert not choices.isHidden()
    assert choices.buttons['cut'].isHidden()
    assert 'chosen starting day' in choices.buttons['skip'].toolTip()
    shift = choices.buttons['shift'].toolTip()
    assert 'Move the start to the last day' in shift
    assert 'keeping its duration' in shift
    assert 'full date range' not in shift


def test_cut_skips_months_without_the_starting_day(editors):
    start = datetime(2027, 1, 30, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=2))
    monthly(dialog)
    dialog.overflow_policy.setCurrentData('cut')
    assert ranges(dialog) == [('2027-01-30', '2027-02-01'),
                              ('2027-03-30', '2027-04-01'),
                              ('2027-04-30', '2027-05-01')]


def test_saved_cut_rule_can_be_explicitly_updated_without_silently_changing_it(editors):
    start = datetime(2027, 1, 30, tzinfo=UTC)
    source = Event('saved-cut', 'local', 'Month end', start, start + timedelta(days=2), all_day=True,
        recurrence_rule='FREQ=MONTHLY;INTERVAL=1;BYMONTHDAY=28,29,30;BYSETPOS=-1;COUNT=3',
        range_policy='cut', schedule_expression='every(1m, overflow=cut, span=2, count=3)',
        expression_anchor=start.date().isoformat(), schedule_syntax=2)
    dialog = editors(source=source)
    assert dialog.build_events()[0].recurrence_rule == source.recurrence_rule
    monthly(dialog)
    assert not dialog.overflow_policy.isHidden()
    dialog.overflow_policy.setCurrentData('cut')
    assert ranges(dialog) == [('2027-01-30', '2027-02-01'),
                              ('2027-03-30', '2027-04-01'),
                              ('2027-04-30', '2027-05-01')]


def test_no_overflow_hides_the_whole_group(editors):
    start = datetime(2027, 1, 12, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=2))
    monthly(dialog)
    assert dialog.overflow_policy.isHidden()


def test_imported_fixed_duration_rule_has_no_policy_selected_and_stays_unchanged(editors):
    source = Event('imported', 'local', 'Imported', JANUARY, JANUARY + timedelta(days=2), all_day=True,
                   recurrence_rule='FREQ=MONTHLY;COUNT=3')
    dialog = editors(source=source)
    assert not dialog.overflow_policy.isHidden()
    assert dialog.overflow_policy.visibleCount() == 3
    assert dialog.overflow_policy.currentData() is None
    unchanged = dialog.build_events()[0]
    assert unchanged.recurrence_rule == source.recurrence_rule
    assert unchanged.range_policy == source.range_policy == ''
    assert ranges(dialog)[1] == ('2027-02-28', '2027-03-02')
    dialog.repeat_count.setValue(4)
    assert dialog.overflow_policy.currentData() is None
    assert ranges(dialog)[1] == ('2027-02-28', '2027-03-02')
    dialog.overflow_policy.setCurrentData('cut')
    assert ranges(dialog)[1] == ('2027-02-28', '2027-03-01')


def test_reference_range_edits_update_policy_span_and_choices(editors):
    dialog = editors()
    monthly(dialog)
    dialog.overflow_policy.setCurrentData('cut')
    dialog.duration.setText('1d')
    dialog._duration_changed()
    assert dialog.overflow_policy.isHidden()  # Every month has a 28th.
    dialog.duration.setText('2d')
    dialog._duration_changed()
    assert not dialog.overflow_policy.isHidden()
    assert dialog.overflow_policy.visibleCount() == 3
    assert ranges(dialog)[1] == ('2027-02-28', '2027-03-01')


def test_persisted_cut_keeps_three_choices_when_its_new_reference_overflows(editors):
    start = datetime(2027, 2, 28, tzinfo=UTC)
    text = 'every(1m, overflow=cut, span=2, count=3)'
    source = Event('cut', 'local', 'Cut series', start, start + timedelta(days=2), all_day=True,
                   schedule_expression=text, expression_anchor=start.date().isoformat(), schedule_syntax=2,
                   **compile_sequence(text, start, all_day=True).fields)
    dialog = editors(source=source)
    assert not dialog.overflow_policy.isHidden()
    assert dialog.overflow_policy.visibleCount() == 3
    assert dialog.overflow_policy.currentData() == 'cut'


def test_shift_is_disabled_when_a_whole_range_cannot_fit_every_month(editors):
    start = datetime(2027, 1, 1, tzinfo=UTC)
    dialog = editors(start, datetime(2027, 2, 1, tzinfo=UTC))
    monthly(dialog)
    shift = dialog.overflow_policy.buttons['shift']
    assert not dialog.overflow_policy.isHidden()
    assert dialog.overflow_policy.visibleCount() == 3
    assert not shift.isEnabled()
    assert 'cannot fit' in shift.toolTip()
    QTest.mouseClick(shift, Qt.MouseButton.LeftButton)
    assert dialog.overflow_policy.currentData() == 'skip'


def test_policy_choice_cannot_replace_pending_invalid_custom_text(editors):
    dialog = editors()
    monthly(dialog)
    dialog.repeat.setCurrentData('RDATE')
    dialog.pattern.setText('every(1m, overflow=')
    pending = dialog.pattern.text()
    dialog.overflow_policy.setCurrentData('cut')
    assert dialog.error.text()
    assert dialog.pattern.text() == pending


def test_mixed_custom_multi_day_program_does_not_offer_a_misleading_whole_range_policy(editors):
    dialog = editors()
    dialog.pattern.setText('+0d +1m +2w')
    assert dialog.overflow_policy.isHidden()


def test_single_day_mixed_custom_program_keeps_skip_and_shift(editors):
    start = datetime(2027, 1, 31, tzinfo=UTC)
    dialog = editors(start, start + timedelta(days=1))
    dialog.pattern.setText('+0d +1m +2w')
    assert not dialog.overflow_policy.isHidden()
    assert dialog.overflow_policy.visibleCount() == 2
    assert dialog.overflow_policy.buttons['cut'].isHidden()
    dialog.overflow_policy.setCurrentData('shift')
    assert dialog.overflow_policy.currentData() == 'shift'
    assert not dialog.error.text()


def test_unannotated_typed_multi_day_program_keeps_its_meaning_until_policy_is_chosen(editors):
    dialog = editors()
    text = '+0d +1m'
    dialog.pattern.setText(text)
    assert not dialog.overflow_policy.isHidden()
    assert dialog.overflow_policy.visibleCount() == 3
    assert dialog.overflow_policy.currentData() is None
    assert dialog.pattern.text() == text
    assert ranges(dialog)[1] == ('2027-02-28', '2027-03-02')
    dialog.overflow_policy.setCurrentData('skip')
    assert dialog.overflow_policy.currentData() == 'skip'
    assert ranges(dialog)[1] == ('2027-03-28', '2027-03-30')
