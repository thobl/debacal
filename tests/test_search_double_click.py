"""Sidebar row double-clicks use the regular editor without changing folds."""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from debacal.models import Event
from debacal.store import Store
from debacal.ui import MainWindow


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    dialogs, errors = [], []

    def record_editor(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Rejected

    with patch('debacal.ui.local_zone', return_value=ZONE), \
         patch('debacal.ui.now', return_value=START), \
         patch('debacal.calendar_widgets.local_zone', return_value=ZONE), \
         patch('debacal.ui.EventDialog.exec', new=record_editor), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        window = MainWindow(store, enable_sync=False)
        window.show()
        window.activateWindow()
        app.processEvents()
        yield app, store, window, dialogs
        for dialog in dialogs:
            dialog.close()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def populate(window, *, series=False, count=3):
    source = Event('source', 'local', 'Needle appointment', START, START + timedelta(hours=1),
                   recurrence_rule=f'FREQ=DAILY;COUNT={count}' if series else '', timezone_id=ZONE.key)
    window.apply_events('Create', [source])
    window.focus_search()
    window.search.setText('Needle')
    window.flush_search()
    QApplication.processEvents()
    return source


def double_click(window, item):
    row_id = window.results.event_id(item)
    point = window.results.visualItemRect(item).center()
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton, pos=point)
    current = window.results.row_item(row_id)
    QTest.mouseDClick(window.results.viewport(), Qt.MouseButton.LeftButton,
                      pos=window.results.visualItemRect(current).center())
    QApplication.processEvents()


@pytest.mark.parametrize('selection_mode', ['single', 'group', 'future'])
def test_double_click_ordinary_search_result_opens_one_event_editor(desktop, selection_mode):
    _, store, window, dialogs = desktop
    source = populate(window)
    window.set_selection_mode(selection_mode)
    before = store.history()
    double_click(window, window.results.row_item(source.id))
    assert len(dialogs) == 1
    assert dialogs[0].original.id == source.id
    assert dialogs[0].series is None
    assert dialogs[0].scope.currentData() == 'single'
    assert store.history() == before


@pytest.mark.parametrize('expanded', [False, True])
def test_double_click_series_root_edits_whole_series_without_toggling_fold(desktop, expanded):
    _, store, window, dialogs = desktop
    source = populate(window, series=True)
    parent = window.results.row_item(source.id)
    parent.setExpanded(expanded)
    QApplication.processEvents()
    before = store.history()
    double_click(window, parent)
    assert len(dialogs) == 1
    assert dialogs[0].series == source
    assert dialogs[0].scope.currentData() == 'group'
    assert dialogs[0].edit_reference == source
    assert window.results.row_item(source.id).isExpanded() is expanded
    assert store.history() == before


def test_double_click_dated_child_uses_single_scope_even_when_calendar_mode_is_all(desktop):
    _, _, window, dialogs = desktop
    source = populate(window, series=True)
    window.set_selection_mode('group')
    parent = window.results.row_item(source.id)
    parent.setExpanded(True)
    QApplication.processEvents()
    child = parent.child(1)
    identifier = window.results.event_id(child)
    double_click(window, child)
    assert len(dialogs) == 1
    assert dialogs[0].original.id == identifier
    assert dialogs[0].series == source
    assert dialogs[0].scope.currentData() == 'single'
    assert dialogs[0].edit_reference.id == identifier
    assert window.results.row_item(source.id).isExpanded()


def test_double_click_fold_control_only_folds_and_never_opens_editor(desktop):
    _, _, window, dialogs = desktop
    source = populate(window, series=True)
    parent = window.results.row_item(source.id)
    point = window.results.disclosure_rect(parent).center().toPoint()
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(window.results.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert parent.isExpanded()
    assert not dialogs


def test_double_click_upcoming_event_uses_current_calendar_scope(desktop):
    _, _, window, dialogs = desktop
    source = populate(window, series=True)
    window.exit_search()
    window.set_selection_mode('future')
    QApplication.processEvents()
    assert window.results.agenda_mode
    item = window.results.topLevelItem(0)
    if not window.results.event_id(item):
        item = window.results.topLevelItem(1)
    double_click(window, item)
    assert len(dialogs) == 1
    assert dialogs[0].series == source
    assert dialogs[0].scope.currentData() == 'future'


def test_double_click_more_dates_loads_one_page_without_editing_a_replacement_row(desktop):
    _, _, window, dialogs = desktop
    source = populate(window, series=True, count=250)
    parent = window.results.row_item(source.id)
    parent.setExpanded(True)
    QApplication.processEvents()
    footer = parent.child(10)
    window.results.scrollToItem(footer)
    QApplication.processEvents()
    point = window.results.visualItemRect(footer).center()
    QTest.mouseClick(window.results.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseDClick(window.results.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert parent.childCount() == 21
    assert not dialogs
