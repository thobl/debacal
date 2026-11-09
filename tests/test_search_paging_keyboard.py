"""Ten-date search pages are reachable without changing event selection."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from debacal.models import Event
from debacal.recurrence import occurrence, recurrence_key
from debacal.search_panel import MORE_ROLE
from debacal.theme import scheme_for
from test_search_panel import START, genuine_children, workspace as panel_workspace
from test_workspace_features import desktop


def source(kind, count=25):
    rule = {'count': f'FREQ=DAILY;COUNT={count}',
            'until': 'FREQ=DAILY;UNTIL=' + (START + timedelta(days=count - 1)).strftime('%Y%m%dT080000Z'),
            'infinite': 'FREQ=DAILY', 'dates': ''}[kind]
    dates = tuple(recurrence_key(START + timedelta(days=offset)) for offset in range(1, count)) if kind == 'dates' else ()
    return Event('series', 'local', 'Needle sequence', START, START + timedelta(hours=1),
                 recurrence_rule=rule, recurrence_dates=dates)


@pytest.mark.parametrize('kind', ['count', 'until', 'dates', 'infinite'])
def test_native_arrow_keys_reach_more_and_enter_preserves_the_selected_event(panel_workspace, kind):
    app, store, panel = panel_workspace
    master = source(kind)
    store.apply('Create', [master])
    before = store.events_revision(), store.history(), store.pending_changes()
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    panel.setCurrentItem(parent)
    panel.setFocus()
    app.processEvents()
    emitted = []
    panel.result_selected.connect(lambda *args: emitted.append(args))
    for _ in range(10):
        QTest.keyClick(panel, Qt.Key.Key_Down)
    last = parent.child(9)
    assert panel.currentItem() is last
    selected, signals = panel.selected_ids(), list(emitted)
    QTest.keyClick(panel, Qt.Key.Key_Down)
    footer = panel.currentItem()
    assert footer is parent.child(10) and footer.data(MORE_ROLE) is True
    assert footer.flags() & Qt.ItemFlag.ItemIsSelectable
    assert '10 dates' in footer.toolTip(0) and '100 dates' in footer.toolTip(0)
    assert panel.selected_ids() == selected and emitted == signals
    QTest.keyClick(panel, Qt.Key.Key_Up)
    assert panel.currentItem() is last
    QTest.keyClick(panel, Qt.Key.Key_Down)
    signals = list(emitted)
    QTest.keyClick(panel, Qt.Key.Key_Return)
    assert len(genuine_children(parent)) == 20
    assert panel.currentItem() is last
    assert panel.selected_ids() == selected and emitted == signals
    QTest.keyClick(panel, Qt.Key.Key_Down)
    assert panel.currentItem() is parent.child(10)
    assert panel.row_scope(panel.currentItem()) == 'single'
    assert emitted[-1][0] == panel.event_id(parent.child(10))
    assert store.events_revision() == before[0]
    assert store.history() == before[1] and store.pending_changes() == before[2]


def test_keyboard_current_paging_action_draws_a_visible_focus_ring_without_event_selection(panel_workspace):
    app, store, panel = panel_workspace
    store.apply('Create', [source('count')])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    panel.setCurrentItem(parent.child(9))
    footer = parent.child(10)
    panel.activateWindow()
    panel.setFocus()
    panel.scrollToItem(footer)
    app.processEvents()
    rect = panel.visualItemRect(footer)
    before = panel.viewport().grab(rect).toImage()
    selected = panel.selected_ids()
    QTest.keyClick(panel, Qt.Key.Key_Down)
    app.processEvents()
    assert panel.currentItem() is footer and not footer.isSelected()
    assert panel.selected_ids() == selected
    assert panel.visualItemRect(footer) == rect
    after = panel.viewport().grab(rect).toImage()
    focus = QColor(scheme_for(panel).focus)
    # The existing delegate paints the keyboard cue inside the left card
    # edge. Inspect that strip so changed text or the old event's border
    # cannot satisfy this rendered-feedback assertion.
    changed_focus_pixels = [(x, y) for x in range(3, 9) for y in range(12, rect.height() - 12)
                            if before.pixelColor(x, y) != after.pixelColor(x, y)
                            and after.pixelColor(x, y) == focus]
    assert len(changed_focus_pixels) >= 3


@pytest.mark.parametrize('kind', ['count', 'until', 'dates'])
def test_final_finite_page_has_no_paging_action_and_arrow_wrap_returns_to_the_series(panel_workspace, kind):
    _, store, panel = panel_workspace
    store.apply('Create', [source(kind)])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    panel.setCurrentItem(parent)
    for _ in range(10):
        QTest.keyClick(panel, Qt.Key.Key_Down)
    for remaining in (20, 25):
        QTest.keyClick(panel, Qt.Key.Key_Down)
        assert panel.is_paging_action(panel.currentItem())
        QTest.keyClick(panel, Qt.Key.Key_Enter)
        assert len(genuine_children(parent)) == remaining
        for _ in range(10 if remaining == 20 else 5):
            QTest.keyClick(panel, Qt.Key.Key_Down)
    assert parent.childCount() == 25 and panel.currentItem() is parent.child(24)
    QTest.keyClick(panel, Qt.Key.Key_Down)
    assert panel.currentItem() is parent and panel.row_scope(parent) == 'group'


@pytest.mark.parametrize('count', [1, 9, 10, 11, 20, 21, 100, 101])
@pytest.mark.parametrize('kind', ['count', 'dates'])
def test_finite_page_boundaries_include_short_final_pages_and_the_same_cap(panel_workspace, kind, count):
    _, store, panel = panel_workspace
    store.apply('Create', [source(kind, count)])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert len(genuine_children(parent)) == min(count, 10)
    for _ in range(9):
        panel.load_more(parent)
    assert len(genuine_children(parent)) == min(count, 100)
    if count > 100:
        assert parent.child(100).data(MORE_ROLE) == 'rebase'
    else:
        assert parent.childCount() == count


def test_all_day_date_sets_use_the_same_dated_child_pages(panel_workspace):
    _, store, panel = panel_workspace
    start = START.replace(hour=0)
    master = replace(source('dates'), all_day=True, start=start, end=start + timedelta(days=1))
    store.apply('Create', [master])
    panel.set_range(start, start + timedelta(days=7))
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert len(genuine_children(parent)) == 10
    panel.load_more(parent)
    assert len(genuine_children(parent)) == 20
    values = [panel.result_event(panel.event_id(item)) for item in genuine_children(parent)]
    assert all(item.all_day for item in values)
    assert [item.start.date() for item in values] == [(start + timedelta(days=offset)).date() for offset in range(20)]


def test_override_only_matches_page_only_the_matching_dated_children(panel_workspace):
    _, store, panel = panel_workspace
    master = replace(source('count', 30), title='Original')
    overrides = {}
    for offset in range(1, 26):
        when = START + timedelta(days=offset)
        item = replace(occurrence(master, when), title='Needle override',
                       start=when + timedelta(hours=1), end=when + timedelta(hours=2))
        overrides[item.recurrence_id] = item.to_dict()
    master = replace(master, recurrence_overrides=overrides)
    store.apply('Create', [master])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    assert len(genuine_children(parent)) == 10
    panel.load_more(parent)
    panel.load_more(parent)
    assert len(genuine_children(parent)) == parent.childCount() == 25
    assert panel.row_scope(parent) == 'group'
    for item in genuine_children(parent):
        assert panel.row_scope(item) == 'single'
        assert panel.result_event(panel.event_id(item)).title == 'Needle override'


@pytest.mark.parametrize('kind', ['count', 'infinite'])
def test_capped_footer_is_arrow_reachable_and_enter_rebases_without_emitting_event_selection(panel_workspace, kind):
    _, store, panel = panel_workspace
    store.apply('Create', [source(kind, count=1000)])
    panel.populate('needle')
    parent = panel.item(0)
    parent.setExpanded(True)
    for _ in range(9):
        panel.load_more(parent)
    assert len(genuine_children(parent)) == 100
    panel.setCurrentItem(parent.child(99))
    QTest.keyClick(panel, Qt.Key.Key_Down)
    footer = panel.currentItem()
    assert footer.data(MORE_ROLE) == 'rebase'
    assert 'date range' in footer.toolTip(0)
    later = START + timedelta(days=500)
    panel.set_range(later, later + timedelta(days=7))
    panel.populate('needle')
    assert panel.currentItem() is footer
    emitted = []
    panel.result_selected.connect(lambda *args: emitted.append(args))
    QTest.keyClick(panel, Qt.Key.Key_Return)
    assert len(genuine_children(parent)) == 10
    assert panel.result_event(panel.event_id(parent.child(0))).start == later
    assert not emitted


@pytest.mark.parametrize('kind', ['count', 'dates', 'infinite'])
def test_workspace_paging_focus_preserves_calendar_selection_and_ignores_event_commands(desktop, kind):
    app, window, store = desktop
    master = source(kind, count=35)
    store.apply('Create', [master])
    window.go_date(START.date())
    window.refresh()
    window.set_selection_mode('group')
    window.focus_search()
    window.search.setText('Needle')
    window.flush_search()
    parent = window.results.item(0)
    parent.setExpanded(True)
    app.processEvents()
    QTest.keyClick(window.search, Qt.Key.Key_Down)
    assert window.results.currentItem() is parent
    assert window.selection.primary == master
    for _ in range(10):
        QTest.keyClick(window.results, Qt.Key.Key_Down)
    last = parent.child(9)
    assert window.results.currentItem() is last
    assert window.selection.primary.id == window.results.event_id(last)
    assert len(window.selection.refs) == 1 and window.selection.primary.series_id == master.id
    before = (window.selection.refs, window.selected_id, set(window.selected_ids), window.anchor,
              window.selected_day, store.events_revision(), store.history())
    QTest.keyClick(window.results, Qt.Key.Key_Down)
    assert window.results.is_paging_action(window.results.currentItem())
    with patch.object(window, 'edit_event') as edit, patch.object(window, 'delete_event') as delete, \
            patch.object(window, 'copy_event') as copy:
        QTest.keyClick(window.results, Qt.Key.Key_E)
        QTest.keyClick(window.results, Qt.Key.Key_Delete)
        QTest.keyClick(window.results, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert not edit.called and not delete.called and not copy.called
    assert (window.selection.refs, window.selected_id, set(window.selected_ids), window.anchor,
            window.selected_day, store.events_revision(), store.history()) == before
    QTest.keyClick(window.results, Qt.Key.Key_Return)
    assert len(genuine_children(parent)) == 20
    assert window.results.currentItem() is last
    assert (window.selection.refs, window.selected_id, set(window.selected_ids), window.anchor,
            window.selected_day, store.events_revision(), store.history()) == before
    QTest.keyClick(window.results, Qt.Key.Key_Down)
    assert window.results.currentItem() is parent.child(10)
    assert window.selection.primary.id == window.results.event_id(parent.child(10))
    assert window.selection.primary.start == START + timedelta(days=10)
