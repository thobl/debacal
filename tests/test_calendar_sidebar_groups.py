"""Overflow calendars use an independent popup without changing source selection."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from debacal.calendar_widgets import at
from debacal.calendar_sources import CalendarSourcesEditor
from debacal.models import Calendar, Event
from debacal.store import Store
from debacal.ui import MainWindow


def settle(app):
    for _ in range(8):
        app.processEvents()


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    windows, stores, errors = [], [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        def make(calendars=(), *, path=':memory:'):
            store = Store(path)
            stores.append(store)
            for calendar in calendars:
                store.save_calendar(calendar)
            window = MainWindow(store, enable_sync=False)
            windows.append(window)
            window.resize(1300, 900)
            window.show()
            settle(app)
            return window, store
        yield app, make
        for window in reversed(windows):
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        for store in reversed(stores):
            store.close()
        assert not errors


def test_many_popup_sources_never_resize_or_scroll_main_and_their_events_still_show(desktop):
    app, make = desktop
    calendars = [Calendar(f'rare-{i}', f'Occasional {i}', '#587c67', in_main_list=False)
                 for i in range(30)]
    window, store = make(calendars)
    start = at(window.anchor, 11)
    store.apply('Synthetic event', [Event('rare-event', 'rare-0', 'A folded source event', start,
                                         start + timedelta(hours=1))])
    window.refresh()
    settle(app)
    rows = dict(window.calendar_rows)
    assert not window.calendar_list.more_expanded
    assert window.calendar_rows['local'].isVisible()
    assert all(not rows[c.id].isVisible() for c in calendars)
    assert not window.calendar_scroll.verticalScrollBar().isVisible()
    assert any(event.id == 'rare-event' for event in window.visible_events)
    height = window.calendar_section.height()
    list_height = window.calendar_list.content_height()
    QTest.mouseClick(window.calendar_list.more_button, Qt.MouseButton.LeftButton)
    settle(app)
    assert window.calendar_list.more_expanded
    assert window.calendar_list.popup.isVisible()
    assert not window.calendar_scroll.verticalScrollBar().isVisible()
    assert window.calendar_list.popup_scroll.verticalScrollBar().isVisible()
    assert window.calendar_section.height() == height
    assert window.calendar_list.content_height() == list_height
    assert all(row.isVisible() for row in rows.values())
    popup = window.calendar_list.popup
    assert not popup.isWindow() and popup.window() is window
    popup_bounds = QRect(popup.mapToGlobal(QPoint()), popup.size())
    sidebar = window.sidebar_scroll.parentWidget()
    assert QRect(sidebar.mapToGlobal(QPoint()), sidebar.size()).contains(popup_bounds)
    scrollbar = window.calendar_list.popup_scroll.verticalScrollBar()
    scrollbar.setValue(min(200, scrollbar.maximum()))
    settle(app)
    geometry, position = popup.geometry(), scrollbar.value()
    for _ in range(6):
        window.refresh_calendar_list(store.list_calendars())
        settle(app)
        assert popup.geometry() == geometry and scrollbar.value() == position
    viewport = window.calendar_list.popup_scroll.viewport()
    target = min(window.calendar_list.display_rows(False), key=lambda row:
                 abs(row.mapTo(viewport, row.rect().center()).y() - viewport.height() / 2))
    QTest.mouseClick(target.eye, Qt.MouseButton.LeftButton)
    settle(app)
    assert not store.get_calendar(target.calendar.id).visible
    assert not store.get_calendar(target.calendar.id).in_main_list
    assert window.calendar_list.more_expanded
    assert popup.geometry() == geometry and scrollbar.value() == position
    assert window.calendar_rows == rows
    QTest.mouseClick(window.calendar_list.more_button, Qt.MouseButton.LeftButton)
    settle(app)
    assert not window.calendar_scroll.verticalScrollBar().isVisible()
    assert store.get_calendar('rare-1').visible


def test_selecting_popup_calendar_keeps_only_current_occasional_source_in_main(desktop):
    app, make = desktop
    window, store = make([Calendar('first', 'First', '#587c67', in_main_list=False),
                          Calendar('second', 'Second', '#7f79a5', in_main_list=False)])
    listing = window.calendar_list
    original_rows = dict(window.calendar_rows)
    listing.set_expanded(True)
    settle(app)
    QTest.mouseClick(original_rows['first'].choice, Qt.MouseButton.LeftButton)
    settle(app)
    assert not listing.popup.isVisible()
    assert store.get_default_calendar().id == 'first'
    assert original_rows['first'].isVisible() and not original_rows['second'].isVisible()
    assert not store.get_calendar('first').in_main_list
    listing.set_expanded(True)
    settle(app)
    assert original_rows['first'] in listing.display_rows(True)
    assert original_rows['first'] not in listing.display_rows(False)
    QTest.mouseClick(original_rows['second'].choice, Qt.MouseButton.LeftButton)
    settle(app)
    assert not listing.more_expanded and not original_rows['first'].isVisible()
    assert original_rows['second'].isVisible()
    assert window.calendar_rows == original_rows
    assert all(not store.get_calendar(identity).in_main_list for identity in ('first', 'second'))
    assert store.history() == []


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_overflow_button_fits_sidebar_heading_at_supported_text_sizes(desktop, scale):
    app, make = desktop
    window, _store = make([Calendar('rare', 'Occasional', '#587c67', in_main_list=False)])
    window.apply_preferences(replace(window.preferences, font_scale=scale), window.bindings)
    settle(app)
    button = window.calendar_list.more_button

    def bounds(widget):
        return QRect(widget.mapToGlobal(QPoint()), widget.size())

    assert button.isVisible() and not button.size().isEmpty()
    assert bounds(window.calendar_section).contains(bounds(button))
    assert bounds(window.sidebar_scroll.viewport()).contains(bounds(button))
    heading = next(label for label in window.calendar_section.findChildren(QLabel)
                   if label.objectName() == 'sidebarSectionHeading' and label.text() == 'Calendars')
    gap = bounds(button).left() - bounds(heading).right() - 1
    assert 0 <= gap <= round(8 * scale / 100)


def test_moves_between_groups_persist_and_empty_main_list_can_be_refilled(desktop, tmp_path):
    app, make = desktop
    path = tmp_path / 'testing.sqlite3'
    window, store = make([Calendar('rare', 'Occasional', '#587c67', in_main_list=False)], path=path)
    listing = window.calendar_list
    assert listing.move_calendar('local', 0, in_main_list=False)
    settle(app)
    assert all(not value.in_main_list for value in store.list_calendars())
    assert store.get_default_calendar().id == 'local'
    listing.set_expanded(True)
    assert listing.move_calendar('rare', 0, in_main_list=True)
    settle(app)
    assert store.list_calendars()[0].id == 'rare'
    assert store.get_calendar('rare').in_main_list
    assert not store.get_calendar('local').in_main_list
    with Store(path) as reopened:
        assert [(c.id, c.in_main_list) for c in reopened.list_calendars()] == [('rare', True), ('local', False)]
    other, _other_store = make(path=path)
    assert not other.calendar_list.more_expanded
    assert other.calendar_rows['rare'].isVisible()
    assert other.calendar_rows['local'].isVisible()
    assert other.calendar_rows['local'].choice.isChecked()


def accepted_calendar_dialog(calendar):
    return SimpleNamespace(
        result_calendar=calendar, result_calendars=[calendar],
        password=SimpleNamespace(text=lambda: ''),
        remember_password=SimpleNamespace(isChecked=lambda: False),
        exec=lambda: QDialog.DialogCode.Accepted, deleteLater=lambda: None,
    )


def test_calendar_edit_updates_an_existing_sidebar_override(desktop):
    app, make = desktop
    window, store = make()
    store.set_calendar_in_main_list('local', False)
    original = store.get_calendar('local')
    edited = replace(original, in_main_list=True)
    with patch('debacal.ui.CalendarDialog', return_value=accepted_calendar_dialog(edited)):
        window.add_calendar(original)
    settle(app)
    assert store.get_calendar('local').in_main_list
    assert window.calendar_rows['local'].isVisible()


def test_new_calendar_and_rediscovered_calendar_keep_their_sidebar_settings(desktop):
    app, make = desktop
    original = Calendar('remote', 'Occasional', '#587c67', kind='caldav',
                        url='https://calendar.example.test/source/', username='synthetic')
    window, store = make([original])
    store.set_calendar_in_main_list(original.id, False)
    rediscovered = replace(original, id='new-discovery-id', in_main_list=True)
    with patch('debacal.ui.CalendarDialog', return_value=accepted_calendar_dialog(rediscovered)):
        window.add_calendar()
    settle(app)
    assert not store.get_calendar(original.id).in_main_list
    assert store.get_calendar(rediscovered.id) is None
    assert not window.calendar_rows[original.id].isVisible()
    new = Calendar('new', 'Another occasional calendar', '#587c67', in_main_list=False)
    with patch('debacal.ui.CalendarDialog', return_value=accepted_calendar_dialog(new)):
        window.add_calendar()
    settle(app)
    assert not store.get_calendar(new.id).in_main_list
    assert not window.calendar_rows[new.id].isVisible()


def test_source_preferences_apply_updates_sidebar_and_unapplied_changes_are_discarded(desktop):
    app, make = desktop
    window, store = make()
    editor = CalendarSourcesEditor(store, parent=window, executor=window.executor)
    editor.changed.connect(window.preferences_sources_changed)
    try:
        editor.sources.setCurrentItem(editor._items['local'])
        editor.in_main_list.setChecked(False)
        assert store.get_calendar('local').in_main_list
        assert window.calendar_rows['local'].isVisible()
        editor.apply()
        settle(app)
        assert not store.get_calendar('local').in_main_list
        assert window.calendar_rows['local'].isVisible()
        assert window.calendar_rows['local'].choice.isChecked()
        editor.in_main_list.setChecked(True)
    finally:
        editor.finish()
        editor.deleteLater()
    assert not store.get_calendar('local').in_main_list
    assert store.history() == []


def test_failed_layout_save_restores_sidebar_membership_without_a_qt_exception(desktop):
    app, make = desktop
    window, store = make()
    with patch.object(store, 'set_calendar_layout', side_effect=ValueError('Calendar changed.')), \
         patch.object(window, 'report_error') as report:
        assert window.calendar_list.move_calendar('local', 0, in_main_list=False)
    settle(app)
    assert report.call_count == 1
    assert store.get_calendar('local').in_main_list
    assert window.calendar_rows['local'].calendar.in_main_list
    assert window.calendar_rows['local'].isVisible()
