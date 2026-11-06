"""Calendar-first review mockup; no production widgets or preferences are changed.

``apply`` operates on the isolated, synthetic window created by mockup_base.
The two header rows deliberately remain stable as the window is resized.
"""
from types import MethodType

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication, QBoxLayout, QSizePolicy, QStyle, QStyleOptionViewItem, QWidget

from debacal.search_panel import COLOR_ROLE, SUBTITLE_ROLE, _ResultDelegate


NAME = "Calendar first"
DESCRIPTION = (
    "A white calendar with quiet neutral chrome. Date navigation and view share "
    "the top row; Selection and Events remain distinct on a stable second row. "
    "The sidebar uses spacing and typography in place of extra boxes."
)

PALETTE = {
    "background": "#ffffff",
    "line": "#e6e9ed",
    "ink": "#27313a",
    "muted": "#65707b",
    "accent": "#475f52",
}


class _QuietAgendaDelegate(_ResultDelegate):
    """Use the same neutral typography in the adjacent agenda and calendar."""

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        title = styled.text
        styled.text, styled.icon = "", QIcon()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
        painter.save()
        painter.setClipRect(option.rect)
        left = option.rect.left() + 4
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(index.data(COLOR_ROLE) or PALETTE["muted"]))
        painter.drawRoundedRect(left, option.rect.top() + 9, 4, 26, 2, 2)
        left += 12
        width = max(0, option.rect.right() - left - 6)
        title_font = QFont(option.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(PALETTE["ink"]))
        painter.drawText(left, option.rect.top() + 18,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width))
        subtitle_font = QFont(option.font)
        if subtitle_font.pixelSize() > 0:
            subtitle_font.setPixelSize(max(12, subtitle_font.pixelSize() - 1))
        elif subtitle_font.pointSizeF() > 0:
            subtitle_font.setPointSizeF(max(9, subtitle_font.pointSizeF() - 1))
        painter.setFont(subtitle_font)
        painter.setPen(QColor(PALETTE["muted"]))
        painter.drawText(left, option.rect.top() + 35,
                         painter.fontMetrics().elidedText(index.data(SUBTITLE_ROLE) or "",
                                                         Qt.TextElideMode.ElideRight, width))
        painter.restore()


_STYLE = """
QWidget { color: #27313a; font-size: 13px; }
QMainWindow, QDialog { background: #ffffff; }
QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter { background: #f6f7f8; }
QWidget#sidebarShell { border-right: 1px solid #e1e5e9; }
QWidget#sidebarFooter { border-top: 1px solid #e1e5e9; }
QLabel[muted="true"] { color: #65707b; font-size: 12px; }
QLabel#brand { font-size: 20px; font-weight: 600; letter-spacing: -0.5px; }
QLabel#heading { color: #202a33; font-size: 24px; font-weight: 600; }
QLabel#section { color: #59646f; font-size: 12px; font-weight: 600; }
QPushButton { background: transparent; border: 1px solid #dce1e5; border-radius: 5px; padding: 7px 11px; }
QPushButton:hover { background: #edf0f2; border-color: #c6cdd3; }
QPushButton:pressed, QPushButton:checked { background: #e5e9ec; color: #27313a; border-color: #aab5be; }
QPushButton:disabled { color: #98a1a9; border-color: #e5e9ec; }
QPushButton[iconOnly="true"] { border-color: transparent; padding: 5px; }
QPushButton[iconOnly="true"]:hover { background: #e8ecef; border-color: #d5dce2; }
QPushButton[compactIcon="true"] { padding: 1px; border-color: transparent; }
QPushButton#primary { background: #475f52; color: #ffffff; border-color: #475f52; font-weight: 600; }
QPushButton#primary:hover { background: #354c40; border-color: #354c40; }
QPushButton#calendarChoice { text-align: left; padding: 6px 7px; border: 1px solid transparent; }
QPushButton#calendarChoice:checked { color: #27313a; background: #ffffff; border-color: #b3bdc5; font-weight: 600; }
QPushButton#calendarChoice:hover { background: #ffffff; border-color: #d1d8de; }
QWidget#segmentedControl { background: #edf0f2; border: 1px solid #d8dfe4; border-radius: 6px; }
QWidget#segmentedControl QPushButton { border: 1px solid transparent; border-radius: 4px; background: transparent; }
QWidget#segmentedControl QPushButton:hover { background: #e1e6e9; }
QWidget#segmentedControl QPushButton:checked { background: #ffffff; border-color: #a7b3bb; color: #27313a; }
QWidget#segmentedControl QPushButton:pressed { background: #dbe2e6; }
QLineEdit, QTextEdit, QDateTimeEdit, QComboBox, QKeySequenceEdit { background: #ffffff; color: #27313a; border: 1px solid #cbd3da; border-radius: 5px; padding: 7px; selection-background-color: #d8e4dd; selection-color: #27313a; }
QLineEdit:focus, QTextEdit:focus { border-color: #647f70; }
QListWidget, QTreeWidget { background: #ffffff; border: 1px solid #dce2e7; border-radius: 5px; outline: none; }
QListWidget::item:selected, QTreeWidget::item:selected { color: #27313a; background: #e8edef; }
QTreeWidget#calendarAgenda { border: none; border-radius: 0; background: transparent; }
QScrollArea { border: none; background: #ffffff; }
QScrollBar:vertical { background: #f3f5f6; width: 8px; }
QScrollBar::handle:vertical { background: #c7cfd6; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCalendarWidget QWidget { background: transparent; font-size: 12px; }
QCalendarWidget QAbstractItemView { background: #f6f7f8; alternate-background-color: #f6f7f8; color: #27313a; selection-background-color: #dbe4df; selection-color: #27313a; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { border-color: #9caba1; background: #ffffff; }
QCheckBox::indicator:checked { background: #547460; border-color: #547460; }
QPushButton#syncStatus { text-align: left; border: none; font-size: 12px; color: #59646f; padding: 5px 0; }
QMenu { background: #ffffff; color: #27313a; border: 1px solid #cbd3da; padding: 4px; }
QMenu::item:selected { background: #e8edef; }
QToolTip { background: #ffffff; color: #27313a; border: 1px solid #a7b3bb; padding: 6px; }
"""


