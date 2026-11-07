"""Agenda-only day grouping experiments; search retains production event cards."""
from datetime import timedelta

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPen
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from debacal import ui
from debacal.event_style import event_colors
from debacal.search_panel import COLOR_ROLE, EVENT_ROLE
from debacal.search_result_style import SearchResultDelegate
from debacal.theme import scheme_for
from debacal.timeutil import local_zone


OPTIONS = {
    'baseline': {
        'name': 'Current event cards',
        'thesis': 'Keep dates and start times inside every event card.',
        'tradeoff': 'Compact and consistent with search, with repeated dates for events on the same day.',
        'search_behavior': 'Search uses the same production cards.',
    },
    'grouped': {
        'name': 'Day headings and cards',
        'thesis': 'Group upcoming cards under day headings, with earlier multi-day events under Ongoing.',
        'tradeoff': 'Dates are easier to scan; each new day adds 20px and requires more scrolling.',
        'search_behavior': 'Search returns to production cards with dates in each row; no day headings.',
    },
    'day_rail': {
        'name': 'A timeline within each day',
        'thesis': 'Use day headings, a time column and a short rail connecting only events within that day.',
        'tradeoff': 'Clarifies time order and shortens ordinary cards, but gives event titles less horizontal room.',
        'search_behavior': 'Search returns to production cards; no timeline, time column or day headings.',
    },
}


def _local_start(event):
    return event.start if event.all_day else event.start.astimezone(local_zone())


def _group(event):
    current = ui.now().astimezone(local_zone())
    day = _local_start(event).date()
    if day < current.date() and event.end.timestamp() > current.timestamp():
        return ('ongoing', None)
    return ('day', day)


def _neighbor_key(index, offset):
    sibling = index.sibling(index.row() + offset, index.column())
    event = sibling.data(EVENT_ROLE) if sibling.isValid() else None
    return _group(event) if event is not None else None


def _heading(event, preferences):
    kind, day = _group(event)
    if kind == 'ongoing':
        return 'Ongoing'
    today = ui.now().astimezone(local_zone()).date()
    prefix = 'Today · ' if day == today else 'Tomorrow · ' if day == today + timedelta(days=1) else ''
    return prefix + preferences.format_date(day, weekday=True)


def _time(event, preferences):
    return 'All day' if event.all_day else preferences.format_time(_local_start(event))


def _ongoing_until(event, preferences):
    end = event.end if event.all_day else event.end.astimezone(local_zone())
    last = end.date() - timedelta(days=1) if event.all_day else end.date()
    return 'Until ' + preferences.format_date(last, weekday=True) + (
        '' if event.all_day else ' · ' + preferences.format_time(end))


class _AgendaDelegate(SearchResultDelegate):
    """Add headings within original rows; never create selectable heading items."""

    def __init__(self, parent, option):
        super().__init__(parent)
        self.option = option

    def _agenda_event(self, option, index):
        widget = option.widget or self.parent()
        return index.data(EVENT_ROLE) if widget and getattr(widget, 'agenda_mode', False) else None

    def _body_height(self, event):
        # Moving time out of an ordinary card avoids leaving a redundant second
        # line. Ongoing cards retain their ending-date line.
        return 49 if self.option == 'grouped' or _group(event)[0] == 'ongoing' else 40

    def sizeHint(self, option, index):
        event = self._agenda_event(option, index)
        if event is None:
            return super().sizeHint(option, index)
        first = _neighbor_key(index, -1) != _group(event)
        scale = self._scale(option.widget or self.parent())
        return QSize(super().sizeHint(option, index).width(),
                     round((self._body_height(event) + (20 if first else 0)) * scale))

    def paint(self, painter, option, index):
        event = self._agenda_event(option, index)
        if event is None:
            return super().paint(painter, option, index)
        widget = option.widget or self.parent()
        preferences = widget.preferences
        scheme = scheme_for(widget)
        scale = self._scale(widget)
        key = _group(event)
        first, last = _neighbor_key(index, -1) != key, _neighbor_key(index, 1) != key
        header = 20 * scale if first else 0
        body = QRectF(option.rect).adjusted(0, header, 0, 0)
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus and widget.hasFocus())
        colors = event_colors(index.data(COLOR_ROLE) or scheme.accent, scheme)

        painter.save()
        painter.setClipRect(option.rect)
        normal = QFont(option.font)
        small = QFont(normal)
        if small.pixelSize() > 0:
            small.setPixelSize(max(9, small.pixelSize() - round(scale)))
        elif small.pointSizeF() > 0:
            small.setPointSizeF(max(7, small.pointSizeF() - scale))
        if first:
            heading_font = QFont(small)
            heading_font.setBold(True)
            painter.setFont(heading_font)
            painter.setPen(QColor(scheme.muted))
            heading = _heading(event, preferences)
            painter.drawText(QPointF(body.left() + scale, option.rect.top() + 14 * scale),
                             painter.fontMetrics().elidedText(heading, Qt.TextElideMode.ElideRight,
                                                             max(0, option.rect.width() - 2)))

        card = body.adjusted(scale, 3 * scale, -scale, -3 * scale)
        if self.option == 'day_rail':
            rail = body.left() + 45 * scale
            node_y = body.center().y()
            start_y, end_y = (node_y if first else body.top()), (node_y if last else body.bottom())
            painter.setPen(QPen(QColor(scheme.control_edge), scale))
            painter.drawLine(QPointF(rail, start_y), QPointF(rail, end_y))
            painter.setPen(QPen(QColor(colors.selected_outline), scale))
            painter.setBrush(QColor(colors.selected_outline if selected else colors.outline))
            painter.drawEllipse(QPointF(rail, node_y), 2.7 * scale, 2.7 * scale)
            painter.setFont(small)
            painter.setPen(QColor(scheme.ink if selected else scheme.muted))
            clock = ('Now' if key[0] == 'ongoing' and not event.all_day else _time(event, preferences))
            painter.drawText(QRectF(body.left(), body.top(), 37 * scale, body.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, clock)
            card.setLeft(rail + 9 * scale)

        painter.setBrush(QColor(colors.fill))
        painter.setPen(QPen(QColor(colors.selected_outline if selected else colors.outline),
                            (2 if selected else 1) * scale))
        painter.drawRoundedRect(card, 4 * scale, 4 * scale)
        if focused:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.focus), 2 * scale, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(card.adjusted(3 * scale, 3 * scale, -3 * scale, -3 * scale),
                                    2 * scale, 2 * scale)
        title_font = QFont(normal)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(colors.ink))
        width = max(0, int(card.width() - 16 * scale))
        title_y = card.top() + (17 * scale if self._body_height(event) == 49 else 21 * scale)
        painter.drawText(QPointF(card.left() + 8 * scale, title_y),
                         painter.fontMetrics().elidedText(styled.text, Qt.TextElideMode.ElideRight, width))
        if self._body_height(event) == 49:
            painter.setFont(small)
            painter.setPen(QColor(colors.muted))
            detail = (_ongoing_until(event, preferences) if key[0] == 'ongoing' else
                      _time(event, preferences))
            painter.drawText(QPointF(card.left() + 8 * scale, card.top() + 34 * scale),
                             painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, width))
        painter.restore()


def apply(window, option='baseline'):
    """Change only the synthetic agenda rendering, preserving production search."""
    if option not in OPTIONS:
        raise ValueError('Unknown agenda option: ' + option)
    window._decision_agenda_option = option
    if option != 'baseline':
        window.results.setUniformRowHeights(False)
        window.results.setItemDelegate(_AgendaDelegate(window.results, option))
        window.results.doItemsLayout()
        window.results.viewport().update()
