"""A twelve-month overview with preindexed, calendar-colored day markers."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from heapq import heappush, heappop
import calendar
import math

from PySide6.QtCore import Qt, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget, QMenu

from .preferences import Preferences
from .timeutil import local_zone, now


BG = QColor('#fafbf8')
LINE = QColor('#e4e8e0')
INK = QColor('#26362e')
MUTED = QColor('#89938a')
GREEN = QColor('#416c51')
BLUE = QColor('#2c527c')


class YearView(QWidget):
    """Paint a year in three or four columns without scanning events on paint.

    Each day's markers summarize calendars, rather than trying to fit event
    titles into small cells. A marker selects its first (or selected) event;
    the day context menu lists up to 100 events. Month headings open the month,
    week numbers open the week, and double-clicking any day creates an all-day
    event.
    """
    day_selected = Signal(object)
    month_requested = Signal(object)
    week_requested = Signal(object)
    open_day = Signal(object)
    create_all_day_requested = Signal(object, object)
    selected = Signal(str)
    selected_scoped = Signal(str, object)
    context_requested = Signal(str, object)
    placement_moved = Signal(object)
    placement_confirmed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preferences = Preferences()
        self.year = now().year
        self.events = []
        self.calendars = {}
        self.selected_ids = set()
        self.selected_id = None
        self.selected_day = None
        self.scope = 'single'
        self.preview_events = []
        self.placement_active = False
        self.creation_calendar = None
        self._placement_consumed = False
        self._event_map = {}
        self._intervals = {}
        self._summaries = []
        self._selected_summaries = []
        self._preview_summaries = []
        self._geometry_key = None
        self._day_rects = {}
        self._month_rects = {}
        self._week_rects = {}
        self._hover_week = None
        self._marker_hits = []
        self.hits = []
        self._set_year(self.year)
        self.set_preferences(self.preferences)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._default_tooltip = ('Click a month heading to open Month view, or a week number to open Week view. '
                                 'Click a colored marker to select an event. '
                                 'Double-click a day to create an all-day event.')
        self.setToolTip(self._default_tooltip)

    @property
    def scale(self):
        return self.preferences.font_scale / 100

    def _set_year(self, year):
        if type(year) is not int or not 1 <= year <= 9999:
            raise ValueError('The year must be between 1 and 9999.')
        self.year = year
        self._first = date(year, 1, 1)
        self._first_ordinal = self._first.toordinal()
        self._day_count = 366 if calendar.isleap(year) else 365
        self.days = [date.fromordinal(self._first_ordinal + offset) for offset in range(self._day_count)]
        self._geometry_key = None
        self._hover_week = None

    def set_preferences(self, preferences):
        self.preferences = preferences
        self._month_names = [preferences.month_name(month) for month in range(1, 13)]
        self._weekday_names = [preferences.weekday_name((preferences.first_weekday + offset) % 7)
                               for offset in range(7)]
        self._geometry_key = None
        self._hover_week = None
        self.setMinimumWidth(math.ceil((2 * 165 + 3 * 12) * self.scale))
        self.updateGeometry()
        self.update()

    def _columns(self, width):
        return 4 if width >= 1000 * self.scale else 3 if width >= 600 * self.scale else 2

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        rows = 12 // self._columns(width)
        return math.ceil((rows * 190 + (rows + 1) * 12) * self.scale)

    def minimumSizeHint(self):
        width = math.ceil(700 * self.scale)
        return QSize(width, self.heightForWidth(width))

    def sizeHint(self):
        width = math.ceil(1000 * self.scale)
        return QSize(width, self.heightForWidth(width))

    def _event_interval(self, event):
        if event.all_day:
            first, last = event.start.date().toordinal(), event.end.date().toordinal()
            if last == first:
                last += 1
        else:
            zone = local_zone()
            first = event.start.astimezone(zone).date().toordinal()
            if event.end.timestamp() == event.start.timestamp():
                last = first + 1
            else:
                last = (event.end.astimezone(timezone.utc) - timedelta(microseconds=1)).astimezone(zone).date().toordinal() + 1
        first, last = max(0, first - self._first_ordinal), min(self._day_count, last - self._first_ordinal)
        return (first, last) if first < last else None

    def _summarize(self, values):
        """Sweep interval endpoints; keep only one event per day/calendar."""
        starts = [[] for _ in range(self._day_count)]
        stops = [[] for _ in range(self._day_count + 1)]
        for position, event, (first, last) in values:
            starts[first].append((position, last, event.id, event.calendar_id))
            stops[last].append(event.calendar_id)
        counts, heaps, result = {}, {}, []
        for day in range(self._day_count):
            for identity in stops[day]:
                counts[identity] -= 1
                if not counts[identity]:
                    del counts[identity]
                    heaps.pop(identity, None)
            for position, last, identity, calendar_id in starts[day]:
                counts[calendar_id] = counts.get(calendar_id, 0) + 1
                heappush(heaps.setdefault(calendar_id, []), (position, last, identity))
            summary = {}
            for identity, count in counts.items():
                active = heaps[identity]
                while active and active[0][1] <= day:
                    heappop(active)
                summary[identity] = (count, active[0][2])
            result.append(summary)
        return result

    def set_data(self, year, events, calendars):
        self._set_year(year)
        self.events = list(events)
        self.calendars = {value.id: value for value in calendars}
        self._event_map = {value.id: value for value in self.events}
        self._intervals = {}
        values = []
        for position, event in enumerate(self.events):
            interval = self._event_interval(event)
            if interval is not None:
                self._intervals[event.id] = interval
                values.append((position, event, interval))
        self._summaries = self._summarize(values)
        self._update_selected()
        self._update_preview()
        self._geometry_key = None
        self.updateGeometry()
        self.update()

    def set_selection(self, identifiers):
        identifiers = set(identifiers)
        if identifiers == self.selected_ids:
            return
        self.selected_ids = identifiers
        if self.selected_id not in identifiers:
            self.selected_id = next(iter(sorted(identifiers)), None)
        self._update_selected()
        self._geometry_key = None
        self.update()

    def _update_selected(self):
        self._selected_summaries = self._summarize(
            (position, self._event_map[identity], self._intervals[identity])
            for position, identity in enumerate(sorted(self.selected_ids)) if identity in self._intervals)

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    def set_scope(self, scope):
        self.scope = scope if scope in ('single', 'group', 'future') else 'single'

    def set_drag_modifiers(self, modifiers):
        pass

    def set_creation_calendar(self, calendar):
        self.creation_calendar = calendar
        self.update()

    def set_preview(self, events):
        self.preview_events = list(events)
        self._update_preview()
        self.update()

    def _update_preview(self):
        self._preview_summaries = self._summarize(
            (position, event, interval) for position, event in enumerate(self.preview_events)
            if (interval := self._event_interval(event)) is not None)

    def _ensure_geometry(self):
        key = (self.width(), self.height(), self.year, self.preferences)
        if key == self._geometry_key:
            return
        self._geometry_key = key
        self._day_rects, self._month_rects, self._week_rects, self._marker_hits = {}, {}, {}, []
        self.hits = []
        scale, columns = self.scale, self._columns(self.width())
        rows, gap = 12 // columns, 12 * scale
        width = max(1, (self.width() - (columns + 1) * gap) / columns)
        height = max(190 * scale, (self.height() - (rows + 1) * gap) / rows)
        for month in range(1, 13):
            row, column = divmod(month - 1, columns)
            outer = QRectF(gap + column * (width + gap), gap + row * (height + gap), width, height)
            heading = QRectF(outer.x() + 10 * scale, outer.y() + 5 * scale, width - 20 * scale, 25 * scale)
            week_width = 23 * scale if self.preferences.show_week_numbers else 0
            grid = QRectF(outer.x() + 6 * scale + week_width, outer.y() + 52 * scale,
                          width - 12 * scale - week_width, height - 60 * scale)
            self._month_rects[month] = (outer, heading, grid)
            cell_width, cell_height = grid.width() / 7, grid.height() / 6
            first = date(self.year, month, 1)
            offset = (first.weekday() - self.preferences.first_weekday) % 7
            if self.preferences.show_week_numbers:
                occupied_rows = math.ceil((offset + calendar.monthrange(self.year, month)[1]) / 7)
                for week_row in range(occupied_rows):
                    ordinal = first.toordinal() - offset + week_row * 7
                    if 1 <= ordinal <= date.max.toordinal():
                        start = date.fromordinal(ordinal)
                        rect = QRectF(outer.x() + 4 * scale, grid.y() + week_row * cell_height,
                                      week_width - 2 * scale, cell_height)
                        self._week_rects[month, week_row] = (rect, start)
            for day in range(1, calendar.monthrange(self.year, month)[1] + 1):
                index = offset + day - 1
                row, column = divmod(index, 7)
                value = date(self.year, month, day)
                rect = QRectF(grid.x() + column * cell_width, grid.y() + row * cell_height, cell_width, cell_height)
                self._day_rects[value] = rect
                summary = self._summaries[(value - self._first).days] if self._summaries else {}
                selected = self._selected_summaries[(value - self._first).days] if self._selected_summaries else {}
                markers = sorted(summary, key=lambda identity: (identity not in selected, identity))[:3]
                marker_width = min(8 * scale, max(3 * scale, (cell_width - 8 * scale) / max(1, len(markers))))
                total = len(markers) * marker_width + max(0, len(markers) - 1) * 2 * scale
                left = rect.center().x() - total / 2
                for number, identity in enumerate(markers):
                    event_id = selected.get(identity, summary[identity])[1]
                    marker = QRectF(left + number * (marker_width + 2 * scale), rect.bottom() - 5 * scale,
                                    marker_width, 3 * scale)
                    hit = marker.adjusted(-2 * scale, -2 * scale, 2 * scale, 2 * scale)
                    self._marker_hits.append((hit, event_id, value, identity))
                    event = self._event_map.get(event_id)
                    if event is not None:
                        self.hits.append((hit, event))

    def day_rect(self, day):
        self._ensure_geometry()
        value = self._day_rects.get(day)
        return QRectF(value) if value is not None else None

    def month_rect(self, month):
        self._ensure_geometry()
        value = self._month_rects.get(month)
        return QRectF(value[1]) if value is not None else None

    def week_rect(self, month, row):
        """Return the week-number target for a populated month row (zero based)."""
        self._ensure_geometry()
        value = self._week_rects.get((month, row))
        return QRectF(value[0]) if value is not None else None

    def week_at(self, point):
        """Return the configured first day of the week under the pointer."""
        self._ensure_geometry()
        return next((start for rect, start in self._week_rects.values() if rect.contains(point)), None)

    def day_at(self, point):
        self._ensure_geometry()
        return next((day for day, rect in self._day_rects.items() if rect.contains(point)), None)

    def event_rect(self, identity):
        self._ensure_geometry()
        hit = next((rect for rect, event in self.hits if event.id == identity), None)
        if hit is not None:
            return QRectF(hit)
        interval = self._intervals.get(identity)
        return self.day_rect(self.days[interval[0]]) if interval else None

    def ensure_event_visible(self, identity):
        return self.event_rect(identity)

    def events_on_day(self, day, *, limit=100):
        """Resolve a bounded day list on demand, outside the painting path."""
        offset = (day - self._first).days
        if not 0 <= offset < self._day_count or limit <= 0:
            return []
        result = []
        for event in self.events:
            interval = self._intervals.get(event.id)
            if interval is not None and interval[0] <= offset < interval[1]:
                result.append(event)
                if len(result) >= limit:
                    break
        return result

    def _text(self, painter, rect, value, color=INK, size=9, bold=False, alignment=Qt.AlignmentFlag.AlignCenter):
        font = QFont('Sans Serif')
        font.setPointSizeF(size * self.scale)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(rect, alignment, painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, max(0, int(rect.width()))))

    def paintEvent(self, event):
        self._ensure_geometry()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), BG)
        scale, today = self.scale, now().date()
        for month, (outer, heading, grid) in self._month_rects.items():
            if not outer.intersects(event.rect()):
                continue
            painter.setPen(QPen(LINE, 1))
            painter.setBrush(QColor('#ffffff'))
            painter.drawRoundedRect(outer, 7 * scale, 7 * scale)
            self._text(painter, heading, self._month_names[month - 1], GREEN, 11, True, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for column, label in enumerate(self._weekday_names):
                rect = QRectF(grid.x() + column * grid.width() / 7, outer.y() + 31 * scale, grid.width() / 7, 18 * scale)
                self._text(painter, rect, label, MUTED, 7)
            if self.preferences.show_week_numbers:
                self._text(painter, QRectF(outer.x() + 4 * scale, outer.y() + 31 * scale,
                                          21 * scale, 18 * scale), 'Wk', MUTED, 6)
                for (week_month, row), (rect, start) in self._week_rects.items():
                    if week_month != month:
                        continue
                    hovered = self._hover_week == (month, row)
                    if hovered:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor('#edf2e7'))
                        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3 * scale, 3 * scale)
                    self._text(painter, rect, str(self.preferences.week_number(start)), GREEN if hovered else MUTED,
                               7, hovered)
        for day, rect in self._day_rects.items():
            if not rect.intersects(event.rect()):
                continue
            offset = (day - self._first).days
            selected = bool(self._selected_summaries[offset]) if self._selected_summaries else False
            preview = bool(self._preview_summaries[offset]) if self._preview_summaries else False
            box = rect.adjusted(1 * scale, 0, -1 * scale, 0)
            if day == self.selected_day or selected or preview or day == today:
                color = QColor('#edf1f8') if day == self.selected_day or selected else QColor('#edf2e7')
                painter.setBrush(color)
                pen = QPen(BLUE if selected else QColor('#91a2b8'), 1) if day == self.selected_day or selected else QPen(Qt.PenStyle.NoPen)
                if preview:
                    identity = min(self._preview_summaries[offset])
                    source = self.calendars.get(identity)
                    if source is None and self.creation_calendar is not None and self.creation_calendar.id == identity:
                        source = self.creation_calendar
                    color = QColor(source.color) if source is not None else QColor(GREEN)
                    pen = QPen(color if color.isValid() else GREEN, 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawRoundedRect(box, 4 * scale, 4 * scale)
            number_rect = rect.adjusted(0, -2 * scale, 0, -3 * scale)
            self._text(painter, number_rect, str(day.day), BLUE if selected else GREEN if day == today else INK,
                       8, selected or day == today)
        for hit, _identity, day, calendar_id in self._marker_hits:
            if not hit.intersects(event.rect()):
                continue
            calendar_value = self.calendars.get(calendar_id)
            color = QColor(calendar_value.color if calendar_value else '#788373')
            if not color.isValid():
                color = GREEN
            marker = hit.adjusted(2 * scale, 2 * scale, -2 * scale, -2 * scale)
            painter.setBrush(color)
            selected = self._selected_summaries[(day - self._first).days]
            painter.setPen(QPen(BLUE, 1) if calendar_id in selected else QPen(Qt.PenStyle.NoPen))
            painter.drawRoundedRect(marker, 1 * scale, 1 * scale)
        painter.end()

    def _select_event(self, identity, modifiers):
        self.selected_id = identity
        self.selected_scoped.emit(identity, modifiers)
        self.selected.emit(identity)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self._ensure_geometry()
        point = event.position()
        for month, (_outer, heading, _grid) in self._month_rects.items():
            if heading.contains(point):
                self.month_requested.emit(date(self.year, month, 1))
                return
        week = self.week_at(point)
        if week is not None:
            self.setFocus()
            self.week_requested.emit(week)
            return
        day = self.day_at(point)
        if day is None:
            return
        self.setFocus()
        if self.placement_active:
            self._placement_consumed = True
            self.placement_confirmed.emit(datetime.combine(day, time.min, local_zone()))
            return
        self._placement_consumed = False
        self.set_selected_day(day)
        marker = next((identity for rect, identity, _day, _calendar in reversed(self._marker_hits) if rect.contains(point)), None)
        if marker is not None:
            self._select_event(marker, event.modifiers())
        else:
            self.day_selected.emit(day)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseDoubleClickEvent(event)
        day = self.day_at(event.position())
        if day is None:
            return
        if self._placement_consumed:
            self._placement_consumed = False
            return
        start = datetime.combine(day, time.min, local_zone())
        if self.placement_active:
            self.placement_confirmed.emit(start)
            return
        self.setFocus()
        self.set_selected_day(day)
        self.day_selected.emit(day)
        if day < date.max:
            self.create_all_day_requested.emit(start, datetime.combine(day + timedelta(days=1), time.min, local_zone()))

    def mouseMoveEvent(self, event):
        self._ensure_geometry()
        hovered = next((key for key, (rect, _start) in self._week_rects.items()
                        if rect.contains(event.position())), None)
        if hovered != self._hover_week:
            previous, self._hover_week = self._hover_week, hovered
            for key in (previous, hovered):
                if key in self._week_rects:
                    self.update(self._week_rects[key][0].toAlignedRect())
            if hovered is not None:
                start = self._week_rects[hovered][1]
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip(f'Open week {self.preferences.week_number(start)} · '
                                + self.preferences.format_date(start))
            else:
                self.unsetCursor()
                self.setToolTip(self._default_tooltip)
        day = self.day_at(event.position())
        if self.placement_active and day is not None:
            self.placement_moved.emit(datetime.combine(day, time.min, local_zone()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        previous, self._hover_week = self._hover_week, None
        if previous in self._week_rects:
            self.update(self._week_rects[previous][0].toAlignedRect())
        self.unsetCursor()
        self.setToolTip(self._default_tooltip)
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        day = self.day_at(event.pos())
        if day is None or self.placement_active:
            return
        self.set_selected_day(day)
        marker = next((identity for rect, identity, _day, _calendar in reversed(self._marker_hits)
                       if rect.contains(event.pos())), None)
        if marker is not None:
            if marker not in self.selected_ids and marker != self.selected_id:
                self._select_event(marker, Qt.KeyboardModifier.NoModifier)
            self.context_requested.emit(marker, event.globalPos())
            return
        menu = QMenu(self)
        menu.addAction('Open day', lambda: self.open_day.emit(day))
        values = self.events_on_day(day)
        if not values:
            self.day_selected.emit(day)
        if values:
            menu.addSeparator()
            for value in values:
                label = ('All day' if value.all_day else self.preferences.format_time(value.start.astimezone(local_zone()))) + ' · ' + value.title
                action = menu.addAction(label.replace('&', '&&'))
                action.triggered.connect(lambda _checked=False, identity=value.id: self._select_event(identity, Qt.KeyboardModifier.NoModifier))
            total = sum(count for count, _identity in self._summaries[(day - self._first).days].values())
            if total > len(values):
                action = menu.addAction(f'{total - len(values):,} more · open the day to see all')
                action.setEnabled(False)
        menu.exec(event.globalPos())
