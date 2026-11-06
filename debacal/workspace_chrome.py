"""Workspace layout and controls, separate from event and synchronization logic."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QStackedWidget, QFrame, QMenu, QComboBox, QSizePolicy, QButtonGroup,
)

from .calendar_widgets import CalendarCanvas, MonthCanvas, DayHeader, AllDayCanvas
from .compact_controls import ElidedLabel, ElidedButton
from .dialogs import hint
from .icons import icon
from .mini_calendar import MiniCalendar
from .search_panel import SearchPanel


class WorkspaceChrome:
    """Build widgets and connect them to the window's application actions."""

    def button(self, title, action, parent_layout=None, primary=False):
        button = QPushButton(title)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _=False: action())
        if primary:
            button.setObjectName('primary')
        if parent_layout is not None:
            parent_layout.addWidget(button)
        return button

    def icon_button(self, name, title, action, layout, binding=None):
        button = self.button('', action, layout)
        button.setIcon(icon(name))
        button.setIconSize(QSize(20, 20))
        button.setProperty('iconOnly', True)
        button.setAccessibleName(title)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.command_buttons.append((button, title, binding))
        return button

    def build_sidebar(self, root):
        self.command_buttons = []
        self.sidebar = QWidget()
        self.sidebar.setObjectName('sidebar')
        self.sidebar.setFixedWidth(280)
        root.addWidget(self.sidebar)
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(16, 18, 16, 12)
        layout.setSpacing(9)
        brand = QLabel('debacal')
        brand.setObjectName('brand')
        brand_row = QHBoxLayout()
        brand_row.addWidget(brand, 1)
        self.icon_button('settings', 'Preferences', self.show_preferences, brand_row, 'preferences')
        self.more_button = self.icon_button('more', 'More actions', lambda: None, brand_row)
        self.app_menu = QMenu(self)
        for title, action in [('Import .ics…', self.import_ics), ('Export .ics…', self.export_ics),
                              ('Operation history…', self.show_history), ('Keyboard shortcuts…', self.show_shortcuts)]:
            self.app_menu.addAction(title, action)
        self.more_button.setMenu(self.app_menu)
        layout.addLayout(brand_row)
        self.new_button = self.button('New event', self.create_event, layout, True)
        self.new_button.setIcon(icon('new', '#ffffff'))
        self.command_buttons.append((self.new_button, 'Create an event', 'new_event'))
        creation_row = QHBoxLayout()
        create_label = QLabel('Create &in')
        self.creation_calendar = QComboBox()
        self.creation_calendar.setAccessibleName('Calendar for new events')
        self.creation_calendar.setToolTip('Choose the calendar used for new events.')
        self.creation_calendar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.creation_calendar.currentIndexChanged.connect(self.change_creation_calendar)
        create_label.setBuddy(self.creation_calendar)
        creation_row.addWidget(create_label)
        creation_row.addWidget(self.creation_calendar, 1)
        layout.addLayout(creation_row)
        if self.demo_mode:
            self.button('Open my calendars', self.real_workspace_requested.emit, layout)
        self.mini = MiniCalendar(self.preferences)
        self.mini.setMinimumWidth(0)
        self.mini.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.mini.clicked.connect(lambda value: self.go_date(value.toPython()))
        sources_body = QWidget()
        sources_layout = QVBoxLayout(sources_body)
        sources_layout.setContentsMargins(0, 0, 0, 0)
        sources_layout.setSpacing(8)
        sources_layout.addWidget(self.mini)
        calendars_heading = QHBoxLayout()
        calendars_label = QLabel('Calendars')
        calendars_label.setObjectName('section')
        calendars_heading.addWidget(calendars_label)
        calendars_heading.addStretch()
        self.icon_button('new', 'Add calendar', self.add_calendar, calendars_heading)
        sources_layout.addLayout(calendars_heading)
        calendars_body = QWidget()
        self.calendar_box = QVBoxLayout(calendars_body)
        self.calendar_box.setContentsMargins(0, 0, 0, 0)
        self.calendar_box.setSpacing(0)
        self.calendar_scroll = QScrollArea()
        self.calendar_scroll.setWidgetResizable(True)
        self.calendar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.calendar_scroll.setWidget(calendars_body)
        sources_layout.addWidget(self.calendar_scroll)
        sources_layout.addStretch()
        self.sources_scroll = QScrollArea()
        self.sources_scroll.setWidgetResizable(True)
        self.sources_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sources_scroll.setWidget(sources_body)
        self.sources_scroll.setMinimumHeight(120)
        layout.addWidget(self.sources_scroll, 3)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('Search events')
        self.search.textChanged.connect(self.run_search)
        layout.addWidget(self.search)
        self.results_heading = QLabel('Next events')
        self.results_heading.setObjectName('section')
        layout.addWidget(self.results_heading)
        self.results = SearchPanel(self.store, self.preferences)
        self.results.setMinimumHeight(70)
        self.results.result_selected.connect(self.select_search_result)
        self.results.activated.connect(self.activate_search_result)
        layout.addWidget(self.results, 1)
        self.build_selection_details(layout)
        footer = QHBoxLayout()
        footer.setSpacing(4)
        self.sync_btn = self.icon_button('sync', 'Sync calendars', self.sync_now, footer, 'sync')
        self.status = ElidedButton()
        self.status.clicked.connect(self.show_sync_status)
        self.status.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.addWidget(self.status)
        self.status.setObjectName('syncStatus')
        self.status.setAccessibleName('Synchronization status and details')
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        footer.setStretch(1, 1)
        self.conflict_btn = self.icon_button('conflict', 'Review conflicts', self.show_conflicts, footer)
        self.conflict_btn.setEnabled(False)
        policy = self.conflict_btn.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.conflict_btn.setSizePolicy(policy)
        self.conflict_btn.hide()
        layout.addLayout(footer)

    def build_selection_details(self, layout):
        self.details = QFrame()
        self.details.setObjectName('details')
        row = QHBoxLayout(self.details)
        row.setContentsMargins(0, 6, 0, 6)
        self.detail_text = QLabel()
        self.detail_text.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_text.setWordWrap(True)
        self.detail_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        row.addWidget(self.detail_text, 1)
        self.confirm_btn = self.icon_button('confirm', 'Confirm this slot and remove alternatives',
                                            self.confirm_candidate, row)
        self.confirm_btn.hide()
        self.details.setFixedHeight(54)
        self.details.hide()
        layout.addWidget(self.details)

    def build_workspace(self, root):
        area = QWidget()
        root.addWidget(area, 1)
        outer = QVBoxLayout(area)
        outer.setContentsMargins(18, 18, 18, 8)
        outer.setSpacing(10)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self.heading = ElidedLabel()
        self.heading.setObjectName('heading')
        self.heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.heading, 1)
        self.icon_button('previous', 'Previous period', lambda: self.navigate(-1), toolbar, 'previous')
        self.today_button = self.button('Today', self.today, toolbar)
        self.command_buttons.append((self.today_button, 'Go to today', 'today'))
        self.icon_button('next', 'Next period', lambda: self.navigate(1), toolbar, 'next')
        toolbar.addSpacing(10)
        self.view_buttons = {}
        self.view_button_group = QButtonGroup(self)
        for mode in ('day', 'week', 'month'):
            button = self.icon_button(mode, mode.capitalize() + ' view', lambda value=mode: self.set_view(value),
                                      toolbar, mode + '_view')
            button.setCheckable(True)
            self.view_button_group.addButton(button)
            self.view_buttons[mode] = button
        toolbar.addSpacing(10)
        self.selection_buttons = {}
        self.selection_button_group = QButtonGroup(self)
        for mode, title, symbol, binding in (
                ('single', 'Select one event', 'single', 'scope_single'),
                ('group', 'Select all in a series', 'series', 'scope_all'),
                ('future', 'Select this and following events', 'following', 'scope_following')):
            button = self.icon_button(symbol, title, lambda value=mode: self.set_selection_mode(value), toolbar, binding)
            button.setCheckable(True)
            self.selection_button_group.addButton(button)
            self.selection_buttons[mode] = button
        toolbar.addSpacing(10)
        self.full_day_button = self.icon_button('clock', 'Show all 24 hours', self.toggle_full_day, toolbar)
        self.full_day_button.setCheckable(True)
        self.full_day_button.setChecked(self.full_day)
        outer.addLayout(toolbar)
        self.range_label = hint('')
        self.range_label.setWordWrap(False)
        outer.addWidget(self.range_label)
        self.stack = QStackedWidget()
        week = QWidget()
        week_layout = QVBoxLayout(week)
        week_layout.setContentsMargins(0, 0, 0, 0)
        week_layout.setSpacing(0)
        self.header = DayHeader()
        week_layout.addWidget(self.header)
        self.all_day_canvas = AllDayCanvas()
        self.all_day_scroll = QScrollArea()
        self.all_day_scroll.setWidgetResizable(True)
        self.all_day_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.all_day_scroll.setWidget(self.all_day_canvas)
        week_layout.addWidget(self.all_day_scroll)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.canvas = CalendarCanvas()
        self.canvas.fixed_all_day = True
        self.scroll.setWidget(self.canvas)
        week_layout.addWidget(self.scroll, 1)
        self.stack.addWidget(week)
        self.month = MonthCanvas()
        self.month.open_day.connect(self.open_day)
        self.stack.addWidget(self.month)
        for widget in (self.canvas, self.month, self.all_day_canvas):
            widget.create_requested.connect(self.create_event)
            widget.edit_scoped_requested.connect(self.edit_from_double_click)
            widget.gesture_started.connect(self.remember_selection_for_gesture)
            widget.selected_scoped.connect(lambda event_id, modifiers, source=widget:
                self.select_event(event_id, day=source.selected_day,
                                  additive=bool(modifiers & Qt.KeyboardModifier.ControlModifier)))
            widget.context_requested.connect(self.event_menu)
            widget.move_scoped_requested.connect(lambda reference, start, end, modifiers:
                self.move_event(reference.id, start, end, reference=reference,
                                copy=bool(modifiers & Qt.KeyboardModifier.ControlModifier)))
            widget.drag_preview_requested.connect(self.preview_drag)
        for widget in (self.header, self.canvas, self.month, self.all_day_canvas):
            widget.day_selected.connect(self.select_day)
            widget.create_all_day_requested.connect(self.create_all_day_event)
            if hasattr(widget, 'placement_moved'):
                widget.placement_moved.connect(lambda target, source=widget: self.move_paste_preview(target, source))
                widget.placement_confirmed.connect(lambda target: self.confirm_paste())
        outer.addWidget(self.stack, 1)
        # A bounded message line keeps errors and transient feedback from moving
        # the calendar. The complete message remains available in its tooltip.
        self.message_stack = QStackedWidget()
        self.notice = ElidedLabel()
        self.navigation_prompt = ElidedLabel()
        for label in (self.notice, self.navigation_prompt):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            self.message_stack.addWidget(label)
        self.message_stack.setFixedHeight(28)
        outer.addWidget(self.message_stack)

    def refresh_creation_calendars(self, calendars):
        default = self.store.get_default_calendar()
        blocked = self.creation_calendar.blockSignals(True)
        try:
            self.creation_calendar.clear()
            for calendar in calendars:
                if calendar.kind != 'ics' and not calendar.read_only:
                    swatch = QPixmap(12, 12)
                    swatch.fill(QColor(calendar.color))
                    self.creation_calendar.addItem(QIcon(swatch), calendar.name, calendar.id)
            index = self.creation_calendar.findData(default.id if default else None)
            self.creation_calendar.setCurrentIndex(index)
            self.creation_calendar.setEnabled(index >= 0)
            self.creation_calendar.setToolTip('Create new events in ' + default.name if default else 'Add a writable calendar first.')
        finally:
            self.creation_calendar.blockSignals(blocked)

    def update_command_tooltips(self):
        self.sources_scroll.setMaximumHeight(self.mini.minimumHeight()
            + self.calendar_scroll.height() + round(48 * self.preferences.font_scale / 100))
        for button, title, binding in self.command_buttons:
            shortcut = self.bindings.get(binding, '') if binding else ''
            button.setToolTip(title + (f' ({shortcut})' if shortcut else ''))
            if button.property('iconOnly'):
                size = max(30, round(32 * self.preferences.font_scale / 100))
                button.setFixedSize(size, size)
