"""Sample-only direction: persistent calendar sources and dated event objects."""
from types import MethodType

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QLabel, QStyle, QStyleOptionViewItem, QWidget

from debacal.search_panel import COLOR_ROLE, SUBTITLE_ROLE, _ResultDelegate


NAME = "Sources & agenda"
THESIS = (
    "Calendars are persistent source controls; agenda entries are dated objects. "
    "Square source chips, a current-target check and an outlined control distinguish "
    "the calendar list from a time column and source-tinted event cards."
)
TRADEOFF = (
    "The wider sidebar gives the agenda more structure and readable dates, while "
    "leaving slightly less room for the main calendar. Event cards consume more "
    "vertical space than a plain results list."
)

INK = "#27313a"
MUTED = "#596570"
SURFACE = "#f6f7f8"


_STYLE = """
QWidget#sourceControl { background: transparent; border: 1px solid transparent; border-radius: 5px; }
QWidget#sourceControl[creationCurrent="true"] { background: #ffffff; border-color: #9eafa4; }
QWidget#sourceControl QPushButton#calendarChoice,
QWidget#sourceControl QPushButton#calendarChoice:checked { text-align: left; padding: 0; color: #27313a; border: 0; background: transparent; font-weight: normal; }
QWidget#sourceControl[creationCurrent="true"] QPushButton#calendarChoice { font-weight: 600; }
QWidget#sourceControl QPushButton#calendarChoice:hover { background: transparent; }
QWidget#sourceControl QLabel#creationCheck { color: #475f52; background: transparent; border: 0; font-size: 13px; font-weight: 600; }
QPushButton#calendarVisibility, QPushButton#calendarVisibility:checked { border: 1px solid transparent; background: transparent; padding: 3px; }
QPushButton#calendarVisibility:hover { background: #e6ece7; border-color: transparent; }
QPushButton#calendarVisibility:pressed { background: #d8e3db; }
QTreeWidget#eventTimeline { background: #f6f7f8; border: none; padding: 0; outline: none; }
QTreeWidget#eventTimeline::item, QTreeWidget#eventTimeline::item:selected { background: transparent; border: none; padding: 0; color: #27313a; }
QTreeWidget#eventTimeline::branch, QTreeWidget#eventTimeline::branch:selected { background: transparent; }
QTreeWidget#eventTimeline::branch:has-children { image: none; }
"""


def _tint(source, weight=.16):
    source = QColor(source)
    return QColor(*(round(channel * weight + 255 * (1 - weight))
                    for channel in (source.red(), source.green(), source.blue())))


def _edge(source):
    color = QColor(source).darker(125)
    for _ in range(24):
        channels = [color.redF(), color.greenF(), color.blueF()]
        channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                    for value in channels]
        luminance = sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))
        if 1.05 / (luminance + .05) >= 3:
            return color
        color = color.darker(112)
    return QColor("#475f52")


class _TimelineDelegate(_ResultDelegate):
    """Event-shaped cards deliberately differ from the source controls above."""

    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), 56)

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus
                       and option.widget and option.widget.hasFocus())
        color = QColor(index.data(COLOR_ROLE) or "#7c8c82")
        edge = _edge(color)
        rect = QRectF(option.rect)
        rail = rect.left() + 46
        card = QRectF(rail + 10, rect.top() + 4, max(0, rect.right() - rail - 12), rect.height() - 8)
        subtitle = str(index.data(SUBTITLE_ROLE) or "")
        date, separator, clock = subtitle.rpartition(" · ")
        if not separator:
            date, clock = subtitle, ""

        painter.save()
        painter.setClipRect(option.rect)
        # The chronology uses a neutral rail. Source color belongs to the object
        # card and its dot, rather than repeating the calendar control's square.
        painter.setPen(QPen(QColor("#d5ddd8"), 1))
        painter.drawLine(QPointF(rail, rect.top()), QPointF(rail, rect.bottom()))
        painter.setBrush(edge)
        painter.setPen(QPen(edge if selected else QColor("#c2cbc5"), 1))
        painter.drawEllipse(QPointF(rail, rect.center().y()), 3.3 if selected else 2.7,
                            3.3 if selected else 2.7)
        painter.setBrush(_tint(color))
        painter.setPen(QPen(edge if selected else _tint(color, .35), 2 if selected else 1))
        painter.drawRoundedRect(card, 6, 6)
        if focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#475f52"), 2, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(card.adjusted(3, 3, -3, -3), 3, 3)

        normal = QFont(option.font)
        normal.setPixelSize(12)
        normal.setBold(selected)
        painter.setFont(normal)
        painter.setPen(QColor(INK if selected else MUTED))
        time_rect = QRectF(rect.left(), rect.top() + 8, 38, 30)
        painter.drawText(time_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, clock)

        title_font = QFont(option.font)
        title_font.setBold(True)
        title_font.setPixelSize(13)
        painter.setFont(title_font)
        painter.setPen(QColor(INK))
        width = max(0, card.width() - 18)
        painter.drawText(card.left() + 9, card.top() + 18,
                         painter.fontMetrics().elidedText(styled.text, Qt.TextElideMode.ElideRight, int(width)))
        normal.setBold(False)
        painter.setFont(normal)
        painter.setPen(QColor(MUTED))
        painter.drawText(card.left() + 9, card.top() + 36,
                         painter.fontMetrics().elidedText(date, Qt.TextElideMode.ElideRight, int(width)))
        painter.restore()

        # Preserve native tree indentation/click targets, and make the fold cue
        # explicit after painting rather than relying on platform branch artwork.
        if index.model().hasChildren(index):
            painter.save()
            x, y = rect.left() - 7, rect.center().y()
            expanded = bool(option.widget and option.widget.isExpanded(index))
            points = ([(x - 3, y - 1.5), (x, y + 1.5), (x + 3, y - 1.5)] if expanded else
                      [(x - 1.5, y - 3), (x + 1.5, y), (x - 1.5, y + 3)])
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(MUTED), 1.3, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
            painter.restore()


