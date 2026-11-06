"""Series roots and dated search rows have independent selection identities."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from debacal.models import Event
from debacal.preferences import Preferences
from debacal.recurrence import expand_series, recurrence_key
from debacal.search_panel import SearchPanel, MORE_ROLE, SUBTITLE_ROLE, page_size
from debacal.store import Store

ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 11, 4, 9, tzinfo=ZONE)


@pytest.fixture
def workspace():
    app = QApplication.instance() or QApplication([])
    store = Store(':memory:')
    errors = []
    with patch('debacal.search_panel.local_zone', return_value=ZONE), \
         patch('debacal.search_panel.now', return_value=START), \
         patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        panel = SearchPanel(store, Preferences())
        panel.resize(400, 600)
        panel.set_range(START, START + timedelta(days=7))
        panel.show()
        app.processEvents()
        yield app, store, panel
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors
    store.close()


def master(rule='FREQ=DAILY;COUNT=5'):
    return Event('source', 'local', 'Synthetic needle', START, START + timedelta(hours=1),
                 recurrence_rule=rule, timezone_id=ZONE.key)


def children(panel, parent):
    return [parent.child(index) for index in range(parent.childCount()) if panel.event_id(parent.child(index))]


def test_first_dated_child_and_parent_remain_independent_despite_same_payload(workspace):
    _, store, panel = workspace
    source = master()
    store.apply('Create', [source])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    child = parent.child(0)
    parent_id, child_id = panel.event_id(parent), panel.event_id(child)
    assert parent_id != child_id
    assert panel.result_event(parent_id) == panel.result_event(child_id)
    assert panel.row_scope(parent) == panel.row_scope(parent_id) == 'group'
    assert panel.row_scope(child) == panel.row_scope(child_id) == 'single'
    assert panel.is_series_root(parent) and not panel.is_series_root(child)
    assert panel.row_item(parent_id) is parent
    assert panel.row_item(child_id) is child
    assert panel.row_item('missing-row') is None
    parent.setExpanded(False)
    assert panel.row_item(child_id) is child
    parent.setExpanded(True)
    signals = []
    panel.result_selected.connect(lambda *args: signals.append(args))
    panel.set_row_selection({child_id})
    assert panel.selected_row_ids() == {child_id}
    assert child.isSelected() and not parent.isSelected()
    panel.set_row_selection({parent_id})
    assert panel.selected_row_ids() == {parent_id}
    assert parent.isSelected() and not child.isSelected()
    panel.set_row_selection({parent_id, child_id})
    assert panel.selected_row_ids() == {parent_id, child_id}
    assert not signals


def test_ctrl_click_reports_distinct_parent_and_child_row_modes(workspace):
    app, store, panel = workspace
    store.apply('Create', [master()])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    child = parent.child(0)
    emitted = []
    panel.result_selected.connect(lambda identifier, modifiers: emitted.append((identifier, panel.row_scope(identifier), modifiers)))
    app.processEvents()
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton,
                     pos=panel.visualItemRect(parent).center())
    QTest.mouseClick(panel.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                     panel.visualItemRect(child).center())
    assert emitted[-2:] == [(panel.event_id(parent), 'group', Qt.KeyboardModifier.NoModifier),
                            (panel.event_id(child), 'single', Qt.KeyboardModifier.ControlModifier)]
    assert panel.selected_row_ids() == {panel.event_id(parent), panel.event_id(child)}


def test_override_only_search_parent_still_has_distinct_series_identity(workspace):
    _, store, panel = workspace
    source = replace(master(), title='Original title')
    store.apply('Create', [source])
    selected = expand_series(source, START, START + timedelta(days=10))[1]
    moved = replace(selected, title='Needle override', start=selected.start + timedelta(days=30),
                    end=selected.end + timedelta(days=30))
    store.edit_occurrence(selected, moved)
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    child = parent.child(0)
    assert panel.event_id(parent) == source.id
    assert panel.event_id(child) == moved.id
    assert panel.result_event(source.id) == panel.result_event(moved.id)
    assert panel.row_scope(source.id) == 'group' and panel.row_scope(moved.id) == 'single'
    panel.set_row_selection({moved.id})
    assert child.isSelected() and not parent.isSelected()


def test_explicit_row_selection_survives_repopulate_and_ordinary_root_is_single(workspace):
    _, store, panel = workspace
    source = master()
    ordinary = replace(source, id='ordinary', ical_uid='ordinary', recurrence_rule='')
    store.apply('Create', [source, ordinary])
    panel.populate('needle')
    parent = next(panel.item(index) for index in range(panel.count()) if panel.event_id(panel.item(index)) == source.id)
    parent.setExpanded(True)
    child_id = panel.event_id(parent.child(0))
    panel.set_row_selection({child_id, ordinary.id})
    panel.invalidate()
    panel.populate('needle')
    assert panel.selected_row_ids() == {child_id, ordinary.id}
    assert panel.row_scope(ordinary.id) == 'single'
    assert not panel.is_series_root(ordinary.id)


@pytest.mark.parametrize(('rule', 'size'), [
    ('FREQ=DAILY', 10), ('FREQ=WEEKLY;INTERVAL=2', 10),
    ('FREQ=DAILY;COUNT=250', 100), ('FREQ=DAILY;UNTIL=20271104T080000Z', 100),
    ('freq=monthly;count=200', 100), ('', 100),
])
def test_page_size_distinguishes_open_ended_rules_from_finite_resources(rule, size):
    assert page_size(master(rule)) == size


def test_infinite_pages_load_ten_more_without_mutating_the_resource(workspace):
    _, store, panel = workspace
    source = master('FREQ=DAILY')
    store.apply('Create', [source])
    previous_history = store.history()
    previous_pending = store.pending_changes()
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert len(children(panel, parent)) == 10
    footer = parent.child(10)
    assert footer.data(MORE_ROLE) is True
    assert '10 dates shown' in footer.data(SUBTITLE_ROLE)
    panel.load_more(footer)
    values = children(panel, parent)
    assert len(values) == len({panel.event_id(item) for item in values}) == 20
    assert '20 dates shown' in parent.child(20).data(SUBTITLE_ROLE)
    assert store.get_event(source.id) == source
    assert store.history() == previous_history and store.pending_changes() == previous_pending


def test_finite_rdate_resource_keeps_hundred_row_page(workspace):
    _, store, panel = workspace
    source = replace(master(''), recurrence_dates=tuple(recurrence_key(START + timedelta(days=day)) for day in range(1, 125)))
    store.apply('Create', [source])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert len(children(panel, parent)) == 100
    panel.load_more(parent)
    assert len(children(panel, parent)) == 125
