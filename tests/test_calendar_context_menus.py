"""Calendar actions remain reachable from locked names and overflow panels."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QPushButton, QVBoxLayout

from debacal.models import Calendar
from debacal.store import Store
from debacal.ui import MainWindow


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    store.save_calendar(Calendar('local', 'Personal', '#587c67'))
    store.set_default_calendar('local')
    owned, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        def make(**values):
            store.save_calendar(Calendar('source', 'Synthetic source', '#587c67', **values))
            window = MainWindow(store, enable_sync=False)
            owned.append(window)
            window.resize(1000, 760)
            window.show()
            app.processEvents()
            return window, window.calendar_rows['source']
        yield app, make
        for window in owned:
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        store.close()
        assert not errors


def open_context(app, widget, interact, *, keyboard=False):
    """Send the native context request after the corresponding name click.

    QTest mouse events do not synthesize platform context-menu events. Sending
    that separate native event preserves the actual disabled widget as target.
    The timer runs inside the real QMenu.exec loop, never a mocked exec method.
    """
    inspected, failures = [], []
    def visit():
        menu = app.activePopupWidget()
        try:
            assert isinstance(menu, QMenu)
            inspected.append(menu)
            interact(menu)
        except BaseException as error:
            failures.append(error)
        finally:
            if menu is not None:
                menu.close()
    QTimer.singleShot(0, visit)
    point = widget.rect().center()
    if keyboard:
        widget.setFocus()
        assert widget.window().focusWidget() is widget
        reason = QContextMenuEvent.Reason.Keyboard
    else:
        QTest.mouseClick(widget, Qt.MouseButton.RightButton, pos=point)
        reason = QContextMenuEvent.Reason.Mouse
    QApplication.sendEvent(widget, QContextMenuEvent(reason, point, widget.mapToGlobal(point)))
    app.processEvents()
    if failures:
        raise failures[0]
    assert len(inspected) == 1


@pytest.mark.parametrize('kind,read_only', [('caldav', True), ('ics', False), ('ics', True)])
def test_real_context_request_on_readonly_name_offers_local_actions_without_creation(workspace, kind, read_only):
    app, make = workspace
    window, row = make(kind=kind, read_only=read_only)
    created = QSignalSpy(row.creation_requested)
    QTest.mouseClick(row.choice, Qt.MouseButton.LeftButton)
    QTest.keyClick(row.choice, Qt.Key.Key_Space)
    assert created.count() == 0 and not row.choice.isChecked()
    assert not row.choice.isEnabled() and row.lock.isVisible()
    def inspect(menu):
        assert {action.text() for action in menu.actions() if not action.isSeparator()} == {
            'Edit calendar…', 'Reminder settings…', 'Export .ics…'}
        QTest.keyClick(menu, Qt.Key.Key_Escape)
    open_context(app, row.choice, inspect)
    assert window.store.get_default_calendar().id == 'local'


def test_readonly_row_accepts_keyboard_context_requests(workspace):
    app, make = workspace
    _, row = make(kind='ics', read_only=True)
    def inspect(menu):
        assert any(action.text() == 'Edit calendar…' for action in menu.actions())
    open_context(app, row, inspect, keyboard=True)
    assert row.focusPolicy() == Qt.FocusPolicy.StrongFocus


@pytest.mark.parametrize('read_only', [False, True])
@pytest.mark.parametrize('dismissal', ['escape', 'export', 'edit', 'reminders'])
def test_overflow_menu_exec_preserves_panel_geometry_focus_and_selection(workspace, monkeypatch, dismissal, read_only):
    app, make = workspace
    window, row = make(in_main_list=False, kind='ics' if read_only else 'local', read_only=read_only)
    listing = window.calendar_list
    listing.set_expanded(True)
    app.processEvents()
    geometry = listing.popup.geometry()
    scroll = listing.popup_scroll.verticalScrollBar().value()
    order = listing.ordered_ids()
    default = window.store.get_default_calendar().id
    focus = app.focusWidget()
    created = QSignalSpy(row.creation_requested)
    calls = []
    def fake_dialog(identity):
        calls.append(identity)
        dialog = QDialog(window)
        layout = QVBoxLayout(dialog)
        accept = QPushButton('Close', dialog)
        accept.clicked.connect(dialog.accept)
        layout.addWidget(accept)
        def interact_dialog():
            # Modal action dialogs have their own lifecycle, even when their
            # parent is the main window rather than the source panel.
            QTest.mouseClick(accept, Qt.MouseButton.LeftButton)
            assert listing.popup.isVisible()
        QTimer.singleShot(0, interact_dialog)
        assert dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
    monkeypatch.setattr(window, 'export_ics', fake_dialog)
    monkeypatch.setattr(window, 'add_calendar', lambda calendar: fake_dialog(calendar.id))
    def reminders(category, *, calendar_id):
        assert category == 'sources'
        fake_dialog(calendar_id)
    monkeypatch.setattr(window, 'show_preferences', reminders)
    def inspect(menu):
        assert listing.popup.isVisible() and listing.more_expanded
        assert listing.popup.geometry() == geometry
        # A menu's separator receives real mouse input without activating an
        # action; keyboard activation is deterministic across Qt platforms.
        separator = next(action for action in menu.actions() if action.isSeparator())
        point = menu.actionGeometry(separator).center()
        QTest.mouseMove(menu, point)
        QTest.mousePress(menu, Qt.MouseButton.LeftButton, pos=point)
        assert listing.popup.isVisible()
        if dismissal == 'escape':
            QTest.keyClick(menu, Qt.Key.Key_Escape)
        else:
            title = {'export': 'Export .ics…', 'edit': 'Edit calendar…', 'reminders': 'Reminder settings…'}[dismissal]
            menu.setActiveAction(next(action for action in menu.actions() if action.text() == title))
            QTest.keyClick(menu, Qt.Key.Key_Return)
        QTest.mouseRelease(menu, Qt.MouseButton.LeftButton, pos=point)
    open_context(app, row.choice, inspect)
    assert calls == ([] if dismissal == 'escape' else ['source'])
    assert listing.popup.isVisible() and listing.more_expanded and listing.more_button.isChecked()
    assert listing.popup.geometry() == geometry
    assert listing.popup_scroll.verticalScrollBar().value() == scroll
    assert listing.ordered_ids() == order and not listing._dragging
    assert window.store.get_default_calendar().id == default and created.count() == 0
    if dismissal == 'escape':
        assert app.focusWidget() is focus
    QTest.keyClick(listing.popup, Qt.Key.Key_Escape)
    assert not listing.popup.isVisible()
    listing.set_expanded(True)
    QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=QPoint(window.width() - 10, window.height() - 10))
    assert not listing.popup.isVisible()


@pytest.mark.parametrize('outside_panel', [False, True])
def test_nested_menu_outside_click_closes_only_the_panels_it_leaves(workspace, outside_panel):
    app, make = workspace
    window, row = make(in_main_list=False, kind='ics', read_only=True)
    listing = window.calendar_list
    listing.set_expanded(True)
    app.processEvents()
    def inspect(menu):
        point = (window.mapToGlobal(QPoint(window.width() - 10, window.height() - 10)) if outside_panel
                 else listing.popup.mapToGlobal(QPoint(2, 2)))
        assert not menu.geometry().contains(point)
        local = menu.mapFromGlobal(point)
        # Popup windows receive grabbed presses outside their own geometry.
        QTest.mousePress(menu, Qt.MouseButton.LeftButton, pos=local)
        assert not menu.isVisible()
        assert listing.popup.isVisible() is not outside_panel
        QTest.mouseRelease(menu, Qt.MouseButton.LeftButton, pos=local)
    open_context(app, row.choice, inspect)
    assert listing.more_expanded is not outside_panel
