"""Workspace layout and controls, separate from event and synchronization logic."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout,
    QScrollArea, QStackedWidget, QSizePolicy, QButtonGroup,
)

from .calendar_widgets import CalendarCanvas, MonthCanvas, DayHeader, AllDayCanvas
from .compact_controls import ElidedLabel
from .dialogs import hint
from .icons import icon
from .theme import scheme_for
from .shortcuts import shortcut_label
from .year_view import YearView
from .workspace_header import SegmentedControl, WorkspaceHeader
from .buttons import action_button


class WorkspaceChrome:
    """Build widgets and connect them to the window's application actions."""

    def button(self, title, action, parent_layout=None, primary=False):
        button = action_button(title, action, primary=primary)
        if parent_layout is not None:
            parent_layout.addWidget(button)
        return button

    def icon_button(self, name, title, action, layout, binding=None):
        button = self.button('', action, layout)
        button.setIcon(icon(name, scheme_for(self.preferences).accent))
        button.setProperty('iconName', name)
        button.setProperty('interfaceIconColor', scheme_for(self.preferences).accent)
        button.setIconSize(QSize(20, 20))
        button.setProperty('iconOnly', True)
        button.setAccessibleName(title)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.command_buttons.append((button, title, binding))
        return button

    def build_sidebar(self, root):
        from .sidebar_chrome import build_sidebar
        build_sidebar(self, root)

    def toolbar_section(self, title):
        section = QWidget()
        section.setAccessibleName(title)
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        label = ElidedLabel(title)
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
        control = SegmentedControl()
        buttons = control.buttons_layout
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
        self.period = QWidget()
        self.previous_button = self.icon_button('previous', 'Previous period', lambda: self.navigate(-1), None, 'previous')
        self.today_button = self.icon_button('today', 'Go to today', self.today, None, 'today')
        self.next_button = self.icon_button('next', 'Next period', lambda: self.navigate(1), None, 'next')
        self.heading = ElidedLabel()
        self.heading.setObjectName('heading')
        self.range_label = hint('')
        self.range_label.setWordWrap(False)
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
        self.view_control.set_buttons(list(self.view_buttons.values()))
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
        self.selection_control.set_buttons(list(self.selection_buttons.values()))
        events_section, _events_heading, events_row = self.toolbar_section('Events')
        events_row.setContentsMargins(0, 3, 0, 3)
        self.new_button = self.icon_button('new', 'Create an event', self.create_event, events_row, 'new_event')
        self.undo_button = self.icon_button('undo', 'Undo', self.undo, events_row, 'undo')
        self.redo_button = self.icon_button('redo', 'Redo', self.redo, events_row, 'redo')
        self.icon_button('history', 'Operation history', self.show_history, events_row, 'history')
        for section in (events_section, selection_section, view_section):
            toolbar.addWidget(section)
        self.toolbar_sections = (events_section, selection_section, view_section)
        self.workspace_header = WorkspaceHeader(self, self.period, self.toolbar)
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
                self.calendar_list.attach_row(row)
            self.calendar_box.insertWidget(index, row)
            if row.preferences != self.preferences:
                row.set_preferences(self.preferences)
            row.set_calendar(calendar, current=bool(default and default.id == calendar.id),
                              scale=self.preferences.font_scale)
        row_height = max((row.sizeHint().height() for row in self.calendar_rows.values()), default=28)
        gap = self.calendar_box.spacing()
        full_height = max(1, len(calendars)) * row_height + max(0, len(calendars) - 1) * gap
        self.calendar_scroll.setMinimumHeight(full_height if len(calendars) <= 6 else 3 * (row_height + gap))
        self.calendar_scroll.setMaximumHeight(min(full_height, 8 * (row_height + gap)))

    def update_command_tooltips(self):
        scheme = scheme_for(self.preferences)
        for button, title, binding in self.command_buttons:
            icon_color = getattr(scheme, button.property('iconColorRole') or 'accent')
            if button.property('interfaceIconColor') != icon_color:
                button.setIcon(icon(button.property('iconName'), icon_color))
                button.setProperty('interfaceIconColor', icon_color)
            shortcut = shortcut_label(self.bindings.get(binding, '')) if binding else ''
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
        # The sidebar layout propagates nested section minimum sizes so large
        # text uses the outer scroll area rather than overlapping adjacent rows.
        self.sidebar.layout().activate()

    def refresh_sidebar_title(self):
        self.brand_label.setText(self.preferences.sidebar_title)
        self.brand_label.setToolTip(self.preferences.sidebar_title)
        show = self.preferences.show_sidebar_title
        destination = self.brand_bar if show else self.sidebar_footer
        if self.preferences_button.parentWidget() is not destination:
            previous = self.preferences_button.parentWidget()
            if previous is not None and previous.layout() is not None:
                previous.layout().removeWidget(self.preferences_button)
            layout = destination.layout()
            if show:
                layout.addWidget(self.preferences_button)
            else:
                layout.addWidget(self.preferences_button)
            self.preferences_button.show()
        self.brand_bar.setVisible(show)

    def update_navigation_width(self):
        """Refresh shared header metrics for the current view and preferences."""
        self.workspace_header.update_preferences()
