"""Native desktop workspace. The local store is always the UI's source of truth."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from queue import SimpleQueue, Empty
from collections import deque
from uuid import uuid4
import calendar as monthlib
import math

from PySide6.QtCore import Qt, QDate, QEvent, QTimer, Signal, QMimeData, QKeyCombination
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QPushButton, QLineEdit, QScrollArea, QStackedWidget, QCalendarWidget,
    QCheckBox, QListWidget, QListWidgetItem, QFrame, QMenu, QDialog,
    QDialogButtonBox, QFileDialog, QInputDialog, QTextEdit, QAbstractSpinBox,
    QComboBox, QSizePolicy, QPlainTextEdit, QSystemTrayIcon, QStyle,
)

from .calendar_widgets import CalendarCanvas, MonthCanvas, DayHeader, at
from .dialogs import EventDialog, CalendarDialog, HistoryDialog, ShortcutsDialog, hint
from .models import Event, Calendar
from .store import Store
from .timeutil import now, local_zone
from .preferences import Preferences
from .preferences_dialog import PreferencesDialog
from .conflict_dialog import ConflictDialog
from .event_scope import normalize_scope_modifiers, scope_from_modifiers, SCOPED_ACTIONS

DEFAULT_BINDINGS = {
    'new_event': 'N', 'edit_event': 'E', 'delete_event': 'Del',
    'search': 'Ctrl+F', 'today': 'T', 'previous': 'H', 'next': 'L',
    'day_view': '1', 'week_view': 'W', 'month_view': 'M',
    'select_event': 'G', 'undo': 'Ctrl+Z', 'redo': 'Ctrl+Shift+Z',
    'history': 'Ctrl+H', 'shortcuts': 'F1', 'sync': 'Ctrl+R',
    'edit_single': '', 'preferences': 'Ctrl+,',
    'copy_event': 'C', 'paste_event': 'V', 'duplicate_event': 'D', 'cycle_scope': 'Alt+S',
    'select_left': 'Left', 'select_right': 'Right',
    'select_up': 'Up', 'select_down': 'Down',
}
EVENT_MIME = 'application/x-lebacal-events+json'

STYLE = '''
QWidget { font-family: "Sans Serif"; font-size: 13px; color: #29382f; }
QMainWindow, QDialog { background: #fafbf8; }
QWidget#sidebar { background: #edf0e8; border-right: 1px solid #dce2d5; }
QLabel[muted="true"] { color: #788373; font-size: 12px; }
QLabel#brand { font-size: 25px; font-weight: 700; letter-spacing: -1px; }
QLabel#heading { font-size: 24px; font-weight: 600; }
QLabel#eyebrow { color: #7b8775; font-size: 10px; font-weight: 700; letter-spacing: 2px; }
QPushButton { background: transparent; border: 1px solid #dce2d6; border-radius: 6px; padding: 7px 12px; }
QPushButton:hover { background: #e5ebde; }
QPushButton:pressed, QPushButton:checked { background: #e0e9d8; border-color: #b7c8ac; color: #35513d; }
QPushButton#primary { background: #446d50; color: #fff; border: 1px solid #446d50; font-weight: 600; }
QPushButton#primary:hover { background: #355b40; }
QPushButton:disabled { color: #a5aea0; }
QLineEdit, QTextEdit, QDateTimeEdit, QComboBox, QKeySequenceEdit { background: #fff; border: 1px solid #dce2d6; border-radius: 5px; padding: 6px; selection-background-color: #c9dcc1; }
QLineEdit:focus, QTextEdit:focus { border-color: #719268; }
QListWidget { background: #fafbf8; border: 1px solid #e1e6da; border-radius: 5px; outline: none; }
QListWidget::item { padding: 8px; }
QListWidget::item:selected { color: #2c4835; background: #e2ebda; }
QScrollArea { border: none; background: #fafbf8; }
QScrollBar:vertical { background: #f0f3ec; width: 9px; }
QScrollBar::handle:vertical { background: #cbd4c4; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCalendarWidget QWidget { background: transparent; font-size: 11px; }
QCalendarWidget QToolButton { color: #3b563e; border: none; padding: 2px; }
QCalendarWidget QAbstractItemView { background: #edf0e8; alternate-background-color: #edf0e8; color: #29382f; selection-background-color: #496e50; selection-color: white; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #aabb9e; border-radius: 3px; background: #fafbf8; }
QCheckBox::indicator:checked { background: #65865b; border-color: #65865b; }
QLabel#notice { background: #edf0dc; padding: 9px; color: #626a39; border-radius: 5px; }
QFrame#details { background: #edf2e7; border-top: 1px solid #dce4d4; }
'''


class MainWindow(QMainWindow):
    real_workspace_requested = Signal()

    def __init__(self, store: Store, enable_sync=True, demo_mode=False):
        super().__init__()
        self.store = store
        self._closed = False
        self.demo_mode = demo_mode
        self._sync_running = False
        self._sync_errors = {}
        self._sync_issues = {}
        self._sync_messages = deque(maxlen=100)
        self._last_sync = None
        self._notified_conflicts = {}
        self.tray_icon = None
        self.preferences = Preferences.load(store)
        self.anchor = now().date()
        self.selected_day = self.anchor
        self.view = 'week'
        self.selected_id = None
        self.nav_day = None
        self.nav_digits = None
        self.select_mode = False
        self._search_index = None
        self._revealing_search_result = False
        self._shortcut_pending = []
        self.shortcut_timer = QTimer(self)
        self.shortcut_timer.setSingleShot(True)
        self.shortcut_timer.timeout.connect(self._shortcut_pending.clear)
        saved_bindings = dict(store.get_setting('keybindings', {}))
        if saved_bindings.get('edit_single') == 'Shift+E':
            saved_bindings['edit_single'] = ''
        if saved_bindings.get('day_view') == 'D' and 'duplicate_event' not in saved_bindings:
            saved_bindings['day_view'] = '1'
        if saved_bindings.get('search') == '/':
            saved_bindings['search'] = 'Ctrl+F'
        self.bindings = DEFAULT_BINDINGS | saved_bindings
        try:
            self.scope_modifiers = normalize_scope_modifiers(store.get_setting('event_scope_modifiers', None))
        except (TypeError, ValueError):
            self.scope_modifiers = normalize_scope_modifiers()
        self._matched_scope = 'single'
        self._pending_scope = 'single'
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lebacal-files')
        self.tasks = SimpleQueue()
        self.engine = None
        if enable_sync and store.path != ':memory:':
            from .sync import SyncEngine
            self.engine = SyncEngine(store.path)
        self.setWindowTitle('Lebacal · a little room for your time')
        self.resize(1400, 900)
        self.setMinimumSize(1030, 690)
        self.setStyleSheet(STYLE)
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.build_sidebar(root)
        self.build_workspace(root)
        if not self.engine:
            self.sync_btn.setEnabled(False)
            self.sync_btn.setToolTip('Demo mode is offline. Open your calendars to connect a remote account.' if self.demo_mode else 'Synchronization is disabled for this workspace.')
        self.refresh()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start(250)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_now)
        self.configure_auto_sync()
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.canvas.update)
        self.clock_timer.start(60_000)
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self.scroll_to_morning)
        QTimer.singleShot(1500, self.startup_sync)

    def button(self, title, action, parent_layout=None, primary=False):
        btn = QPushButton(title)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _=False: action())
        if primary:
            btn.setObjectName('primary')
        if parent_layout:
            parent_layout.addWidget(btn)
        return btn

    def build_sidebar(self, root):
        side = QWidget()
        side.setObjectName('sidebar')
        side.setFixedWidth(230)
        root.addWidget(side)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(20, 28, 20, 20)
        layout.setSpacing(13)
        brand = QLabel('lebacal')
        brand.setObjectName('brand')
        layout.addWidget(brand)
        layout.addWidget(hint('A little room for your time.'))
        layout.addSpacing(10)
        self.new_button = self.button('+  New event', self.create_event, layout, True)
        if self.demo_mode:
            self.button('Open my calendars', self.real_workspace_requested.emit, layout)
        self.mini = QCalendarWidget()
        self.mini.setGridVisible(False)
        self.mini.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.mini.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.mini.setFirstDayOfWeek(Qt.DayOfWeek(self.preferences.first_weekday + 1))
        self.mini.setFixedHeight(195)
        self.mini.setMinimumWidth(0)
        self.mini.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.mini.clicked.connect(lambda value: self.go_date(value.toPython()))
        layout.addWidget(self.mini)
        label = QLabel('MY CALENDARS')
        label.setObjectName('eyebrow')
        layout.addWidget(label)
        self.calendar_box = QVBoxLayout()
        self.calendar_box.setSpacing(1)
        layout.addLayout(self.calendar_box)
        self.button('+  Add calendar', self.add_calendar, layout)
        layout.addWidget(hint('Right-click a calendar to edit its source.'))
        layout.addStretch()
        self.button('Import .ics', self.import_ics, layout)
        self.button('Export .ics', self.export_ics, layout)
        self.button('Operation history', self.show_history, layout)
        self.button('Keyboard shortcuts', self.show_shortcuts, layout)
        self.button('Preferences', self.show_preferences, layout)
        layout.addSpacing(8)
        layout.addWidget(hint('LOCAL FIRST\nYour edits are saved here, instantly.'))

    def build_workspace(self, root):
        area = QWidget()
        root.addWidget(area, 1)
        outer = QVBoxLayout(area)
        outer.setContentsMargins(24, 25, 24, 12)
        outer.setSpacing(12)
        eyebrow = QLabel('MAKE TIME FOR WHAT MATTERS')
        eyebrow.setObjectName('eyebrow')
        outer.addWidget(eyebrow)
        top = QHBoxLayout()
        self.heading = QLabel()
        self.heading.setObjectName('heading')
        top.addWidget(self.heading)
        top.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search events   Ctrl+F')
        self.search.setMaximumWidth(250)
        self.search.textChanged.connect(self.run_search)
        top.addWidget(self.search)
        outer.addLayout(top)
        toolbar = QHBoxLayout()
        self.button('‹', lambda: self.navigate(-1), toolbar)
        self.button('›', lambda: self.navigate(1), toolbar)
        self.today_button = self.button('Today', self.today, toolbar)
        self.range_label = hint('')
        self.range_label.setWordWrap(False)
        toolbar.addWidget(self.range_label)
        toolbar.addStretch()
        self.view_buttons = {}
        for mode in ['day', 'week', 'month']:
            btn = self.button(mode.capitalize(), lambda m=mode: self.set_view(m), toolbar)
            btn.setCheckable(True)
            self.view_buttons[mode] = btn
        self.sync_btn = self.button('↻ Sync', self.sync_now, toolbar)
        self.button('Sync status', self.show_sync_status, toolbar)
        self.conflict_btn = self.button('Review conflicts', self.show_conflicts, toolbar)
        self.conflict_btn.hide()
        outer.addLayout(toolbar)
        self.notice = QLabel()
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setObjectName('notice')
        self.notice.setWordWrap(True)
        self.notice.hide()
        outer.addWidget(self.notice)
        content = QHBoxLayout()
        self.stack = QStackedWidget()
        content.addWidget(self.stack, 1)
        week = QWidget()
        week_layout = QVBoxLayout(week)
        week_layout.setContentsMargins(0, 0, 0, 0)
        week_layout.setSpacing(0)
        self.header = DayHeader()
        week_layout.addWidget(self.header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.canvas = CalendarCanvas()
        self.scroll.setWidget(self.canvas)
        week_layout.addWidget(self.scroll, 1)
        self.stack.addWidget(week)
        self.month = MonthCanvas()
        self.month.open_day.connect(self.open_day)
        self.stack.addWidget(self.month)
        for widget in [self.canvas, self.month]:
            widget.create_requested.connect(self.create_event)
            widget.edit_scoped_requested.connect(lambda event_id, modifiers: self.edit_event(event_id, scope=self.operation_scope(modifiers)))
            widget.selected.connect(lambda event_id, source=widget: self.select_event(event_id, day=source.selected_day))
            widget.context_requested.connect(self.event_menu)
        for widget in (self.header, self.canvas, self.month):
            widget.day_selected.connect(self.select_day)
            widget.create_all_day_requested.connect(self.create_all_day_event)
        self.canvas.set_scope_modifiers(self.scope_modifiers)
        self.canvas.move_scoped_requested.connect(lambda reference, start, end, modifiers:
            self.move_event(reference.id, start, end, scope=self.operation_scope(modifiers), reference=reference))
        self.results = QListWidget()
        self.results.setFixedWidth(285)
        self.results.hide()
        self.results.currentItemChanged.connect(self.search_result_changed)
        self.results.itemClicked.connect(self.reveal_search_result)
        self.results.itemActivated.connect(self.open_search_result)
        content.addWidget(self.results)
        outer.addLayout(content, 1)
        self.details = QFrame()
        self.details.setObjectName('details')
        detail_layout = QHBoxLayout(self.details)
        self.detail_text = QLabel()
        self.detail_text.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_text.setWordWrap(True)
        detail_layout.addWidget(self.detail_text, 1)
        self.edit_button = self.button('Edit', self.edit_event, detail_layout)
        self.single_button = self.button('Edit only this event', lambda: self.edit_event(group=False), detail_layout)
        self.single_button.hide()
        self.confirm_btn = self.button('Confirm this slot', self.confirm_candidate, detail_layout, True)
        self.button('Delete', self.delete_event, detail_layout)
        self.details.hide()
        outer.addWidget(self.details)
        self.navigation_prompt = QLabel()
        self.navigation_prompt.setObjectName('notice')
        self.navigation_prompt.setWordWrap(True)
        self.navigation_prompt.hide()
        outer.addWidget(self.navigation_prompt)
        self.scope_hint = hint('')
        outer.addWidget(self.scope_hint)
        self.update_scope_hint()
        footer = QHBoxLayout()
        self.keyboard_hint = hint('')
        footer.addWidget(self.keyboard_hint, 1)
        self.status = hint('Saved on this device')
        footer.addWidget(self.status)
        outer.addLayout(footer)

    def dates(self):
        if self.view == 'day':
            return [self.anchor]
        if self.view == 'week':
            start = self.preferences.week_start(self.anchor)
            return [start + timedelta(days=i) for i in range(7)]
        start = self.anchor.replace(day=1)
        start = self.preferences.week_start(start)
        return [start + timedelta(days=i) for i in range(42)]

    def refresh(self, calendars=False):
        if self._closed:
            return
        if not self._revealing_search_result:
            self._search_index = None
        cals = self.store.list_calendars()
        days = self.dates()
        if self.selected_day not in days:
            self.selected_day = self.anchor
        self.visible_events = self.store.query_occurrences(at(days[0]), at(days[-1] + timedelta(days=1)), [c.id for c in cals if c.visible])
        if self.selected_id:
            selected = next((event for event in self.visible_events if event.id == self.selected_id), None)
            if selected is None:
                self.selected_id = self.canvas.selected_id = self.month.selected_id = None
            elif not any(event.id == selected.id for event in self.events_on_day(self.selected_day)):
                self.selected_day = selected.start.date() if selected.all_day else selected.start.astimezone(local_zone()).date()
        self.heading.setText(self.anchor.strftime('%B %Y'))
        self.range_label.setText(self.preferences.format_date(days[0]) + ' — ' + self.preferences.format_date(days[-1]) if self.view != 'month' else 'A little perspective on the month')
        self.mini.setFirstDayOfWeek(Qt.DayOfWeek(self.preferences.first_weekday + 1))
        self.mini.setSelectedDate(QDate(self.selected_day.year, self.selected_day.month, self.selected_day.day))
        self.header.days = days[:7]
        for widget in (self.header, self.canvas, self.month):
            widget.set_preferences(self.preferences)
            widget.set_selected_day(self.selected_day)
        self.header.update()
        self.canvas.set_data(days[:7], self.visible_events, cals)
        self.month.set_data(days[0], self.anchor.month, self.visible_events, cals)
        self.stack.setCurrentIndex(1 if self.view == 'month' else 0)
        for name, btn in self.view_buttons.items():
            btn.setChecked(name == self.view)
        if calendars or tuple(cals) != getattr(self, '_shown_calendars', ()):
            self._shown_calendars = tuple(cals)
            default_calendar = self.store.get_default_calendar()
            while self.calendar_box.count():
                item = self.calendar_box.takeAt(0)
                item.widget().deleteLater()
            for cal in cals:
                cb = QCheckBox(cal.name.replace('&', '&&') + ('  ★' if default_calendar and cal.id == default_calendar.id else '') + ('  ↗' if cal.kind != 'local' else ''))
                cb.setStyleSheet(f'QCheckBox::indicator:checked {{ background: {cal.color}; border-color: {cal.color}; }}')
                cb.setChecked(cal.visible)
                cb.setToolTip(cal.kind + (' · ' + cal.url if cal.url else '') + '\nRight-click to edit source')
                cb.toggled.connect(lambda checked, cid=cal.id: self.toggle_calendar(cid, checked))
                cb.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                cb.customContextMenuRequested.connect(lambda _, c=cal: self.add_calendar(c))
                self.calendar_box.addWidget(cb)
        self.update_details()
        self.update_hints()
        if self.search.text() and not self._revealing_search_result:
            self.run_search()
        self.update_sync_status()

    def update_hints(self):
        def key(action):
            return self.bindings.get(action) or '—'
        self.new_button.setText('+  New event   ' + key('new_event'))
        self.new_button.setToolTip(key('new_event'))
        self.search.setPlaceholderText('Search events   ' + key('search'))
        self.navigation_prompt.setVisible(self.select_mode)
        if self.select_mode:
            if self.nav_day is None:
                message = f'Select an event · choose a day (1–{len(self.canvas.days)}), or use the arrow keys. Esc cancels.'
            else:
                day = self.canvas.days[self.nav_day]
                options = self.events_on_day(day)
                index = int(self.nav_digits or '0') - 1
                target = options[index].title if 0 <= index < len(options) else ('No matching event' if self.nav_digits else f'{len(options)} events')
                message = f'{self.preferences.format_date(day, weekday=True)} → event {self.nav_digits or "number"} · {target} · Enter selects · Backspace goes back · Esc cancels'
            self.keyboard_hint.setText('Enter confirms the highlighted event · Esc cancels selection')
            self.navigation_prompt.setText(message)
        else:
            self.keyboard_hint.setText(f'{key("new_event")} new · {key("edit_event")} edit · '
                                      f'{key("copy_event")}/{key("paste_event")}/{key("duplicate_event")} copy/paste/duplicate · '
                                      f'{key("select_event")} select · {key("previous")}/{key("next")} navigate · {key("shortcuts")} shortcuts')
        self.header.set_navigation(self.select_mode, self.nav_day, self.nav_digits or '')
        self.canvas.set_navigation(self.select_mode, self.nav_day, self.nav_digits or '')

    def events_on_day(self, day):
        from .recurrence import overlaps
        return [e for e in self.visible_events if overlaps(e, at(day), at(day + timedelta(days=1)))]

    def show_preferences(self):
        default = self.store.get_default_calendar()
        dialog = PreferencesDialog(self.preferences, self, calendars=self.store.list_calendars(), default_calendar_id=default.id if default else None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.preferences = dialog.result_preferences
            self.preferences.save(self.store)
            if dialog.result_default_calendar_id:
                self.store.set_default_calendar(dialog.result_default_calendar_id)
            self.select_mode = False
            self.configure_auto_sync()
            self.refresh(calendars=True)

    def configure_auto_sync(self):
        if self.engine and not self.demo_mode and self.preferences.auto_sync_enabled:
            self.sync_timer.start(self.preferences.sync_interval_minutes * 60_000)
        else:
            self.sync_timer.stop()

    def startup_sync(self):
        if not self._closed and self.engine and not self.demo_mode and self.preferences.auto_sync_enabled:
            self.sync_now()

    def scroll_to_morning(self):
        if not self._closed:
            self.scroll.verticalScrollBar().setValue(7 * 64)

    def toggle_calendar(self, cid, checked):
        self.store.set_calendar_visible(cid, checked)
        self.refresh()

    def set_view(self, view):
        self.anchor = self.selected_day
        self.view = view
        self.select_mode = False
        self.refresh()

    def go_date(self, day):
        self.select_day(day, reveal=True)

    def select_day(self, day, reveal=False, clear_event=True):
        self.selected_day = day
        if clear_event:
            self.selected_id = self.canvas.selected_id = self.month.selected_id = None
            self.select_mode = False
            self.nav_day, self.nav_digits = None, ''
            self._shortcut_pending.clear()
            self.shortcut_timer.stop()
        if reveal:
            self.anchor = day
            self.refresh()
        else:
            self.mini.setSelectedDate(QDate(day.year, day.month, day.day))
            for widget in (self.header, self.canvas, self.month):
                widget.set_selected_day(day)
            self.update_details()
            self.update_hints()

    def open_day(self, day):
        self.select_day(day)
        self.set_view('day')

    def today(self):
        self.go_date(now().date())

    def navigate(self, direction):
        if self.view == 'month':
            value = self.anchor.year * 12 + self.anchor.month - 1 + direction
            year, month = divmod(value, 12)
            self.anchor = self.anchor.replace(year=year, month=month + 1, day=min(self.anchor.day, monthlib.monthrange(year, month + 1)[1]))
            self.selected_day = self.anchor.replace(day=min(self.selected_day.day, monthlib.monthrange(year, month + 1)[1]))
        else:
            shift = timedelta(days=direction * (7 if self.view == 'week' else 1))
            self.anchor += shift
            self.selected_day += shift
        self.refresh()

    def show_notice(self, message):
        self.notice.setText(message)
        self.notice.setVisible(bool(message))

    def create_event(self, start=None, end=None, all_day=False):
        if start is None:
            start = at(self.selected_day, 0 if all_day else min(now().hour + 1, 23))
        if all_day:
            end = end or start + timedelta(days=1)
        default = self.store.get_default_calendar()
        dialog = EventDialog(self.store.list_calendars(), start, end, parent=self, preferences=self.preferences, default_calendar_id=default.id if default else None)
        dialog.all_day.setChecked(all_day)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if self.apply_events('Create ' + str(len(dialog.result_events)) + ' event(s)', dialog.result_events):
                for event in dialog.result_events:
                    calendar = self.store.get_calendar(event.calendar_id)
                    if calendar and not calendar.visible:
                        self.store.set_calendar_visible(calendar.id, True)
                self.refresh(calendars=True)

    def create_all_day_event(self, start=None, end=None):
        self.create_event(start, end, all_day=True)

    def apply_events(self, label, events=(), delete_ids=()):
        try:
            self.store.apply(label, upserts=events, delete_ids=delete_ids)
            self.show_notice('')
            self.refresh()
            return True
        except ValueError as exc:
            self.show_notice(str(exc))
            return False

    def event_by_id(self, event_id):
        event = self.store.get_occurrence(event_id) if event_id else None
        if event and not event.series_id and event.recurring:
            from .recurrence import expand_event
            start = now()
            occurrences = expand_event(event, start, start + timedelta(days=732))
            if not occurrences:
                occurrences = expand_event(event, event.start, event.start + timedelta(days=1))
            return occurrences[0] if occurrences else None
        return event

    def operation_scope(self, modifiers=None):
        return scope_from_modifiers(QApplication.keyboardModifiers() if modifiers is None else modifiers,
                                    self.scope_modifiers)

    def update_scope_hint(self, modifiers=None):
        modifiers = QApplication.keyboardModifiers() if modifiers is None else modifiers
        scope = self.operation_scope(modifiers)
        label = {'single': 'One event', 'group': 'All events', 'future': 'This and following'}[scope]
        cycle = self.bindings.get('cycle_scope', '')
        self.scope_hint.setText(f"Scope: {label} · {self.scope_modifiers['all']}: all · "
                               f"{self.scope_modifiers['following']}: this and following"
                               + (f" · {cycle}: change scope in the editor" if cycle else ''))
        if hasattr(self, 'canvas'):
            self.canvas.gesture_modifiers = modifiers
            if self.canvas.drag:
                self.canvas.update()

    def scoped_members(self, event, scope):
        if event.series_id:
            return [self.store.get_event(event.series_id) or event]
        members = self.group_members(event) if scope != 'single' else [event]
        if scope == 'future':
            members = [member for member in members if member.start >= event.start]
        return members

    def select_series_result(self, series, target, previous_ids=None):
        resources = {series.id}
        if previous_ids is not None:
            current_ids = {event.id for event in self.store.list_events(calendar_ids=[series.calendar_id])}
            resources = current_ids - previous_ids or resources
        self.selected_id = None
        self.refresh()
        expected = target.astimezone(timezone.utc)
        selected = next((event for event in self.visible_events
                         if (event.series_id or event.id) in resources
                         and event.start.astimezone(timezone.utc) == expected), None)
        if selected:
            self.select_event(selected.id)

    def edit_event(self, event_id=None, group=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if not event:
            self.show_notice('Select an event to edit. Use the select shortcut or click an event.')
            return
        series = self.store.get_event(event.series_id) if event.series_id else None
        linked = bool(event.group_id or series)
        if scope is None:
            scope = ('group' if group else 'single') if group is not None else self.operation_scope()
        members = self.scoped_members(event, scope)
        if not self.event_writable(event) or any(not self.event_writable(member) for member in members):
            self.show_notice('This event is read only. Export it or copy it into a local calendar to make changes.')
            return
        try:
            dialog = EventDialog(self.store.list_calendars(), event.start, event.end, event, self,
                                 preferences=self.preferences, edit_scope=scope, series=series,
                                 scope_shortcut=self.bindings.get('cycle_scope', ''), scope_modifiers=self.scope_modifiers)
        except ValueError as exc:
            self.show_notice(str(exc))
            return
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.update_scope_hint()
            return
        self.update_scope_hint()
        edited = dialog.result_events[0]
        if linked:
            scope = dialog.scope.currentData()
            members = self.scoped_members(event, scope)
            if any(not self.event_writable(member) for member in members):
                self.show_notice('This group contains a read-only event. Choose single-event editing for its writable members.')
                return
        if series:
            try:
                previous_ids = ({item.id for item in self.store.list_events(calendar_ids=[series.calendar_id])}
                                if scope == 'future' else None)
                if scope == 'group':
                    self.store.edit_series(dialog.edit_reference, edited, replace_schedule=dialog.schedule_replacement)
                elif scope == 'future':
                    self.store.edit_future(event, edited, replace_schedule=dialog.schedule_replacement, expected_master=series)
                else:
                    self.store.edit_occurrence(event, edited)
                reference = dialog.edit_reference
                shift = edited.start.astimezone(local_zone()).replace(tzinfo=None) - reference.start.astimezone(local_zone()).replace(tzinfo=None)
                target = (event.start.astimezone(local_zone()).replace(tzinfo=None) + shift).replace(tzinfo=local_zone(), fold=event.start.fold)
                self.select_series_result(series, target, previous_ids)
                self.show_notice({'group': 'Updated all events in the series.',
                                  'future': 'Updated this and following events; earlier events are retained.',
                                  'single': 'Updated only this occurrence.'}[scope])
            except ValueError as exc:
                self.show_notice(str(exc))
            return
        events = [edited]
        if scope != 'single' and event.group_id:
            # Shift wall-clock times in the system zone, preserving the chosen
            # hour across daylight-saving transitions in an irregular series.
            zone = local_zone()
            wall_start = edited.start.astimezone(zone).replace(tzinfo=None)
            shift = wall_start - event.start.astimezone(zone).replace(tzinfo=None)
            duration = edited.end.astimezone(zone).replace(tzinfo=None) - wall_start
            try:
                events = []
                for member in members:
                    start = (member.start.astimezone(zone).replace(tzinfo=None) + shift).replace(tzinfo=zone)
                    end = (start.replace(tzinfo=None) + duration).replace(tzinfo=zone)
                    EventDialog.validate_time(start)
                    EventDialog.validate_time(end)
                    events.append(replace(member, title=edited.title, description=edited.description,
                                          location=edited.location, start=start, end=end, all_day=edited.all_day))
            except ValueError as exc:
                self.show_notice(str(exc))
                return
        self.apply_events('Edit following events' if scope == 'future' else 'Edit group' if scope == 'group' else 'Edit ' + event.title, events)

    def event_writable(self, event):
        calendar = self.store.get_calendar(event.calendar_id)
        return bool(calendar and calendar.kind != 'ics' and not calendar.read_only and not event.read_only)

    def group_members(self, event):
        if not event.group_id:
            return [event]
        return [member for member in self.store.list_events(calendar_ids=[event.calendar_id])
                if member.group_id == event.group_id]

    def select_event(self, event_id, day=None):
        self.selected_id = event_id
        self.canvas.selected_id = self.month.selected_id = event_id
        event = next((value for value in self.visible_events if value.id == event_id), None) or self.event_by_id(event_id)
        if event:
            day = day or (event.start.date() if event.all_day else event.start.astimezone(local_zone()).date())
            self.select_day(day, clear_event=False)
        self.canvas.update()
        self.month.update()
        self.update_details()

    def ensure_event_visible(self, event_id):
        """Keep the current scroll unless the selected event is outside it."""
        if self.view == 'month':
            return
        self.details.parentWidget().layout().activate()
        self.scroll.parentWidget().layout().activate()
        rects = [rect for rect, event, _ in self.canvas.rects() if event.id == event_id]
        if not rects:
            return
        bar = self.scroll.verticalScrollBar()
        top, height = bar.value(), self.scroll.viewport().height()
        rect = min(rects, key=lambda value: abs(value.top() - top))
        margin = 8
        if rect.top() < top + margin:
            bar.setValue(max(0, math.floor(rect.top() - margin)))
        elif rect.bottom() > top + height - margin:
            bar.setValue(math.ceil(rect.bottom() - height + margin))

    def update_details(self):
        event = self.event_by_id(self.selected_id or '')
        visible = event is not None and any(e.id == event.id for e in self.visible_events)
        self.details.setVisible(visible)
        if visible:
            local = event.start.astimezone(local_zone())
            value = self.preferences.format_date(local.date(), weekday=True) + ' · ' + self.preferences.format_time(local)
            self.detail_text.setText(event.title + '   ·   ' + value + ('   ·   ' + event.location if event.location else ''))
            self.confirm_btn.setVisible(event.candidate)
            linked = bool(event.group_id or event.series_id)
            self.edit_button.setText('Edit')
            self.edit_button.setToolTip(f"{self.bindings['edit_event']}: one event; hold {self.scope_modifiers['all']} for all, "
                                       f"{self.scope_modifiers['following']} for this and following.")
            self.single_button.hide()

    def move_event(self, event_id, start, end, scope=None, reference=None):
        event = reference or self.event_by_id(event_id)
        if event:
            scope = scope or self.operation_scope()
            members = self.scoped_members(event, scope)
            if not self.event_writable(event) or any(not self.event_writable(member) for member in members):
                self.show_notice('This selection contains a read-only event.')
                return
            try:
                EventDialog.validate_time(start)
                EventDialog.validate_time(end)
                moved = replace(event, start=start, end=end)
            except ValueError as exc:
                self.show_notice(str(exc))
                return
            if event.series_id:
                try:
                    master = self.store.get_event(event.series_id)
                    if master is None:
                        raise ValueError('This series changed. Refresh before moving it.')
                    previous_ids = ({item.id for item in self.store.list_events(calendar_ids=[master.calendar_id])}
                                    if scope == 'future' else None)
                    if scope == 'group':
                        edited = replace(master, start=start, end=end)
                        self.store.edit_series(event, edited, label='Move all events in series', preserve_exceptions=True)
                    elif scope == 'future':
                        edited = replace(master, start=start, end=end)
                        self.store.edit_future(event, edited, label='Move this and following events',
                                               expected_master=master, preserve_exceptions=True)
                    else:
                        self.store.edit_occurrence(event, moved, label='Move one occurrence')
                    self.select_series_result(master, start, previous_ids)
                    self.show_notice({'single': 'Moved one event.', 'group': 'Moved all events in the series.',
                                      'future': 'Moved this and following events; earlier events are retained.'}[scope])
                except ValueError as exc:
                    self.show_notice(str(exc))
            else:
                if scope != 'single' and event.group_id:
                    shift = start.astimezone(local_zone()).replace(tzinfo=None) - event.start.astimezone(local_zone()).replace(tzinfo=None)
                    duration = end.astimezone(local_zone()).replace(tzinfo=None) - start.astimezone(local_zone()).replace(tzinfo=None)
                    try:
                        moved_events = []
                        for member in members:
                            begin = (member.start.astimezone(local_zone()).replace(tzinfo=None) + shift).replace(tzinfo=local_zone())
                            finish = begin + duration
                            EventDialog.validate_time(begin)
                            EventDialog.validate_time(finish)
                            moved_events.append(replace(member, start=begin, end=finish))
                        self.apply_events('Move selected group events', moved_events)
                    except ValueError as exc:
                        self.show_notice(str(exc))
                else:
                    self.apply_events('Move ' + event.title, [moved])

    def delete_event(self, event_id=None, group=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if not event:
            return
        if scope is None:
            scope = ('group' if group else 'single') if group is not None else self.operation_scope()
        members = self.scoped_members(event, scope)
        if not self.event_writable(event) or any(not self.event_writable(member) for member in members):
            self.show_notice('This selection contains a read-only event.')
            return
        if event.series_id:
            try:
                if scope == 'group':
                    self.store.apply('Delete recurring series', delete_ids=[event.series_id])
                elif scope == 'future':
                    self.store.delete_future(event, expected_master=self.store.get_event(event.series_id))
                else:
                    self.store.delete_occurrence(event)
                self.refresh()
                self.show_notice({'single': 'Deleted one event.', 'group': 'Deleted all events in the series.',
                                  'future': 'Deleted this and following events.'}[scope] + ' Undo restores them.')
            except ValueError as exc:
                self.show_notice(str(exc))
            return
        ids = [member.id for member in members]
        if self.apply_events('Delete following events' if scope == 'future' else 'Delete group' if scope == 'group' else 'Delete ' + event.title, delete_ids=ids):
            self.show_notice({'single': 'Deleted one event.', 'group': 'Deleted all group events.',
                              'future': 'Deleted this and following events.'}[scope] + ' Undo restores them.')

    def capture_events(self, event, scope):
        from .event_clipboard import EventClipboard
        if event.series_id:
            master = self.store.get_event(event.series_id)
            if master is None:
                raise ValueError('This event changed before it could be copied. Select it again.')
            masters = [master]
        else:
            masters = self.group_members(event) if scope != 'single' else [event]
        return EventClipboard.capture(masters, event, scope)

    def copy_event(self, event_id=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if event is None:
            self.show_notice('Select an event to copy.')
            return
        scope = scope or self.operation_scope()
        try:
            from .ics import export_ics
            selection = self.capture_events(event, scope)
            mime = QMimeData()
            mime.setData(EVENT_MIME, selection.to_bytes())
            portable = selection.instantiate('group', selection.reference.start, selection.masters[0].calendar_id)
            data = export_ics(portable)
            mime.setData('text/calendar', data)
            mime.setText(data.decode('utf-8'))
            QApplication.clipboard().setMimeData(mime)
            paste = self.bindings.get('paste_event', '')
            self.show_notice({'single': f'Copied one event. {paste or "Paste"} pastes it.',
                              'group': f"Copied all events. {self.scope_modifiers['all']}+{paste} pastes the whole selection." if paste else 'Copied all events.',
                              'future': f"Copied this and following events. {self.scope_modifiers['following']}+{paste} pastes that selection." if paste else 'Copied this and following events.'}[scope])
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def read_event_clipboard(self):
        from .event_clipboard import EventClipboard, MAX_CLIPBOARD_BYTES
        from .ics import parse_ics
        from .recurrence import recurrence_set, occurrence
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            raise ValueError('Copy a calendar event before pasting.')
        if mime.hasFormat(EVENT_MIME):
            return EventClipboard.from_bytes(bytes(mime.data(EVENT_MIME)))
        data = bytes(mime.data('text/calendar')) if mime.hasFormat('text/calendar') else mime.text().encode('utf-8')
        if len(data) > MAX_CLIPBOARD_BYTES or b'BEGIN:VCALENDAR' not in data.upper():
            raise ValueError('The clipboard does not contain a calendar event.')
        masters = parse_ics(data, 'clipboard')
        if not masters:
            raise ValueError('The clipboard does not contain an event.')
        first = masters[0]
        if first.recurring:
            try:
                reference = occurrence(first, next(iter(recurrence_set(first))))
            except StopIteration as exc:
                raise ValueError('The copied series contains no occurrences.') from exc
        else:
            reference = first
        return EventClipboard.capture(masters, reference, 'group')

    def paste_target(self, selection):
        source = selection.reference.start
        if not selection.reference.all_day:
            source = source.astimezone(local_zone())
        day = self.selected_day
        return source.replace(year=day.year, month=day.month, day=day.day)

    def persist_copies(self, events, target, label):
        if self.apply_events(label, events):
            self.anchor = self.selected_day = (target.date() if events[0].all_day else target.astimezone(local_zone()).date())
            self.selected_id = None
            self.refresh()
            ids = {event.id for event in events}
            choices = [event for event in self.visible_events if (event.series_id or event.id) in ids]
            if choices:
                selected = min(choices, key=lambda event: abs(event.start.timestamp() - target.timestamp()))
                self.select_event(selected.id)
                self.ensure_event_visible(selected.id)
            self.show_notice(label + '. Undo restores the previous calendar.')

    def paste_events(self, scope=None, target=None):
        scope = scope or self.operation_scope()
        try:
            selection = self.read_event_clipboard()
            calendar = self.store.get_default_calendar()
            if calendar is None or calendar.read_only or calendar.kind == 'ics':
                raise ValueError('Choose a writable default calendar before pasting events.')
            target = target or self.paste_target(selection)
            EventDialog.validate_time(target)
            events = selection.instantiate(scope, target, calendar.id)
            self.persist_copies(events, target, 'Paste ' + {'single': 'one event', 'group': 'all copied events', 'future': 'following copied events'}[scope])
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def duplicate_event(self, event_id=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if event is None:
            self.show_notice('Select an event to duplicate.')
            return
        scope = scope or self.operation_scope()
        try:
            selection = self.capture_events(event, scope)
            calendar = self.store.get_calendar(event.calendar_id)
            if calendar is None or calendar.read_only or calendar.kind == 'ics':
                calendar = self.store.get_default_calendar()
            if calendar is None or calendar.read_only or calendar.kind == 'ics':
                raise ValueError('Choose a writable default calendar before duplicating events.')
            events = selection.instantiate('group', event.start, calendar.id)
            self.persist_copies(events, event.start, 'Duplicate ' + {'single': 'one event', 'group': 'all events', 'future': 'this and following events'}[scope])
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def confirm_candidate(self, event_id=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if not event or not event.candidate:
            return
        try:
            self.store.confirm_candidate(event)
            self.selected_id = event.series_id or event.id
            self.refresh()
            self.select_event(self.selected_id)
            self.show_notice('Confirmed this slot. All other candidate choices were removed; undo restores them.')
        except ValueError as exc:
            self.show_notice(str(exc))

    def event_menu(self, event_id, point):
        self.select_event(event_id, day=self.selected_day if self.selected_id == event_id else None)
        event = self.event_by_id(event_id)
        if event is None:
            return
        menu = QMenu(self)
        menu.addAction('Edit this event', lambda: self.edit_event(event_id, scope='single'))
        menu.addAction('Delete this event', lambda: self.delete_event(event_id, scope='single'))
        menu.addAction('Copy this event', lambda: self.copy_event(event_id, scope='single'))
        menu.addAction('Duplicate this event', lambda: self.duplicate_event(event_id, scope='single'))
        if event.group_id or event.series_id:
            menu.addSeparator()
            for label, scope in [('All events', 'group'), ('This and following', 'future')]:
                submenu = menu.addMenu(label)
                submenu.addAction('Edit…', lambda checked=False, value=scope: self.edit_event(event_id, scope=value))
                submenu.addAction('Delete', lambda checked=False, value=scope: self.delete_event(event_id, scope=value))
                submenu.addAction('Copy', lambda checked=False, value=scope: self.copy_event(event_id, scope=value))
                submenu.addAction('Duplicate', lambda checked=False, value=scope: self.duplicate_event(event_id, scope=value))
        menu.addSeparator()
        menu.addAction('Paste one event here', lambda: self.paste_events(scope='single', target=event.start))
        menu.addAction('Paste all copied events here', lambda: self.paste_events(scope='group', target=event.start))
        menu.addAction('Paste following copied events here', lambda: self.paste_events(scope='future', target=event.start))
        if event.candidate:
            menu.addAction('Confirm this slot; remove alternatives', lambda: self.confirm_candidate(event_id))
        try:
            menu.exec(point)
        finally:
            menu.deleteLater()

    def undo(self):
        try:
            result = self.store.undo()
            self.show_notice('Undone. Redo is available.' if result else 'Nothing to undo.')
            self.refresh()
        except ValueError as exc:
            self.show_notice(str(exc))

    def redo(self):
        try:
            result = self.store.redo()
            self.show_notice('Redone.' if result else 'Nothing to redo.')
            self.refresh()
        except ValueError as exc:
            self.show_notice(str(exc))

    def show_history(self):
        HistoryDialog(self.store, self.refresh, self).exec()

    def show_shortcuts(self):
        dialog = ShortcutsDialog(self.bindings, DEFAULT_BINDINGS, self, scope_modifiers=self.scope_modifiers)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.bindings = dialog.result_bindings
            self.scope_modifiers = dialog.result_scope_modifiers
            self.store.set_setting('keybindings', self.bindings)
            self.store.set_setting('event_scope_modifiers', self.scope_modifiers)
            self.canvas.set_scope_modifiers(self.scope_modifiers)
            self.update_scope_hint()
            self.update_hints()

    def run_search(self, *_):
        if self._revealing_search_result:
            return
        query = self.search.text().strip().casefold()
        self.results.setVisible(bool(query))
        current = self.results.currentItem()
        previous_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        blocked = self.results.blockSignals(True)
        try:
            self.results.clear()
            if not query:
                return
            if self._search_index is None:
                cals = {c.id: c for c in self.store.list_calendars()}
                searchable_events = []
                for master in self.store.list_events():
                    searchable_events.append(master)
                    searchable_events.extend(Event.from_dict(payload) for key, payload in master.recurrence_overrides.items() if key not in master.excluded_dates)
                self._search_index = [
                    (event, (event.title + ' ' + event.description + ' ' + event.location).casefold(), cals[event.calendar_id].name)
                    for event in searchable_events
                ]
            matches, current_row = 0, -1
            for event, searchable, calendar_name in self._search_index:
                if query not in searchable:
                    continue
                matches += 1
                if matches > 200:
                    break
                local = event.start.astimezone(local_zone())
                when = self.preferences.format_date(local.date(), weekday=True) + ' · ' + self.preferences.format_time(local)
                if event.recurring:
                    when += ' · repeating'
                item = QListWidgetItem(event.title + '\n' + when + '\n' + calendar_name)
                item.setData(Qt.ItemDataRole.UserRole, event.id)
                self.results.addItem(item)
                if event.id == previous_id:
                    current_row = self.results.count() - 1
            if not matches or matches > 200:
                item = QListWidgetItem('No matching events' if not matches else 'First 200 results · refine your search')
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
                self.results.addItem(item)
            self.results.setCurrentRow(current_row)
        finally:
            self.results.blockSignals(blocked)

    def focus_search(self):
        self._shortcut_pending.clear()
        self.shortcut_timer.stop()
        self.select_mode = False
        self.search.setFocus()
        self.search.selectAll()
        self.update_hints()

    def search_result_changed(self, current, _previous):
        if current is not None:
            self.reveal_search_result(current)

    def cycle_search_result(self, direction):
        rows = [row for row in range(self.results.count())
                if self.results.item(row).data(Qt.ItemDataRole.UserRole)]
        if not rows:
            return
        current = self.results.currentRow()
        index = rows.index(current) if current in rows else (-1 if direction > 0 else 0)
        row = rows[(index + direction) % len(rows)]
        if row == current:
            self.reveal_search_result(self.results.item(row))
        else:
            self.results.setCurrentRow(row)
        self.results.scrollToItem(self.results.currentItem())

    def search_result_event(self, event_id):
        master = self.store.get_occurrence(event_id)
        if master is None or master.series_id or not master.recurring:
            return master
        visible = [event for event in self.visible_events if event.series_id == master.id]
        if visible:
            return min(visible, key=lambda event: event.start.timestamp())
        event = self.event_by_id(event_id)
        if event is not None:
            return event
        # A finished series can have an excluded DTSTART. Its next retained
        # historical occurrence still needs to be reachable from search.
        from .recurrence import recurrence_set, occurrence
        rules = recurrence_set(master)
        start = rules.after(master.start, inc=True) or rules.before(master.start, inc=True)
        return occurrence(master, start) if start is not None else None

    def reveal_search_result(self, item):
        event_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not event_id or self._revealing_search_result:
            return False
        try:
            event = self.search_result_event(event_id)
        except (ValueError, OverflowError) as exc:
            self.show_notice(str(exc))
            return False
        if event is None:
            return False
        cal = self.store.get_calendar(event.calendar_id)
        if cal is None:
            return False
        focus = QApplication.focusWidget()
        day = event.start.date() if event.all_day else event.start.astimezone(local_zone()).date()
        self._revealing_search_result = True
        try:
            if not cal.visible:
                self.store.set_calendar_visible(cal.id, True)
            visible = any(value.id == event.id for value in self.visible_events)
            if (not cal.visible or not visible or day not in self.dates()
                    or (self.view == 'month' and (day.year, day.month) != (self.anchor.year, self.anchor.month))):
                self.anchor = day
                self.refresh(calendars=not cal.visible)
            self.select_event(event.id)
            self.ensure_event_visible(event.id)
        finally:
            self._revealing_search_result = False
        if focus in (self.search, self.results):
            focus.setFocus()
        return True

    def open_search_result(self, item):
        if self.reveal_search_result(item):
            (self.month if self.view == 'month' else self.canvas).setFocus()

    def exit_search(self):
        self.search.clear()
        self.results.hide()
        (self.month if self.view == 'month' else self.canvas).setFocus()

    def add_calendar(self, calendar=None):
        default = self.store.get_default_calendar()
        dialog = CalendarDialog(calendar, self, allow_remote=not self.demo_mode,
            credentials=self.engine.credentials if self.engine else None, default_calendar_id=default.id if default else None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                password_set = False
                chosen_default = None
                credential_save = None
                for calendar in dialog.result_calendars:
                    # Discovery can be repeated without adding duplicate sources.
                    existing = next((c for c in self.store.list_calendars() if c.kind == calendar.kind and c.url and c.url == calendar.url and c.username == calendar.username), None)
                    if existing and existing.id != calendar.id:
                        calendar = replace(calendar, id=existing.id, visible=existing.visible,
                            use_server_color=existing.use_server_color, color=existing.color)
                    self.store.save_calendar(calendar)
                    if chosen_default is None and calendar.kind != 'ics' and not calendar.read_only:
                        chosen_default = calendar.id
                    if self.engine and dialog.password.text() and not password_set:
                        credential_save = self.engine.set_password(calendar.id, dialog.password.text(), remember=dialog.remember_password.isChecked())
                        password_set = True
                if dialog.make_default.isChecked() and chosen_default:
                    self.store.set_default_calendar(chosen_default)
                self.refresh(calendars=True)
                if dialog.result_calendar.kind != 'local':
                    if credential_save is not None:
                        engine = self.engine
                        credential_save.add_done_callback(lambda _: engine.sync_now())
                    self.sync_now()
            except ValueError as exc:
                self.show_notice(str(exc))

    def choose_calendar(self, writable=False):
        calendars = [c for c in self.store.list_calendars() if not writable or (c.kind != 'ics' and not c.read_only)]
        if not calendars:
            self.show_notice('Add a writable calendar first.')
            return None
        labels = [f'{c.name} ({c.kind})' for c in calendars]
        label, ok = QInputDialog.getItem(self, 'Choose calendar', 'Calendar', labels, 0, False)
        return calendars[labels.index(label)] if ok else None

    def import_ics(self):
        calendar = self.choose_calendar(writable=True)
        if not calendar:
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Import calendar', '', 'Calendar files (*.ics);;All files (*)')
        if not path:
            return
        self.show_notice('Reading calendar in the background…')
        def work():
            from .ics import parse_ics_report
            if Path(path).stat().st_size > 20_000_000:
                raise ValueError('Choose a calendar file smaller than 20 MB.')
            return parse_ics_report(Path(path).read_bytes(), calendar.id)
        future = self.executor.submit(work)
        future.calendar_id = calendar.id
        future.add_done_callback(lambda result: self.tasks.put(('import', result)))

    def export_ics(self):
        calendar = self.choose_calendar()
        if not calendar:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Export calendar', calendar.name + '.ics', 'Calendar files (*.ics)')
        if not path:
            return
        try:
            from .ics import export_ics
            Path(path).write_bytes(export_ics(self.store.list_events(calendar_ids=[calendar.id])))
            self.show_notice('Calendar exported to ' + path)
        except (OSError, ValueError, ImportError) as exc:
            self.show_notice(str(exc))

    def sync_now(self):
        if self._closed:
            return
        if not self.engine:
            self.show_notice('Demo mode is offline. Open my calendars to connect and synchronize Nextcloud.' if self.demo_mode else 'Synchronization is disabled for this workspace.')
            return
        if not any(calendar.kind != 'local' for calendar in self.store.list_calendars()):
            self.show_notice('No remote calendars configured. Add a CalDAV account or ICS subscription first.')
            return
        self._sync_running = True
        self.update_sync_status()
        self.engine.sync_now()

    def update_sync_status(self):
        pending = len(self.store.pending_changes())
        self.sync_btn.setText('Syncing…' if self._sync_running else '↻ Sync')
        if self.demo_mode:
            value = 'Demo · offline · changes are temporary'
        elif self._sync_running:
            value = 'Syncing in background…'
        elif self._sync_errors:
            value = 'Sync failed · open Sync status'
        elif self.engine and self.engine.conflicts:
            value = f'{len(self.engine.conflicts)} conflict(s) need review'
        elif self._sync_issues:
            value = 'Synced with resource issues · open Sync status'
        elif self._last_sync:
            value = 'Last synced ' + self.preferences.format_time(self._last_sync)
        else:
            value = 'Saved locally'
        if pending:
            value += f' · {pending} change(s) waiting'
        self.status.setText(value)
        self.status.setToolTip('\n'.join([*self._sync_errors.values(), *self._sync_issues.values()]) or value)

    def show_sync_status(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Calendar sync status')
        dialog.resize(720, 470)
        layout = QVBoxLayout(dialog)
        details = QTextEdit()
        details.setReadOnly(True)
        lines = [self.status.text(), '']
        if self.engine and not self.demo_mode:
            lines.append(f'Automatic syncing: every {self.preferences.sync_interval_minutes} minute(s) while open.' if self.preferences.auto_sync_enabled
                         else 'Automatic syncing is paused. The Sync button still works.')
        if not self.engine:
            lines.append('Demo mode is offline. Choose Open my calendars to use your persistent workspace.' if self.demo_mode else 'Sync is disabled in this workspace.')
        for calendar in self.store.list_calendars():
            pending = len(self.store.pending_changes(calendar.id))
            cached = self.store.count_events(calendar.id)
            lines.append(f'{calendar.name} · {calendar.kind} · {cached} cached event series · {pending} pending change(s)')
            if calendar.id in self._sync_errors:
                lines.append(self._sync_errors[calendar.id])
            if calendar.id in self._sync_issues:
                lines.append(self._sync_issues[calendar.id])
        quarantined = self.store.list_quarantined_resources()
        if quarantined:
            names = {calendar.id: calendar.name for calendar in self.store.list_calendars()}
            lines += ['', 'Skipped resources retained for retry:']
            lines.extend(f'{names.get(item["calendar_id"], "Calendar")} · {item["resource_id"]} · {item["reason"]}' for item in quarantined)
        lines += ['', 'Recent synchronization activity:'] + list(self._sync_messages)
        details.setPlainText('\n'.join(lines))
        layout.addWidget(details)
        self.button('Close', dialog.accept, layout)
        dialog.exec()

    def notify_conflict(self, message):
        conflict_id = message.conflict.id if message.conflict else message.message
        comparison = message.conflict or message.message
        if self._notified_conflicts.get(conflict_id) == comparison:
            return
        self._notified_conflicts[conflict_id] = comparison
        mode = self.preferences.conflict_notifications
        desktop_available = QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages()
        if mode in ('in_app', 'both') or not desktop_available:
            self.show_notice(message.message)
        if mode in ('desktop', 'both') and desktop_available:
            if self.tray_icon is None:
                self.tray_icon = QSystemTrayIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation), self)
                self.tray_icon.setToolTip('Lebacal · calendar conflicts')
                self.tray_icon.messageClicked.connect(self.show_conflicts)
                self.tray_icon.show()
            self.tray_icon.showMessage('Calendar conflict', message.message, QSystemTrayIcon.MessageIcon.Information)

    def poll(self):
        if self._closed:
            return
        while not self.tasks.empty():
            _, future = self.tasks.get()
            try:
                report = future.result()
                events = report.events
                for resource in report.rejected:
                    calendar_id = events[0].calendar_id if events else getattr(future, 'calendar_id', None)
                    if calendar_id:
                        self.store.set_quarantined_resource(calendar_id, resource.resource_id, resource.raw_ics, resource.reason)
                if self.apply_events('Import ' + str(len(events)) + ' event(s)', events):
                    detail = f'Imported {len(events)} event resource(s).'
                    if report.issues:
                        detail += f' {len(report.issues)} resource issue(s); skipped resources remain in the source file.'
                        self._sync_messages.extend(f'{issue.resource_id}: {issue.reason}' for issue in report.issues)
                    self.show_notice(detail)
            except Exception as exc:
                self.show_notice('Import failed: ' + str(exc))
        if not self.engine:
            return
        updated = False
        while True:
            try:
                message = self.engine.events.get_nowait()
            except Empty:
                break
            self._sync_messages.append(self.preferences.format_time(now()) + ' · ' + message.message)
            if message.kind == 'error':
                self._sync_errors[message.calendar_id or 'sync'] = message.message
                self.show_notice(message.message)
            elif message.kind == 'issue':
                self._sync_issues[message.calendar_id or 'sync'] = message.message
                self.show_notice(message.message)
            elif message.kind == 'conflict':
                self.notify_conflict(message)
            elif message.kind == 'started':
                self._sync_running = True
                self._sync_errors.clear()
                self._sync_issues.clear()
            elif message.kind in ('updated', 'finished'):
                updated = True
                if message.kind == 'finished':
                    self._sync_running = False
                    if not self._sync_errors and not self.engine.conflicts:
                        self._last_sync = now()
        self.conflict_btn.setVisible(bool(self.engine.conflicts))
        if updated:
            self.refresh()
        else:
            self.update_sync_status()

    def show_conflicts(self):
        if not self.engine or not self.engine.conflicts:
            return
        conflict = self.engine.conflicts[0]
        dialog = ConflictDialog(conflict, self.preferences, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.choice:
                self.engine.resolve_conflict(conflict.id, dialog.choice, expected=conflict)
        finally:
            dialog.deleteLater()

    def start_selection(self):
        if self.view == 'month':
            self.set_view('week')
        self.select_mode = True
        self.nav_day = None
        self.nav_digits = ''
        self.canvas.setFocus()
        self.update_hints()

    def select_by_arrow(self, direction):
        events = self.visible_events
        current = self.event_by_id(self.selected_id or '')
        if direction in ('left', 'right') and current is None:
            day = self.selected_day + timedelta(days=-1 if direction == 'left' else 1)
            self.select_day(day, reveal=day not in self.dates())
            return
        if not events:
            return
        if direction in ('left', 'right') and current:
            target = current.start.astimezone(local_zone()) + timedelta(days=-1 if direction == 'left' else 1)
            options = [e for e in events if e.start.astimezone(local_zone()).date() == target.date()]
            event = min(options, key=lambda e: abs((e.start - target).total_seconds())) if options else current
        else:
            ids = [e.id for e in events]
            index = ids.index(self.selected_id) if self.selected_id in ids else (-1 if direction == 'down' else 0)
            event = events[(index + (-1 if direction == 'up' else 1)) % len(events)]
        self.select_event(event.id)
        self.ensure_event_visible(event.id)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and self.isActiveWindow() and not QApplication.activeModalWidget():
            modifiers = event.modifiers()
            if event.type() == QEvent.Type.KeyRelease:
                released = {Qt.Key.Key_Control: Qt.KeyboardModifier.ControlModifier,
                            Qt.Key.Key_Shift: Qt.KeyboardModifier.ShiftModifier,
                            Qt.Key.Key_Alt: Qt.KeyboardModifier.AltModifier,
                            Qt.Key.Key_Meta: Qt.KeyboardModifier.MetaModifier}.get(event.key())
                if released:
                    modifiers &= ~released
            self.update_scope_hint(modifiers)
        if event.type() != QEvent.Type.KeyPress or not self.isActiveWindow() or QApplication.activeModalWidget():
            return super().eventFilter(obj, event)
        focus = QApplication.focusWidget()
        text_focus = isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox))
        command_modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        search_binding = QKeySequence(self.bindings.get('search', ''))
        if ((not self._shortcut_pending or text_focus) and search_binding.count() == 1
                and (event.modifiers() & command_modifiers or event.key() >= Qt.Key.Key_Escape)
                and QKeySequence(event.keyCombination()).matches(search_binding) == QKeySequence.SequenceMatch.ExactMatch):
            self.focus_search()
            return True
        searching = focus in (self.search, self.results)
        if event.key() == Qt.Key.Key_Escape and (searching or (self.search.text() and self.results.isVisible())):
            self.exit_search()
            return True
        if searching and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self.cycle_search_result(-1 if event.key() == Qt.Key.Key_Up else 1)
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.results.currentItem()
                if item is None or not item.data(Qt.ItemDataRole.UserRole):
                    self.cycle_search_result(1)
                    item = self.results.currentItem()
                self.open_search_result(item)
                return True
        if text_focus:
            self._shortcut_pending.clear()
            return super().eventFilter(obj, event)
        if self.select_mode:
            char = event.text()
            if event.key() == Qt.Key.Key_Escape:
                self.select_mode = False
            elif char.isdigit() and char.isascii() and len(char) == 1:
                if self.nav_day is None:
                    n = int(char) - 1
                    if 0 <= n < len(self.canvas.days):
                        self.nav_day = n
                else:
                    self.nav_digits = (self.nav_digits + char)[:4]
            elif event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and self.nav_day is not None:
                day = self.canvas.days[self.nav_day]
                options = self.events_on_day(day)
                index = int(self.nav_digits or '0') - 1
                if 0 <= index < len(options):
                    self.select_event(options[index].id)
                    self.select_mode = False
                    self.ensure_event_visible(options[index].id)
            elif event.key() == Qt.Key.Key_Backspace:
                if self.nav_digits:
                    self.nav_digits = self.nav_digits[:-1]
                else:
                    self.nav_day = None
            else:
                action = self.match_shortcut(event)
                if action == 'pending':
                    return True
                if action in ('select_left', 'select_right', 'select_up', 'select_down'):
                    self.select_by_arrow(action.removeprefix('select_'))
                    self.select_mode = False
                    self.update_hints()
                    return True
                return super().eventFilter(obj, event)
            self.update_hints()
            return True
        action = self.match_shortcut(event)
        if (action is None and self.bindings.get('search') == 'Ctrl+F' and event.text() == '/'
                and not event.modifiers() & command_modifiers):
            action = 'search'
        if action == 'pending':
            return True
        actions = {
            'new_event': self.create_event, 'edit_event': lambda: self.edit_event(scope=self._matched_scope),
            'edit_single': lambda: self.edit_event(group=False), 'preferences': self.show_preferences,
            'delete_event': lambda: self.delete_event(scope=self._matched_scope), 'search': self.focus_search,
            'copy_event': lambda: self.copy_event(scope=self._matched_scope),
            'paste_event': lambda: self.paste_events(scope=self._matched_scope),
            'duplicate_event': lambda: self.duplicate_event(scope=self._matched_scope),
            'today': self.today, 'previous': lambda: self.navigate(-1), 'next': lambda: self.navigate(1),
            'day_view': lambda: self.set_view('day'), 'week_view': lambda: self.set_view('week'),
            'month_view': lambda: self.set_view('month'), 'select_event': self.start_selection,
            'undo': self.undo, 'redo': self.redo, 'history': self.show_history,
            'shortcuts': self.show_shortcuts, 'sync': self.sync_now,
            'select_left': lambda: self.select_by_arrow('left'), 'select_right': lambda: self.select_by_arrow('right'),
            'select_up': lambda: self.select_by_arrow('up'), 'select_down': lambda: self.select_by_arrow('down'),
        }
        if action in actions:
            actions[action]()
            return True
        return super().eventFilter(obj, event)

    def match_shortcut(self, event):
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return None
        if not self._shortcut_pending:
            self._pending_scope = 'single'
        variants = [(event.keyCombination(), 'single', False)]
        char = event.text()
        command_modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        if (not event.modifiers() & command_modifiers and len(char) == 1
                and char.isprintable() and not char.isalnum() and not char.isspace()):
            # Punctuation may require Shift on a keyboard layout (for example
            # '/' is Shift+7 in German). Respect explicitly configured physical
            # Shift combinations first, then match the printable character.
            printable = QKeySequence(char)
            if not printable.isEmpty() and printable[0] != event.keyCombination():
                variants.append((printable[0], 'single', False))
        modifier_bits = {'Ctrl': Qt.KeyboardModifier.ControlModifier, 'Shift': Qt.KeyboardModifier.ShiftModifier,
                         'Alt': Qt.KeyboardModifier.AltModifier, 'Meta': Qt.KeyboardModifier.MetaModifier}
        scope_bits = modifier_bits[self.scope_modifiers['all']] | modifier_bits[self.scope_modifiers['following']]
        if event.modifiers() & scope_bits:
            key = QKeyCombination(event.modifiers() & ~scope_bits, Qt.Key(event.key()))
            variants.append((key, self.operation_scope(event.modifiers()), True))
        attempts = [(self._shortcut_pending + [key], scope if scoped else self._pending_scope, scoped or self._pending_scope != 'single')
                    for key, scope, scoped in variants]
        if self._shortcut_pending:
            attempts.extend(([key], scope, scoped) for key, scope, scoped in variants)
        for attempt, scope, scoped in attempts:
            if len(attempt) > 4:
                continue
            sequence = QKeySequence(*attempt)
            partial = False
            for action, binding in self.bindings.items():
                if not binding:
                    continue
                if scoped and action not in SCOPED_ACTIONS:
                    continue
                match = sequence.matches(QKeySequence(binding))
                if match == QKeySequence.SequenceMatch.ExactMatch:
                    self._shortcut_pending.clear()
                    self.shortcut_timer.stop()
                    self._matched_scope = scope
                    return action
                partial |= match == QKeySequence.SequenceMatch.PartialMatch
            if partial:
                self._shortcut_pending = attempt
                self._pending_scope = scope
                self.shortcut_timer.start(1200)
                return 'pending'
        self._shortcut_pending.clear()
        return None

    def closeEvent(self, event):
        self._closed = True
        QApplication.instance().removeEventFilter(self)
        self.poll_timer.stop()
        self.sync_timer.stop()
        self.clock_timer.stop()
        self.shortcut_timer.stop()
        if self.engine:
            self.engine.close()
        if self.tray_icon:
            self.tray_icon.hide()
        self.executor.shutdown(wait=False, cancel_futures=True)
        event.accept()
