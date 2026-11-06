"""Design-only exploration: one visual language for sidebar navigation lists."""
from types import MethodType
from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QPen, QPolygonF, QTextCharFormat
from PySide6.QtWidgets import QApplication, QFrame, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout, QWidget

from debacal.search_panel import COLOR_ROLE, SUBTITLE_ROLE


NAME = 'Shared list language'
THESIS = ('Treat calendars and agenda/search as one navigation system: aligned source bars and text, '
          'the same flat selection treatment, and one slate pane with clear section rules.')
TRADEOFF = ('Creation targets and event selections look deliberately related. Section headings and '
            'independent eye controls must explain their different roles; the wider sidebar gives less room to the calendar.')

INK = '#253342'
MUTED = '#586979'
PANE = '#f0f3f6'
SELECTED = '#dce5ed'
EDGE = '#6f8498'
RULE = '#ccd7e0'
GUTTER = 16


_STYLE = """
QWidget { color: #253342; font-size: 13px; }
QMainWindow, QDialog { background: #ffffff; }
QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter { background: #ffffff; }
QWidget#sidebarShell { border-right: 1px solid #bbc9d4; }
QWidget#sidebarFooter { border-top: 1px solid #ccd7e0; }
QLabel#brand { color: #253342; font-size: 19px; font-weight: 600; letter-spacing: -0.3px; }
QLabel#profileBadge, QLabel[muted="true"] { color: #586979; font-size: 12px; }
QLabel#section { color: #526678; font-size: 12px; font-weight: 600; }
QFrame#sharedNavigationPane { background: #f0f3f6; border: 0; border-top: 1px solid #ccd7e0; }
QFrame#sharedSectionHeader { background: transparent; border: 0; }
QFrame#sharedSectionRule { background: #ccd7e0; border: 0; min-height: 1px; max-height: 1px; }
QWidget#sharedCalendarBody { background: #f0f3f6; }
QWidget#sharedSourceRow { background: transparent; border: 0; border-left: 3px solid transparent; border-radius: 0; }
QWidget#sharedSourceRow[creationCurrent="true"] { background: #dce5ed; border-left-color: #6f8498; }
QPushButton#calendarChoice, QPushButton#calendarChoice:checked {
    color: #253342; text-align: left; background: transparent; border: 0; border-radius: 0;
    padding: 0; font-size: 13px; font-weight: 400;
}
QWidget#sharedSourceRow[creationCurrent="true"] QPushButton#calendarChoice { font-weight: 600; }
QPushButton#calendarChoice:hover { background: transparent; border: 0; }
QPushButton#sharedVisibility, QPushButton#sharedVisibility:checked {
    background: transparent; border: 0; padding: 3px; border-radius: 2px;
}
QPushButton#sharedVisibility:hover { background: #dbe4ec; }
QTreeWidget#sharedAgenda { background: #f0f3f6; border: 0; border-radius: 0; padding: 0; outline: 0; }
QTreeWidget#sharedAgenda::item { background: transparent; border: 0; padding: 0; }
QTreeWidget#sharedAgenda::item:selected { background: transparent; color: #253342; }
QTreeWidget#sharedAgenda::branch { background: transparent; }
QTreeWidget#sharedAgenda::branch:has-children { image: none; }
QPushButton { background: #ffffff; border: 1px solid #b7c5d0; border-radius: 3px; padding: 6px 10px; }
QPushButton:hover { background: #edf2f6; border-color: #8fa3b3; }
QPushButton:pressed { background: #dce5ed; border-color: #7e94a6; }
QPushButton[iconOnly="true"] { background: #ffffff; border: 1px solid #c1cdd7; padding: 4px; }
QPushButton[iconOnly="true"]:hover { background: #e7eef4; border-color: #91a4b5; }
QWidget#sidebar QPushButton[iconOnly="true"], QWidget#sidebarFooter QPushButton[iconOnly="true"] {
    background: transparent; border-color: transparent;
}
QWidget#sidebar QPushButton[iconOnly="true"]:hover, QWidget#sidebarFooter QPushButton[iconOnly="true"]:hover {
    background: #e7eef4; border-color: transparent;
}
QPushButton[compactIcon="true"] { background: transparent; border: 0; padding: 1px; }
QWidget#segmentedControl { background: #ffffff; border: 1px solid #9aafbf; border-radius: 3px; }
QWidget#segmentedControl QPushButton { background: #ffffff; border: 1px solid transparent; border-radius: 1px; }
QWidget#segmentedControl QPushButton:checked { background: #dce5ed; border-color: #8ea3b4; color: #253342; }
QWidget#segmentedControl QPushButton:hover { background: #edf2f6; }
QPushButton#primary { background: #475f52; color: #ffffff; border-color: #475f52; font-weight: 600; }
QLineEdit, QTextEdit, QDateTimeEdit, QComboBox, QKeySequenceEdit {
    background: #ffffff; color: #253342; border: 1px solid #b7c5d0; border-radius: 3px;
    padding: 7px; selection-background-color: #dce5ed; selection-color: #253342;
}
QLineEdit:focus, QTextEdit:focus { border-color: #718b9f; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { width: 8px; background: transparent; }
QScrollBar::handle:vertical { background: #aebfcd; border-radius: 3px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QPushButton#syncStatus { color: #586979; background: transparent; border: 0; text-align: left; padding: 5px 0; font-size: 12px; }
QCalendarWidget QWidget { background: #ffffff; font-size: 12px; }
QCalendarWidget QAbstractItemView {
    color: #253342; background: #ffffff; alternate-background-color: #ffffff;
    selection-background-color: #dce5ed; selection-color: #253342; outline: 0; border: 0;
}
QToolTip { background: #ffffff; color: #253342; border: 1px solid #a8bac9; padding: 6px; }
"""


