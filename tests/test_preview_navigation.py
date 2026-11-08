"""The production preview navigates real events without owning editor state."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal.appearance import stylesheet
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preview_navigation import PreviewNavigation
from debacal.recurrence import occurrence, recurrence_key
from debacal.series_preview import SeriesPreview


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


def master(**values):
    return Event('root', 'local', 'Project conversation', START, START + timedelta(minutes=45), **values)


def settle(app):
    for _ in range(6):
        app.processEvents()


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr('debacal.series_preview.local_zone', lambda: ZONE)
    app = QApplication.instance() or QApplication([])
    owned = []
    def make(events):
        widget = SeriesPreview()
        owned.append(widget)
        preferences = Preferences(start_weekday=0, locale_name='en_GB', time_format='24h')
        widget.setStyleSheet(stylesheet(100, preferences))
        widget.set_events(events, preferences, [Calendar('local', 'Local', '#719689')])
        widget.resize(600, 620)
        widget.show()
        settle(app)
        return widget
    yield app, make
    for widget in owned:
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_last_then_previous_and_first_respect_exclusions_and_moved_exceptions(desktop):
    app, make = desktop
    source = master(recurrence_rule='FREQ=DAILY;COUNT=5')
    nominal = occurrence(source, START + timedelta(days=2))
    moved = replace(nominal, start=START + timedelta(days=20), end=START + timedelta(days=20, minutes=45))
    source = replace(source, excluded_dates=(recurrence_key(START + timedelta(days=4)),),
                     recurrence_overrides={nominal.recurrence_id: moved.to_dict()})
    preview = make([source])
    selected = QSignalSpy(preview.occurrence_selected)
    assert preview.last_button.isEnabled()
    assert preview.position_label.text().startswith('1/4')
    preview.last_button.click()
    settle(app)
    assert preview.selected_event.start == moved.start and selected.count() == 1
    assert preview.position_label.text().startswith('4/4')
    preview.previous_occurrence_button.click()
    assert preview.selected_event.start == START + timedelta(days=3)
    assert preview.position_label.text().startswith('3/4')
    preview.first_button.click()
    assert preview.selected_event.start == START
    assert preview.masters == [source]


def test_primary_jump_is_independent_of_chronological_first(desktop):
    _, make = desktop
    source = master(recurrence_dates=(recurrence_key(START - timedelta(days=2)), recurrence_key(START + timedelta(days=2))))
    preview = make([source])
    preview.previous_occurrence_button.click()
    assert preview.selected_event.start == START - timedelta(days=2)
    preview.first_button.click()
    assert preview.selected_event.start == START and preview.is_primary(preview.selected_event)


def test_unbounded_preview_expands_visible_month_only_and_disables_last(desktop):
    _, make = desktop
    source = master(recurrence_rule='FREQ=DAILY')
    with patch.object(PreviewNavigation, 'finite_events', side_effect=AssertionError('Do not enumerate an infinite schedule')):
        preview = make([source])
        assert len(preview.visible_events) == 42 - (START.date() - preview.grid_start).days
        assert not preview.last_button.isEnabled()
        assert 'no last occurrence' in preview.last_button.toolTip()
        assert preview.position_label.text().startswith('1/∞')
        preview.next_occurrence_button.click()
        assert preview.selected_event.start == START + timedelta(days=1)
        assert preview.position_label.text().startswith('2/∞')


@pytest.mark.parametrize('rule', ['', 'FREQ=DAILY;COUNT=1'])
def test_empty_schedule_never_selects_an_excluded_reference(desktop, rule):
    _, make = desktop
    source = master(recurrence_rule=rule, excluded_dates=(recurrence_key(START),))
    preview = make([source])
    assert preview.selected_event is None and preview.selected_id is None
    assert preview.total_occurrences == 0 and preview.visible_events == []
    assert not preview.previous_occurrence_button.isEnabled()
    assert not preview.next_occurrence_button.isEnabled()
    assert not preview.last_button.isEnabled()
    preview.show_month((START + timedelta(days=80)).date())
    preview.first_button.click()
    assert preview.current_month == START.date().replace(day=1)
    assert preview.selected_event is None and preview.total_occurrences == 0


def test_excluded_reference_selects_first_actual_and_navigates_without_history_scan(desktop):
    _, make = desktop
    source = master(recurrence_rule='FREQ=DAILY', excluded_dates=(recurrence_key(START),))
    with patch.object(PreviewNavigation, 'finite_events', side_effect=AssertionError('No history scan')):
        preview = make([source])
        assert preview.selected_event.start == START + timedelta(days=1)
        assert preview.position_label.text().startswith('1/∞')
        assert not preview.is_primary(preview.selected_event)
        preview.next_occurrence_button.click()
        assert preview.selected_event.start == START + timedelta(days=2)
        preview.first_button.click()
        assert preview.current_month == START.date().replace(day=1)
        assert preview.selected_event.start != START


@pytest.mark.parametrize('count,expected_offset', [(1, 40), (3, 1)])
def test_moved_reference_selects_an_actual_range_not_its_former_start(desktop, count, expected_offset):
    _, make = desktop
    source = master(recurrence_rule=f'FREQ=DAILY;COUNT={count}')
    original = occurrence(source, START)
    moved = replace(original, start=START + timedelta(days=40), end=START + timedelta(days=40, minutes=45))
    source = replace(source, recurrence_overrides={original.recurrence_id: moved.to_dict()})
    preview = make([source])
    assert preview.selected_event.start == START + timedelta(days=expected_offset)
    assert preview._primary_occurrence() is None
    assert preview.total_occurrences == count
    preview.first_button.click()
    assert preview.current_month == START.date().replace(day=1)
    assert preview.selected_event.start != START


def test_first_actual_navigation_includes_dates_before_an_excluded_reference():
    earlier = START - timedelta(days=100)
    source = master(recurrence_rule='FREQ=DAILY;COUNT=100000',
                    recurrence_dates=(recurrence_key(earlier),), excluded_dates=(recurrence_key(START),))
    navigation = PreviewNavigation([source])
    with patch.object(navigation, 'finite_events', side_effect=AssertionError('No history scan')):
        assert navigation.first().start == earlier
        assert navigation.first().start == earlier


def test_today_browses_current_month_without_moving_the_primary_or_selection(desktop):
    _, make = desktop
    source = master(recurrence_rule='FREQ=DAILY;COUNT=5')
    preview = make([source])
    preview.next_occurrence_button.click()
    selected = preview.selected_event
    today = START.replace(year=2027, month=2, day=12)
    with patch('debacal.series_preview.now', return_value=today):
        preview.today_button.click()
    assert preview.current_month == today.date().replace(day=1)
    assert preview.selected_event == selected
    assert preview.primary == source


def test_occurrence_position_tracks_all_dates_without_a_second_footer(desktop):
    _, make = desktop
    preview = make([master(recurrence_rule='FREQ=DAILY;COUNT=5')])
    for _ in range(3):
        preview.next_occurrence_button.click()
    assert preview.position_label.text().startswith('4/5')
    assert preview.error_label.isHidden()


def test_position_cache_survives_title_and_duration_edits_without_rescanning_rules():
    source = master(recurrence_rule='FREQ=DAILY;COUNT=500')
    navigation = PreviewNavigation([source])
    assert navigation.position(occurrence(source, START)) == (1, 500)
    changed = replace(source, title='Renamed', end=source.end + timedelta(hours=1))
    navigation.rebind([changed])
    with patch('debacal.preview_navigation.recurrence_set', side_effect=AssertionError('Position cache was lost')):
        assert navigation.position(occurrence(changed, START + timedelta(days=3))) == (4, 500)


def test_position_does_not_enumerate_large_or_unbounded_rules():
    for rule in ('FREQ=DAILY', 'FREQ=DAILY;COUNT=100000'):
        source = master(recurrence_rule=rule)
        navigation = PreviewNavigation([source])
        with patch('debacal.preview_navigation.recurrence_set', side_effect=AssertionError('Do not scan history')):
            assert navigation.position(occurrence(source, START)) == (1, None)


def test_large_explicit_schedule_counts_dates_without_expanding_invisible_events(desktop):
    _, make = desktop
    source = master(recurrence_dates=tuple(recurrence_key(START + timedelta(days=index)) for index in range(1, 11000)))
    with patch.object(PreviewNavigation, 'finite_events', side_effect=AssertionError('No offscreen expansion')):
        preview = make([source])
        preview.last_button.click()
        assert preview.selected_event.start == START + timedelta(days=10999)
    assert preview.total_occurrences == 11000 and not preview.error
    assert len(preview.visible_events) < 43


def test_adjacent_navigation_keeps_distinct_events_at_the_same_start():
    first = master(recurrence_rule='FREQ=DAILY;COUNT=2')
    second = replace(first, id='second')
    first_occurrence, second_occurrence = occurrence(first, START), occurrence(second, START)
    navigation = PreviewNavigation([first, second])
    assert navigation.adjacent(first_occurrence, 1).id == second_occurrence.id
    assert navigation.adjacent(second_occurrence, -1).id == first_occurrence.id


def test_selection_changes_only_card_border_and_keeps_fill_and_text(desktop):
    app, make = desktop
    first = master()
    second = replace(first, id='second', start=START + timedelta(days=1), end=START + timedelta(days=1, minutes=45))
    preview = make([first, second])
    rect = preview.grid.chip_rects(second.start.date())[0][0]
    before = preview.grid.grab().toImage()
    preview.set_selected_occurrence(second)
    settle(app)
    after = preview.grid.grab().toImage()
    inside = QRect(round(rect.left() + 3), round(rect.top() + 3), round(rect.width() - 6), round(rect.height() - 6))
    assert before.copy(inside) == after.copy(inside)
    assert before != after
    assert not preview.is_primary(second) and preview.is_primary(first)


def test_crowded_day_keeps_every_direct_remove_target_with_stable_columns(desktop):
    app, make = desktop
    source = master()
    preview = make([source])
    preview.enable_date_editing(True)
    width = preview.grid.width()
    extras = [replace(source, id=f'extra-{index}', start=START + timedelta(minutes=30 * index),
                      end=START + timedelta(minutes=30 * index + 45)) for index in range(1, 21)]
    preview.set_events([source, *extras])
    settle(app)
    chips = preview.grid.chip_rects(START.date())
    assert len(chips) == 21 and preview.grid.width() == width
    assert preview.scroll.verticalScrollBar().maximum() > 0
    assert all(not remove.isEmpty() for _rect, _event, remove in chips)
    removed = QSignalSpy(preview.occurrence_remove_requested)
    target, event, remove = chips[-1]
    preview.scroll.ensureVisible(round(target.center().x()), round(target.center().y()), 0, 20)
    QTest.mouseClick(preview.grid, Qt.MouseButton.LeftButton, pos=remove.center().toPoint())
    assert removed.count() == 1 and removed.at(0)[0].id == event.id
    assert preview.selected_id == source.id
    primary_rect, primary, primary_remove = chips[0]
    preview.scroll.ensureVisible(round(primary_rect.center().x()), round(primary_rect.center().y()), 0, 20)
    QTest.mouseClick(preview.grid, Qt.MouseButton.LeftButton, pos=primary_remove.center().toPoint())
    assert removed.count() == 2 and removed.at(1)[0].id == primary.id


def test_large_plain_count_last_does_not_iterate_history():
    source = master(recurrence_rule='FREQ=DAILY;COUNT=100000')
    navigator = PreviewNavigation([source])
    with patch.object(navigator, 'finite_events', side_effect=AssertionError('No history scan')):
        last = navigator.last()
    assert last.start == START + timedelta(days=99999)


def test_large_weekly_last_uses_calendar_zone_for_utc_stored_start():
    local = datetime(2026, 11, 2, 0, 30, tzinfo=ZONE)
    source = replace(master(recurrence_rule='FREQ=WEEKLY;BYDAY=MO;COUNT=20000'),
                     start=local.astimezone(ZoneInfo('UTC')),
                     end=(local + timedelta(minutes=45)).astimezone(ZoneInfo('UTC')),
                     timezone_id='Europe/Berlin')
    navigator = PreviewNavigation([source])
    with patch.object(navigator, 'finite_events', side_effect=AssertionError('No history scan')):
        last = navigator.last()
    assert last.start == local + timedelta(weeks=19999)


def test_large_until_last_seeks_to_boundary_and_includes_later_added_dates():
    last_rule_day = START + timedelta(days=25000)
    added = START + timedelta(days=25010)
    source = master(recurrence_rule=f'FREQ=DAILY;UNTIL={last_rule_day.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")}',
                    recurrence_dates=(recurrence_key(added),))
    navigator = PreviewNavigation([source])
    with patch.object(navigator, 'finite_events', side_effect=AssertionError('No history scan')):
        last = navigator.last()
    assert last.start == added


def test_primary_reference_can_keep_an_occurrence_id_without_pinning_the_wrong_date(desktop):
    _, make = desktop
    source = master(recurrence_rule='FREQ=DAILY;COUNT=4')
    preview = make([source])
    later = occurrence(source, START + timedelta(days=2))
    primary = replace(later, start=START, end=START + timedelta(minutes=45))
    preview.set_primary(primary)
    assert preview.is_primary(preview.events_on(START.date())[0])
    assert not preview.is_primary(preview.events_on(later.start.date())[0])
    preview.set_selected_occurrence(later)
    preview.first_button.click()
    assert preview.selected_event.start == START and preview.selected_event.id != later.id


def test_primary_reference_resolves_a_moved_exception_in_a_new_future_tail(desktop):
    _, make = desktop
    original = master(recurrence_rule='FREQ=DAILY;COUNT=4')
    selected = occurrence(original, START + timedelta(days=2))
    moved = replace(selected, start=selected.start + timedelta(hours=2), end=selected.end + timedelta(hours=2))
    tail = replace(original, id='new-tail', start=selected.start, end=selected.end,
                   recurrence_rule='FREQ=DAILY;COUNT=2', recurrence_overrides={selected.recurrence_id: moved.to_dict()})
    preview = make([tail])
    preview.set_primary(moved)
    assert preview.is_primary(preview.events_on(moved.start.date())[0])
    preview.first_button.click()
    assert preview.selected_event.start == moved.start and preview.selected_event.series_id == tail.id


def test_primary_pin_distinguishes_another_exception_moved_onto_the_primary_start(desktop):
    _, make = desktop
    source = master(recurrence_rule='FREQ=DAILY;COUNT=3')
    other = occurrence(source, START + timedelta(days=1))
    moved = replace(other, start=START, end=START + timedelta(minutes=45))
    source = replace(source, recurrence_overrides={other.recurrence_id: moved.to_dict()})
    preview = make([source])
    coincident = preview.events_on(START.date())
    assert len(coincident) == 2
    assert [event.recurrence_id for event in coincident if preview.is_primary(event)] == [recurrence_key(START)]
    preview.enable_date_editing(True)
    assert sum(not remove.isEmpty() for _rect, _event, remove in preview.grid.chip_rects(START.date())) == 2


def test_future_primary_pin_keeps_nominal_identity_when_another_exception_has_the_same_actual_start(desktop):
    _, make = desktop
    original = master(recurrence_rule='FREQ=DAILY;COUNT=5')
    selected = occurrence(original, START + timedelta(days=2))
    another = occurrence(original, START + timedelta(days=3))
    actual = START + timedelta(days=20)
    moved = replace(selected, start=actual, end=actual + timedelta(minutes=45))
    collision = replace(another, start=moved.start, end=moved.end)
    tail = replace(original, id='new-tail', start=selected.start, end=selected.end,
                   recurrence_rule='FREQ=DAILY;COUNT=3',
                   recurrence_overrides={selected.recurrence_id: moved.to_dict(), another.recurrence_id: collision.to_dict()})
    preview = make([tail])
    preview.set_primary(moved)
    preview.show_month(actual.date())
    coincident = preview.events_on(actual.date())
    assert len(coincident) == 2
    assert [event.recurrence_id for event in coincident if preview.is_primary(event)] == [selected.recurrence_id]
    preview.show_month((START + timedelta(days=60)).date())
    assert preview._primary_occurrence().recurrence_id == selected.recurrence_id
    preview.first_button.click()
    assert preview.selected_event.recurrence_id == selected.recurrence_id


def test_explicit_off_rule_primary_key_distinguishes_it_from_the_rule_anchor_at_the_same_clock(desktop):
    _, make = desktop
    original = master(recurrence_rule='FREQ=WEEKLY;COUNT=4')
    extra = occurrence(original, START + timedelta(days=2))
    actual = START + timedelta(days=7)
    moved = replace(extra, start=actual, end=actual + timedelta(minutes=45))
    tail = replace(original, start=actual, end=actual + timedelta(minutes=45),
                   recurrence_rule='FREQ=WEEKLY;COUNT=3', recurrence_dates=(extra.recurrence_id,),
                   recurrence_overrides={extra.recurrence_id: moved.to_dict()})
    preview = make([tail])
    preview.set_primary(moved)
    pins = [event for event in preview.events_on(actual.date()) if preview.is_primary(event)]
    assert len(pins) == 1 and pins[0].recurrence_id == extra.recurrence_id
    preview.first_button.click()
    assert preview.selected_event.recurrence_id == extra.recurrence_id


def test_editing_primary_updates_its_preview_selection_without_retargeting_browsed_occurrences(desktop):
    _, make = desktop
    source = master(recurrence_rule='FREQ=DAILY;COUNT=4')
    preview = make([source])
    preview.set_primary(source)
    moved = replace(source, start=source.start + timedelta(hours=1), end=source.end + timedelta(hours=1))
    preview.set_events([moved])
    preview.set_primary(moved)
    assert preview.selected_event.start == moved.start
    following = preview.events_on((START + timedelta(days=1)).date())[0]
    preview.set_selected_occurrence(following)
    preview.set_primary(moved)
    assert preview.selected_event.id == following.id and preview.primary.start == moved.start


def test_all_day_end_is_exclusive_and_overnight_labels_show_real_end(desktop):
    _, make = desktop
    all_day = replace(master(), all_day=True, start=START.replace(hour=0), end=START.replace(hour=0) + timedelta(days=2))
    preview = make([all_day])
    assert preview.events_on(date(2026, 11, 5))
    assert not preview.events_on(date(2026, 11, 6))
    overnight = replace(master(), start=START.replace(hour=21), end=(START + timedelta(days=1)).replace(hour=9))
    preview.set_events([overnight])
    from debacal import series_preview
    with patch.object(series_preview, 'text', wraps=series_preview.text) as draw:
        preview.grid.grab()
    captions = [call.args[2] for call in draw.call_args_list]
    assert '21:00 →' in captions and '→ 09:00' in captions


@pytest.mark.parametrize('name', ['month_label', 'position_label'])
def test_compact_preview_captions_elide_and_keep_the_full_tooltip(desktop, name):
    app, make = desktop
    preview = make([master(recurrence_rule='FREQ=WEEKLY;COUNT=12')])
    preview.resize(200, 360)
    settle(app)
    label = getattr(preview, name)
    full = label.text()
    assert full in label.toolTip()
    assert label.fontMetrics().horizontalAdvance(full) > label.contentsRect().width()
    with patch('debacal.compact_controls.QPainter') as painter:
        label.paintEvent(None)
    rendered = painter.return_value.drawText.call_args.args[-1]
    assert rendered.endswith('…') and rendered != full
    assert label.text() == full
