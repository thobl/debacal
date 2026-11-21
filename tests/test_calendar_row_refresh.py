"""Source rows retain native state without redoing unchanged presentation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QButtonGroup, QScrollArea, QVBoxLayout, QWidget

from debacal.calendar_list import CalendarList, CalendarRow
from debacal.icons import icon
from debacal.models import Calendar
from debacal.preferences import Preferences
from debacal.workspace_chrome import WorkspaceChrome


@pytest.fixture
def desktop():
    app = QApplication.instance() or QApplication([])
    owned = []
    yield app, owned
    for widget in reversed(owned):
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def source(index=0, **changes):
    return replace(Calendar(str(index), f'Calendar {index}', '#587c67'), **changes)


def row(desktop, calendar=None):
    app, owned = desktop
    widget = CalendarRow(calendar or source())
    owned.append(widget)
    widget.set_preferences(Preferences())
    widget.resize(280, 40)
    widget.show()
    app.processEvents()
    widget.set_calendar(widget.calendar, current=False)
    return widget


def listing(desktop, calendars):
    app, owned = desktop
    parent = QWidget()
    owned.append(parent)
    layout = QVBoxLayout(parent)
    area = QScrollArea()
    area.setWidgetResizable(True)
    layout.addWidget(area)
    body = CalendarList()
    area.setWidget(body)
    context = SimpleNamespace(
        store=SimpleNamespace(get_default_calendar=lambda: calendars[0]),
        preferences=Preferences(), calendar_rows={}, calendar_list=body,
        calendar_box=body.box, creation_button_group=QButtonGroup(parent), calendar_scroll=area,
        set_creation_calendar=lambda _identity: None,
        toggle_calendar=lambda _identity, _visible: None,
        calendar_menu=lambda _identity, _point: None,
    )
    WorkspaceChrome.refresh_calendar_list(context, calendars)
    parent.resize(300, 260)
    parent.show()
    app.processEvents()
    WorkspaceChrome.refresh_calendar_list(context, calendars)
    return context


def test_unchanged_rows_skip_native_polishing_but_keep_current_action_identity(desktop):
    widget = row(desktop)
    created = []
    widget.creation_requested.connect(created.append)
    with patch.object(widget.swatch, 'setStyleSheet', wraps=widget.swatch.setStyleSheet) as styles, \
            patch('debacal.calendar_list.icon', wraps=icon) as icons:
        for index in range(20):
            latest = replace(widget.calendar, id=f'updated-{index}', server_name=f'Remote label {index}')
            assert not widget.set_calendar(latest)
            assert widget.calendar is latest
        assert styles.call_count == icons.call_count == 0
    widget.choice.click()
    assert created == [latest.id]


def test_refresh_reorder_and_removal_never_borrow_layout_item_wrappers(desktop):
    app, _owned = desktop
    first, second = source(0), source(1)
    context = listing(desktop, [first, second])

    class LayoutAccess:
        # Do not mock Qt's virtual itemAt: indexOf itself calls it natively.
        # Guard Python access without interfering with C++ virtual dispatch.
        def __init__(self, native):
            self.native = native

        def __getattr__(self, name):
            if name == 'itemAt':
                raise AssertionError('Borrowed QWidgetItem wrappers may outlive native row moves/removal')
            return getattr(self.native, name)

    context.calendar_box = LayoutAccess(context.calendar_box)
    context.calendar_list.box = context.calendar_box
    for index in range(20):
        added = source(index + 10)
        WorkspaceChrome.refresh_calendar_list(context, [first, added, second])
        assert context.calendar_list.ordered_ids() == [first.id, added.id, second.id]
        WorkspaceChrome.refresh_calendar_list(context, [second, first, added])
        assert context.calendar_list.ordered_ids() == [second.id, first.id, added.id]
        WorkspaceChrome.refresh_calendar_list(context, [first, second])
        # Removed rows remain QObjects until DeferredDelete; rows() must
        # already exclude them while retaining the native layout order.
        assert context.calendar_list.ordered_ids() == [first.id, second.id]
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_presentation_changes_update_colors_scale_corners_and_readonly_state(desktop):
    widget = row(desktop)
    original = widget.swatch.styleSheet()
    preferences = Preferences()
    with patch.object(widget.swatch, 'setStyleSheet', wraps=widget.swatch.setStyleSheet) as styles:
        widget.set_preferences(replace(preferences, sync_interval_minutes=19, time_format='12h'))
        assert styles.call_count == 0
        widget.set_preferences(replace(preferences, appearance_mode='dark', corner_style='rounded', font_scale=140))
        assert styles.call_count == 1
        assert widget.swatch.styleSheet() != original
        assert widget.swatch.width() == 17
        assert 'border-radius: 6px' in widget.swatch.styleSheet()
        locked = replace(widget.calendar, read_only=True, visible=False, name='Changed source')
        assert widget.set_calendar(locked, current=True, scale=140)
    assert not widget.choice.isEnabled() and not widget.choice.isChecked()
    assert not widget.choice.font().underline()
    assert not widget.lock.isHidden()
    assert widget.eye.isEnabled() and not widget.eye.isChecked()
    assert widget.eye.toolTip() == 'Show events from Changed source'
    assert 'read only' in widget.choice.accessibleName()


def test_font_and_exclusive_button_changes_cannot_leave_stale_selection(desktop):
    first, second = row(desktop, source(1)), row(desktop, source(2))
    group = QButtonGroup(first)
    group.addButton(first.choice)
    group.addButton(second.choice)
    first.set_calendar(first.calendar, current=True)
    second.set_calendar(second.calendar, current=False)
    second.choice.setChecked(True)
    assert not first.choice.isChecked()
    assert first.set_calendar(first.calendar, current=True)
    assert first.choice.isChecked() and not second.choice.isChecked()
    assert first.choice.font().bold() and first.choice.font().underline()
    font = QFont(first.choice.font())
    font.setFamily('DejaVu Serif')
    font.setPointSizeF(font.pointSizeF() + 2)
    first.choice.setFont(font)
    assert first.set_calendar(first.calendar, current=True)
    assert first.choice.font().family() == 'DejaVu Serif'
    assert first.choice.font().bold() and first.choice.font().underline()
    font = first.choice.font()
    font.setWeight(QFont.Weight.DemiBold)
    first.choice.setFont(font)
    assert first.set_calendar(first.calendar, current=True)
    assert first.choice.font().weight() == QFont.Weight.Bold
    first.eye.setChecked(False)
    assert first.set_calendar(first.calendar, current=True)
    assert first.eye.isChecked()


def test_workspace_keeps_unchanged_rows_in_place_and_reorders_only_when_needed(desktop):
    calendars = [source(index) for index in range(4)]
    context = listing(desktop, calendars)
    original = dict(context.calendar_rows)
    first = original['0']
    with patch.object(context.calendar_box, 'insertWidget', wraps=context.calendar_box.insertWidget) as inserts, \
            patch.object(first.swatch, 'setStyleSheet', wraps=first.swatch.setStyleSheet) as styles:
        for _ in range(20):
            WorkspaceChrome.refresh_calendar_list(context, [replace(item, username='updated') for item in calendars])
        assert inserts.call_count == styles.call_count == 0
        reordered = [calendars[-1], *calendars[:-1]]
        WorkspaceChrome.refresh_calendar_list(context, reordered)
        assert inserts.call_count == 1
    assert context.calendar_rows == original
    assert context.calendar_list.ordered_ids() == ['3', '0', '1', '2']
    assert first.choice.isChecked() and first.choice.font().underline()
    WorkspaceChrome.refresh_calendar_list(context, [reordered[0], source(4), reordered[1]])
    assert context.calendar_list.ordered_ids() == ['3', '4', '0']
    assert set(context.calendar_rows) == {'3', '4', '0'}


def test_drag_reordering_moves_one_native_row_and_ignores_noop_drops(desktop):
    context = listing(desktop, [source(index) for index in range(4)])
    with patch.object(context.calendar_box, 'insertWidget', wraps=context.calendar_box.insertWidget) as inserts:
        assert context.calendar_list.move_calendar('3', 0)
        assert inserts.call_count == 1
        assert not context.calendar_list.move_calendar('3', 0)
        assert inserts.call_count == 1
    assert context.calendar_list.ordered_ids() == ['3', '0', '1', '2']
