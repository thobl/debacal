"""Review-only direction C: Clear and compact.

This module styles a synthetic window in the screenshot harness. It deliberately
does not change Debacal's application code, preferences, or stored calendars.
"""
from datetime import timedelta

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QPen, QTextCharFormat
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QWidget

from debacal import calendar_widgets as cw


NAME = 'Clear and compact'
INK = QColor('#263139')
MUTED = QColor('#59636c')
LINE = QColor('#dde2e5')
ACCENT = QColor('#425e53')

STYLESHEET = """
QWidget { font-family: "Sans Serif"; font-size: 14px; color: #263139; }
QMainWindow, QDialog { background: #ffffff; }
QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter { background: #f4f6f7; }
QWidget#sidebarShell { border-right: 1px solid #dde2e5; }
QWidget#sidebarFooter { border-top: 1px solid #dde2e5; }
QLabel#brand { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
QLabel#heading { font-size: 23px; font-weight: 600; }
QLabel#section { color: #53606a; font-size: 13px; font-weight: 600; }
QLabel[muted="true"], QLabel#profileBadge { color: #59636c; font-size: 13px; }
QPushButton { background: transparent; border: 1px solid #d8dfe3; border-radius: 3px; padding: 6px 10px; }
QPushButton:hover, QToolButton:hover { background: #e8edf0; }
QPushButton:pressed, QPushButton:checked { background: #e1e8e5; border-color: #8ea398; color: #263f34; }
QPushButton:disabled { color: #8b949b; border-color: #e1e5e8; }
QPushButton#primary { background: #425e53; color: #ffffff; border-color: #425e53; font-weight: 600; }
QPushButton#primary:hover { background: #344e43; }
QPushButton[iconOnly="true"] { padding: 4px; border-color: transparent; }
QPushButton[iconOnly="true"]:hover { border-color: #d3dce1; }
QPushButton[compactIcon="true"] { padding: 1px; border: 0; border-radius: 2px; }
QWidget#segmentedControl { background: #e8edf0; border: 1px solid #d4dde2; border-radius: 4px; }
QWidget#segmentedControl QPushButton { border: 1px solid transparent; border-radius: 2px; background: transparent; padding: 3px; }
QWidget#segmentedControl QPushButton:checked { background: #ffffff; color: #263f34; border-color: #9cadb6; }
QWidget#segmentedControl QPushButton:hover { background: #f5f7f8; }
QLineEdit, QComboBox, QSpinBox, QDateEdit, QTimeEdit, QTextEdit, QPlainTextEdit {
    background: #ffffff; color: #263139; border: 1px solid #bcc8cf;
    border-radius: 3px; padding: 6px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus, QTimeEdit:focus,
QTextEdit:focus, QPlainTextEdit:focus { border-color: #637d70; }
QListWidget, QTreeWidget { background: transparent; border: 0; border-radius: 0; outline: 0; }
QListWidget::item, QTreeWidget::item { padding: 5px 0; border: 0; }
QListWidget::item:selected, QTreeWidget::item:selected { background: #e1e8e5; color: #263139; }
QListWidget::item:hover, QTreeWidget::item:hover { background: #eaf0f2; }
QScrollArea { border: 0; background: #ffffff; }
QScrollBar:vertical { background: #f1f4f5; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #bac5cc; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QPushButton#calendarChoice { text-align: left; padding: 5px 7px; border: 1px solid transparent; border-radius: 3px; }
QPushButton#calendarChoice:checked { background: #e0e7e3; color: #263f34; border-color: #b4c3bb; font-weight: 600; }
QPushButton#syncStatus { border: 0; text-align: left; padding: 5px 0; color: #59636c; font-size: 13px; }
QCalendarWidget { background: transparent; font-size: 13px; }
QCalendarWidget QAbstractItemView { background: #f4f6f7; alternate-background-color: #f4f6f7; color: #263139; selection-background-color: #425e53; selection-color: #ffffff; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #99a8b2; border-radius: 2px; background: #ffffff; }
QCheckBox::indicator:checked { background: #637d70; border-color: #637d70; }
QToolTip { color: #263139; background: #ffffff; border: 1px solid #b8c5cc; padding: 5px; }
"""


def _luminance(color):
    def linear(channel):
        value = channel / 255
        return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
    return sum(weight * linear(channel) for weight, channel in
               zip((.2126, .7152, .0722), (color.red(), color.green(), color.blue())))


def _outline(color):
    """Retain source hue while making a selection edge visible on white."""
    result = QColor(color)
    for _ in range(20):
        if 1.05 / (_luminance(result) + .05) >= 3:
            break
        result = result.darker(115)
    return result


def _source(widget, item):
    calendar = widget.calendars.get(item.calendar_id)
    color = QColor(calendar.color) if calendar is not None else QColor(ACCENT)
    return color if color.isValid() else QColor(ACCENT)


