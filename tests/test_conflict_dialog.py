"""Conflict previews stay aligned and only explicit choices reach the engine."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from queue import Queue
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.conflict_dialog import ConflictDialog
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.preferences_dialog import PreferencesDialog
from debacal.store import Store
from debacal.sync import SyncConflict
from debacal.ui import MainWindow
from debacal.theme import scheme_for
from debacal.event_style import contrast_ratio


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


def make_conflict():
    start = datetime(2026, 11, 30, 10, tzinfo=ZoneInfo('Europe/Berlin'))
    local = Event('example', 'local', 'Local title', start, start + timedelta(hours=1),
                  recurrence_rule='FREQ=WEEKLY;COUNT=4')
    remote = replace(local, title='Server title', start=start + timedelta(days=1),
                     end=local.end + timedelta(days=1), location='Server room')
    return SyncConflict('conflict', 'local', local.id, local, remote, 'Changed on both sides')


@pytest.mark.parametrize('mode', ['light', 'dark'])
def test_calendar_comparison_aligns_months_and_expands_each_version(app, mode):
    conflict = make_conflict()
    dialog = ConflictDialog(conflict, Preferences(0, 'iso', '24h', appearance_mode=mode))
    try:
        dialog.show()
        app.processEvents()
        colors = scheme_for(dialog.preferences)
        body = dialog.content_scroll.widget()
        assert body.palette().color(QPalette.ColorRole.Window).name() == colors.background
        assert dialog.content_scroll.viewport().palette().color(QPalette.ColorRole.Window).name() == colors.background
        assert body.grab().toImage().pixelColor(1, 1).name() == colors.background
        assert dialog.local_preview.current_month == dialog.remote_preview.current_month == date(2026, 11, 1)
        assert dialog.local_preview.events_on(date(2026, 11, 30))
        assert dialog.remote_preview.events_on(date(2026, 12, 1))
        row = dialog.fields.index('Title')
        assert dialog.comparison.item(row, 1).text() == 'Local title'
        assert dialog.comparison.item(row, 2).text() == 'Server title'
        changed = dialog.comparison.item(row, 1)
        assert changed.background().color().name() == scheme_for(dialog.preferences).notice_background
        assert contrast_ratio(changed.foreground().color().name(), changed.background().color().name()) >= 4.5
        QTest.mouseClick(dialog.remote_date_button, Qt.MouseButton.LeftButton)
        assert dialog.local_preview.current_month == dialog.remote_preview.current_month == date(2026, 12, 1)
        QTest.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)
        assert dialog.local_preview.current_month == dialog.remote_preview.current_month == date(2027, 1, 1)
        assert not dialog.grab().isNull()
        assert dialog.choice is None
        assert conflict == make_conflict()  # Viewing and navigating never mutate snapshots.
    finally:
        dialog.close()
        dialog.deleteLater()


def test_deleted_version_and_postpone_do_not_choose_automatically(app):
    conflict = replace(make_conflict(), remote=None)
    dialog = ConflictDialog(conflict, Preferences())
    try:
        assert not dialog.remote_date_button.isEnabled()
        assert dialog.remote_preview.masters == []
        assert dialog.remote_button.text() == 'Keep server deletion'
        dialog.later_button.click()
        assert dialog.result() == QDialog.DialogCode.Rejected
        assert dialog.choice is None
    finally:
        dialog.close()
        dialog.deleteLater()


def test_equal_endpoints_still_explain_duration_and_timezone_conflicts(app):
    start = datetime(2026, 1, 15, 9, tzinfo=ZoneInfo('Europe/Berlin'))
    nominal = Event('example', 'local', 'Meeting', start, start + timedelta(days=1),
                    recurrence_rule='FREQ=DAILY', duration_spec='P1D')
    exact = replace(nominal, duration_spec='')
    dialog = ConflictDialog(replace(make_conflict(), local=nominal, remote=exact), Preferences())
    try:
        row = dialog.fields.index('Duration')
        assert dialog.comparison.item(row, 1).text() == '1d (calendar)'
        assert dialog.comparison.item(row, 2).text() == '24h (elapsed)'
        assert dialog.comparison.item(row, 1).background().color().name() == scheme_for(dialog.preferences).notice_background
        assert dialog.describe(nominal)['Start'] == dialog.describe(exact)['Start']
        assert dialog.describe(nominal)['End'] == dialog.describe(exact)['End']
        utc = replace(exact, start=start.astimezone(timezone.utc), end=exact.end.astimezone(timezone.utc), timezone_id='')
        assert dialog.describe(nominal)['Timezone'] == 'Europe/Berlin'
        assert dialog.describe(utc)['Timezone'] == 'UTC'
    finally:
        dialog.close()
        dialog.deleteLater()


def test_all_day_conflicts_keep_calendar_dates_and_chosen_date_language(app):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    local = Event('all-day', 'local', 'November date', start, start + timedelta(days=1), all_day=True)
    remote = replace(local, start=start.replace(month=12), end=start.replace(month=12, day=2))
    conflict = replace(make_conflict(), local=local, remote=remote)
    preferences = Preferences(locale_name='de_DE', date_format='european')
    with patch('debacal.conflict_dialog.local_zone', return_value=ZoneInfo('America/New_York')):
        dialog = ConflictDialog(conflict, preferences)
        try:
            dialog.show()
            app.processEvents()
            assert dialog.current_month == date(2026, 11, 1)
            assert dialog.describe(local)['Start'] == preferences.format_date(start.date(), weekday=True)
            dialog.remote_date_button.click()
            assert dialog.current_month == date(2026, 12, 1)
            assert dialog.month_label.text() == preferences.format_month(date(2026, 12, 1))
        finally:
            dialog.close()
            dialog.deleteLater()


def test_conflict_decisions_stay_reachable_on_a_small_window_with_large_text(app):
    dialog = ConflictDialog(make_conflict(), Preferences(font_scale=140, appearance_mode='dark'))
    try:
        dialog.resize(800, 520)
        dialog.show()
        app.processEvents()
        assert dialog.height() <= 520
        assert dialog.content_scroll.verticalScrollBar().maximum() > 0
        for button in (dialog.local_button, dialog.remote_button, dialog.later_button):
            assert button.isVisible()
            assert dialog.rect().contains(QRect(button.mapTo(dialog, QPoint()), button.size()))
        dialog.content_scroll.verticalScrollBar().setValue(dialog.content_scroll.verticalScrollBar().maximum())
        app.processEvents()
        assert not dialog.grab().isNull()
        dialog.later_button.click()
        assert dialog.choice is None and dialog.result() == QDialog.DialogCode.Rejected
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize('choice', ['local', 'remote', None])
def test_window_submits_exact_displayed_snapshot_only_on_choice(app, choice):
    with Store(':memory:') as store:
        window = MainWindow(store, enable_sync=False)
        conflict = make_conflict()
        engine = Mock(conflicts=[conflict])
        window.engine = engine
        def choose(dialog):
            if choice:
                dialog.choose(choice)
            else:
                dialog.reject()
            return dialog.result()
        try:
            with patch.object(ConflictDialog, 'exec', choose):
                window.show_conflicts()
            if choice:
                engine.resolve_conflict.assert_called_once_with(conflict.id, choice, expected=conflict)
            else:
                engine.resolve_conflict.assert_not_called()
        finally:
            window.close()
            window.deleteLater()


def test_automatic_sync_can_be_paused_and_manual_sync_still_works(app):
    with Store(':memory:') as store:
        store.save_calendar(Calendar('remote', 'Remote', '#123456', kind='caldav', url='https://example.test/cal/'))
        window = MainWindow(store, enable_sync=False)
        engine = Mock(conflicts=[], events=Queue())
        window.engine = engine
        try:
            window.configure_auto_sync()
            assert window.sync_timer.isActive()
            assert window.sync_timer.interval() == 5 * 60_000
            window.startup_sync()
            engine.sync_now.assert_called_once()
            engine.reset_mock()
            window.preferences = replace(window.preferences, auto_sync_enabled=False, sync_interval_minutes=7)
            window.configure_auto_sync()
            assert not window.sync_timer.isActive()
            window.startup_sync()
            engine.sync_now.assert_not_called()
            window.sync_now()
            engine.sync_now.assert_called_once()
            window.preferences = replace(window.preferences, auto_sync_enabled=True)
            window.configure_auto_sync()
            assert window.sync_timer.interval() == 420_000
            window.close()
            window.startup_sync()
            assert not window.sync_timer.isActive()
            assert engine.sync_now.call_count == 1
        finally:
            window.close()
            window.deleteLater()


def test_automatic_sync_preferences_persist_and_validate(app, tmp_path):
    preferences = Preferences(auto_sync_enabled=False, sync_interval_minutes=15)
    path = tmp_path / 'calendar.sqlite3'
    with Store(path) as store:
        preferences.save(store)
    with Store(path) as store:
        assert Preferences.load(store) == preferences
    dialog = PreferencesDialog(preferences)
    try:
        assert not dialog.sync_interval.isEnabled()
        dialog.auto_sync.setChecked(True)
        assert dialog.sync_interval.isEnabled()
        dialog.sync_interval.setValue(5)
        dialog.save()
        assert dialog.result_preferences.auto_sync_enabled
        assert dialog.result_preferences.sync_interval_minutes == 5
        dialog.restore_defaults()
        assert dialog.value() == Preferences()
    finally:
        dialog.close()
        dialog.deleteLater()
    for invalid in (0, -1, 1441, True, '5'):
        with pytest.raises(ValueError):
            Preferences(sync_interval_minutes=invalid)
    assert Preferences.from_dict({'auto_sync_enabled': 'yes', 'sync_interval_minutes': 0}) == Preferences()