def _palette(widget, surface):
    palette = widget.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role in (QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase, QPalette.ColorRole.Window):
            palette.setColor(group, role, QColor(surface))
        palette.setColor(group, QPalette.ColorRole.Text, QColor(INK))
        palette.setColor(group, QPalette.ColorRole.Highlight, QColor(SELECTED))
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(INK))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


class _SharedNavigationDelegate(QStyledItemDelegate):
    """Keep native tree selection/folding; paint the common flat row language."""
    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), 48)

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        title = styled.text
        styled.text, styled.icon = '', QIcon()
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus and option.widget and option.widget.hasFocus())
        styled.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
        painter.save()
        if selected:
            row = QRectF(option.rect)
            row.setLeft(0)
            painter.fillRect(row, QColor(SELECTED))
            painter.fillRect(QRectF(0, row.top(), 3, row.height()), QColor(EDGE))
        if focused:
            focus = QRectF(option.rect)
            focus.setLeft(4)
            focus.adjust(1, 2, -2, -2)
            painter.setPen(QPen(QColor(EDGE), 1, Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(focus)
        if index.model().hasChildren(index):
            x, y = option.rect.left() - GUTTER / 2, option.rect.center().y()
            expanded = bool(option.widget and option.widget.isExpanded(index))
            points = ([(x - 3, y - 1.5), (x, y + 1.5), (x + 3, y - 1.5)] if expanded else
                      [(x - 1.5, y - 3), (x + 1.5, y), (x - 1.5, y + 3)])
            painter.setPen(QPen(QColor(MUTED), 1.3, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.setClipRect(option.rect)
        left = option.rect.left()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(index.data(COLOR_ROLE) or MUTED))
        painter.drawRect(left, option.rect.top() + 12, 4, 24)
        left += 12
        width = max(0, option.rect.right() - left - 7)
        title_font = QFont(option.font)
        title_font.setPixelSize(13)
        title_font.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Normal)
        painter.setFont(title_font)
        painter.setPen(QColor(INK))
        painter.drawText(left, option.rect.top() + 18,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width))
        subtitle_font = QFont(option.font)
        subtitle_font.setPixelSize(12)
        painter.setFont(subtitle_font)
        painter.setPen(QColor(MUTED))
        painter.drawText(left, option.rect.top() + 35,
                         painter.fontMetrics().elidedText(index.data(SUBTITLE_ROLE) or '',
                                                         Qt.TextElideMode.ElideRight, width))
        painter.restore()


def _contains(layout, target):
    return any(layout.itemAt(index).widget() is target for index in range(layout.count()))


def _integrate_lists(window):
    """One continuous pane, rather than independent cards around each list."""
    outer = window.sidebar.layout()
    items = [outer.takeAt(0) for _index in range(outer.count())]
    sources_heading, search_row, results_heading, calendar_list, results = (None,) * 5
    for item in items:
        widget, layout = item.widget(), item.layout()
        if item.spacerItem() is not None:
            continue
        if widget is window.calendar_scroll:
            calendar_list = item
        elif widget is window.results:
            results = item
        elif widget is window.results_heading:
            results_heading = item
        elif layout is not None and _contains(layout, window.search):
            search_row = item
        elif layout is not None and any(layout.itemAt(i).widget() is not None
                                        and layout.itemAt(i).widget().objectName() == 'section'
                                        for i in range(layout.count())):
            sources_heading = item
        else:
            outer.addItem(item)
    pane = QFrame()
    pane.setObjectName('sharedNavigationPane')
    pane.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    column = QVBoxLayout(pane)
    column.setContentsMargins(0, 10, 0, 8)
    column.setSpacing(0)
    if sources_heading is not None:
        heading = QWidget()
        heading.setObjectName('sharedSectionHeader')
        heading.setLayout(sources_heading.layout())
        heading.layout().setContentsMargins(12, 0, 5, 6)
        column.addWidget(heading)
    if calendar_list is not None:
        column.addWidget(window.calendar_scroll)
    column.addSpacing(16)
    rule = QFrame()
    rule.setObjectName('sharedSectionRule')
    column.addWidget(rule)
    column.addSpacing(13)
    if results_heading is not None:
        window.results_heading.setContentsMargins(12, 0, 0, 0)
        column.addWidget(window.results_heading)
    column.addSpacing(7)
    if search_row is not None:
        search_widget = QWidget()
        search_widget.setLayout(search_row.layout())
        search_widget.layout().setContentsMargins(10, 0, 5, 0)
        search_widget.layout().setSpacing(4)
        column.addWidget(search_widget)
    column.addSpacing(7)
    if results is not None:
        column.addWidget(window.results)
        column.setStretch(column.count() - 1, 1)
    outer.addSpacing(12)
    outer.addWidget(pane, 1)
    window._shared_navigation_pane = pane


def _mark_row(row):
    row.setProperty('creationCurrent', row.choice.isChecked())
    row.style().unpolish(row)
    row.style().polish(row)
    row.update()


def apply_rows(window):
    """Useful after the harness changes a synthetic calendar's source color."""
    body = window.calendar_box.parentWidget()
    body.setObjectName('sharedCalendarBody')
    body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    window.calendar_box.setContentsMargins(0, 0, 0, 0)
    window.calendar_box.setSpacing(0)
    window.calendar_scroll.setViewportMargins(0, 0, 0, 0)
    window.calendar_scroll.setStyleSheet('QScrollArea { background: #f0f3f6; border: 0; }')
    for row in window.calendar_rows.values():
        row.setObjectName('sharedSourceRow')
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.layout().setContentsMargins(GUTTER, 0, 5, 0)
        row.layout().setSpacing(8)
        row.setMinimumHeight(36)
        row.swatch.setFixedSize(4, 22)
        row.swatch.setStyleSheet(f'background: {row.calendar.color}; border: 0; border-radius: 0;')
        row.choice.setFixedHeight(34)
        row.eye.setObjectName('sharedVisibility')
        if not getattr(row, '_shared_choice_connected', False):
            row.choice.toggled.connect(lambda _checked, source=row: _mark_row(source))
            row._shared_choice_connected = True
        _mark_row(row)
    count = max(1, len(window.calendar_rows))
    window.calendar_scroll.setMinimumHeight(count * 36 if count <= 6 else 3 * 36)
    window.calendar_scroll.setMaximumHeight(min(count * 36, 8 * 36))
    _palette(body, PANE)
    _palette(window.calendar_scroll.viewport(), PANE)


def apply(window):
    window.setStyleSheet(window.styleSheet() + _STYLE)
    window.findChild(QWidget, 'sidebarShell').setFixedWidth(292)
    outer = window.sidebar.layout()
    outer.setContentsMargins(14, 16, 14, 0)
    outer.setSpacing(8)
    _integrate_lists(window)
    apply_rows(window)
    original_refresh = window.refresh_calendar_list
    def refresh_calendar_list(self, calendars):
        original_refresh(calendars)
        apply_rows(self)
    window.refresh_calendar_list = MethodType(refresh_calendar_list, window)
    window.results.setObjectName('sharedAgenda')
    window.results.setIndentation(GUTTER)
    window.results.setItemDelegate(_SharedNavigationDelegate(window.results))
    window.results.setFrameShape(QFrame.Shape.NoFrame)
    _palette(window.results, PANE)
    _palette(window.results.viewport(), PANE)
    window.search.setPlaceholderText('Search events')
    window.search.setMinimumHeight(34)
    window.mini.setMinimumHeight(174)
    window.mini.setMaximumHeight(174)
    window.mini.calendar.setStyleSheet('''
        QCalendarWidget QWidget { color: #253342; background: #ffffff; font-size: 12px; }
        QCalendarWidget QAbstractItemView {
            color: #253342; background: #ffffff; alternate-background-color: #ffffff;
            selection-background-color: #dce5ed; selection-color: #253342; border: 0; outline: 0;
        }
    ''')
    neutral = QTextCharFormat()
    neutral.setForeground(QColor(INK))
    weekend = QTextCharFormat()
    weekend.setForeground(QColor('#546c83'))
    weekend.setBackground(QColor('#eef3f8'))
    for day in Qt.DayOfWeek:
        if 1 <= day.value <= 7:
            window.mini.calendar.setWeekdayTextFormat(day, weekend if day.value >= 6 else neutral)
    for button in (window.mini.previous_button, window.mini.next_button,
                   window.mini.month_button, window.mini.year_button):
        button.setStyleSheet('QToolButton { color: #3f5365; background: transparent; border: 0; padding: 3px; border-radius: 2px; } '
                            'QToolButton:hover { background: #e7eef4; }')
    _palette(window.mini.calendar, '#ffffff')
    for view in window.mini.calendar.findChildren(QFrame):
        if hasattr(view, 'viewport'):
            _palette(view, '#ffffff')
            _palette(view.viewport(), '#ffffff')


def apply_dialog(dialog):
    dialog.setStyleSheet(dialog.styleSheet() + _STYLE)


refresh_rows = apply_rows
