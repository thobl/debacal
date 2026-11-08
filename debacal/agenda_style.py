"""Cached day grouping and agenda painting, independent of search row identity."""
from dataclasses import dataclass
from datetime import timedelta

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPen
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from .event_style import event_colors, paint_event_card
from .search_result_style import SearchResultDelegate
from .theme import scheme_for


@dataclass(frozen=True, slots=True)
class AgendaRow:
    group: str
    heading: str
    first: bool
    last: bool
    clock: str
    detail: str
    body_height: int
    all_day: bool = False
    rail_first: bool = True
    rail_last: bool = True


class AgendaPresentation:
    """Format bounded upcoming rows once per relevant context, never per paint."""

    def __init__(self):
        self.rows = {}
        self._signature = None
        self._metrics = None
        self._valid_until = float('inf')
        self.revision = 0

    def update(self, events, preferences, current, zone):
        current = current.astimezone(zone)
        signature = (preferences, str(zone), int(current.timestamp() // 60),
                     tuple((event.id, event.start.timestamp(), event.end.timestamp(), event.all_day,
                            event.start.date(), event.end.date()) for event in events))
        if signature == self._signature and current.timestamp() < self._valid_until:
            return False
        self._signature = signature
        metrics = preferences.agenda_style, preferences.font_scale
        rows = {}
        self._valid_until = float('inf')
        if preferences.agenda_style != 'cards':
            today = current.date()
            starts = [event.start if event.all_day else event.start.astimezone(zone) for event in events]
            keys = [('ongoing', None) if start.date() < today and event.end.timestamp() > current.timestamp()
                    else ('day', start.date()) for event, start in zip(events, starts)]
            headings = {}
            for index, (event, start, key) in enumerate(zip(events, starts, keys)):
                ongoing = key[0] == 'ongoing'
                if ongoing:
                    self._valid_until = min(self._valid_until, event.end.timestamp())
                first, last = index == 0 or keys[index - 1] != key, index + 1 == len(keys) or keys[index + 1] != key
                if first and key not in headings:
                    if ongoing:
                        headings[key] = 'Ongoing'
                    else:
                        day = start.date()
                        prefix = 'Today · ' if day == today else 'Tomorrow · ' if day == today + timedelta(days=1) else ''
                        headings[key] = prefix + preferences.format_date(day, weekday=True)
                clock = 'All day' if event.all_day else preferences.format_time(start)
                if ongoing:
                    end = event.end if event.all_day else event.end.astimezone(zone)
                    last_day = end.date() - timedelta(days=1) if event.all_day else end.date()
                    detail = 'Until ' + preferences.format_date(last_day, weekday=True)
                    if not event.all_day:
                        detail += ' · ' + preferences.format_time(end)
                    if preferences.agenda_style == 'timeline' and not event.all_day:
                        clock = 'Now'
                else:
                    detail = clock
                if event.all_day:
                    last_day = event.end.date() - timedelta(days=1)
                    detail = 'All day'
                    if ongoing or last_day > event.start.date():
                        detail += ' · Until ' + preferences.format_date(last_day, weekday=True)
                body_height = 49 if preferences.agenda_style == 'grouped' or ongoing or event.all_day else 40
                rail_first = first or events[index - 1].all_day
                rail_last = last or events[index + 1].all_day
                rows[event.id] = AgendaRow(key[0], headings[key] if first else '', first, last,
                                           clock, detail, body_height, event.all_day, rail_first, rail_last)
        changed = rows != self.rows or metrics != self._metrics
        self.rows, self._metrics = rows, metrics
        if changed:
            self.revision += 1
        return changed


class AgendaResultDelegate(SearchResultDelegate):
    """Agenda day headings/rails; search always uses the original event cards."""

    def __init__(self, parent=None, *, subtitle_role=int(Qt.ItemDataRole.UserRole) + 1,
                 color_role=int(Qt.ItemDataRole.UserRole) + 2,
                 event_role=int(Qt.ItemDataRole.UserRole) + 5):
        super().__init__(parent, subtitle_role=subtitle_role, color_role=color_role)
        self.event_role = event_role
        self.presentation = AgendaPresentation()
        self._clock_metrics = None
        self._clock_width = 0

    def prepare_agenda(self, events, preferences, current, zone):
        return self.presentation.update(events, preferences, current, zone)

    def row_layout(self, index):
        widget = self.parent()
        if not getattr(widget, 'agenda_mode', False) or widget.preferences.agenda_style == 'cards':
            return None
        event = index.data(self.event_role)
        return self.presentation.rows.get(event.id) if event is not None else None

    def sizeHint(self, option, index):
        row = self.row_layout(index)
        if row is None:
            return super().sizeHint(option, index)
        scale = self._scale(option.widget or self.parent())
        return QSize(super().sizeHint(option, index).width(),
                     round((row.body_height + (20 if row.heading else 0)) * scale))

    def geometry(self, option, row):
        """Shared native row geometry, also useful for isolated design previews."""
        widget = option.widget or self.parent()
        scale = self._scale(widget)
        body = QRectF(option.rect).adjusted(0, 20 * scale if row.heading else 0, 0, 0)
        card = body.adjusted(scale, 3 * scale, -scale, -3 * scale)
        rail = None
        if widget.preferences.agenda_style == 'timeline' and not row.all_day:
            signature = option.font.key(), scale, self.presentation.revision
            if signature != self._clock_metrics:
                small = self._small_font(option.font, scale)
                metrics = QFontMetricsF(small)
                self._clock_width = max(44 * scale,
                                        max((metrics.horizontalAdvance(value.clock) + 3 * scale
                                             for value in self.presentation.rows.values() if not value.all_day), default=0))
                self._clock_metrics = signature
            rail = body.left() + self._clock_width + 6 * scale
            card.setLeft(rail + 9 * scale)
        return body, card, rail

    @staticmethod
    def _small_font(font, scale):
        small = QFont(font)
        if small.pixelSize() > 0:
            small.setPixelSize(max(9, small.pixelSize() - round(scale)))
        elif small.pointSizeF() > 0:
            small.setPointSizeF(max(7, small.pointSizeF() - scale))
        return small

    def paint(self, painter, option, index):
        row = self.row_layout(index)
        if row is None:
            return super().paint(painter, option, index)
        widget = option.widget or self.parent()
        scheme = scheme_for(widget)
        scale = self._scale(widget)
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        focused = bool(styled.state & QStyle.StateFlag.State_HasFocus and widget.hasFocus())
        colors = event_colors(index.data(self.color_role) or scheme.accent, scheme)
        body, card, rail = self.geometry(option, row)
        painter.save()
        painter.setClipRect(option.rect)
        normal = QFont(option.font)
        small = self._small_font(normal, scale)
        if row.heading:
            heading_font = QFont(small)
            heading_font.setWeight(QFont.Weight.Medium)
            painter.setFont(heading_font)
            painter.setPen(QColor(scheme.muted))
            painter.drawText(QPointF(option.rect.left(), option.rect.top() + 14 * scale),
                             painter.fontMetrics().elidedText(row.heading, Qt.TextElideMode.ElideRight,
                                                             max(0, option.rect.width() - 1)))
        if rail is not None:
            node_y = body.center().y()
            start_y, end_y = (node_y if row.rail_first else body.top()), (node_y if row.rail_last else body.bottom())
            painter.setPen(QPen(QColor(scheme.control_edge), scale))
            painter.drawLine(QPointF(rail, start_y), QPointF(rail, end_y))
            painter.setPen(QPen(QColor(colors.selected_outline), scale))
            painter.setBrush(QColor(colors.selected_outline if selected else colors.outline))
            painter.drawEllipse(QPointF(rail, node_y), 2.7 * scale, 2.7 * scale)
            painter.setFont(small)
            painter.setPen(QColor(scheme.ink if selected else scheme.muted))
            painter.drawText(QRectF(body.left(), body.top(), rail - body.left() - 6 * scale, body.height()),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, row.clock)
        paint_event_card(painter, card, colors, selected=selected, focused=focused,
                         focus_color=scheme.focus, scale=scale, preferences=widget)
        title_font = QFont(normal)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(colors.ink))
        width = max(0, int(card.width() - 16 * scale))
        title_y = card.top() + (17 if row.body_height == 49 else 21) * scale
        painter.drawText(QPointF(card.left() + 8 * scale, title_y),
                         painter.fontMetrics().elidedText(styled.text, Qt.TextElideMode.ElideRight, width))
        if row.body_height == 49:
            painter.setFont(small)
            painter.setPen(QColor(colors.muted))
            painter.drawText(QPointF(card.left() + 8 * scale, card.top() + 34 * scale),
                             painter.fontMetrics().elidedText(row.detail, Qt.TextElideMode.ElideRight, width))
        painter.restore()
