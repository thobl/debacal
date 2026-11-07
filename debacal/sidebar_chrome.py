"""Source controls and agenda/search grouped independently of the calendar grid."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .compact_controls import ElidedButton
from .calendar_list import CalendarList
from .mini_calendar import MiniCalendar
from .search_panel import SearchPanel
from .search_header import SearchHeader


def _section(name, margins, parent_layout, stretch=0):
    widget = QWidget()
    widget.setObjectName(name)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(*margins)
    layout.setSpacing(8)
    parent_layout.addWidget(widget, stretch)
    return widget, layout


def build_sidebar(window, root):
    window.command_buttons = []
    shell = QWidget()
    shell.setObjectName('sidebarShell')
    shell.setFixedWidth(300)
    shell_layout = QVBoxLayout(shell)
    shell_layout.setContentsMargins(0, 0, 0, 0)
    shell_layout.setSpacing(0)
    root.addWidget(shell)

    window.sidebar = QWidget()
    window.sidebar.setObjectName('sidebar')
    window.sidebar_scroll = QScrollArea()
    window.sidebar_scroll.setWidgetResizable(True)
    window.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.sidebar_scroll.setWidget(window.sidebar)
    shell_layout.addWidget(window.sidebar_scroll, 1)
    outer = QVBoxLayout(window.sidebar)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

    window.sidebar_overview, overview = _section('sidebarOverview', (18, 16, 18, 16), outer)
    brand_row = QHBoxLayout()
    brand = QLabel('debacal')
    brand.setObjectName('brand')
    brand_row.addWidget(brand, 1)
    window.icon_button('settings', 'Preferences', window.show_preferences, brand_row, 'preferences')
    overview.addLayout(brand_row)
    if window.profile_label:
        profile = QLabel(window.profile_label + ' profile')
        profile.setObjectName('profileBadge')
        profile.setProperty('muted', True)
        overview.addWidget(profile)
    if window.demo_mode:
        window.button('Open ' + window.profile_label + ' profile' if window.profile_label else 'Open my calendars',
                      window.real_workspace_requested.emit, overview)
    window.mini = MiniCalendar(window.preferences)
    window.mini.setMinimumWidth(0)
    window.mini.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    window.mini.clicked.connect(lambda value: window.go_date(value.toPython()))
    overview.addWidget(window.mini)

    window.source_sections, sections = _section('sourceSections', (0, 0, 0, 0), outer, 1)
    sections.setSpacing(0)
    window.calendar_section, calendars = _section('calendarSection', (18, 12, 14, 16), sections)
    heading = QHBoxLayout()
    heading.setSpacing(4)
    label = QLabel('Calendars')
    label.setObjectName('section')
    heading.addWidget(label, 1)
    window.icon_button('import', 'Import calendar file', window.import_ics, heading)
    window.icon_button('export', 'Export calendar file', window.export_ics, heading)
    window.icon_button('new', 'Add calendar', window.add_calendar, heading)
    calendars.addLayout(heading)
    body = window.calendar_list = CalendarList()
    window.calendar_box = body.box
    body.order_changed.connect(window.reorder_calendars)
    window.calendar_rows = {}
    window.creation_button_group = QButtonGroup(window)
    window.calendar_scroll = QScrollArea()
    window.calendar_scroll.setObjectName('calendarSources')
    window.calendar_scroll.viewport().setObjectName('calendarListViewport')
    window.calendar_scroll.setWidgetResizable(True)
    window.calendar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    window.calendar_scroll.setWidget(body)
    calendars.addWidget(window.calendar_scroll)

    divider = QFrame()
    divider.setObjectName('sourceDivider')
    divider.setFixedHeight(1)
    sections.addWidget(divider)
    window.agenda_section, agenda = _section('agendaSection', (18, 14, 14, 8), sections, 1)
    window.search = QLineEdit()
    window.search.setClearButtonEnabled(True)
    window.search.setAccessibleName('Search events')
    window.search.textChanged.connect(window.search_changed)
    window.filter_search = window.icon_button('filter', 'Filter calendar to match search',
                                              window.toggle_search_filter, None)
    window.filter_search.setCheckable(True)
    window.filter_search.setChecked(bool(window.store.get_setting('filter_calendar_by_search', False)))
    window.results_heading = QLabel('Next events')
    window.results_heading.setObjectName('section')
    window.search_header = SearchHeader(window.search, window.filter_search, window.results_heading,
                                        window.preferences, window.bindings.get('search', ''))
    window.search_button = window.search_header.button
    window.search_header.search_requested.connect(window.focus_search)
    agenda.addWidget(window.search_header)
    window.results = SearchPanel(window.store, window.preferences)
    window.results.setMinimumHeight(60)
    window.results.result_selected.connect(window.select_search_result)
    window.results.activated.connect(window.activate_search_result)
    agenda.addWidget(window.results, 1)

    footer_widget = QWidget()
    footer_widget.setObjectName('sidebarFooter')
    footer = QHBoxLayout(footer_widget)
    footer.setContentsMargins(18, 0, 14, 12)
    footer.setSpacing(4)
    window.sync_btn = window.icon_button('sync', 'Sync calendars', window.sync_now, footer, 'sync')
    window.status = ElidedButton()
    window.status.clicked.connect(window.show_sync_status)
    window.status.setCursor(Qt.CursorShape.PointingHandCursor)
    window.status.setObjectName('syncStatus')
    window.status.setAccessibleName('Synchronization status and details')
    window.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    footer.addWidget(window.status, 1)
    window.conflict_btn = window.icon_button('conflict', 'Review conflicts', window.show_conflicts, footer)
    window.conflict_btn.setEnabled(False)
    policy = window.conflict_btn.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    window.conflict_btn.setSizePolicy(policy)
    window.conflict_btn.hide()
    shell_layout.addWidget(footer_widget)
