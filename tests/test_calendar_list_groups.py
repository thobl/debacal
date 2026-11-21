"""Sidebar grouping is separate from showing events and supports empty targets."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDrag, QDragEnterEvent, QDropEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from debacal.calendar_list import CALENDAR_ORDER_MIME, CalendarList, CalendarRow
from debacal.models import Calendar
from debacal.preferences import Preferences


def calendar(identity, *, main=True, visible=True):
    return Calendar(identity, identity, '#587c67', in_main_list=main, visible=visible)


class DragMixin:
    def source(self):
        return self.row


class Enter(DragMixin, QDragEnterEvent):
    def __init__(self, row, point):
        self.row, self.mime = row, QMimeData()
        self.mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode())
        super().__init__(QPoint(point), Qt.DropAction.MoveAction, self.mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


class Drop(DragMixin, QDropEvent):
    def __init__(self, row, point):
        self.row, self.mime = row, QMimeData()
        self.mime.setData(CALENDAR_ORDER_MIME, row.calendar.id.encode())
        super().__init__(QPointF(point), Qt.DropAction.MoveAction, self.mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(error)):
        def make(calendars):
            listing = CalendarList()
            owned.append(listing)
            rows = []
            for source in calendars:
                row = CalendarRow(source)
                row.set_preferences(Preferences())
                listing.attach_row(row)
                rows.append(row)
            listing.set_rows(rows)
            listing.resize(290, 400)
            listing.show()
            app.processEvents()
            return listing
        yield app, make
        for widget in reversed(owned):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


def test_main_list_stays_above_collapsed_more_independent_of_event_visibility(desktop):
    app, make = desktop
    listing = make([calendar('rare', main=False), calendar('main', visible=False), calendar('other', main=False)])
    main, rare, other = listing.rows()
    assert listing.ordered_ids() == ['main', 'rare', 'other']
    assert listing.visible_row_count() == 1
    assert not listing.more_expanded and listing.more_button.isVisible()
    assert main.isVisible() and rare.isHidden() and other.isHidden()
    assert rare.calendar.visible and not main.calendar.visible
    height = listing.content_height()
    QTest.mouseClick(listing.more_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert listing.more_expanded and listing.visible_row_count() == 3
    assert rare.isVisible() and other.isVisible() and listing.content_height() > height
    visible = QSignalSpy(rare.visibility_requested)
    QTest.mouseClick(rare.eye, Qt.MouseButton.LeftButton)
    assert visible.at(0) == ['rare', False]
    assert not rare.calendar.in_main_list
    QTest.mouseClick(listing.more_button, Qt.MouseButton.LeftButton)
    assert rare.isHidden() and other.isHidden() and listing.content_height() == height
    listing.more_button.setFocus()
    QTest.keyClick(listing.more_button, Qt.Key.Key_Space)
    assert listing.more_expanded and rare.isVisible()


def test_status_change_emits_even_when_order_is_unchanged_and_preserves_source_metadata(desktop):
    _, make = desktop
    source = replace(calendar('selected', visible=False), kind='caldav',
                     url='https://calendar.example.test/dav/synthetic/', username='synthetic')
    listing = make([source, calendar('rare', main=False)])
    row = listing.rows()[0]
    row.set_calendar(source, current=True)
    changed = QSignalSpy(listing.grouped_layout_changed)
    order = QSignalSpy(listing.order_changed)
    assert listing.move_calendar('selected', 0, in_main_list=False)
    assert listing.ordered_ids() == ['selected', 'rare']
    assert changed.at(0) == [['selected', 'rare'], []] and order.count() == 0
    assert row.calendar == replace(source, in_main_list=False)
    assert row.choice.isChecked() and row.choice.font().underline()
    assert listing.visible_row_count() == 0
    assert not listing.move_calendar('selected', 0, in_main_list=False)
    assert changed.count() == 1


@pytest.mark.parametrize('initial_main', [True, False])
def test_authenticated_drag_can_drop_into_either_empty_group(desktop, initial_main):
    app, make = desktop
    listing = make([calendar('one', main=initial_main), calendar('two', main=initial_main)])
    if not initial_main:
        listing.set_expanded(True)
    row = listing.rows()[0]
    height = listing.more_button.height()
    assert listing.more_button.text() == ''
    listing.dragEnterEvent(Enter(row, QPoint(10, 0)))
    app.processEvents()
    assert listing.more_button.text() == 'More calendars'
    assert listing.more_button.height() == height
    target = listing.more_button if initial_main else listing.main_drop_target
    assert target.isVisible()
    request = Drop(row, target.geometry().center())
    listing.dropEvent(request)
    assert request.isAccepted() and row.calendar.in_main_list != initial_main
    assert set(listing.ordered_ids()) == {'one', 'two'}
    assert [value.calendar.id for value in listing.group_rows(True)] == (['two'] if initial_main else ['one'])
    assert [value.calendar.id for value in listing.group_rows(False)] == (['one'] if initial_main else ['two'])
    assert listing.main_drop_target.isHidden()
    assert listing.more_button.text() == ''
    assert listing.more_button.height() == height


def test_closed_more_header_accepts_drop_and_hover_opens_it(desktop):
    _, make = desktop
    listing = make([calendar('main'), calendar('first', main=False), calendar('last', main=False)])
    main = listing.rows()[0]
    point = listing.more_button.geometry().center()
    listing.dragEnterEvent(Enter(main, point))
    assert listing._drop_group is False and listing._expand_timer.isActive()
    listing._expand_during_drag()
    assert listing.more_expanded and listing.visible_row_count() == 3
    listing.dropEvent(Drop(main, listing.more_button.geometry().center()))
    assert listing.ordered_ids() == ['main', 'first', 'last']
    assert listing.group_rows(True) == []
    assert not listing._scroll_timer.isActive() and not listing._expand_timer.isActive()
    assert listing.more_button.isVisible()


def test_external_drag_never_reveals_empty_targets_or_changes_groups(desktop):
    _, make = desktop
    listing, other = make([calendar('main')]), make([calendar('outside')])
    request = Enter(other.rows()[0], QPoint(10, 0))
    listing.dragEnterEvent(request)
    assert not request.isAccepted() and not listing._dragging
    assert listing.more_button.isHidden() and listing.main_drop_target.isHidden()
    assert listing.ordered_ids() == ['main']


def test_refresh_preserves_open_group_and_row_identity_without_repolishing_controls(desktop):
    _, make = desktop
    listing = make([calendar('main'), calendar('rare', main=False)])
    listing.set_expanded(True)
    rows = listing.rows()
    with patch.object(listing.box, 'insertWidget', wraps=listing.box.insertWidget) as insert, \
            patch.object(listing.more_button, 'setStyleSheet', wraps=listing.more_button.setStyleSheet) as polish:
        for _ in range(20):
            listing.set_rows(rows)
        assert insert.call_count == polish.call_count == 0
    assert listing.more_expanded and listing.rows() == rows and listing.visible_row_count() == 2
    listing.set_rows([rows[0]])
    assert listing.more_button.isHidden() and listing.rows() == [rows[0]] and rows[1].isHidden()


def test_drag_keeps_original_pointer_offset_when_empty_main_target_moves_source(desktop, monkeypatch):
    _, make = desktop
    listing = make([calendar('one', main=False), calendar('two', main=False)])
    listing.set_expanded(True)
    row = listing.rows()[0]
    hotspot = QPoint(12, 10)
    anchor = row.mapToGlobal(hotspot)
    original_image = row.grab().size()
    observed = []
    def execute(drag, _allowed, _default):
        assert listing.main_drop_target.isVisible()
        assert row.mapFromGlobal(anchor) != hotspot
        assert drag.hotSpot() == hotspot and drag.pixmap().size() == original_image
        observed.append(True)
        return Qt.DropAction.IgnoreAction
    monkeypatch.setattr(QDrag, 'exec', execute)
    listing.start_drag(row, anchor)
    assert observed == [True] and listing.main_drop_target.isHidden()