def _tint(color, amount=.05):
    return QColor(*(round(255 * (1 - amount) + channel * amount)
                    for channel in (color.red(), color.green(), color.blue())))


def _card(painter, rect, source, selected=False, preview=False, candidate=False):
    edge = _outline(source) if selected or candidate or preview else LINE
    painter.save()
    painter.setPen(QPen(edge, 2 if selected else 1,
                        Qt.PenStyle.DashLine if candidate or preview else Qt.PenStyle.SolidLine))
    painter.setBrush(_tint(source, .08 if selected else .045))
    painter.drawRoundedRect(rect, 2, 2)
    painter.setClipRect(rect.adjusted(1, 1, -1, -1))
    painter.fillRect(QRectF(rect.left() + 1, rect.top() + 1, 4, max(0, rect.height() - 2)), source)
    painter.restore()


def _paint_event(self, painter, rect, item, number, preview=False):
    source = _source(self, item)
    day_width = (self.width() - self.gutter) / max(1, len(self.days))
    day_index = int((rect.center().x() - self.gutter) / day_width)
    numbered = not preview and self.show_numbers and self.navigation_day == day_index
    target = (numbered and self.navigation_digits.isascii()
              and self.navigation_digits.isdigit() and int(self.navigation_digits) == number)
    selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
    scale = cw.display_scale(self)
    confirm = self.confirmation_rect(rect, item, preview)
    _card(painter, rect, source, selected or target, preview, item.candidate)
    if rect.width() > 14 and rect.height() > 3:
        painter.save()
        painter.setClipRect(rect.adjusted(7, 1, -3, -1))
        point = item.start.timestamp() == item.end.timestamp()
        start = item.start.astimezone(cw.local_zone())
        title = (self.preferences.format_time(start) + ' · ' if point else '') + item.title
        reserve = (32 if numbered else 9) + (confirm.width() + 5 * scale if confirm is not None else 0)
        cw.text(painter, QRectF(rect.left() + 9, rect.top() + 2, rect.width() - reserve - 5,
                               min(21 * scale, rect.height() - 3)), title, INK, 10.5, selected)
        if rect.height() > 42 * scale:
            end = item.end.astimezone(cw.local_zone())
            label = self.preferences.format_time(start) + '–' + self.preferences.format_time(end)
            if item.candidate:
                label += ' · option'
            cw.text(painter, QRectF(rect.left() + 9, rect.top() + 23 * scale,
                                   rect.width() - 14, 18 * scale), label, MUTED, 9.5)
        if rect.height() > 68 * scale and item.location:
            cw.text(painter, QRectF(rect.left() + 9, rect.top() + 43 * scale,
                                   rect.width() - 14, 18 * scale), item.location, MUTED, 9.5)
        painter.restore()
    if numbered:
        offset = confirm.width() + 6 * scale if confirm is not None else 0
        cw.badge(painter, QRectF(rect.right() - 26 - offset, rect.top() + 3,
                                22, min(22, max(12, rect.height() - 4))), str(number), target)
    self.paint_confirmation(painter, confirm, item)


def _paint_month_event(self, painter, rect, item, *, preview=False, before=False, after=False):
    source = _source(self, item)
    selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
    _card(painter, rect, source, selected, preview, item.candidate)
    confirm = self.confirmation_rect(rect, item, preview, trailing=18 if after else 0)
    if before:
        cw.text(painter, QRectF(rect.left() + 7, rect.top(), 12, rect.height()), '‹', INK, 10, True)
    if after:
        cw.text(painter, QRectF(rect.right() - 18, rect.top(), 12, rect.height()), '›', INK, 10, True)
    label = ('' if item.all_day else self.preferences.format_time(item.start.astimezone(cw.local_zone())) + ' ') + item.title
    label_rect = rect.adjusted(21 if before else 9, 0, -21 if after else -3, 0)
    if confirm is not None:
        label_rect.setRight(confirm.left() - 4 * cw.display_scale(self))
    painter.save()
    painter.setClipRect(label_rect)
    cw.text(painter, label_rect, label, INK, 9.5, selected)
    painter.restore()
    self.paint_confirmation(painter, confirm, item)


def _paint_bar(self, painter, rect, item, preview=False):
    source = _source(self, item)
    selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
    _card(painter, rect, source, selected, preview, item.candidate)
    first, last = self.days[0], self.days[-1] + timedelta(days=1)
    before, after = item.start.date() < first, item.end.date() > last
    confirm = self.confirmation_rect(rect, item, preview, trailing=18 if after else 0)
    if before:
        cw.text(painter, QRectF(rect.left() + 7, rect.top(), 12, rect.height()), '‹', INK, 10, True)
    if after:
        cw.text(painter, QRectF(rect.right() - 18, rect.top(), 12, rect.height()), '›', INK, 10, True)
    label = rect.adjusted(21 if before else 9, 0, -21 if after else -6, 0)
    if confirm is not None:
        label.setRight(confirm.left() - 4 * cw.display_scale(self))
    painter.save()
    painter.setClipRect(label)
    cw.text(painter, label, item.title, INK, 9.5, selected)
    painter.restore()
    self.paint_confirmation(painter, confirm, item)


