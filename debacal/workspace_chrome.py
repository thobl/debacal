"""Workspace layout and controls, separate from event and synchronization logic."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QWidget, QBoxLayout, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QStackedWidget, QSizePolicy, QButtonGroup,
)

from .calendar_widgets import CalendarCanvas, MonthCanvas, DayHeader, AllDayCanvas
from .compact_controls import ElidedLabel, ElidedButton
from .dialogs import hint
from .icons import icon
from .mini_calendar import MiniCalendar
from .search_panel import SearchPanel
from .year_view import YearView


class WorkspaceHeader(QWidget):
    """Keep controls on the title line only when their measured widths fit."""

    def __init__(self, title, controls):
        super().__init__()
        self.title, self.controls = title, controls
        self.inline = None
        self.row = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(12)
        self.row.setSizeConstraint(QBoxLayout.SizeConstraint.SetNoConstraint)
        self.row.addWidget(title, 1, Qt.AlignmentFlag.AlignTop)
        self.row.addWidget(controls, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self):
        # A wide layout must not increase the window's minimum width and then
        # prevent the resize that would let this header wrap onto two lines.
        return QSize(0, self.row.minimumSize().height())

    def reflow(self):
        needed = self.title.minimumSizeHint().width() + self.controls.sizeHint().width() + self.row.spacing()
        inline = self.width() >= needed
        if inline != self.inline:
            self.inline = inline
            self.row.setDirection(QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


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
        sidebar_shell = QWidget()
        sidebar_shell.setObjectName('sidebarShell')
        sidebar_shell.setFixedWidth(280)
        shell_layout = QVBoxLayout(sidebar_shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        root.addWidget(sidebar_shell)
        self.sidebar = QWidget()
        self.sidebar.setObjectName('sidebar')
        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setWidget(self.sidebar)
        shell_layout.addWidget(self.sidebar_scroll, 1)
        layout = QVBoxLayout(self.sidebar)
        layout.setContentsMargins(16, 18, 16, 12)
        layout.setSpacing(8)
        brand = QLabel('debacal')
        brand.setObjectName('brand')
        brand_row = QHBoxLayout()
        brand_row.addWidget(brand, 1)
        self.icon_button('settings', 'Preferences', self.show_preferences, brand_row, 'preferences')
        layout.addLayout(brand_row)
        if self.demo_mode:
            self.button('Open my calendars', self.real_workspace_requested.emit, layout)
        self.mini = MiniCalendar(self.preferences)
        self.mini.setMinimumWidth(0)
        self.mini.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.mini.clicked.connect(lambda value: self.go_date(value.toPython()))
        layout.addWidget(self.mini)
        calendars_heading = QHBoxLayout()
        calendars_label = QLabel('Calendars')
        calendars_label.setObjectName('section')
        calendars_heading.addWidget(calendars_label, 1)
        self.icon_button('import', 'Import calendar file', self.import_ics, calendars_heading)
        self.icon_button('export', 'Export calendar file', self.export_ics, calendars_heading)
        self.icon_button('new', 'Add calendar', self.add_calendar, calendars_heading)
        layout.addLayout(calendars_heading)
        calendars_body = QWidget()
        self.calendar_box = QVBoxLayout(calendars_body)
        self.calendar_box.setContentsMargins(0, 0, 0, 0)
        self.calendar_box.setSpacing(2)
        self.calendar_rows = {}
        self.creation_button_group = QButtonGroup(self)
        self.calendar_scroll = QScrollArea()
        self.calendar_scroll.setWidgetResizable(True)
        self.calendar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.calendar_scroll.setWidget(calendars_body)
        layout.addWidget(self.calendar_scroll)
        search_row = QHBoxLayout()
        search_row.setSpacing(4)
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('Search events')
        self.search.textChanged.connect(self.search_changed)
        search_row.addWidget(self.search, 1)
        self.filter_search = self.icon_button('filter', 'Filter calendar to match search',
                                              self.toggle_search_filter, search_row)
        self.filter_search.setCheckable(True)
        self.filter_search.setChecked(bool(self.store.get_setting('filter_calendar_by_search', False)))
        layout.addLayout(search_row)
        self.results_heading = QLabel('Next events')
        self.results_heading.setObjectName('section')
        layout.addWidget(self.results_heading)
        self.results = SearchPanel(self.store, self.preferences)
        self.results.setMinimumHeight(60)
        self.results.result_selected.connect(self.select_search_result)
        self.results.activated.connect(self.activate_search_result)
        layout.addWidget(self.results, 1)
        footer_widget = QWidget()
        footer_widget.setObjectName('sidebarFooter')
        footer = QHBoxLayout(footer_widget)
        footer.setContentsMargins(16, 0, 16, 12)
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
        shell_layout.addWidget(footer_widget)

    def toolbar_section(self, title):
        section = QWidget()
        section.setAccessibleName(title)
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        label = QLabel(title)
        label.setObjectName('section')
        heading = QHBoxLayout()
        heading.setSpacing(4)
        heading.addWidget(label)
        heading.addStretch()
        column.addLayout(heading)
        buttons = QHBoxLayout()
        buttons.setSpacing(3)
        column.addLayout(buttons)
        return section, heading, buttons

    def segmented_control(self, row):
        control = QWidget()
        control.setObjectName('segmentedControl')
        buttons = QHBoxLayout(control)
        buttons.setContentsMargins(3, 3, 3, 3)
        buttons.setSpacing(0)
        row.addWidget(control)
        return control, buttons

    @staticmethod
    def style_segments(buttons):
        for index, button in enumerate(buttons):
            button.setProperty('segment', 'first' if index == 0 else 'last' if index == len(buttons) - 1 else 'middle')

    def build_workspace(self, root):
        area = QWidget()
        root.addWidget(area, 1)
        outer = QVBoxLayout(area)
        outer.setContentsMargins(18, 18, 18, 8)
        outer.setSpacing(10)
        title_widget = QWidget()
        header = QHBoxLayout(title_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)
        navigation = QHBoxLayout()
        navigation.setSpacing(3)
        self.previous_button = self.icon_button('previous', 'Previous period', lambda: self.navigate(-1), navigation, 'previous')
        self.today_button = self.icon_button('today', 'Go to today', self.today, navigation, 'today')
        self.next_button = self.icon_button('next', 'Next period', lambda: self.navigate(1), navigation, 'next')
        header.addLayout(navigation)
        self.period = QWidget()
        period_labels = QVBoxLayout(self.period)
        period_labels.setContentsMargins(0, 0, 0, 0)
        period_labels.setSpacing(3)
        self.heading = ElidedLabel()
        self.heading.setObjectName('heading')
        self.heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        period_labels.addWidget(self.heading)
        self.range_label = hint('')
        self.range_label.setWordWrap(False)
        self.range_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        period_labels.addWidget(self.range_label)
        header.addWidget(self.period)
        header.addStretch()
        self.toolbar = QWidget()
        toolbar = QHBoxLayout(self.toolbar)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(16)
        view_section, _view_heading, view_row = self.toolbar_section('View')
        self.view_control, view_segments = self.segmented_control(view_row)
        self.view_buttons = {}
        self.view_button_group = QButtonGroup(self)
        for mode in ('day', 'week', 'month', 'year'):
            button = self.icon_button(mode, mode.capitalize() + ' view', lambda value=mode: self.set_view(value),
                                      view_segments, mode + '_view')
            button.setCheckable(True)
            self.view_button_group.addButton(button)
            self.view_buttons[mode] = button
        self.style_segments(list(self.view_buttons.values()))
        view_row.addSpacing(5)
        self.full_day_button = self.icon_button('expand', 'Expand to show all 24 hours', self.toggle_full_day,
                                                view_row, 'full_day')
        self.full_day_button.setCheckable(True)
        self.full_day_button.setChecked(self.full_day)
        selection_section, selection_heading, selection_row = self.toolbar_section('Selection')
        self.export_selection_button = self.icon_button('export', 'Export selected events as .ics', self.export_selection,
                                                        selection_heading, 'export_selection')
        self.export_selection_button.setProperty('compactIcon', True)
        self.export_selection_button.setEnabled(False)
        self.selection_control, selection_segments = self.segmented_control(selection_row)
        self.selection_buttons = {}
        self.selection_button_group = QButtonGroup(self)
        for mode, title, symbol, binding in (
                ('single', 'Select one event', 'single', 'scope_single'),
                ('group', 'Select all in a series', 'series', 'scope_all'),
                ('future', 'Select this and following events', 'following', 'scope_following')):
            button = self.icon_button(symbol, title, lambda value=mode: self.set_selection_mode(value), selection_segments, binding)
            button.setCheckable(True)
            self.selection_button_group.addButton(button)
            self.selection_buttons[mode] = button
        self.style_segments(list(self.selection_buttons.values()))
        events_section, _events_heading, events_row = self.toolbar_section('Events')
        events_row.setContentsMargins(0, 3, 0, 3)
        self.new_button = self.icon_button('new', 'Create an event', self.create_event, events_row, 'new_event')
        self.icon_button('undo', 'Undo', self.undo, events_row, 'undo')
        self.redo_button = self.icon_button('redo', 'Redo', self.redo, events_row, 'redo')
        self.icon_button('history', 'Operation history', self.show_history, events_row, 'history')
        for section in (events_section, selection_section, view_section):
            toolbar.addWidget(section)
        self.workspace_header = WorkspaceHeader(title_widget, self.toolbar)
        outer.addWidget(self.workspace_header)
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
        self.year = YearView()
        self.year_scroll = QScrollArea()
        self.year_scroll.setWidgetResizable(True)
        self.year_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.year_scroll.setWidget(self.year)
        self.stack.addWidget(self.year_scroll)
        self.year.day_selected.connect(self.select_day)
        self.year.month_requested.connect(self.open_month)
        self.year.open_day.connect(self.open_day)
        self.year.selected_scoped.connect(lambda event_id, modifiers: self.select_event(
            event_id, day=self.year.selected_day, additive=bool(modifiers & Qt.KeyboardModifier.ControlModifier)))
        self.year.create_all_day_requested.connect(self.create_all_day_event)
        self.year.context_requested.connect(self.event_menu)
        self.year.placement_moved.connect(lambda target: self.move_paste_preview(target, self.year))
        self.year.placement_confirmed.connect(lambda target: self.confirm_paste())
        for widget in (self.canvas, self.month, self.all_day_canvas):
            widget.create_requested.connect(self.create_event)
            widget.edit_scoped_requested.connect(self.edit_from_double_click)
            widget.gesture_started.connect(self.remember_selection_for_gesture)
            widget.selected_scoped.connect(lambda event_id, modifiers, source=widget:
                self.select_event(event_id, day=source.selected_day,
                                  additive=bool(modifiers & Qt.KeyboardModifier.ControlModifier)))
            widget.context_requested.connect(self.event_menu)
            widget.confirm_requested.connect(self.confirm_candidate)
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

    def refresh_calendar_list(self, calendars):
        from .calendar_list import CalendarRow
        default = self.store.get_default_calendar()
        identifiers = {calendar.id for calendar in calendars}
        for calendar_id in self.calendar_rows.keys() - identifiers:
            row = self.calendar_rows.pop(calendar_id)
            self.calendar_box.removeWidget(row)
            row.deleteLater()
        for index, calendar in enumerate(calendars):
            row = self.calendar_rows.get(calendar.id)
            if row is None:
                row = CalendarRow(calendar)
                row.creation_requested.connect(self.set_creation_calendar)
                row.visibility_requested.connect(self.toggle_calendar)
                row.context_requested.connect(self.calendar_menu)
                self.creation_button_group.addButton(row.choice)
                self.calendar_rows[calendar.id] = row
            self.calendar_box.insertWidget(index, row)
            row.set_calendar(calendar, current=bool(default and default.id == calendar.id),
                              scale=self.preferences.font_scale)
        row_height = max((row.sizeHint().height() for row in self.calendar_rows.values()), default=28)
        full_height = max(1, len(calendars)) * (row_height + 2)
        self.calendar_scroll.setMinimumHeight(full_height if len(calendars) <= 6 else 3 * (row_height + 2))
        self.calendar_scroll.setMaximumHeight(min(full_height, 8 * (row_height + 2)))

    def update_command_tooltips(self):
        for button, title, binding in self.command_buttons:
            shortcut = self.bindings.get(binding, '') if binding else ''
            button.setToolTip(title + (f' ({shortcut})' if shortcut else ''))
            if button.property('iconOnly'):
                compact = button.property('compactIcon')
                size = max(18 if compact else 30, round((20 if compact else 32) * self.preferences.font_scale / 100))
                button.setFixedSize(size, size)
                if compact:
                    button.setIconSize(QSize(max(12, round(14 * self.preferences.font_scale / 100)),
                                             max(12, round(14 * self.preferences.font_scale / 100))))
        self.update_navigation_width()
        self.workspace_header.reflow()
        # Large text can exceed a short window. Keep controls apart and let the
        # whole sidebar scroll in that case; ordinary calendar lists remain full.
        self.sidebar.layout().activate()
        self.sidebar.setMinimumHeight(self.sidebar.layout().minimumSize().height())

    def update_navigation_width(self):
        """Reserve every month/range label's width so repeated clicks stay put."""
        from datetime import date
        self.heading.ensurePolished()
        self.range_label.ensurePolished()
        signature = self.preferences, self.preferences.qt_locale().name(), self.heading.font().key(), self.range_label.font().key()
        if signature == getattr(self, '_navigation_width_signature', None):
            return
        self._navigation_width_signature = signature
        heading_width = max(self.heading.fontMetrics().horizontalAdvance(self.preferences.format_month(date(2026, month, 1)))
                            for month in range(1, 13))
        date_width = max(self.range_label.fontMetrics().horizontalAdvance(self.preferences.format_date(date(2026, month, day)))
                         for month in range(1, 13) for day in (1, 28))
        range_width = date_width * 2 + self.range_label.fontMetrics().horizontalAdvance(' — ')
        self.period.setFixedWidth(max(heading_width, range_width) + 6)
