"""Native desktop workspace. The local store is always the UI's source of truth."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from queue import SimpleQueue, Empty
from collections import deque, OrderedDict
import calendar as monthlib
import math

from PySide6.QtCore import Qt, QDate, QEvent, QTimer, Signal, QMimeData, QItemSelectionModel
from PySide6.QtGui import QKeySequence, QCursor, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLineEdit, QMenu, QDialog, QFileDialog, QInputDialog,
    QTextEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QSystemTrayIcon, QStyle,
)

from .calendar_widgets import at
from .dialogs import EventDialog, CalendarDialog, HistoryDialog
from .store import Store
from .timeutil import now, local_zone
from .preferences import Preferences
from .preferences_dialog import PreferencesDialog
from .import_dialog import ImportDialog
from .import_sources import ImportSource, MAX_PENDING_IMPORT_BYTES, MAX_PENDING_IMPORT_REQUESTS
from .conflict_dialog import ConflictDialog
from .event_scope import DEFAULT_MODE_BINDINGS
from .workspace_chrome import WorkspaceChrome
from .appearance import stylesheet
from .selection import EventSelection, affected_visible, capture_selection, selection_roots
from .placement import PastePlacement, initial_paste_target
from .search_panel import MORE_ROLE, event_matches_query

DEFAULT_BINDINGS = {
    'new_event': 'N', 'edit_event': 'E', 'delete_event': 'Del',
    'search': 'Ctrl+F', 'today': 'T', 'previous': 'H', 'next': 'L',
    'day_view': 'D', 'week_view': 'W', 'month_view': 'M', 'year_view': 'Y', 'full_day': 'F',
    'select_event': 'G', 'undo': 'Ctrl+Z', 'redo': 'Ctrl+Y',
    'history': 'Ctrl+H', 'shortcuts': 'F1', 'sync': 'Ctrl+R',
    'edit_single': '', 'preferences': 'Ctrl+,', 'export_selection': 'Ctrl+Shift+E',
    'copy_event': 'Ctrl+C', 'paste_event': 'Ctrl+V', 'cycle_scope': 'Alt+S',
    'select_left': 'Left', 'select_right': 'Right',
    'select_up': 'Up', 'select_down': 'Down',
} | DEFAULT_MODE_BINDINGS
EVENT_MIME = 'application/x-debacal-events+json'


class MainWindow(WorkspaceChrome, QMainWindow):
    real_workspace_requested = Signal()

    def __init__(self, store: Store, enable_sync=True, demo_mode=False, *, profile_label='', credential_namespace=''):
        super().__init__()
        self.store = store
        self._closed = False
        self.demo_mode = demo_mode
        self.profile_label = profile_label
        self._import_requests = deque()
        self._import_memory_bytes = 0
        self._import_dialog_active = False
        self._import_timer = QTimer(self)
        self._import_timer.setSingleShot(True)
        self._import_timer.timeout.connect(self._process_import_request)
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
        try:
            self.selection = EventSelection(scope=store.get_setting('selection_mode', 'single'))
        except ValueError:
            self.selection = EventSelection()
        self.selected_ids = set()
        self.pending_paste = None
        self._paste_mouse_origin = None
        self._drag_preview_events = []
        self._drag_preview_request = None
        self.full_day = bool(store.get_setting('show_full_day', False))
        self.nav_day = None
        self.nav_digits = None
        self.select_mode = False
        self._search_index = None
        self._events_revision = None
        self._range_cache = OrderedDict()
        self._filter_masters = {}
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(80)
        self.search_timer.timeout.connect(self.run_search)
        self._revealing_search_result = False
        self._search_selection_active = False
        self._search_row_ids = set()
        self._gesture_selection = None
        self._shortcut_pending = []
        self.shortcut_timer = QTimer(self)
        self.shortcut_timer.setSingleShot(True)
        self.shortcut_timer.timeout.connect(self._shortcut_pending.clear)
        saved_bindings = dict(store.get_setting('keybindings', {}))
        if saved_bindings.get('edit_single') == 'Shift+E':
            saved_bindings['edit_single'] = ''
        saved_bindings.pop('duplicate_event', None)
        migrate_bindings = store.get_setting('keybindings_version', 0) < 2
        if migrate_bindings:
            for action, old_default in (('day_view', '1'), ('copy_event', 'C'), ('paste_event', 'V')):
                if saved_bindings.get(action) == old_default:
                    saved_bindings.pop(action)
        if saved_bindings.get('search') == '/':
            saved_bindings['search'] = 'Ctrl+F'
        if migrate_bindings:
            # New defaults yield to deliberately configured actions, including
            # multi-stroke shortcuts that share their first key.
            for action in ('day_view', 'copy_event', 'paste_event', 'full_day'):
                if action in saved_bindings:
                    continue
                sequence = QKeySequence(DEFAULT_BINDINGS[action])
                if any(value and (sequence.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                                   or QKeySequence(value).matches(sequence) != QKeySequence.SequenceMatch.NoMatch)
                       for other, value in saved_bindings.items() if other != action):
                    saved_bindings[action] = ''
            store.set_setting('keybindings', saved_bindings)
            store.set_setting('keybindings_version', 2)
        if store.get_setting('keybindings_version', 0) < 3:
            for action in ('year_view', 'export_selection'):
                if action in saved_bindings:
                    continue
                sequence = QKeySequence(DEFAULT_BINDINGS[action])
                if any(value and (sequence.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                                   or QKeySequence(value).matches(sequence) != QKeySequence.SequenceMatch.NoMatch)
                       for other, value in saved_bindings.items() if other != action):
                    saved_bindings[action] = ''
            store.set_setting('keybindings', saved_bindings)
            store.set_setting('keybindings_version', 3)
        if store.get_setting('keybindings_version', 0) < 5:
            alternative = saved_bindings.pop('redo_alternative', None)
            if saved_bindings.get('redo') in (None, 'Ctrl+Y', 'Ctrl+Shift+Z'):
                # Consolidate former defaults into one action. Retain a custom
                # alternative when the primary action still used a default.
                if alternative and alternative not in ('Ctrl+Y', 'Ctrl+Shift+Z'):
                    saved_bindings['redo'] = alternative
                else:
                    saved_bindings.pop('redo', None)
                    sequence = QKeySequence(DEFAULT_BINDINGS['redo'])
                    if any(value and (sequence.matches(QKeySequence(value)) != QKeySequence.SequenceMatch.NoMatch
                                       or QKeySequence(value).matches(sequence) != QKeySequence.SequenceMatch.NoMatch)
                           for value in saved_bindings.values()):
                        saved_bindings['redo'] = ''
            store.set_setting('keybindings', saved_bindings)
            store.set_setting('keybindings_version', 5)
        self.bindings = DEFAULT_BINDINGS | saved_bindings
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='debacal-files')
        self.tasks = SimpleQueue()
        self.engine = None
        if enable_sync and store.path != ':memory:':
            from .sync import SyncEngine
            from .credentials import CredentialStore
            self.engine = SyncEngine(store.path, credential_store=CredentialStore(namespace=credential_namespace))
        self.setWindowTitle('Debacal' + (' — ' + profile_label if profile_label else ''))
        self.resize(1400, 900)
        self.setMinimumSize(1030, 690)
        self.setLocale(self.preferences.qt_locale())
        self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences.color_scheme))
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.build_sidebar(root)
        self.build_workspace(root)
        self.scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self.sync_week_columns())
        self.all_day_scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self.sync_week_columns())
        for surface in (self.header, self.month, self.year):
            surface.week_requested.connect(self.open_week)
        self.install_mode_shortcuts()
        self.refresh()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start(250)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_now)
        self.configure_auto_sync()
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.canvas.update)
        self.clock_timer.timeout.connect(self.year.update)
        self.clock_timer.timeout.connect(self.refresh_agenda_clock)
        self.clock_timer.start(60_000)
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self.fit_calendar)
        QTimer.singleShot(1500, self.startup_sync)

    @property
    def event_surfaces(self):
        return self.canvas, self.month, self.all_day_canvas, self.year

    @property
    def day_surfaces(self):
        return self.header, *self.event_surfaces

    def active_canvas(self):
        return self.year if self.view == 'year' else self.month if self.view == 'month' else self.canvas

    def update_creation_target(self):
        calendar = self.store.get_default_calendar()
        for widget in self.day_surfaces:
            widget.set_creation_calendar(calendar)

    def fit_year(self):
        if not self._closed and self.view == 'year':
            self.year.setMinimumHeight(self.year.heightForWidth(self.year_scroll.viewport().width()))

    def dates(self):
        if self.view == 'year':
            first = self.anchor.replace(month=1, day=1)
            return [first + timedelta(days=offset) for offset in range(366 if monthlib.isleap(first.year) else 365)]
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
        revision = self.store.events_revision()
        if revision != self._events_revision:
            self._events_revision = revision
            self._range_cache.clear()
            self._filter_masters.clear()
            self._search_index = None
            self.results.invalidate()
        cals = self.store.list_calendars()
        days = self.dates()
        if self.selected_day not in days:
            self.selected_day = self.anchor
        from .recurrence import RecurrenceRangeError
        try:
            self.range_events = self.query_view_events(days, cals, revision)
        except RecurrenceRangeError as exc:
            self.range_events = []
            self.selection.clear()
            self.selected_id = None
            self._search_selection_active = False
            self._search_row_ids.clear()
            self.show_notice(str(exc))
        self.visible_events = self.filter_calendar_events(self.range_events)
        refs = [value for reference in self.selection.refs
                if (value := self.store.get_occurrence(reference.id)) is not None]
        self.selection.replace(refs)
        if not refs and self.selected_id:
            previous = self.event_by_id(self.selected_id)
            if previous:
                self.selection.select(previous)
        self.selected_id = self.selection.primary.id if self.selection.primary else None
        self.heading.setText(str(self.anchor.year) if self.view == 'year' else self.preferences.format_month(self.anchor))
        self.heading.setToolTip(self.heading.text())
        self.range_label.setText(self.preferences.format_date(days[0], weekday=self.view == 'day')
                                + (' — ' + self.preferences.format_date(days[-1]) if self.view != 'day' else ''))
        self.mini.set_preferences(self.preferences)
        self.mini.setSelectedDate(QDate(self.selected_day.year, self.selected_day.month, self.selected_day.day))
        self.header.days = days[:7]
        for widget in self.day_surfaces:
            widget.set_preferences(self.preferences)
            widget.set_selected_day(self.selected_day)
        self.header.update()
        if self.view == 'year':
            self.year.set_data(self.anchor.year, self.visible_events, cals)
        else:
            self.canvas.set_data(days[:7], self.visible_events, cals)
            self.all_day_canvas.set_data(days[:7], self.visible_events, cals)
            self.all_day_scroll.setFixedHeight(min(180, self.all_day_canvas.height()))
            self.month.set_data(days[0], self.anchor.month, self.visible_events, cals)
        self.results.update_context(cals, self.preferences)
        self.results.set_range(at(days[0]), at(days[-1] + timedelta(days=1)))
        self.stack.setCurrentIndex({'day': 0, 'week': 0, 'month': 1, 'year': 2}[self.view])
        self.sync_week_columns()
        for name, btn in self.view_buttons.items():
            btn.setChecked(name == self.view)
        self.full_day_button.setEnabled(self.view in ('day', 'week'))
        self.refresh_calendar_list(cals)
        self.update_creation_target()
        self.sync_selection()
        self.render_placement()
        self.fit_calendar()
        self.fit_year()
        self.update_hints()
        if not self._revealing_search_result:
            self.run_search()
        self.update_sync_status()

    def query_view_events(self, days, calendars, revision):
        identifiers = tuple(calendar.id for calendar in calendars if calendar.visible)
        begin, end = at(days[0]), at(days[-1] + timedelta(days=1))
        key = revision, begin.timestamp(), end.timestamp(), identifiers
        events = self._range_cache.get(key)
        if events is not None:
            self._range_cache.move_to_end(key)
            return list(events)
        events = self.store.query_occurrences(begin, end, identifiers)
        # Bound both page count and occurrence count: a dense calendar should
        # not retain arbitrarily many old pages or visibility combinations.
        if len(events) <= 50_000:
            self._range_cache[key] = tuple(events)
            while len(self._range_cache) > 8 or sum(map(len, self._range_cache.values())) > 50_000:
                self._range_cache.popitem(last=False)
        return events

    def update_hints(self):
        def key(action):
            return self.bindings.get(action) or '—'
        self.update_command_tooltips()
        self.search.setPlaceholderText('Search events   ' + key('search'))
        self.search.setToolTip('Search titles, places, and notes (' + key('search') + ')')
        self.search_header.update_preferences(self.preferences, self.bindings.get('search', ''))
        self.message_stack.setCurrentWidget(self.navigation_prompt if self.select_mode else self.notice)
        if self.select_mode:
            if self.nav_day is None:
                message = f'Select an event · choose a day (1–{len(self.canvas.days)}), or use the arrow keys. Esc cancels.'
            else:
                day = self.canvas.days[self.nav_day]
                options = self.events_on_day(day)
                index = int(self.nav_digits or '0') - 1
                target = options[index].title if 0 <= index < len(options) else ('No matching event' if self.nav_digits else f'{len(options)} events')
                message = f'{self.preferences.format_date(day, weekday=True)} → event {self.nav_digits or "number"} · {target} · Enter selects · Backspace goes back · Esc cancels'
            self.navigation_prompt.setText(message)
            self.navigation_prompt.setToolTip(message)
        self.header.set_navigation(self.select_mode, self.nav_day, self.nav_digits or '')
        self.canvas.set_navigation(self.select_mode, self.nav_day, self.nav_digits or '')

    def events_on_day(self, day):
        from .recurrence import overlaps
        return [e for e in self.visible_events if overlaps(e, at(day), at(day + timedelta(days=1)))]

    def show_preferences(self, category=None):
        dialog = PreferencesDialog(self.preferences, self,
                                   bindings=self.bindings, default_bindings=DEFAULT_BINDINGS)
        if category:
            dialog.set_category(category)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.preferences = dialog.result_preferences
            self.preferences.save(self.store)
            self.bindings = dialog.result_bindings
            self.store.set_setting('keybindings', self.bindings)
            self.install_mode_shortcuts()
            self.setLocale(self.preferences.qt_locale())
            self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences.color_scheme))
            self.select_mode = False
            self.configure_auto_sync()
            self.refresh(calendars=True)

    def set_creation_calendar(self, calendar_id):
        self.store.set_default_calendar(calendar_id)
        self.refresh_calendar_list(self.store.list_calendars())
        self.update_creation_target()

    def reorder_calendars(self, calendar_ids):
        self.store.set_calendar_order(calendar_ids)
        self.refresh_calendar_list(self.store.list_calendars())

    def calendar_menu(self, calendar_id, point):
        calendar = self.store.get_calendar(calendar_id)
        if calendar is None:
            return
        menu = QMenu(self)
        if calendar.kind != 'ics' and not calendar.read_only:
            action = menu.addAction('Create events in this calendar', lambda: self.set_creation_calendar(calendar.id))
            action.setCheckable(True)
            default = self.store.get_default_calendar()
            action.setChecked(bool(default and default.id == calendar.id))
        menu.addAction('Edit calendar…', lambda: self.add_calendar(calendar))
        menu.addSeparator()
        if calendar.kind != 'ics' and not calendar.read_only:
            menu.addAction('Import .ics here…', lambda: self.import_ics(calendar.id))
        menu.addAction('Export .ics…', lambda: self.export_ics(calendar.id))
        try:
            menu.exec(point)
        finally:
            menu.deleteLater()

    def configure_auto_sync(self):
        if self.engine and not self.demo_mode and self.preferences.auto_sync_enabled:
            self.sync_timer.start(self.preferences.sync_interval_minutes * 60_000)
        else:
            self.sync_timer.stop()

    def startup_sync(self):
        if not self._closed and self.engine and not self.demo_mode and self.preferences.auto_sync_enabled:
            self.sync_now()

    def scroll_to_morning(self):
        self.fit_calendar()

    def fit_calendar(self):
        if not self._closed and self.view in ('day', 'week'):
            # Leave one pixel for integer rounding before Qt sees the minimum
            # size. Clamping afterward can repeatedly show/hide a scrollbar.
            height = max(1, self.scroll.viewport().height() - 1)
            self.canvas.fit_to_viewport(height, full_day=self.full_day)
            self.sync_week_columns()

    def sync_week_columns(self):
        """Reserve the same trailing scrollbar space in month and week views."""
        if self._closed:
            return
        trailing = max(self.scroll.verticalScrollBar().sizeHint().width(),
                       self.all_day_scroll.verticalScrollBar().sizeHint().width())
        width = self.stack.contentsRect().width() - trailing
        if width <= self.canvas.gutter:
            return
        for widget in (self.header, self.canvas, self.all_day_canvas, self.month):
            if widget.maximumWidth() != width:
                widget.setMaximumWidth(width)

    def toggle_full_day(self):
        if self.view not in ('day', 'week'):
            return
        self.full_day = not self.full_day
        self.full_day_button.setChecked(self.full_day)
        self.store.set_setting('show_full_day', self.full_day)
        self.fit_calendar()

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
            self._search_selection_active = False
            self._search_row_ids.clear()
            self.selection.clear()
            self.selected_id = None
            for widget in self.event_surfaces:
                widget.selected_id = None
            self.select_mode = False
            self.nav_day, self.nav_digits = None, ''
            self._shortcut_pending.clear()
            self.shortcut_timer.stop()
        if reveal:
            self.anchor = day
            self.refresh()
        else:
            self.mini.setSelectedDate(QDate(day.year, day.month, day.day))
            for widget in self.day_surfaces:
                widget.set_selected_day(day)
            self.sync_selection()
            self.update_hints()

    def open_day(self, day):
        self.select_day(day)
        self.set_view('day')

    def open_month(self, day):
        self.select_day(day)
        self.set_view('month')

    def open_week(self, day):
        self.select_day(day)
        self.set_view('week')

    def today(self):
        self.go_date(now().date())

    def navigate(self, direction):
        self._search_selection_active = False
        self._search_row_ids.clear()
        self.selection.clear()
        self.selected_id = None
        if self.view == 'year':
            year = min(9998, max(1, self.anchor.year + direction))
            day = min(self.selected_day.day, monthlib.monthrange(year, self.selected_day.month)[1])
            self.anchor = self.selected_day = self.selected_day.replace(year=year, day=day)
        elif self.view == 'month':
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
        self.notice.setToolTip(message)
        if not self.select_mode:
            self.message_stack.setCurrentWidget(self.notice)

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
            cached = self.results.result_event(event_id)
            if cached is not None:
                current = self.store.get_occurrence(cached.id)
                if current is not None:
                    return current
            visible = [value for value in self.visible_events if value.series_id == event.id]
            if visible:
                return min(visible, key=lambda value: value.start.timestamp())
            from .recurrence import expand_event
            start = now()
            occurrences = expand_event(event, start, start + timedelta(days=732))
            if not occurrences:
                occurrences = expand_event(event, event.start, event.start + timedelta(days=1))
            return occurrences[0] if occurrences else None
        return event

    def operation_scope(self, modifiers=None):
        return 'single' if self._search_selection_active else self.selection.scope

    def set_selection_mode(self, scope):
        if scope != 'group':
            converted = [self.search_result_event(value.id) if value.recurring and not value.series_id else value
                         for value in self.selection.refs]
            self.selection.replace(value for value in converted if value is not None)
        if self._search_selection_active:
            self._search_selection_active = False
            self._search_row_ids.clear()
        self.selection.set_scope(scope)
        self.store.set_setting('selection_mode', scope)
        self.sync_selection()
        self.update_scope_hint()

    def install_mode_shortcuts(self):
        for shortcut in getattr(self, 'mode_shortcuts', ()):
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self.mode_shortcuts = []
        for name, mode in (('scope_single', 'single'), ('scope_all', 'group'), ('scope_following', 'future')):
            binding = self.bindings.get(name, '')
            if binding:
                shortcut = QShortcut(QKeySequence(binding), self)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(lambda value=mode: self.set_selection_mode(value))
                self.mode_shortcuts.append(shortcut)

    def sync_selection(self):
        try:
            self.selected_ids = affected_visible(self.store, self.selection.refs, self.operation_scope(), self.visible_events)
        except (ValueError, KeyError, OverflowError):
            self.selected_ids = set()
        self.selected_id = self.selection.primary.id if self.selection.primary else None
        for widget in self.event_surfaces:
            widget.selected_id = self.selected_id
            widget.set_selection(self.selected_ids)
            widget.set_scope(self.operation_scope())
        self.sync_search_selection()
        self.update_scope_hint()
        self.export_selection_button.setEnabled(bool(self.selection.refs))

    def sync_search_selection(self):
        if self.results.agenda_mode:
            try:
                targets = [event for _, event in self.results.selection_targets()]
                identifiers = affected_visible(self.store, self.selection.refs, self.operation_scope(), targets)
            except (ValueError, KeyError, OverflowError):
                identifiers = set()
            self.results.set_row_selection(identifiers)
            return
        if self._search_selection_active:
            self.results.set_row_selection(self._search_row_ids)
            return
        identifiers = set()
        for reference in self.selection.refs:
            if reference.series_id and self.selection.scope == 'group':
                identifiers.add(reference.series_id)
            else:
                identifiers.add(reference.id)
        self.results.set_row_selection(identifiers)

    def update_scope_hint(self, modifiers=None):
        scope = self.selection.scope
        for mode, button in self.selection_buttons.items():
            button.setChecked(mode == scope)

    def remember_selection_for_gesture(self, event_id):
        self._gesture_selection = (self.selection.refs, self.selection.scope, self._search_selection_active,
                                   set(self._search_row_ids))

    def edit_from_double_click(self, event_id, modifiers=None):
        event = self.event_by_id(event_id)
        if event and self._gesture_selection:
            refs, scope, searching, rows = self._gesture_selection
            if event.id in affected_visible(self.store, refs, 'single' if searching else scope, [event]):
                self.selection.replace(refs)
                self.selection.set_scope(scope)
                self._search_selection_active, self._search_row_ids = searching, rows
                self.sync_selection()
        self._gesture_selection = None
        self.edit_event(event_id)



    def edit_event(self, event_id=None, group=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if event is None:
            self.show_notice('Select an event to edit.')
            return
        refs = self.operation_references(event)
        scope = scope or (('group' if group else 'single') if group is not None else self.operation_scope())
        dialog_scope = ('group' if self._search_selection_active and self.selection.primary
                        and self.selection.primary.recurring and not self.selection.primary.series_id else scope)
        series = self.store.get_event(event.series_id) if event.series_id else None
        expected = self.store.list_events()
        try:
            members = selection_roots(self.store, refs, scope)
            if any(not self.event_writable(member) for member in members):
                raise ValueError('This selection contains a read-only event. Copy it into a writable calendar to edit it.')
            dialog = EventDialog(self.store.list_calendars(), event.start, event.end, event, self,
                                 preferences=self.preferences, edit_scope=dialog_scope, series=series,
                                 scope_bindings=self.bindings,
                                 common_fields_only=len({value.series_id or value.id for value in refs}) > 1)
            mode_changes = []
            def change_editor_mode(value):
                mode_changes.append(value)
                self.set_selection_mode(value)
            dialog.mode_changed.connect(change_editor_mode)
            if len(refs) > 1:
                dialog.setWindowTitle(f'Edit {len(refs)} selections')
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if mode_changes:
                scope = dialog.scope.currentData()
                if scope != 'group':
                    refs = tuple(value for reference in refs
                                 if (value := (self.search_result_event(reference.id)
                                               if reference.recurring and not reference.series_id else reference)) is not None)
                self.set_selection_mode(scope)
            edited = dialog.result_events[0]
            reference = dialog.edit_reference
            previous_ids = {value.id for value in expected}
            self.store.edit_selection(refs, scope, reference, edited,
                                      replace_schedule=dialog.schedule_replacement,
                                      expected_masters=expected)
            zone = local_zone()
            shift = edited.start.astimezone(zone).replace(tzinfo=None) - reference.start.astimezone(zone).replace(tzinfo=None)
            target = (event.start.astimezone(zone).replace(tzinfo=None) + shift).replace(tzinfo=zone, fold=event.start.fold)
            self.reselect_after_operation(refs, target, previous_ids, shift)
            self.show_notice('Updated the selected events. Undo restores them.')
        except (ValueError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def event_writable(self, event):
        calendar = self.store.get_calendar(event.calendar_id)
        return bool(calendar and calendar.kind != 'ics' and not calendar.read_only and not event.read_only)


    def select_event(self, event_id, day=None, additive=False):
        event = next((value for value in self.visible_events if value.id == event_id), None) or self.event_by_id(event_id)
        if event is None:
            return
        self._search_selection_active = False
        self._search_row_ids.clear()
        self.select_mode = False
        self.nav_day, self.nav_digits = None, ''
        self.selection.select(event, additive=additive)
        self.selected_day = day or (event.start.date() if event.all_day else event.start.astimezone(local_zone()).date())
        self.mini.setSelectedDate(QDate(self.selected_day.year, self.selected_day.month, self.selected_day.day))
        self.sync_selection()
        self.update_hints()

    def operation_references(self, event):
        if self.selection.refs and event.id in affected_visible(self.store, self.selection.refs, self.operation_scope(), [event]):
            return self.selection.refs
        return (event,)

    def reselect_after_operation(self, refs, target, previous_ids, shift=None):
        self.selection.clear()
        self.selected_id = None
        self.anchor = self.selected_day = target.astimezone(local_zone()).date()
        self.refresh()
        current_ids = {value.id for value in self.store.list_events()}
        allowed = (current_ids - previous_ids) | {value.series_id or value.id for value in refs}
        choices = [value for value in self.visible_events if (value.series_id or value.id) in allowed]
        selected = []
        for reference in refs:
            expected = reference.start.astimezone(local_zone())
            if shift is not None:
                expected = (expected.replace(tzinfo=None) + shift).replace(tzinfo=local_zone(), fold=expected.fold)
            else:
                expected = target
            available = [value for value in choices if value.id not in {item.id for item in selected}]
            if available:
                same_resource = [value for value in available if (value.series_id or value.id) == (reference.series_id or reference.id)]
                exact = [value for value in available if abs(value.start.timestamp() - expected.timestamp()) < .000001]
                identity_exact = [value for value in exact if value.id == reference.id]
                same_exact = [value for value in exact if value in same_resource]
                value = min(identity_exact or same_exact or exact or same_resource or available,
                            key=lambda item: abs(item.start.timestamp() - expected.timestamp()))
                if reference.recurring and not reference.series_id:
                    value = self.store.get_event(value.series_id or value.id) or value
                selected.append(value)
        self.selection.replace(selected)
        self.sync_selection()
        if self.selected_id:
            self.ensure_event_visible(self.selected_id)

    def ensure_event_visible(self, event_id):
        """Keep the current scroll unless the selected event is outside it."""
        if self.view == 'year':
            self.year_scroll.parentWidget().layout().activate()
            rect = self.year.ensure_event_visible(event_id)
            if rect is not None:
                self.year_scroll.ensureVisible(round(rect.center().x()), round(rect.center().y()), 12, 12)
            return
        if self.view == 'month':
            return
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

    def move_event(self, event_id, start, end, scope=None, reference=None, copy=False):
        event = reference or self.event_by_id(event_id)
        if event is None:
            return
        refs = self.operation_references(event)
        scope = scope or self.operation_scope()
        previous_ids = {value.id for value in self.store.list_events()}
        try:
            EventDialog.validate_time(start)
            EventDialog.validate_time(end)
            result = self.store.move_selection(refs, scope, event, start, end, copy=copy)
            self.clear_drag_preview()
            if copy:
                self.persist_copies(result, start, 'Copy selected events')
            else:
                shift = start.astimezone(local_zone()).replace(tzinfo=None) - event.start.astimezone(local_zone()).replace(tzinfo=None)
                self.reselect_after_operation(refs, start, previous_ids, shift)
                self.show_notice('Moved the selected events. Undo restores them.')
        except (ValueError, KeyError, OverflowError) as exc:
            self.clear_drag_preview()
            self.show_notice(str(exc))

    def preview_drag(self, event_id, start, end, modifiers):
        event = self.event_by_id(event_id)
        if event is None:
            return
        self._drag_preview_request = event_id, start, end, modifiers
        try:
            prepared = self.store.move_selection(self.operation_references(event), self.operation_scope(),
                                                  event, start, end, copy=True,
                                                  preserve_calendars=not bool(modifiers & Qt.KeyboardModifier.ControlModifier))
            from .recurrence import expand_events
            days = self.dates()
            self._drag_preview_events = expand_events(prepared, at(days[0]), at(days[-1] + timedelta(days=1)))
            self.render_placement()
        except (ValueError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def clear_drag_preview(self):
        self._drag_preview_events = []
        self._drag_preview_request = None
        self.render_placement()

    def delete_event(self, event_id=None, group=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if event is None:
            return
        scope = scope or (('group' if group else 'single') if group is not None else self.operation_scope())
        try:
            row = self.results.currentRow()
            self.store.delete_selection(self.operation_references(event), scope)
            self.selection.clear()
            self._search_selection_active = False
            self._search_row_ids.clear()
            self.selected_id = None
            self.refresh()
            if self.search.text() and self.results.count():
                self.results.setCurrentRow(min(max(row, 0), self.results.count() - 1))
            self.show_notice('Deleted the selected events. Undo restores them.')
        except (ValueError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def capture_events(self, event, scope):
        return capture_selection(self.store, self.operation_references(event), scope,
                                 view_origin=self.dates()[0], view_kind=self.view, reference=event)

    def copy_event(self, event_id=None, scope=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if event is None:
            self.show_notice('Select an event to copy.')
            return
        try:
            from .ics import export_ics
            selection = self.capture_events(event, scope or self.operation_scope())
            mime = QMimeData()
            mime.setData(EVENT_MIME, selection.to_bytes())
            portable = selection.instantiate('group', selection.reference.start, selection.masters[0].calendar_id)
            data = export_ics(portable)
            mime.setData('text/calendar', data)
            mime.setText(data.decode('utf-8'))
            QApplication.clipboard().setMimeData(mime)
            self.show_notice('Copied the selection. Paste to preview it, then click or press Enter to place it.')
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
        return initial_paste_target(selection, self.dates()[0], self.selected_day)

    def persist_copies(self, events, target, label):
        if not events:
            return
        self.selection.clear()
        self._search_selection_active = False
        self._search_row_ids.clear()
        self.selected_id = None
        for calendar_id in {event.calendar_id for event in events}:
            calendar = self.store.get_calendar(calendar_id)
            if calendar and not calendar.visible:
                self.store.set_calendar_visible(calendar_id, True)
        if self.apply_events(label, events):
            self.anchor = self.selected_day = (target.date() if events[0].all_day else target.astimezone(local_zone()).date())
            self.refresh()
            ids = {event.id for event in events}
            choices = [event for event in self.visible_events if (event.series_id or event.id) in ids]
            if choices:
                selected = min(choices, key=lambda event: abs(event.start.timestamp() - target.timestamp()))
                by_resource = {}
                for value in choices:
                    resource = value.series_id or value.id
                    previous = by_resource.get(resource)
                    if previous is None or abs(value.start.timestamp() - target.timestamp()) < abs(previous.start.timestamp() - target.timestamp()):
                        by_resource[resource] = value
                self.selection.replace([value for value in by_resource.values() if value.id != selected.id] + [selected])
                self.sync_selection()
                self.ensure_event_visible(selected.id)
            self.show_notice(label + '. Undo restores the previous calendar.')

    def paste_events(self, scope=None, target=None):
        try:
            selection = self.read_event_clipboard()
            calendar = self.store.get_default_calendar()
            if calendar is None or calendar.read_only or calendar.kind == 'ics':
                raise ValueError('Select a writable calendar before pasting events.')
            self.cancel_paste(clear_notice=False)
            self.pending_paste = PastePlacement(selection, calendar.id, target or self.paste_target(selection), scope or 'group')
            self._paste_mouse_origin = QCursor.pos()
            for widget in self.day_surfaces:
                widget.placement_active = True
            self.render_placement()
            self.fit_calendar()
            self.active_canvas().setFocus()
            self.show_notice(self.pending_paste.error or 'Move to position the selection. Click or Enter places it; Escape cancels.')
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.show_notice(str(exc))

    def render_placement(self):
        events = self._drag_preview_events
        if self.pending_paste is not None:
            days = self.dates()
            try:
                events = self.pending_paste.visible(at(days[0]), at(days[-1] + timedelta(days=1)))
            except (ValueError, OverflowError) as exc:
                events = []
                self.pending_paste.error = str(exc)
        for widget in self.event_surfaces:
            widget.set_preview(events)

    def move_paste_preview(self, target, source=None):
        pending = self.pending_paste
        if pending is None or QCursor.pos() == self._paste_mouse_origin:
            return
        if source in (self.month, self.header, self.all_day_canvas, self.year):
            day = target.date()
            initial = pending.clipboard.reference.start
            if not pending.clipboard.reference.all_day:
                initial = initial.astimezone(local_zone())
            target = initial.replace(year=day.year, month=day.month, day=day.day)
        pending.moved = True
        pending.update(target)
        self.render_placement()
        self.show_notice(pending.error or 'Click or Enter to place the selection; Escape cancels.')

    def confirm_paste(self):
        pending = self.pending_paste
        if pending is None:
            return
        if pending.error:
            self.show_notice(pending.error)
            return
        calendar = self.store.get_calendar(pending.calendar_id)
        if calendar is None or calendar.read_only or calendar.kind == 'ics':
            self.show_notice('The destination calendar is no longer writable.')
            return
        events, target = pending.events, pending.target
        self.cancel_paste(clear_notice=False)
        self.persist_copies(events, target, 'Paste selected events')

    def cancel_paste(self, clear_notice=True):
        self.pending_paste = None
        self._paste_mouse_origin = None
        for widget in self.day_surfaces:
            widget.placement_active = False
        self.render_placement()
        if clear_notice:
            self.show_notice('')

    def confirm_candidate(self, event_id=None):
        event = self.event_by_id(event_id or self.selected_id or '')
        if not event or not event.candidate:
            return
        try:
            self.store.confirm_candidate(event)
            self.selection.clear()
            self.selected_id = event.series_id or event.id
            self.refresh()
            self.select_event(self.selected_id)
            self.show_notice('Confirmed this slot. All other candidate choices were removed; undo restores them.')
        except ValueError as exc:
            self.show_notice(str(exc))

    def event_menu(self, event_id, point):
        event = self.event_by_id(event_id)
        if event is None:
            return
        if event.id not in affected_visible(self.store, self.selection.refs, self.selection.scope, [event]):
            self.select_event(event.id)
        menu = QMenu(self)
        menu.addAction('Edit selection', lambda: self.edit_event(event_id))
        menu.addAction('Delete selection', lambda: self.delete_event(event_id))
        menu.addAction('Copy selection', lambda: self.copy_event(event_id))
        menu.addAction('Export selection as .ics…', self.export_selection)
        modes = menu.addMenu('Selection mode')
        for label, mode in (('One event', 'single'), ('All in series', 'group'), ('This and following', 'future')):
            action = modes.addAction(label, lambda checked=False, value=mode: self.set_selection_mode(value))
            action.setCheckable(True)
            action.setChecked(mode == self.selection.scope)
        menu.addSeparator()
        menu.addAction('Paste here…', lambda: self.paste_events(target=event.start))
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
        self.show_preferences('keyboard')

    def run_search(self, *_):
        if self._revealing_search_result:
            return
        self.search_timer.stop()
        self.results.setEnabled(True)
        query = self.search.text().strip()
        self.results_heading.setText('Search results' if query else 'Next events')
        self.results.setAccessibleName('Search results' if query else 'Next events')
        if query:
            self.results.populate(query)
        else:
            self.results.populate_upcoming(now())
        self._search_index = getattr(self.results, '_search_index', None)
        self.apply_search_filter()
        self.sync_search_selection()

    def refresh_agenda_clock(self):
        if not self._closed and self.results.agenda_mode:
            current = now()
            self.results.populate_upcoming(current)
            self.results.refresh_agenda_presentation(current)
            self.sync_search_selection()

    def search_changed(self, *_):
        # Programmatic queries and clearing stay immediate. Actual typing is
        # coalesced so a burst of keys does one result/tree update.
        if self.search.isModified() and self.search.text().strip():
            self.search_timer.start()
            self.results.setEnabled(False)
        else:
            self.run_search()

    def flush_search(self):
        if self.search_timer.isActive():
            self.run_search()

    def toggle_search_filter(self):
        self.store.set_setting('filter_calendar_by_search', self.filter_search.isChecked())
        self.apply_search_filter()

    def filter_calendar_events(self, events):
        query = self.search.text().strip()
        if not self.filter_search.isChecked() or not query:
            return list(events)
        for event in events:
            if event.series_id and event.series_id not in self._filter_masters:
                self._filter_masters[event.series_id] = self.store.get_event(event.series_id)
        return [event for event in events if event_matches_query(event, query, self._filter_masters.get(event.series_id))]

    def apply_search_filter(self):
        events = self.filter_calendar_events(getattr(self, 'range_events', ()))
        selection_changed = False
        if self.filter_search.isChecked() and self.search.text().strip():
            retained = [reference for reference in self.selection.refs
                        if affected_visible(self.store, (reference,), self.operation_scope(), events)]
            if tuple(retained) != self.selection.refs:
                self.selection.replace(retained)
                self._search_row_ids.intersection_update(reference.id for reference in retained)
                selection_changed = True
        if events == self.visible_events:
            if selection_changed:
                self.sync_selection()
            return
        self.visible_events = events
        cals, days = self.store.list_calendars(), self.dates()
        if self.view == 'year':
            self.year.set_data(self.anchor.year, events, cals)
        else:
            self.canvas.set_data(days[:7], events, cals)
            self.all_day_canvas.set_data(days[:7], events, cals)
            self.all_day_scroll.setFixedHeight(min(180, self.all_day_canvas.height()))
            self.month.set_data(days[0], self.anchor.month, events, cals)
        self.sync_selection()
        self.render_placement()
        self.fit_calendar()

    def focus_search(self):
        self._shortcut_pending.clear()
        self.shortcut_timer.stop()
        self.select_mode = False
        self.search_header.reveal()
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()
        self.update_hints()


    def cycle_search_result(self, direction):
        self.flush_search()
        self.results.cycle(direction)
        self.results.setFocus()

    def search_result_event(self, event_id):
        cached = self.results.result_event(event_id)
        if cached is not None:
            return cached
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

    def reveal_search_result(self, item, modifiers=Qt.KeyboardModifier.NoModifier):
        event_id = item if isinstance(item, str) else (item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None)
        if not event_id or self._revealing_search_result:
            return False
        try:
            event = self.search_result_event(event_id)
        except (ValueError, OverflowError) as exc:
            self.show_notice(str(exc))
            return False
        if event is None:
            return False
        calendar = self.store.get_calendar(event.calendar_id)
        if calendar is None:
            return False
        day = event.start.date() if event.all_day else event.start.astimezone(local_zone()).date()
        self._revealing_search_result = True
        try:
            if not calendar.visible:
                self.store.set_calendar_visible(calendar.id, True)
            if not calendar.visible or not any(value.id == event.id for value in self.visible_events) or day not in self.dates():
                self.anchor = day
                self.refresh(calendars=not calendar.visible)
            if self.results.agenda_mode:
                self.select_event(event.id, day=day, additive=bool(modifiers & Qt.KeyboardModifier.ControlModifier))
                self.ensure_event_visible(event.id)
                self.results.setFocus()
                return True
            root = self.results.is_series_root(event_id)
            selected = self.store.get_event(event_id) if root else event
            if selected is None:
                return False
            additive = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            if not additive:
                refs, self._search_row_ids = [], set()
            else:
                if self._search_selection_active:
                    refs = list(self.selection.refs)
                else:
                    refs = selection_roots(self.store, self.selection.refs, self.selection.scope)
                    self._search_row_ids = {value.id for value in refs}
            if additive and event_id in self._search_row_ids:
                refs = [value for value in refs if value.id != selected.id]
                self._search_row_ids.discard(event_id)
            else:
                refs = [value for value in refs if value.id != selected.id] + [selected]
                self._search_row_ids.add(event_id)
            self.selection.replace(refs)
            self._search_selection_active = True
            self.selected_day = day
            self.mini.setSelectedDate(QDate(day.year, day.month, day.day))
            self.sync_selection()
            self.ensure_event_visible(event.id)
        finally:
            self._revealing_search_result = False
        row = self.results.row_item(event_id)
        if row is not None and row is not self.results.currentItem():
            blocked = self.results.blockSignals(True)
            try:
                self.results.setCurrentItem(row, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
            finally:
                self.results.blockSignals(blocked)
        self.results.setFocus()
        return True

    def select_search_result(self, event_id, modifiers):
        self.reveal_search_result(event_id, modifiers)

    def activate_search_result(self, event_id):
        if self.reveal_search_result(event_id):
            self.active_canvas().setFocus()

    def open_search_result(self, item):
        if self.reveal_search_result(item):
            self.active_canvas().setFocus()

    def exit_search(self):
        self.search.clear()
        self.flush_search()
        self.active_canvas().setFocus()
        self.search_header.sync_state()

    def add_calendar(self, calendar=None):
        dialog = CalendarDialog(calendar, self, allow_remote=not self.demo_mode,
            credentials=self.engine.credentials if self.engine else None)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                password_set = False
                credential_save = None
                for calendar in dialog.result_calendars:
                    # Discovery can be repeated without adding duplicate sources.
                    existing = next((c for c in self.store.list_calendars() if c.kind == calendar.kind and c.url and c.url == calendar.url and c.username == calendar.username), None)
                    if existing and existing.id != calendar.id:
                        calendar = replace(calendar, id=existing.id, visible=existing.visible,
                            use_server_color=existing.use_server_color, color=existing.color)
                    self.store.save_calendar(calendar)
                    if self.engine and dialog.password.text() and not password_set:
                        credential_save = self.engine.set_password(calendar.id, dialog.password.text(), remember=dialog.remember_password.isChecked())
                        password_set = True
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

    def import_ics(self, calendar_id=None):
        self.import_files((), calendar_id=calendar_id)

    def import_files(self, paths, *, calendar_id=None):
        if isinstance(paths, (str, Path)):
            paths = (paths,)
        return self.import_sources((ImportSource.from_path(path) for path in paths), calendar_id=calendar_id)

    def import_data(self, data, *, label='Standard input', calendar_id=None):
        return self.import_sources((ImportSource.from_data(data, label),), calendar_id=calendar_id)

    def can_accept_import(self, memory_bytes=0, *, requests=1):
        """Check capacity without reading sources or changing queue state."""
        return (not self._closed and memory_bytes >= 0 and requests >= 1
                and len(self._import_requests) + requests <= MAX_PENDING_IMPORT_REQUESTS
                and self._import_memory_bytes + memory_bytes <= MAX_PENDING_IMPORT_BYTES)

    def import_sources(self, sources, *, calendar_id=None):
        """Queue a reviewed import, including contents received during another dialog."""
        if self._closed:
            return False
        sources = tuple(sources)
        if any(not isinstance(source, ImportSource) for source in sources):
            raise ValueError('Choose calendar import sources.')
        memory_bytes = sum(len(source.data) for source in sources if source.data is not None)
        if not self.can_accept_import(memory_bytes):
            self.show_notice('Too many calendar imports are waiting. Finish or cancel current imports before sending more.')
            return False
        self._import_memory_bytes += memory_bytes
        self._import_requests.append((sources, calendar_id))
        if not self._import_dialog_active:
            self._import_timer.start(0)
        return True

    def _process_import_request(self):
        if self._closed or self._import_dialog_active or not self._import_requests:
            return
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            self._import_timer.start(100)
            return
        sources, calendar_id = self._import_requests.popleft()
        calendar = self.store.get_default_calendar()
        self._import_dialog_active = True
        memory_bytes = sum(len(source.data) for source in sources if source.data is not None)
        submitted_bytes = 0
        dialog = None
        try:
            dialog = ImportDialog(self.store.list_calendars(), sources=sources,
                                  calendar_id=calendar_id or (calendar.id if calendar else None), parent=self,
                                  preferences=self.preferences, executor=self.executor)
            if dialog.exec() == QDialog.DialogCode.Accepted and not self._closed:
                target = self.store.get_calendar(dialog.selected_calendar_id)
                if target is None or target.read_only or target.kind == 'ics':
                    self.show_notice('Choose a writable calendar for the import.')
                elif dialog.selected_prepared is None:
                    self.show_notice('Wait for the calendar preview before importing.')
                else:
                    batch = {'remaining': len(dialog.selected_prepared.items), 'messages': []}
                    self.show_notice('Importing the reviewed events…')
                    for item in dialog.selected_prepared.items:
                        source = dialog.selected_sources[item.index]
                        future = self.executor.submit(item.remap, target.id)
                        future.calendar_id = target.id
                        future.source_path = str(source.path) if source.path is not None else ''
                        future.source_label = item.label
                        future.import_memory_bytes = len(source.data) if source.data is not None else 0
                        future.import_batch = batch
                        future.add_done_callback(lambda result: self.tasks.put(('import', result)))
                        submitted_bytes += future.import_memory_bytes
        finally:
            self._import_memory_bytes -= memory_bytes - submitted_bytes
            if dialog is not None:
                dialog.deleteLater()
            self._import_dialog_active = False
            if self._import_requests and not self._closed:
                self._import_timer.start(0)

    def export_ics(self, calendar_id=None):
        calendar = self.store.get_calendar(calendar_id) if calendar_id else self.choose_calendar()
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

    def export_selection(self):
        if not self.selection.refs:
            self.show_notice('Select events to export.')
            return
        try:
            from .selection_export import export_selection
            data = export_selection(self.store, self.selection.refs, self.operation_scope())
            path, _ = QFileDialog.getSaveFileName(self, 'Export selected events', 'selected-events.ics', 'Calendar files (*.ics)')
            if path:
                Path(path).write_bytes(data)
                self.show_notice('Selected events exported to ' + path)
        except (OSError, ValueError, KeyError, OverflowError, ImportError) as exc:
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
        pending = self.store.count_pending_changes()
        self.sync_btn.setEnabled(bool(self.engine) and not self._sync_running)
        self.sync_btn.setToolTip('Syncing in background…' if self._sync_running else
                                 ('Sync calendars (' + self.bindings.get('sync', '') + ')' if self.engine else
                                  ('Demo mode is offline. Open your calendars to connect a remote account.'
                                   if self.demo_mode else 'Synchronization is disabled for this workspace.')))
        self.conflict_btn.setEnabled(bool(self.engine and self.engine.conflicts))
        self.conflict_btn.setVisible(bool(self.engine and self.engine.conflicts))
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
        self.status.setToolTip(value + '\n' + ('\n'.join([*self._sync_errors.values(), *self._sync_issues.values()])
                                              or 'Click for synchronization details.'))

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
            pending = self.store.count_pending_changes(calendar.id)
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
                self.tray_icon.setToolTip('Debacal · calendar conflicts')
                self.tray_icon.messageClicked.connect(self.show_conflicts)
                self.tray_icon.show()
            self.tray_icon.showMessage('Calendar conflict', message.message, QSystemTrayIcon.MessageIcon.Information)

    def poll(self):
        if self._closed:
            return
        while not self.tasks.empty():
            _, future = self.tasks.get()
            detail = ''
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
                        detail += f' {len(report.issues)} import warning(s).'
                        if report.rejected:
                            detail += f' {len(report.rejected)} unsupported event resource(s) were skipped.'
                        self._sync_messages.extend(f'{issue.resource_id}: {issue.reason}' for issue in report.issues)
                    self.show_notice(detail)
            except Exception as exc:
                source = getattr(future, 'source_path', '')
                label = getattr(future, 'source_label', Path(source).name if source else '')
                detail = 'Import failed' + (' for ' + label if label else '') + ': ' + str(exc)
                self._sync_messages.append(detail)
                self.show_notice(detail)
            finally:
                self._import_memory_bytes = max(0, self._import_memory_bytes - getattr(future, 'import_memory_bytes', 0))
                batch = getattr(future, 'import_batch', None)
                if batch is not None:
                    source = getattr(future, 'source_path', '')
                    label = getattr(future, 'source_label', Path(source).name if source else '')
                    batch['messages'].append((label + ': ' if label else '') + (detail or 'No events imported.'))
                    batch['remaining'] -= 1
                    if batch['remaining'] == 0 and len(batch['messages']) > 1:
                        self.show_notice('Import complete · ' + ' · '.join(batch['messages']))
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
        self.conflict_btn.setEnabled(bool(self.engine.conflicts))
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
        if self.view in ('month', 'year'):
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
        if (event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                and self.isActiveWindow() and not QApplication.activeModalWidget()):
            modifiers = event.modifiers()
            bit = {Qt.Key.Key_Control: Qt.KeyboardModifier.ControlModifier,
                   Qt.Key.Key_Shift: Qt.KeyboardModifier.ShiftModifier,
                   Qt.Key.Key_Alt: Qt.KeyboardModifier.AltModifier,
                   Qt.Key.Key_Meta: Qt.KeyboardModifier.MetaModifier}.get(event.key())
            if bit:
                modifiers = (modifiers | bit if event.type() == QEvent.Type.KeyPress else modifiers & ~bit)
            for widget in self.event_surfaces:
                widget.set_drag_modifiers(modifiers)
            if self._drag_preview_request is not None:
                event_id, start, end, previous = self._drag_preview_request
                if bool(previous & Qt.KeyboardModifier.ControlModifier) != bool(modifiers & Qt.KeyboardModifier.ControlModifier):
                    self.preview_drag(event_id, start, end, modifiers)
        if event.type() == QEvent.Type.Resize and hasattr(self, 'scroll') and obj is self.scroll.viewport():
            QTimer.singleShot(0, self.fit_calendar)
        if event.type() == QEvent.Type.Resize and hasattr(self, 'all_day_scroll') and obj is self.all_day_scroll.viewport():
            QTimer.singleShot(0, self.sync_week_columns)
        if event.type() == QEvent.Type.Resize and hasattr(self, 'stack') and obj is self.stack:
            QTimer.singleShot(0, self.sync_week_columns)
        if event.type() == QEvent.Type.Resize and hasattr(self, 'year_scroll') and obj is self.year_scroll.viewport():
            QTimer.singleShot(0, self.fit_year)
        if (event.type() != QEvent.Type.KeyPress or not self.isActiveWindow()
                or QApplication.activeModalWidget() or QApplication.activePopupWidget()):
            return super().eventFilter(obj, event)
        focus = QApplication.focusWidget()
        text_focus = isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox))
        for action, scope in (('scope_single', 'single'), ('scope_all', 'group'), ('scope_following', 'future')):
            binding = QKeySequence(self.bindings.get(action, ''))
            if binding.count() == 1 and QKeySequence(event.keyCombination()).matches(binding) == QKeySequence.SequenceMatch.ExactMatch:
                self.set_selection_mode(scope)
                return True
        if self.pending_paste is not None:
            if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Delete):
                self.cancel_paste()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.confirm_paste()
                return True
        command_modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        search_binding = QKeySequence(self.bindings.get('search', ''))
        if ((not self._shortcut_pending or text_focus) and search_binding.count() == 1
                and (event.modifiers() & command_modifiers or event.key() >= Qt.Key.Key_Escape)
                and QKeySequence(event.keyCombination()).matches(search_binding) == QKeySequence.SequenceMatch.ExactMatch):
            self.focus_search()
            return True
        searching = focus in (self.search, self.results)
        if searching and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.flush_search()
        if event.key() == Qt.Key.Key_Escape:
            if searching or (self.search.text() and self.results.isVisible()):
                self.exit_search()
                return True
            if self.selection.refs and not self.select_mode:
                self.selection.clear()
                self.selected_id = None
                self.sync_selection()
                return True
        if searching and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                self.cycle_search_result(-1 if event.key() == Qt.Key.Key_Up else 1)
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = self.results.currentItem()
                if item is None:
                    self.cycle_search_result(1)
                    item = self.results.currentItem()
                if item is not None and item.data(MORE_ROLE) is True:
                    self.results.load_more(item)
                    self.sync_search_selection()
                    return True
                self.open_search_result(item)
                return True
            if focus is self.results and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                return super().eventFilter(obj, event)
        if text_focus:
            self._shortcut_pending.clear()
            return super().eventFilter(obj, event)
        if self.select_mode:
            char = event.text()
            if event.key() == Qt.Key.Key_Escape:
                self.select_mode = False
            elif char.isdigit() and char.isascii() and len(char) == 1:
                if self.nav_day is None:
                    index = int(char) - 1
                    if 0 <= index < len(self.canvas.days):
                        self.nav_day = index
                else:
                    self.nav_digits = (self.nav_digits + char)[:4]
            elif event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return) and self.nav_day is not None:
                options = self.events_on_day(self.canvas.days[self.nav_day])
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
            'new_event': self.create_event, 'edit_event': self.edit_event,
            'edit_single': lambda: self.edit_event(group=False), 'preferences': self.show_preferences,
            'delete_event': self.delete_event, 'search': self.focus_search,
            'copy_event': self.copy_event, 'paste_event': self.paste_events,
            'scope_single': lambda: self.set_selection_mode('single'),
            'scope_all': lambda: self.set_selection_mode('group'),
            'scope_following': lambda: self.set_selection_mode('future'),
            'cycle_scope': lambda: self.set_selection_mode({'single': 'group', 'group': 'future', 'future': 'single'}[self.selection.scope]),
            'today': self.today, 'previous': lambda: self.navigate(-1), 'next': lambda: self.navigate(1),
            'day_view': lambda: self.set_view('day'), 'week_view': lambda: self.set_view('week'),
            'full_day': self.toggle_full_day,
            'month_view': lambda: self.set_view('month'), 'year_view': lambda: self.set_view('year'), 'select_event': self.start_selection,
            'undo': self.undo, 'redo': self.redo, 'history': self.show_history,
            'export_selection': self.export_selection,
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
        variants = [event.keyCombination()]
        char = event.text()
        command_modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        if (not event.modifiers() & command_modifiers and len(char) == 1
                and char.isprintable() and not char.isalnum() and not char.isspace()):
            printable = QKeySequence(char)
            if not printable.isEmpty() and printable[0] != event.keyCombination():
                variants.append(printable[0])
        attempts = [self._shortcut_pending + [key] for key in variants]
        if self._shortcut_pending:
            attempts.extend([key] for key in variants)
        for attempt in attempts:
            if len(attempt) > 4:
                continue
            sequence = QKeySequence(*attempt)
            partial = False
            for action, binding in self.bindings.items():
                if not binding:
                    continue
                match = sequence.matches(QKeySequence(binding))
                if match == QKeySequence.SequenceMatch.ExactMatch:
                    self._shortcut_pending.clear()
                    self.shortcut_timer.stop()
                    return action
                partial |= match == QKeySequence.SequenceMatch.PartialMatch
            if partial:
                self._shortcut_pending = attempt
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
        self.search_timer.stop()
        self._import_timer.stop()
        self._import_memory_bytes -= sum(len(source.data) for sources, _calendar in self._import_requests
                                         for source in sources if source.data is not None)
        self._import_requests.clear()
        self._range_cache.clear()
        if self.engine:
            self.engine.close()
        if self.tray_icon:
            self.tray_icon.hide()
        self.executor.shutdown(wait=False, cancel_futures=True)
        event.accept()