def _sources(window):
    window.calendar_scroll.setViewportMargins(0, 0, 0, 0)
    window.calendar_scroll.setStyleSheet("QScrollArea { background: #f6f7f8; border: none; }")
    window.calendar_box.setContentsMargins(0, 0, 0, 0)
    window.calendar_box.setSpacing(4)
    for row in window.calendar_rows.values():
        current = row.choice.isChecked()
        row.setObjectName("sourceControl")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.setProperty("creationCurrent", current)
        row.layout().setContentsMargins(10, 2, 4, 2)
        row.layout().setSpacing(8)
        row.setMinimumHeight(36)
        row.choice.setFixedHeight(30)
        row.swatch.setFixedSize(12, 12)
        row.swatch.setStyleSheet("background: " + row.calendar.color + "; border: 1px solid "
                                 + _edge(row.calendar.color).name() + "; border-radius: 3px;")
        check = row.findChild(QLabel, "creationCheck")
        if check is None:
            check = QLabel()
            check.setObjectName("creationCheck")
            check.setFixedWidth(14)
            check.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.layout().insertWidget(row.layout().indexOf(row.eye), check)
        check.setText("✓" if current else "")
        check.setAccessibleName("Current creation calendar" if current else "")
        row.eye.setObjectName("calendarVisibility")
        for widget in (row, row.choice, row.eye):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
    count = max(1, len(window.calendar_rows))
    height = 36 * count + 4 * (count - 1)
    window.calendar_scroll.setMinimumHeight(height if count <= 6 else 116)
    window.calendar_scroll.setMaximumHeight(min(height, 316))


def refresh_rows(window):
    """Restore source controls after a synthetic calendar color refresh."""
    _sources(window)


def apply(window):
    """Use the neutral shared scene and change only the sidebar's visual grammar."""
    window.results.setObjectName("eventTimeline")
    window.setStyleSheet(window.styleSheet() + "\n" + _STYLE)
    shell = window.findChild(QWidget, "sidebarShell")
    if shell is None:
        raise RuntimeError("The synthetic workspace has no sidebar shell")
    shell.setFixedWidth(312)
    window.sidebar.layout().setContentsMargins(18, 16, 18, 16)
    window.results.setStyleSheet("""
        QTreeWidget#eventTimeline { background: #f6f7f8; border: none; padding: 0; outline: none; }
        QTreeWidget#eventTimeline::item, QTreeWidget#eventTimeline::item:selected { border: none; padding: 0; background: transparent; }
        QTreeWidget#eventTimeline::branch { background: #f6f7f8; }
        QTreeWidget#eventTimeline::branch:selected { background: #f6f7f8; }
        QTreeWidget#eventTimeline::branch:has-children { image: none; }
    """)
    window.results.setIndentation(14)
    window.results.setItemDelegate(_TimelineDelegate(window.results))
    window.results.setMinimumHeight(56)
    _sources(window)
    original = window.refresh_calendar_list

    def refresh_calendar_list(self, calendars):
        original(calendars)
        _sources(self)

    window.refresh_calendar_list = MethodType(refresh_calendar_list, window)
    window.sidebar.setMinimumHeight(0)
    window.sidebar.layout().activate()
    QApplication.processEvents()


def apply_dialog(dialog):
    """Import keeps the shared neutral fields and preview, without adding a sidebar."""
    dialog.setStyleSheet(dialog.styleSheet() + "\n" + _STYLE)
