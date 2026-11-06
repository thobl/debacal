"""Typographic direction: fewer surfaces, stronger role and weight changes."""
from types import MethodType

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QBoxLayout, QLabel, QStyle, QStyleOptionViewItem

from debacal.search_panel import COLOR_ROLE, SUBTITLE_ROLE, _ResultDelegate

NAME = 'Typographic'
THESIS = 'Use text hierarchy and small marks rather than repeated boxes. Put group captions beside controls and retain a familiar, muted red weekend cue.'
TRADEOFF = 'A quieter page with more calendar height, but weaker action boundaries and less obvious selected rows. Inline group captions consume more horizontal space.'


class _TextAgenda(_ResultDelegate):
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
        painter.setClipRect(option.rect.adjusted(-16, 0, 0, 0))
        left = option.rect.left()
        if index.model().hasChildren(index):
            x, y = left - 9, option.rect.center().y()
            expanded = bool(option.widget and option.widget.isExpanded(index))
            points = [(x-3,y-1.5),(x,y+1.5),(x+3,y-1.5)] if expanded else [(x-1.5,y-3),(x+1.5,y),(x-1.5,y+3)]
            painter.setPen(QPen(QColor('#596570'), 1.2))
            painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
        if selected:
            painter.setPen(QPen(QColor('#475f52'), 2))
            painter.drawLine(left - 7, option.rect.top() + 9, left - 7, option.rect.bottom() - 9)
        if focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#475f52'), 1.5, Qt.PenStyle.DotLine))
            painter.drawRect(QRectF(option.rect).adjusted(-10, 2, -2, -2))
        width = max(0, option.rect.width() - 21)
        font = QFont(option.font)
        font.setPixelSize(14)
        font.setBold(selected)
        painter.setFont(font)
        painter.setPen(QColor('#27313a'))
        painter.drawText(left, option.rect.top() + 19,
                         painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width))
        font.setPixelSize(12)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor('#596570'))
        painter.drawText(left, option.rect.top() + 36,
                         painter.fontMetrics().elidedText(index.data(SUBTITLE_ROLE) or '',
                                                         Qt.TextElideMode.ElideRight, width))
        # This source mark remains available without repeating a tall bar on
        # every text row. Selected state uses a separate neutral vertical rule.
        painter.setPen(QPen(QColor('#758079'), 1))
        painter.setBrush(QColor(index.data(COLOR_ROLE) or '#596570'))
        painter.drawEllipse(QRectF(option.rect.right() - 10, option.rect.top() + 13, 7, 7))
        painter.restore()


def _reflow(header):
    needed = header.title.minimumSizeHint().width() + header.controls.sizeHint().width() + 24
    inline = header.width() >= needed
    if inline != header.inline:
        header.inline = inline
        header.row.setDirection(QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom)
        header.row.setSpacing(24 if inline else 12)
        header.updateGeometry()


def refresh_rows(window):
    for row in window.calendar_rows.values():
        row.swatch.setFixedSize(9, 9)
        row.swatch.setStyleSheet(f'background: {row.calendar.color}; border-radius: 4px; border: 1px solid #758079;')
        row.layout().setContentsMargins(16, 0, 0, 0)
        row.layout().setSpacing(10)
        row.choice.setFixedHeight(30)
        row.choice.setStyleSheet('QPushButton { border: 0; border-bottom: 2px solid transparent; border-radius: 0; '
                                'padding: 0; text-align: left; background: transparent; } '
                                'QPushButton:checked { border-bottom-color: #84998c; font-weight: 600; }')


def apply(window):
    window.setStyleSheet(window.styleSheet() + '''
        QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter { background: #f7f8f9; }
        QWidget#calendarRow, QWidget#calendarRow[creationCurrent="true"] { background: transparent; border: 0; }
        QPushButton#calendarChoice { padding: 0; border: 0; border-bottom: 2px solid transparent; border-radius: 0; }
        QWidget#calendarRow[creationCurrent="true"] QPushButton#calendarChoice { border-bottom-color: #84998c; font-weight: 600; }
        QPushButton#calendarChoice:hover { border-bottom-color: #b9c8bf; }
        QTreeWidget#sidebarAgenda::item:selected { background: transparent; }
        QWidget#segmentedControl { background: transparent; border: 0; border-bottom: 1px solid #cbd3da; border-radius: 0; }
        QWidget#segmentedControl QPushButton { border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: transparent; }
        QWidget#segmentedControl QPushButton:checked { background: transparent; border-bottom-color: #475f52; }
        QWidget#segmentedControl QPushButton:hover { background: #edf1ee; }
    ''')
    window.results.setItemDelegate(_TextAgenda(window.results))
    window.results.setStyleSheet('QTreeWidget#sidebarAgenda { border: 0; background: #f7f8f9; } '
                                'QTreeWidget#sidebarAgenda::item:selected { background: transparent; } '
                                'QTreeWidget#sidebarAgenda::branch, QTreeWidget#sidebarAgenda::branch:selected { background: #f7f8f9; } '
                                'QTreeWidget#sidebarAgenda::branch:has-children { image: none; }')
    window.results.setIndentation(16)
    window.results.setMinimumHeight(48)
    window.heading.setStyleSheet('color: #27313a; font-size: 22px; font-weight: 600;')
    window.range_label.setFixedHeight(18)
    window.period.layout().setSpacing(2)
    header = window.workspace_header
    header._navigation.setContentsMargins(0, 0, 0, 0)
    controls = window.toolbar.layout()
    controls.setSpacing(20)
    for index in range(controls.count()):
        section = controls.itemAt(index).widget()
        if section is None:
            continue
        column = section.layout()
        column.setDirection(QBoxLayout.Direction.LeftToRight)
        column.setSpacing(6)
        section.setFixedHeight(36)
        labels = section.findChildren(QLabel)
        for label in labels:
            label.setFixedHeight(32)
            label.setContentsMargins(0, 0, 0, 0)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            label.setStyleSheet('color: #596570; font-size: 12px; font-weight: 500;')
    header.reflow = MethodType(_reflow, header)
    header.inline = None
    header.reflow()
    window._navigation_width_signature = None
    window.update_navigation_width()
    refresh_rows(window)
    original = window.refresh_calendar_list
    def refreshed(self, calendars):
        original(calendars)
        refresh_rows(self)
    window.refresh_calendar_list = MethodType(refreshed, window)
