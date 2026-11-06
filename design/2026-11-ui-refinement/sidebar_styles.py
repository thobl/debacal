"""Two close, sample-only sidebar treatments for the second design review.

Only disposable Qt instances are modified. The open and panels variants share
alignment, type, spacing, control roles and selection; their list surfaces differ.
"""
from types import MethodType

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette, QPen, QPolygonF, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication, QFrame, QStyle, QStyleOptionViewItem,
)

from debacal.search_panel import COLOR_ROLE, SUBTITLE_ROLE, _ResultDelegate


INK = "#27313a"
MUTED = "#596570"
SIDEBAR = "#f6f7f8"
SELECTED = "#e7eee8"
SELECTED_BORDER = "#bfd0c0"
GUTTER = 16

VARIANTS = {
    "open": {
        "name": "Open sidebar",
        "description": "Whitespace separates the lists; no extra list containers.",
    },
    "panels": {
        "name": "Soft panels",
        "description": "Quiet white list surfaces define the calendar and agenda regions.",
    },
}


_COMMON_STYLE = """
QWidget { color: #27313a; font-size: 13px; }
QMainWindow, QDialog { background: #ffffff; }
QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter { background: #f6f7f8; }
QWidget#sidebarShell { border-right: 1px solid #e1e5e9; }
QWidget#sidebarFooter { border-top: 1px solid #e1e5e9; }
QLabel[muted="true"] { color: #596570; font-size: 12px; }
QLabel#brand { font-size: 20px; font-weight: 600; letter-spacing: -0.5px; }
QLabel#section { color: #596570; font-size: 12px; font-weight: 600; }
QPushButton { background: transparent; border: 1px solid #cbd3da; border-radius: 4px; padding: 7px 11px; }
QPushButton:hover { background: #edf1ee; border-color: #b9c8bf; }
QPushButton:pressed { background: #dce7df; border-color: #a4b9aa; }
QPushButton:disabled { color: #98a1a9; border-color: #e1e6e9; }
QPushButton[iconOnly="true"], QPushButton[compactIcon="true"] { background: transparent; border-color: transparent; padding: 5px; }
QPushButton[iconOnly="true"]:checked { background: #e7eee8; border-color: #bfd0c0; }
QPushButton[iconOnly="true"]:hover { background: #e7eee8; border-color: transparent; }
QPushButton[compactIcon="true"] { padding: 1px; }
QPushButton#primary { background: #475f52; color: #ffffff; border-color: #475f52; font-weight: 600; }
QPushButton#primary:hover { background: #354c40; border-color: #354c40; }
QWidget#segmentedControl { background: #edf0ee; border: 1px solid #d4ddd7; border-radius: 6px; }
QWidget#segmentedControl QPushButton { background: transparent; border: 1px solid transparent; border-radius: 4px; }
QWidget#segmentedControl QPushButton:hover { background: #e1e9e3; }
QWidget#segmentedControl QPushButton:checked { color: #354d3e; background: #ffffff; border-color: #a9bcb0; }
QWidget#segmentedControl QPushButton:pressed { background: #d9e4dc; }
QLineEdit, QTextEdit, QDateTimeEdit, QComboBox, QKeySequenceEdit { background: #ffffff; color: #27313a; border: 1px solid #cbd3da; border-radius: 4px; padding: 7px; selection-background-color: #d8e4dd; selection-color: #27313a; }
QLineEdit:focus, QTextEdit:focus { border-color: #647f70; }
QListWidget { background: #ffffff; color: #27313a; border: 1px solid #cbd3da; border-radius: 4px; outline: none; }
QListWidget::item:selected { color: #27313a; background: #e7eee8; }
QWidget#calendarRow { background: transparent; border: 1px solid transparent; border-radius: 4px; }
QWidget#calendarRow[creationCurrent="true"] { background: #e7eee8; border-color: #bfd0c0; }
QPushButton#calendarChoice, QPushButton#calendarChoice:checked { text-align: left; padding: 0; background: transparent; color: #27313a; border: 0; font-weight: normal; }
QWidget#calendarRow[creationCurrent="true"] QPushButton#calendarChoice { font-weight: 600; }
QPushButton#calendarChoice:hover { background: transparent; border: 0; }
QPushButton#calendarVisibility, QPushButton#calendarVisibility:checked { border: 1px solid transparent; background: transparent; padding: 3px; }
QPushButton#calendarVisibility:hover { background: #e0e8e2; border-color: transparent; }
QPushButton#calendarVisibility:pressed { background: #d5e1d8; }
QScrollArea { border: none; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #c7cfd6; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTreeWidget#sidebarAgenda { border: none; outline: none; background: transparent; padding: 0; }
QTreeWidget#sidebarAgenda::item { border: none; background: transparent; padding: 0; }
QTreeWidget#sidebarAgenda::item:selected { color: #27313a; background: #e7eee8; }
QTreeWidget#sidebarAgenda::branch { background: transparent; }
QTreeWidget#sidebarAgenda::branch:has-children { image: none; }
QPushButton#syncStatus { text-align: left; background: transparent; border: none; font-size: 12px; color: #596570; padding: 5px 0; }
QMenu { background: #ffffff; color: #27313a; border: 1px solid #cbd3da; padding: 4px; }
QMenu::item:selected { background: #e7eee8; }
QToolTip { background: #ffffff; color: #27313a; border: 1px solid #a7b3bb; padding: 6px; }
"""