class _CompactResultDelegate(QStyledItemDelegate):
    """The existing two-line search rows, with legible neutral metadata."""
    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), 45)

    def paint(self, painter, option, index):
        from debacal.search_panel import COLOR_ROLE, SUBTITLE_ROLE

        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        title = styled.text
        styled.text, styled.icon = '', QIcon()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
        painter.save()
        painter.setClipRect(option.rect)
        left = option.rect.left() + 5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(index.data(COLOR_ROLE) or '#59636c'))
        painter.drawRect(left, option.rect.top() + 9, 4, 26)
        left += 12
        width = max(0, option.rect.right() - left - 6)
        title_font = QFont(option.font)
        title_font.setPixelSize(14)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(INK)
        painter.drawText(left, option.rect.top() + 18,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width))
        detail_font = QFont(option.font)
        detail_font.setPixelSize(13)
        painter.setFont(detail_font)
        painter.setPen(MUTED)
        painter.drawText(left, option.rect.top() + 35,
                         painter.fontMetrics().elidedText(index.data(SUBTITLE_ROLE) or '',
                                                         Qt.TextElideMode.ElideRight, width))
        painter.restore()


def apply(window):
    from mockup_base import install_paint_palette

    install_paint_palette(background='#ffffff', muted='#59636c', ink='#263139',
                          line='#dde2e5', accent='#425e53', text_scale=1.0,
                          neutral_event_text=True)
    # A readable minimum on metadata formerly painted at 7–8 points.
    original_text = cw.text
    def readable_text(painter, rect, value, color=cw.INK, size=10, bold=False, align=None):
        return original_text(painter, rect, value, color, max(9.25, size), bold, align)
    cw.text = readable_text
    cw.CalendarCanvas.paint_event = _paint_event
    cw.MonthCanvas._paint_month_event = _paint_month_event
    cw.AllDayCanvas._paint_bar = _paint_bar
    # Keep Qt's virtual paintEvent binding intact after the live QWidget has
    # been created; the shared palette and readable text apply to its header.
    window.setStyleSheet(window.styleSheet() + STYLESHEET)
    sidebar_shell = window.findChild(QWidget, 'sidebarShell')
    sidebar_shell.setFixedWidth(264)
    window.sidebar.layout().setContentsMargins(14, 14, 14, 10)
    window.sidebar.layout().setSpacing(8)
    window.stack.parentWidget().layout().setContentsMargins(16, 14, 16, 8)
    window.stack.parentWidget().layout().setSpacing(8)
    window.toolbar.layout().setSpacing(12)
    window.workspace_header.row.setSpacing(12)
    window.workspace_header.title.layout().setSpacing(10)
    window.period.layout().setSpacing(2)
    window.search.setPlaceholderText('Search events')
    window.search.setMinimumHeight(34)
    window.results.setItemDelegate(_CompactResultDelegate(window.results))
    for control in (window.view_control, window.selection_control):
        control.layout().setContentsMargins(2, 2, 2, 2)
    for row in window.calendar_rows.values():
        row.swatch.setFixedWidth(4)
        row.swatch.setStyleSheet(f'background: {row.calendar.color}; border-radius: 0;')
    for button in (window.mini.previous_button, window.mini.next_button,
                   window.mini.month_button, window.mini.year_button):
        button.setStyleSheet('QToolButton { color: #34434c; border: 0; padding: 2px; background: transparent; border-radius: 2px; } '
                            'QToolButton:hover { background: #e1e8ed; }')
    window.mini.setMinimumHeight(184)
    # Transparent item-view backgrounds produce a black grid in the offscreen
    # Qt backend; an explicit surface also keeps the design self-contained.
    weekend_format = QTextCharFormat()
    weekend_format.setForeground(MUTED)
    for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
        window.mini.calendar.setWeekdayTextFormat(day, weekend_format)
    mini_view = window.mini.calendar.findChild(QWidget, 'qt_calendar_calendarview')
    if mini_view is not None:
        palette = mini_view.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor('#f4f6f7'))
        palette.setColor(QPalette.ColorRole.Window, QColor('#f4f6f7'))
        mini_view.setPalette(palette)
        mini_view.viewport().setAutoFillBackground(True)
    window.update_navigation_width()
    window.workspace_header.reflow()


def apply_dialog(dialog):
    dialog.setStyleSheet(dialog.styleSheet() + STYLESHEET)
