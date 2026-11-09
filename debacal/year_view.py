"""A twelve-month overview with preindexed, calendar-colored day markers."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from heapq import heappush, heappop
from html import escape
import calendar
import math

from PySide6.QtCore import Qt, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget, QMenu

from .preferences import Preferences
from .timeutil import local_zone, now
from .calendar_widgets import paint_colors
from .theme import corner_radius
from .calendar_appearance import weekend_text, weekend_fill
from .fonts import ui_font
from .year_presence import paint_day_presence
from .calendar_choice import calendar_color_icon


class YearView(QWidget):
    """Paint a year in three or four columns without scanning events on paint.

    Each day's markers summarize calendars, rather than trying to fit event
    titles into small cells. Days select a date; their context menu lists up to
    100 events. Month headings open the month,
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
        self._source_order = {}
        self._day_sources = []
        self._fonts = {}
        self._number_fonts = {}
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
        self._data_inputs = None
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
        previous = self.preferences
        if preferences == previous and hasattr(self, '_label_language'):
            return
        self.preferences = preferences
        language = preferences.qt_locale().name(), preferences.first_weekday
        if language != getattr(self, '_label_language', None):
            self._label_language = language
            self._month_names = [preferences.month_name(month) for month in range(1, 13)]
            self._weekday_names = [preferences.weekday_name((preferences.first_weekday + offset) % 7)
                                   for offset in range(7)]
        if (previous.font_scale, previous.font_family) != (preferences.font_scale, preferences.font_family):
            self._fonts.clear()
            self._number_fonts.clear()
        geometry = (preferences.font_scale, preferences.font_family, preferences.first_weekday,
                    preferences.locale_name, preferences.show_week_numbers)
        if geometry != getattr(self, '_preference_geometry', None):
            self._preference_geometry = geometry
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

    def _event_interval(self, event, zone=None):
        if event.all_day:
            first, last = event.start.date().toordinal(), event.end.date().toordinal()
            if last == first:
                last += 1
        else:
            zone = zone or local_zone()
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
        # Query pages reuse frozen Event values. Retain one shallow input
        # snapshot, including filtered sources, so appearance-only refreshes
        # need neither rebuild intervals nor discard valid grid geometry.
        events, calendars = tuple(events), tuple(calendars)
        zone = local_zone()
        inputs = year, events, calendars, zone
        previous = self._data_inputs
        # Value equality can equate distinct repeated-hour instants; require
        # the unchanged Event objects supplied by the occurrence-page cache.
        if (type(year) is int and previous is not None and year == previous[0]
                and calendars == previous[2] and zone == previous[3]
                and len(events) == len(previous[1])
                and all(current is old for current, old in zip(events, previous[1]))):
            return
        self._data_inputs = None
        self._set_year(year)
        self.events = list(events)
        self.calendars = {value.id: value for value in calendars}
        self._source_order = {identity: index for index, identity in enumerate(self.calendars)}
        self.events = [value for value in self.events
                       if value.calendar_id not in self.calendars
                       or (self.calendars[value.calendar_id].visible and self.calendars[value.calendar_id].enabled)]
        self._event_map = {value.id: value for value in self.events}
        self._intervals = {}
        values = []
        for position, event in enumerate(self.events):
            interval = self._event_interval(event, zone)
            if interval is not None:
                self._intervals[event.id] = interval
                values.append((position, event, interval))
        self._summaries = self._summarize(values)
        self._day_sources = [tuple(sorted(summary, key=lambda identity:
                             (self._source_order.get(identity, len(self._source_order)), identity)))
                             for summary in self._summaries]
        self._update_selected()
        self._update_preview()
        self._data_inputs = inputs
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
        self.update()

    def _update_selected(self):
        self._selected_summaries = self._summarize(
            (position, self._event_map[identity], self._intervals[identity])
            for position, identity in enumerate(sorted(self.selected_ids)) if identity in self._intervals)

    def set_selected_day(self, day):
        if day == self.selected_day:
            return
        self.selected_day = day
        self.update()

    def set_scope(self, scope):
        self.scope = scope if scope in ('single', 'group', 'future') else 'single'

    def set_drag_modifiers(self, modifiers):
        pass

    def set_creation_calendar(self, calendar):
        if calendar == self.creation_calendar:
            return
        self.creation_calendar = calendar
        self.update()

    def set_preview(self, events):
        events = list(events)
        if events == self.preview_events:
            return
        self.preview_events = events
        self._update_preview()
        self.update()

    def _update_preview(self):
        zone = local_zone()
        self._preview_summaries = self._summarize(
            (position, event, interval) for position, event in enumerate(self.preview_events)
            if (interval := self._event_interval(event, zone)) is not None)

    def _ensure_geometry(self):
        key = (self.width(), self.height(), self.year, self._preference_geometry)
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

    def calendars_on_day(self, day):
        """Distinct active sources, in the same order as the calendar list."""
        offset = (day - self._first).days
        if not 0 <= offset < len(self._day_sources):
            return ()
        return self._day_sources[offset]

    def _day_tooltip(self, day):
        heading = '<b>' + escape(self.preferences.format_date(day, weekday=True)) + '</b>'
        summary = self._summaries[(day - self._first).days]
        rows = []
        for identity in self.calendars_on_day(day):
            source = self.calendars.get(identity)
            count = summary[identity][0]
            rows.append(escape(source.name if source else identity) + f' · {count:,} '
                        + ('event' if count == 1 else 'events'))
        return heading + ('<br>' + '<br>'.join(rows) if rows else '')

    def _font(self, size, bold):
        key = size, bold
        if key not in self._fonts:
            self._fonts[key] = ui_font(self.preferences, size, bold)
        return self._fonts[key]

    def _number_font(self, rect, size, bold):
        key = round(rect.width(), 2), round(rect.height(), 2), size, bold
        if key not in self._number_fonts:
            font = QFont(self._font(size, bold))
            metrics = QFontMetricsF(font)
            label_width = max(metrics.horizontalAdvance(str(day)) for day in range(1, 32))
            factor = min(1, rect.width() / max(1, label_width),
                         rect.height() / max(1, metrics.height()))
            if factor < 1:
                font.setPointSizeF(max(5 * self.scale, font.pointSizeF() * factor))
            if len(self._number_fonts) >= 128:
                self._number_fonts.clear()
            self._number_fonts[key] = font
        return self._number_fonts[key]

    def _text(self, painter, rect, value, color=None, size=9, bold=False, alignment=Qt.AlignmentFlag.AlignCenter,
              *, number=False, underline=False):
        font = self._number_font(rect, size, bold) if number else self._font(size, bold)
        if underline:
            font = QFont(font)
            font.setUnderline(True)
        painter.setFont(font)
        painter.setPen(color if color is not None else paint_colors(self).ink)
        painter.drawText(rect, alignment, value if number else
                         painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, max(0, int(rect.width()))))

    def paintEvent(self, event):
        self._ensure_geometry()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        painter.fillRect(self.rect(), colors.background)
        scale, today = self.scale, now().date()
        for month, (outer, heading, grid) in self._month_rects.items():
            if not outer.intersects(event.rect()):
                continue
            painter.setPen(QPen(colors.line, 1))
            painter.setBrush(colors.panel)
            painter.drawRoundedRect(outer, corner_radius(self, 7) * scale, corner_radius(self, 7) * scale)
            self._text(painter, heading, self._month_names[month - 1], colors.accent, 11, True, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for column, label in enumerate(self._weekday_names):
                rect = QRectF(grid.x() + column * grid.width() / 7, outer.y() + 31 * scale, grid.width() / 7, 18 * scale)
                weekday = (self.preferences.first_weekday + column) % 7
                shade = weekend_fill(weekday, self.preferences, colors)
                if shade is not None:
                    painter.fillRect(rect, shade)
                self._text(painter, rect, label, weekend_text(weekday, self.preferences, colors, colors.muted), 8)
            if self.preferences.show_week_numbers:
                self._text(painter, QRectF(outer.x() + 4 * scale, outer.y() + 31 * scale,
                                          21 * scale, 18 * scale), 'Wk', colors.muted, 7)
                for (week_month, row), (rect, start) in self._week_rects.items():
                    if week_month != month:
                        continue
                    hovered = self._hover_week == (month, row)
                    if hovered:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(colors.selection)
                        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), corner_radius(self, 3) * scale, corner_radius(self, 3) * scale)
                    self._text(painter, rect, str(self.preferences.week_number(start)), colors.accent if hovered else colors.muted,
                               7, hovered)
            painter.setPen(QPen(colors.line, .45))
            for row in range(7):
                y = grid.y() + row * grid.height() / 6
                painter.drawLine(grid.x(), y, grid.right(), y)
        for day, rect in self._day_rects.items():
            if not rect.intersects(event.rect()):
                continue
            offset = (day - self._first).days
            selected = bool(self._selected_summaries[offset]) if self._selected_summaries else False
            preview = bool(self._preview_summaries[offset]) if self._preview_summaries else False
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                painter.fillRect(rect, shade)
            identities = self.calendars_on_day(day)
            sources = [self.calendars[identity].color if identity in self.calendars else colors.muted for identity in identities]
            number_rect = paint_day_presence(painter, rect, sources, self.preferences, colors)
            if day == self.selected_day or selected or preview:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                pen = QPen(colors.line, 1.5 * scale)
                if preview:
                    identity = min(self._preview_summaries[offset])
                    source = self.calendars.get(identity)
                    if source is None and self.creation_calendar is not None and self.creation_calendar.id == identity:
                        source = self.creation_calendar
                    color = QColor(source.color) if source is not None else colors.accent
                    pen = QPen(color if color.isValid() else colors.accent, 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                indicator = rect.adjusted(scale, scale, -scale, -scale)
                painter.drawRoundedRect(indicator, corner_radius(self, 4) * scale, corner_radius(self, 4) * scale)
            self._text(painter, number_rect, str(day.day),
                       weekend_text(day, self.preferences, colors, colors.accent if day == today else colors.ink),
                       10 if self.preferences.year_view_style == 'tints' else 8, selected or day == today,
                       number=True, underline=day == today)
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
        if hovered is None:
            self.setToolTip(self._day_tooltip(day) if day is not None else self._default_tooltip)
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
        menu = QMenu(self)
        menu.addAction('Open day', lambda: self.open_day.emit(day))
        values = self.events_on_day(day)
        if not values:
            self.day_selected.emit(day)
        if values:
            menu.addSeparator()
            source_icons = {}
            for value in values:
                source = self.calendars.get(value.calendar_id)
                source_name = source.name if source else value.calendar_id
                label = ('All day' if value.all_day else self.preferences.format_time(value.start.astimezone(local_zone())))
                label += ' · ' + source_name + ' · ' + value.title
                if value.calendar_id not in source_icons:
                    color = source.color if source else paint_colors(self).muted
                    source_icons[value.calendar_id] = calendar_color_icon(color, self.preferences)
                action = menu.addAction(source_icons[value.calendar_id], label.replace('&', '&&'))
                action.triggered.connect(lambda _checked=False, identity=value.id: self._select_event(identity, Qt.KeyboardModifier.NoModifier))
            total = sum(count for count, _identity in self._summaries[(day - self._first).days].values())
            if total > len(values):
                action = menu.addAction(f'{total - len(values):,} more · open the day to see all')
                action.setEnabled(False)
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()