class _AlignedAgenda(_ResultDelegate):
    """Reserve the same source-bar and text gutters as the calendar rows."""

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        title = styled.text
        styled.text, styled.icon = "", QIcon()
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus
                       and option.widget and option.widget.hasFocus())
        styled.state &= ~QStyle.StateFlag.State_HasFocus
        styled.state &= ~QStyle.StateFlag.State_Selected
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
        painter.save()
        if selected:
            # Include the branch gutter so a selected series has the same whole
            # row treatment as the selected creation calendar above it.
            row = QRectF(option.rect)
            row.setLeft(0)
            row.adjust(.5, .5, -.5, -.5)
            painter.setBrush(QColor(SELECTED))
            painter.setPen(QPen(QColor(SELECTED_BORDER), 1))
            painter.drawRoundedRect(row, 4, 4)
        if focused:
            focus_row = QRectF(option.rect)
            focus_row.setLeft(0)
            focus_row.adjust(2, 2, -2, -2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#475f52"), 2, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(focus_row, 3, 3)
        if index.model().hasChildren(index):
            # Paint an explicit chevron after the whole-row surface. The native
            # branch click target and expand/collapse behavior remain unchanged.
            x, y = option.rect.left() - GUTTER / 2, option.rect.center().y()
            expanded = bool(option.widget and option.widget.isExpanded(index))
            points = ([(x - 3, y - 1.5), (x, y + 1.5), (x + 3, y - 1.5)] if expanded else
                      [(x - 1.5, y - 3), (x + 1.5, y), (x - 1.5, y + 3)])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(MUTED), 1.3, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
        painter.setClipRect(option.rect)
        left = option.rect.left()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(index.data(COLOR_ROLE) or MUTED))
        painter.drawRoundedRect(left, option.rect.top() + 9, 4, 26, 2, 2)
        left += 12
        width = max(0, option.rect.right() - left - 6)
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(INK))
        painter.drawText(left, option.rect.top() + 18,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width))
        subtitle_font = QFont(option.font)
        if subtitle_font.pixelSize() > 0:
            subtitle_font.setPixelSize(max(12, subtitle_font.pixelSize() - 1))
        else:
            subtitle_font.setPointSizeF(max(9, subtitle_font.pointSizeF() - 1))
        painter.setFont(subtitle_font)
        painter.setPen(QColor(MUTED))
        painter.drawText(left, option.rect.top() + 35,
                         painter.fontMetrics().elidedText(index.data(SUBTITLE_ROLE) or "",
                                                         Qt.TextElideMode.ElideRight, width))
        painter.restore()


def _palette(widget, background):
    palette = widget.palette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        palette.setColor(group, QPalette.ColorRole.Base, QColor(background))
        palette.setColor(group, QPalette.ColorRole.AlternateBase, QColor(background))
        palette.setColor(group, QPalette.ColorRole.Window, QColor(background))
        palette.setColor(group, QPalette.ColorRole.Text, QColor(INK))
        palette.setColor(group, QPalette.ColorRole.Highlight, QColor(SELECTED))
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor(INK))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def _style(widget):
    family = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family().replace('"', '')
    widget.setStyleSheet(widget.styleSheet() + f'\nQWidget {{ font-family: "{family}"; }}\n' + _COMMON_STYLE)


def _style_rows(window, variant):
    panels = variant == "panels"
    inset = 6 if panels else 0
    body = window.calendar_box.parentWidget()
    body.setObjectName("calendarListBody")
    body.setStyleSheet("QWidget#calendarListBody { background: " + ("#ffffff" if panels else SIDEBAR) + "; }")
    # Inset the viewport itself so its opaque white contents cannot cover the
    # rounded surface corners. The source-bar position stays identical to agenda.
    window.calendar_scroll.setViewportMargins(inset, inset, inset, inset)
    window.calendar_box.setContentsMargins(0, 0, 0, 0)
    window.calendar_box.setSpacing(4)
    for row in window.calendar_rows.values():
        row.setObjectName("calendarRow")
        row.setProperty("creationCurrent", row.choice.isChecked())
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.layout().setContentsMargins(GUTTER, 0, 0, 0)
        row.layout().setSpacing(8)
        row.setMinimumHeight(32)
        row.swatch.setFixedSize(4, 22)
        row.choice.setFixedHeight(30)
        row.eye.setObjectName("calendarVisibility")
        row.style().unpolish(row)
        row.style().polish(row)
        row.choice.style().unpolish(row.choice)
        row.choice.style().polish(row.choice)
        row.eye.style().unpolish(row.eye)
        row.eye.style().polish(row.eye)
    # Account for the optional surface padding as well as the row gap. Normal
    # calendar lists remain fully visible; long lists retain the existing fallback.
    count = max(1, len(window.calendar_rows))
    full_height = count * 32 + (count - 1) * 4 + 2 * inset + (2 if panels else 0)
    window.calendar_scroll.setMinimumHeight(full_height if count <= 6 else 3 * 36 + 2 * inset)
    window.calendar_scroll.setMaximumHeight(min(full_height, 8 * 36 + 2 * inset))