def _style(widget):
    family = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()
    family = family.replace('"', '')
    widget.setStyleSheet(widget.styleSheet() + f'\nQWidget {{ font-family: "{family}"; }}\n' + _STYLE)


def _section(toolbar, name):
    for widget in toolbar.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if widget.accessibleName() == name:
            return widget
    raise RuntimeError(f"Synthetic workspace is missing its {name!r} section")


def _stable_rows(header):
    """Keep both rows rather than moving controls while resizing or changing view."""
    header.inline = False
    header.row.setDirection(QBoxLayout.Direction.TopToBottom)
    header.row.setSpacing(12)
    header.updateGeometry()


def apply(window):
    """Apply this direction to an isolated synthetic MainWindow."""
    from mockup_base import install_paint_palette

    install_paint_palette(**PALETTE)
    window.results.setObjectName("calendarAgenda")
    _style(window)

    header = window.workspace_header
    view = _section(window.toolbar, "View")
    selection = _section(window.toolbar, "Selection")
    events = _section(window.toolbar, "Events")

    # Navigation remains to the left of the reserved-width period label. View
    # immediately follows the period, so related controls stay close at any width.
    tools = window.toolbar.layout()
    for section in (view, selection, events):
        tools.removeWidget(section)
    title = header.title.layout()
    title.setSpacing(12)
    view.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    title.insertWidget(2, view, 0, Qt.AlignmentFlag.AlignVCenter)

    # The scope controls keep their export affordance. Event creation/history
    # occupy their own group at the other end of the quiet secondary row.
    tools.setSpacing(18)
    for section in (selection, events):
        section.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        section.layout().setDirection(QBoxLayout.Direction.LeftToRight)
        section.layout().setSpacing(10)
    events.layout().itemAt(1).layout().setContentsMargins(0, 0, 0, 0)
    tools.addWidget(selection)
    tools.addStretch(1)
    tools.addWidget(events)
    window.toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    header.row.setAlignment(window.toolbar, Qt.AlignmentFlag.AlignTop)
    header.reflow = MethodType(_stable_rows, header)
    header.reflow()

    outer = header.parentWidget().layout()
    outer.setContentsMargins(22, 20, 22, 8)
    outer.setSpacing(12)
    sidebar = window.sidebar.layout()
    sidebar.setContentsMargins(18, 20, 18, 14)
    sidebar.setSpacing(10)
    window.results.setItemDelegate(_QuietAgendaDelegate(window.results))
    window.results.setIndentation(15)
    window.search.setPlaceholderText("Search events")
    window.search.setMinimumHeight(34)

    # These tool buttons carry their own stylesheet in the real mini calendar;
    # give them the same neutral interaction colors as the rest of this mockup.
    for button in (window.mini.previous_button, window.mini.next_button,
                   window.mini.month_button, window.mini.year_button):
        button.setStyleSheet(
            "QToolButton { border: none; background: transparent; color: #3f4954; "
            "padding: 3px; border-radius: 4px; } "
            "QToolButton:hover { background: #e5eaee; } "
            "QToolButton:pressed { background: #dce3e8; }"
        )

    # The canvas already retains calendar source fills and its restrained bar +
    # outline selection. Paint role changes make all metadata readable even on
    # yellow source calendars; the calendar's stored color is never changed.
    for target in (window.header, window.canvas, window.month, window.all_day_canvas):
        target.update()
    QApplication.processEvents()


def apply_dialog(dialog):
    """Use the same typographic and contrast roles for the existing import flow."""
    _style(dialog)
    dialog.layout().setContentsMargins(18, 18, 18, 16)
    dialog.layout().setSpacing(10)
