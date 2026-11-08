"""Source-list reordering is local, persistent, and separate from calendar data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QDrag, QDragEnterEvent, QDropEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from debacal.calendar_list import CalendarList, CalendarRow, CALENDAR_ORDER_MIME
from debacal.models import Calendar, Event
from debacal.preferences import Preferences
from debacal.store import Store


CALENDARS = [Calendar('personal', 'Personal', '#b6815c'),
             Calendar('work', 'Work', '#587c67'),
             Calendar('shared', 'Shared', '#7f79a5', read_only=True)]


def ids(store):
    return [calendar.id for calendar in store.list_calendars()]


def test_order_persists_per_profile_without_changing_legacy_order_or_default(tmp_path):
    path = tmp_path / 'testing.sqlite'
    with Store(path) as store:
        for calendar in CALENDARS:
            store.save_calendar(calendar)
        original = ids(store)
        assert store.get_setting('calendar_order') is None
        default = store.get_default_calendar()
        store.set_calendar_order(['shared', 'work', 'personal', 'local'])
        assert store.get_default_calendar() == default
        store.set_default_calendar('work')
        store.set_calendar_order(['personal', 'local', 'shared', 'work'])
        assert store.get_default_calendar().id == 'work'
    with Store(path) as reopened, Store(tmp_path / 'production.sqlite') as other:
        assert ids(reopened) == ['personal', 'local', 'shared', 'work']
        assert reopened.get_default_calendar().id == 'work'
        assert ids(other) == ['local']
        assert other.get_setting('calendar_order') is None
    assert original == ['local', 'personal', 'work', 'shared']


def test_new_calendars_append_and_deleted_or_duplicate_saved_ids_are_ignored():
    with Store(':memory:') as store:
        for calendar in CALENDARS:
            store.save_calendar(calendar)
        store.set_calendar_order(['work', 'shared'])
        assert ids(store) == ['work', 'shared', 'local', 'personal']
        store.save_calendar(Calendar('new', 'New source', '#ffff00', kind='ics'))
        assert ids(store) == ['work', 'shared', 'local', 'personal', 'new']
        store.connection.execute('DELETE FROM calendars WHERE id=?', ('shared',))
        store.set_setting('calendar_order', ['gone', 'work', 'work', 'shared', 'local', 'personal'])
        assert ids(store) == ['work', 'local', 'personal', 'new']
        store.set_calendar_order(['new', 'work'])
        assert store.get_setting('calendar_order') == ['new', 'work', 'local', 'personal']


@pytest.mark.parametrize('invalid', ['local', ['missing'], ['local', 'local'], [None], [[]]])
def test_bad_order_is_rejected_without_changing_saved_order(invalid):
    with Store(':memory:') as store:
        with pytest.raises(ValueError):
            store.set_calendar_order(invalid)
        assert store.get_setting('calendar_order') is None
        assert ids(store) == ['local']


def test_order_does_not_modify_sources_events_history_or_sync_data():
    with Store(':memory:') as store:
        remote = Calendar('remote', 'Remote', '#ffff00', kind='caldav',
                          url='https://calendar.example.test/collection/', username='synthetic', server_color='#ffff00')
        store.save_calendar(remote)
        begin = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)
        item = Event('one', remote.id, 'Synthetic event', begin, begin + timedelta(hours=1))
        store.apply('Create', [item])
        store.set_sync_metadata(remote.id, item.id, {'etag': 'synthetic-etag'})
        snapshots = {}
        for table in ('calendars', 'events', 'operations', 'outbox', 'sync_metadata'):
            snapshots[table] = [tuple(row) for row in store.connection.execute(f'SELECT * FROM {table}')]
        version = store.events_revision()
        store.set_calendar_order(['remote', 'local'])
        assert store.events_revision() == version
        for table, expected in snapshots.items():
            assert [tuple(row) for row in store.connection.execute(f'SELECT * FROM {table}')] == expected


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    widgets, errors = [], []
    with patch('sys.excepthook', side_effect=lambda _, error, __: errors.append(error)):
        def make(calendars=CALENDARS, scale=100):
            listing = CalendarList()
            widgets.append(listing)
            for calendar in calendars:
                row = CalendarRow(calendar)
                row.set_preferences(Preferences(font_scale=scale))
                listing.box.addWidget(row)
                listing.attach_row(row)
            listing.resize(300, len(calendars) * round(36 * scale / 100))
            listing.show()
            app.processEvents()
            return listing
        yield app, make, widgets
        for widget in reversed(widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not errors


class Drop(QDropEvent):
    def __init__(self, source, point, mime=None):
        if mime is None:
            mime = QMimeData()
            mime.setData(CALENDAR_ORDER_MIME, source.calendar.id.encode())
        super().__init__(QPointF(point), Qt.DropAction.MoveAction, mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self._source, self._mime = source, mime

    def source(self):
        return self._source


class Enter(QDragEnterEvent):
    def __init__(self, source, point):
        mime = QMimeData()
        mime.setData(CALENDAR_ORDER_MIME, source.calendar.id.encode())
        super().__init__(QPoint(point), Qt.DropAction.MoveAction, mime,
                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self._source, self._mime = source, mime

    def source(self):
        return self._source


@pytest.mark.parametrize('scale', [80, 100, 140])
def test_drop_endpoints_reorder_same_rows_without_changing_creation_selection(desktop, scale):
    app, make, _ = desktop
    listing = make(scale=scale)
    rows = listing.rows()
    rows[1].set_calendar(rows[1].calendar, current=True, scale=scale)
    chosen = rows[1].choice
    changed = QSignalSpy(listing.order_changed)
    listing.dropEvent(Drop(rows[-1], QPoint(10, 0)))
    assert listing.ordered_ids() == ['shared', 'personal', 'work']
    assert changed.at(0) == [['shared', 'personal', 'work']]
    assert rows[1].choice is chosen and chosen.isChecked()
    app.processEvents()
    listing.dropEvent(Drop(rows[-1], QPoint(10, listing.height() + 20)))
    assert listing.ordered_ids() == ['personal', 'work', 'shared']
    assert {id(row) for row in listing.rows()} == {id(row) for row in rows}
    assert chosen.isChecked()


def test_noop_external_cross_profile_and_event_drops_are_ignored(desktop):
    _, make, _ = desktop
    listing, other = make(), make()
    changed = QSignalSpy(listing.order_changed)
    original = listing.ordered_ids()
    noop = Drop(listing.rows()[0], QPoint(0, 0))
    listing.dropEvent(noop)
    assert noop.isAccepted() and changed.count() == 0
    outside = Drop(other.rows()[0], QPoint(0, 0))
    listing.dropEvent(outside)
    assert not outside.isAccepted()
    mime = QMimeData()
    mime.setData('application/x-debacal-events', b'not-calendar-order')
    event_drop = Drop(listing.rows()[0], QPoint(0, 0), mime)
    listing.dropEvent(event_drop)
    assert not event_drop.isAccepted()
    assert listing.ordered_ids() == original and changed.count() == 0


def test_insertion_indicator_stays_above_row_controls_at_both_endpoints(desktop):
    _, make, _ = desktop
    listing = make()
    for point in (QPointF(5, 0), QPointF(5, listing.height() + 10)):
        listing._set_drop(point)
        indicator = listing._indicator
        assert indicator.isVisible() and indicator.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        image = listing.grab().toImage()
        assert image.pixelColor(indicator.geometry().center()) == indicator.grab().toImage().pixelColor(3, 0)
    listing._clear_drop()
    assert not listing._indicator.isVisible()


def test_native_drag_starts_after_threshold_and_carries_only_source_identity(desktop, monkeypatch):
    _, make, _ = desktop
    listing = make()
    row = listing.rows()[0]
    launched = []
    def execute(drag, allowed, default):
        assert isinstance(drag, QDrag)
        assert drag.source() is row
        assert allowed == default == Qt.DropAction.MoveAction
        assert drag.mimeData().formats() == [CALENDAR_ORDER_MIME]
        assert bytes(drag.mimeData().data(CALENDAR_ORDER_MIME)) == b'personal'
        launched.append(drag)
        return Qt.DropAction.IgnoreAction
    monkeypatch.setattr(QDrag, 'exec', execute)
    created = QSignalSpy(row.creation_requested)
    point = row.choice.rect().center()
    QTest.mouseClick(row.choice, Qt.MouseButton.LeftButton, pos=point)
    assert created.count() == 1 and not launched
    QTest.mousePress(row.choice, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(row.choice, point + QPoint(1, 0))
    assert not launched
    QTest.mouseMove(row.choice, point + QPoint(QApplication.startDragDistance() + 2, 0))
    assert len(launched) == 1
    QTest.mouseRelease(row.choice, Qt.MouseButton.LeftButton, pos=point)
    assert created.count() == 1


def test_readonly_name_can_drag_while_lock_eye_and_context_actions_remain_available(desktop, monkeypatch):
    _, make, _ = desktop
    listing = make()
    row = listing.rows()[-1]
    assert not row.choice.isEnabled() and row.lock.isVisible() and row.lock.isEnabled()
    assert 'read only' in row.lock.accessibleName() and 'Read-only' in row.lock.toolTip()
    assert row.choice.property('focusFrame') is False
    created, visibility, context = (QSignalSpy(row.creation_requested), QSignalSpy(row.visibility_requested),
                                    QSignalSpy(row.context_requested))
    launched = []
    monkeypatch.setattr(QDrag, 'exec', lambda drag, *_: launched.append(drag.source()) or Qt.DropAction.IgnoreAction)
    point = row.choice.rect().center()
    QTest.mousePress(row.choice, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(row.choice, point + QPoint(QApplication.startDragDistance() + 2, 0))
    QTest.mouseRelease(row.choice, Qt.MouseButton.LeftButton, pos=point)
    assert launched == [row] and created.count() == 0
    QTest.mouseClick(row.eye, Qt.MouseButton.LeftButton)
    assert visibility.count() == 1 and visibility.at(0) == ['shared', False]
    request = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, row.lock.rect().center(),
                                row.lock.mapToGlobal(row.lock.rect().center()))
    QApplication.sendEvent(row.lock, request)
    assert context.count() == 1 and context.at(0)[0] == 'shared'
    assert launched == [row]


def test_drag_scrolls_long_source_list_and_keeps_slot_geometry_current(desktop):
    app, make, owned = desktop
    listing = make([Calendar(str(index), f'Calendar {index}', '#587c67') for index in range(30)])
    area = QScrollArea()
    owned.append(area)
    area.setWidgetResizable(True)
    area.setWidget(listing)
    area.resize(300, 140)
    area.show()
    app.processEvents()
    row = listing.rows()[0]
    global_point = area.viewport().mapToGlobal(QPoint(30, area.viewport().height() - 3))
    local = listing.mapFromGlobal(global_point)
    event = Enter(row, local)
    listing.dragEnterEvent(event)
    assert event.isAccepted() and listing._drop_slot is not None
    before = area.verticalScrollBar().value()
    listing._scroll_drag()
    assert area.verticalScrollBar().value() > before
    assert listing._drop_slot == listing.slot_at(QPointF(listing.mapFromGlobal(global_point)))
    assert not listing.grab().isNull()
    listing._clear_drop()


def test_workspace_drop_persists_while_selected_source_and_rows_keep_their_identity(desktop, tmp_path):
    from debacal.ui import MainWindow
    app, _, _ = desktop
    path = tmp_path / 'workspace.sqlite'
    with Store(path) as store:
        for calendar in CALENDARS:
            store.save_calendar(calendar)
        window = MainWindow(store, enable_sync=False)
        try:
            window.show()
            app.processEvents()
            QTest.mouseClick(window.calendar_rows['work'].choice, Qt.MouseButton.LeftButton)
            before = dict(window.calendar_rows)
            listing = window.calendar_list
            listing.dropEvent(Drop(window.calendar_rows['shared'], QPoint(10, 0)))
            app.processEvents()
            assert ids(store) == listing.ordered_ids() == ['shared', 'local', 'personal', 'work']
            assert window.calendar_rows == before
            assert store.get_default_calendar().id == window.canvas.creation_calendar.id == 'work'
            selected = window.calendar_rows['work'].choice
            assert selected.isChecked() and selected.font().bold() and selected.font().underline()
            QTest.mouseClick(window.calendar_rows['shared'].choice, Qt.MouseButton.LeftButton)
            assert store.get_default_calendar().id == 'work'
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
    with Store(path) as reopened:
        window = MainWindow(reopened, enable_sync=False)
        try:
            assert window.calendar_list.ordered_ids() == ['shared', 'local', 'personal', 'work']
            assert window.calendar_rows['work'].choice.isChecked()
        finally:
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