def apply_rows(window, variant="open"):
    """Refresh source-row presentation after synthetic scene changes."""
    if variant not in VARIANTS:
        raise ValueError("Unknown sidebar variant: " + variant)
    _style_rows(window, variant)


def _sidebar_spacing(window):
    layout = window.sidebar.layout()
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(0)
    items = [layout.takeAt(0) for _ in range(layout.count())]
    for item in items:
        layout.addItem(item)
        widget = item.widget()
        if widget is window.results:
            continue
        if widget is window.mini or widget is window.calendar_scroll:
            layout.addSpacing(24)
        elif widget and widget.objectName() == "profileBadge":
            layout.addSpacing(16)
        else:
            layout.addSpacing(8)


def _mini(window):
    mini = window.mini
    mini.setMinimumHeight(176)
    mini.setMaximumHeight(176)
    calendar = mini.calendar
    neutral = QTextCharFormat()
    neutral.setForeground(QColor(INK))
    for day in Qt.DayOfWeek:
        if day.value >= 1 and day.value <= 7:
            calendar.setWeekdayTextFormat(day, neutral)
    calendar.setStyleSheet("""
        QCalendarWidget QWidget { background: #f6f7f8; color: #27313a; font-size: 12px; }
        QCalendarWidget QAbstractItemView { background-color: #f6f7f8; alternate-background-color: #f6f7f8; color: #27313a; selection-background-color: #e7eee8; selection-color: #27313a; outline: none; border: none; }
        QCalendarWidget QAbstractItemView:disabled { color: #7a838c; }
    """)
    _palette(calendar, SIDEBAR)
    for view in calendar.findChildren(QFrame):
        if hasattr(view, "viewport"):
            _palette(view, SIDEBAR)
            _palette(view.viewport(), SIDEBAR)
    for button in (mini.previous_button, mini.next_button, mini.month_button, mini.year_button):
        button.setStyleSheet(
            "QToolButton { border: none; background: transparent; color: #3f4954; "
            "font-size: 12px; padding: 3px; border-radius: 4px; } "
            "QToolButton:hover { background: #e7eee8; } "
            "QToolButton:pressed { background: #dce7df; }"
        )


def apply(window, variant="open"):
    """Refine the synthetic sidebar without altering header or canvas layout."""
    if variant not in VARIANTS:
        raise ValueError("Unknown sidebar variant: " + variant)
    window.results.setObjectName("sidebarAgenda")
    _style(window)
    panels = variant == "panels"
    surface = "#ffffff" if panels else SIDEBAR
    window.calendar_scroll.setStyleSheet(
        "QScrollArea { background: " + surface + "; border: "
        + ("1px solid #dce2e7; border-radius: 6px;" if panels else "none;") + " }"
    )
    window.results.setStyleSheet(
        "QTreeWidget#sidebarAgenda { background: " + surface + "; outline: none; "
        + ("border: 1px solid #dce2e7; border-radius: 6px; padding: 6px;" if panels else "border: none; padding: 0;")
        + " } QTreeWidget#sidebarAgenda::item { padding: 0; border: none; }"
        "QTreeWidget#sidebarAgenda::item:selected { color: #27313a; background: #e7eee8; }"
    )
    _palette(window.calendar_scroll.viewport(), surface)
    _palette(window.results, surface)
    _palette(window.results.viewport(), surface)
    window.results.setItemDelegate(_AlignedAgenda(window.results))
    window.results.setIndentation(GUTTER)
    window.results.setMinimumHeight(48)
    window.search.setPlaceholderText("Search events")
    window.search.setMinimumHeight(34)
    _sidebar_spacing(window)
    _mini(window)
    _style_rows(window, variant)

    # Calendar refresh can recreate rows or update the creation target. Reapply
    # their transient presentation, preserving the application's existing logic.
    original = window.refresh_calendar_list

    def refresh_calendar_list(self, calendars):
        original(calendars)
        _style_rows(self, variant)

    window.refresh_calendar_list = MethodType(refresh_calendar_list, window)
    window.sidebar.setMinimumHeight(0)
    window.sidebar.layout().activate()
    QApplication.processEvents()


def apply_dialog(dialog, variant="open"):
    """Import shares control roles; sidebar-only surfaces do not change the flow."""
    if variant not in VARIANTS:
        raise ValueError("Unknown sidebar variant: " + variant)
    _style(dialog)
    dialog.layout().setContentsMargins(20, 16, 20, 16)
    dialog.layout().setSpacing(8)
