"""Paint-only calendar surfaces: visible events are cheap; gestures commit on release."""
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import QWidget, QMenu

from .layout import layout_events
from .recurrence import overlaps
from .preferences import Preferences
from .timeutil import local_zone, now
from .event_scope import normalize_scope_modifiers, scope_from_modifiers

BG = QColor('#fafbf8')
LINE = QColor('#e4e8e0')
INK = QColor('#26362e')
MUTED = QColor('#89938a')
GREEN = QColor('#416c51')
SELECTED_DAY = QColor('#edf1f8')
SELECTED_DAY_BORDER = QColor('#91a2b8')
SELECTED_EVENT = QColor('#dce8f5')
SELECTED_EVENT_BORDER = QColor('#2c527c')


def at(day, hour=0, minute=0):
    return datetime.combine(day, time(hour, minute), local_zone())


def display_scale(widget):
    preferences = getattr(widget, 'preferences', None)
    if preferences is None:
        preferences = getattr(getattr(widget, 'preview', None), 'preferences', None)
    return getattr(preferences, 'font_scale', 100) / 100


def text(painter, rect, value, color=INK, size=10, bold=False, align=None):
    painter.setPen(color)
    font = QFont('Sans Serif')
    font.setPointSizeF(size * display_scale(painter.device()))
    font.setBold(bold)
    painter.setFont(font)
    painter.drawText(rect, align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), value)


def badge(painter, rect, value, selected=False):
    painter.setPen(QPen(GREEN if selected else QColor('#bccbb4'), 1))
    painter.setBrush(GREEN if selected else QColor('#f8fbf3'))
    painter.drawRoundedRect(rect, 4, 4)
    text(painter, rect, value, QColor('#ffffff') if selected else GREEN, 8, True,
         Qt.AlignmentFlag.AlignCenter)


def week_spans(events, first, days=7, *, capacity=None, selected=(), primary=None, reserve_timed=()):
    """Place whole DATE spans in shared lanes, optionally bounded by cell rows."""
    last = first + timedelta(days=days)
    visible = [item for item in events if item.all_day and item.start.date() < last and item.end.date() > first]
    visible.sort(key=lambda item: (0 if item.id == primary else 1 if item.id in selected else 2,
                                  max(first, item.start.date()), -(item.end.date() - item.start.date()).days, item.id))
    lanes, result = [], []
    reserved = sum(1 << index for index in reserve_timed)
    for number, item in enumerate(visible, 1):
        start = (max(first, item.start.date()) - first).days
        end = (min(last, item.end.date()) - first).days
        mask = ((1 << (end - start)) - 1) << start
        limit = capacity
        if limit is not None and item.id != primary and mask & reserved:
            limit = max(0, limit - 1)
        lane = next((index for index, occupied in enumerate(lanes)
                     if (limit is None or index < limit) and not occupied & mask), None)
        if lane is None:
            if limit is not None and len(lanes) >= limit:
                continue
            lane = len(lanes)
            lanes.append(0)
        lanes[lane] |= mask
        result.append((item, lane, start, end, number, item.start.date() < first, item.end.date() > last))
    return result


class DayHeader(QWidget):
    day_selected = Signal(object)
    create_all_day_requested = Signal(object, object)
    placement_moved = Signal(object)
    placement_confirmed = Signal(object)
    gutter = 62
    all_day_top = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days = []
        self.preferences = Preferences()
        self.navigation_active = False
        self.navigation_day = None
        self.navigation_digits = ''
        self.selected_day = None
        self.placement_active = False
        self._placement_consumed = False
        self.setFixedHeight(84)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Click to select a day. Double-click to create an all-day event.')

    def set_preferences(self, preferences):
        self.preferences = preferences
        scale = display_scale(self)
        self.gutter = math.ceil(62 * scale)
        self.all_day_top = math.ceil(60 * scale)
        self.setFixedHeight(math.ceil(84 * scale))
        self.update()

    def set_navigation(self, active, day=None, digits=''):
        self.navigation_active = bool(active)
        self.navigation_day = day if active else None
        self.navigation_digits = str(digits or '') if active else ''
        self.update()

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    def day_at(self, point):
        if not self.days or point.x() < self.gutter or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        width = (self.width() - self.gutter) / len(self.days)
        if width <= 0:
            return None
        return self.days[min(len(self.days) - 1, int((point.x() - self.gutter) / width))]

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            day = self.day_at(event.position())
            if day is not None:
                self.setFocus()
                if self.placement_active:
                    self._placement_consumed = True
                    self.placement_confirmed.emit(at(day))
                    return
                self._placement_consumed = False
                self.set_selected_day(day)
                self.day_selected.emit(day)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            day = self.day_at(event.position())
            if day is not None:
                if self._placement_consumed:
                    self._placement_consumed = False
                    return
                if self.placement_active:
                    self.placement_confirmed.emit(at(day))
                    return
                self.setFocus()
                self.set_selected_day(day)
                self.day_selected.emit(day)
                self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))

    def mouseMoveEvent(self, event):
        day = self.day_at(event.position())
        if self.placement_active and day is not None:
            self.placement_moved.emit(at(day))

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        if not self.days:
            return
        w = (self.width() - self.gutter) / len(self.days)
        scale = display_scale(self)
        for i, day in enumerate(self.days):
            x = self.gutter + i * w
            today = day == now().date()
            selected_day = self.navigation_active and i == self.navigation_day
            if today or selected_day:
                p.setPen(QPen(GREEN, 2) if selected_day else QPen(Qt.PenStyle.NoPen))
                p.setBrush(QColor('#dceace') if selected_day else QColor('#e6eedf'))
                p.drawRoundedRect(QRectF(x + 8, 5 * scale, w - 16, 54 * scale), 7, 7)
            text(p, QRectF(x + 14, 8 * scale, w - 22, 20 * scale), self.preferences.weekday_name(day).upper(), GREEN if today or selected_day else MUTED, 9, True)
            text(p, QRectF(x + 14, 27 * scale, w - 22, math.ceil(28 * scale)), str(day.day), GREEN if today else INK, 18, True)
            if self.navigation_active:
                badge(p, QRectF(x + w - 34, 11, 21, 21), str(i + 1), selected_day)
            p.setPen(QPen(LINE, 1))
            p.drawLine(QPointF(x + 8, self.all_day_top), QPointF(x + w - 8, self.all_day_top))
            text(p, QRectF(x + 8, self.all_day_top + 1, w - 16, 20 * scale), '+ All day', GREEN, 8,
                 align=Qt.AlignmentFlag.AlignCenter)
        p.end()


class CalendarCanvas(QWidget):
    create_requested = Signal(object, object)
    create_all_day_requested = Signal(object, object)
    day_selected = Signal(object)
    edit_requested = Signal(str)
    move_requested = Signal(str, object, object)
    edit_scoped_requested = Signal(str, object)
    move_scoped_requested = Signal(object, object, object, object)
    selected = Signal(str)
    selected_scoped = Signal(str, object)
    gesture_started = Signal(str)
    drag_preview_requested = Signal(str, object, object, object)
    placement_moved = Signal(object)
    placement_confirmed = Signal(object)
    context_requested = Signal(str, object)

    hour_height = 64
    gutter = 62
    all_day_height = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days, self.events, self.calendars = [], [], {}
        self.selected_id = None
        self.selected_ids = set()
        self.selected_day = None
        self.scope = 'single'
        self.preview_events = []
        self.placement_active = False
        self._placement_consumed = False
        self._deferred_selection = None
        self.fixed_all_day = False
        self.start_hour, self.end_hour = 0, 24
        self._viewport_height = None
        self.full_day = False
        self.hits = []
        self.drag = None
        self.ghost = None
        self.hover_time = None
        self._drag_pointer = None
        self.show_numbers = False
        self.preferences = Preferences()
        self.scope_modifiers = normalize_scope_modifiers()
        self.gesture_modifiers = Qt.KeyboardModifier.NoModifier
        self.navigation_day = None
        self.navigation_digits = ''
        # Keep a late-night point's minimum visual marker fully visible below
        # its true time, without extending the event itself into tomorrow.
        self.setMinimumHeight(self.hour_height * 24 + self.all_day_height + 20)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_data(self, days, events, calendars):
        self.days, self.events = days, events
        self.hover_time = None
        self.calendars = {c.id: c for c in calendars}
        self._refit()
        self.update()

    @property
    def time_top(self):
        return 0 if self.fixed_all_day else self.all_day_height

    def set_selection(self, ids):
        self.selected_ids = set(ids)
        if self.selected_id not in self.selected_ids:
            self.selected_id = next(iter(sorted(self.selected_ids)), None)
        self.update()

    def set_scope(self, scope):
        self.scope = scope if scope in ('single', 'group', 'future') else 'single'
        self.update()

    def set_preview(self, events):
        self.preview_events = list(events)
        if self.placement_active:
            self.hover_time = None
        self.update()

    def set_drag_modifiers(self, modifiers):
        self.gesture_modifiers = modifiers
        self.update()

    def copy_badge_rect(self):
        if (self.placement_active or not self.drag or not self.ghost or self.drag[0] == 'create'
                or not self.gesture_modifiers & Qt.KeyboardModifier.ControlModifier or self._drag_pointer is None):
            return None
        scale = display_scale(self)
        return QRectF(min(max(2, self._drag_pointer.x() + 12 * scale), self.width() - 24 * scale),
                      min(max(2, self._drag_pointer.y() + 10 * scale), self.height() - 23 * scale), 21 * scale, 21 * scale)

    def paint_copy_badge(self, painter):
        rect = self.copy_badge_rect()
        if rect is not None:
            painter.setPen(QPen(SELECTED_EVENT_BORDER, 1.5))
            painter.setBrush(QColor('#f8fbf3'))
            painter.drawRoundedRect(rect, 4, 4)
            text(painter, rect, '+', SELECTED_EVENT_BORDER, 13, True, Qt.AlignmentFlag.AlignCenter)

    def time_feedback(self):
        if self.placement_active:
            return None
        if self.drag:
            if self.drag[0] != 'create':
                return None
            if self.ghost:
                return (*self.ghost[:2], True)
        return (*self.hover_time, False) if self.hover_time is not None else None

    def paint_time_feedback(self, painter):
        feedback = self.time_feedback()
        if feedback is None:
            return
        day, minute, creating = feedback
        if not 0 <= day < len(self.days):
            return
        width = (self.width() - self.gutter) / len(self.days)
        x = self.gutter + day * width
        y = self.time_top + (minute / 60 - self.start_hour) * self.hour_height
        color = QColor(GREEN)
        color.setAlpha(210 if creating else 125)
        painter.setPen(QPen(color, 1.5 if creating else 1))
        painter.drawLine(QPointF(x + 3, y), QPointF(x + width - 3, y))
        label = self.preferences.format_time(time(int(minute // 60) % 24, int(minute % 60)))
        scale = display_scale(self)
        label_width = min(width - 12, max(46, len(label) * 7 + 10) * scale)
        rect = QRectF(x + 6, min(max(self.time_top + 2, y - 22 * scale), self.height() - 23 * scale), label_width, 20 * scale)
        painter.setPen(QPen(GREEN if creating else LINE, 1))
        painter.setBrush(QColor('#e7efdf') if creating else BG)
        painter.drawRoundedRect(rect, 3, 3)
        text(painter, rect, label, GREEN, 8, creating, Qt.AlignmentFlag.AlignCenter)

    def _refit(self):
        if self._viewport_height is not None and not self.drag:
            self.fit_to_viewport(self._viewport_height, self.full_day)

    def fit_to_viewport(self, height, full_day=False):
        self._viewport_height = max(1, int(height))
        self.full_day = bool(full_day)
        if self.drag:
            return
        first, last = (0, 24) if full_day else (self.preferences.visible_start_hour, self.preferences.visible_end_hour)
        if not full_day:
            for day in self.days:
                begin, finish = at(day), at(day + timedelta(days=1))
                for event in [*self.events, *self.preview_events]:
                    if event.all_day or not overlaps(event, begin, finish):
                        continue
                    start = max(event.start.astimezone(local_zone()), begin)
                    end = min(event.end.astimezone(local_zone()), finish)
                    start_min = (start - begin).total_seconds() / 60
                    end_min = (end - begin).total_seconds() / 60
                    if event.start.timestamp() == event.end.timestamp():
                        end_min = min(1440, start_min + 15)
                    first = min(first, max(0, math.floor(start_min / 60)))
                    last = max(last, min(24, math.ceil(end_min / 60)))
        self.start_hour, self.end_hour = first, last
        hours = max(1, last - first)
        self.hour_height = max(24 * display_scale(self), (self._viewport_height - self.time_top - 20) / hours)
        self.setMinimumHeight(math.ceil(self.time_top + hours * self.hour_height + 20))
        self.update()

    def set_preferences(self, preferences):
        self.preferences = preferences
        scale = display_scale(self)
        self.gutter = math.ceil(62 * scale)
        self.all_day_height = math.ceil(44 * scale)
        self._refit()
        self.update()

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    def set_scope_modifiers(self, settings):
        self.scope_modifiers = normalize_scope_modifiers(settings)
        self.update()

    def set_navigation(self, active, day=None, digits=''):
        self.show_numbers = bool(active)
        self.navigation_day = day if active else None
        self.navigation_digits = str(digits or '') if active else ''
        self.update()

    def navigation_target_id(self):
        if not self.show_numbers or self.navigation_day is None or not 0 <= self.navigation_day < len(self.days):
            return None
        if not self.navigation_digits.isascii() or not self.navigation_digits.isdigit():
            return None
        day = self.days[self.navigation_day]
        start, end = at(day), at(day + timedelta(days=1))
        options = [event for event in self.events if overlaps(event, start, end)]
        index = int(self.navigation_digits) - 1
        return options[index].id if 0 <= index < len(options) else None

    def rects(self, events=None):
        if not self.days:
            return []
        width = (self.width() - self.gutter) / len(self.days)
        result = []
        for index, day in enumerate(self.days):
            start, end = at(day), at(day + timedelta(days=1))
            daily = [e for e in (self.events if events is None else events)
                     if (not self.fixed_all_day or not e.all_day) and overlaps(e, start, end)]
            timed = []
            for event in daily:
                if not event.all_day:
                    timed.append(replace(event, start=max(event.start, start), end=min(event.end, end)))
            placements = {v.event_id: v for v in layout_events(timed)}
            day_numbers = {e.id: n + 1 for n, e in enumerate(daily)}
            allday = [e for e in daily if e.all_day]
            for event in daily:
                x = self.gutter + index * width
                if event.all_day:
                    slot = allday.index(event)
                    n = max(1, len(allday))
                    rect = QRectF(x + 3 + slot * width / n, 4, width / n - 6, 33 * display_scale(self))
                else:
                    placement = placements[event.id]
                    local_start = max(event.start.astimezone(local_zone()), start)
                    local_end = min(event.end.astimezone(local_zone()), end)
                    top = ((local_start - start).total_seconds() / 3600 - self.start_hour) * self.hour_height
                    bottom = ((local_end - start).total_seconds() / 3600 - self.start_hour) * self.hour_height
                    rect = QRectF(x + placement.column * width / placement.columns + 3,
                                  self.time_top + top + 1,
                                  width / placement.columns - 6,
                                  max((18 if event.start.timestamp() == event.end.timestamp() else 14) * display_scale(self), bottom - top - 2))
                result.append((rect, event, day_numbers[event.id]))
        return result

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        if not self.days:
            return
        w = (self.width() - self.gutter) / len(self.days)
        scale = display_scale(self)
        for i, day in enumerate(self.days):
            if day.weekday() >= 5:
                p.fillRect(QRectF(self.gutter + i * w, 0, w, self.height()), QColor('#f4f6f0'))
            if self.show_numbers and i == self.navigation_day:
                p.fillRect(QRectF(self.gutter + i * w, 0, w, self.height()), QColor('#eef4e7'))
        p.setPen(QPen(LINE, 1))
        for i in range(len(self.days) + 1):
            x = self.gutter + i * w
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        if not self.fixed_all_day:
            text(p, QRectF(4, 4, 56 * scale, 28 * scale), 'ALL DAY', MUTED, 7)
            p.setPen(QPen(LINE, 1))
            p.drawLine(0, self.time_top, self.width(), self.time_top)
        for hour in range(self.start_hour, self.end_hour + 1):
            y = self.time_top + (hour - self.start_hour) * self.hour_height
            p.setPen(QPen(LINE, 1))
            p.drawLine(QPointF(self.gutter, y), QPointF(self.width(), y))
            if hour and hour < 24:
                label = self.preferences.format_time(time(hour))
                text(p, QRectF(2, max(0, y - 11 * scale), 54 * scale, 22 * scale), label, MUTED, 7 if len(label) > 5 else 8,
                     align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if hour < self.end_hour:
                p.setPen(QPen(QColor('#edf0e9'), 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(self.gutter, y + self.hour_height / 2), QPointF(self.width(), y + self.hour_height / 2))
        self.hits = self.rects()
        for rect, event, number in self.hits:
            self.paint_event(p, rect, event, number)
        for rect, event, number in self.rects(self.preview_events):
            self.paint_event(p, rect, event, number, preview=True)
        current = now()
        if current.date() in self.days and self.start_hour <= current.hour < self.end_hour:
            i = self.days.index(current.date())
            y = self.time_top + (current.hour + current.minute / 60 - self.start_hour) * self.hour_height
            p.setPen(QPen(QColor('#c16f55'), 1.5))
            p.drawLine(QPointF(self.gutter + i * w, y), QPointF(self.gutter + (i + 1) * w, y))
            p.setBrush(QColor('#c16f55'))
            p.drawEllipse(QPointF(self.gutter + i * w, y), 3, 3)
        if self.ghost and not self.preview_events:
            day, start_min, end_min = self.ghost
            if 0 <= day < len(self.days):
                rect = QRectF(self.gutter + day * w + 3, self.time_top + (start_min / 60 - self.start_hour) * self.hour_height,
                              w - 6, max(12, (end_min - start_min) / 60 * self.hour_height))
                p.setPen(QPen(GREEN, 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(80, 130, 90, 45))
                p.drawRoundedRect(rect, 5, 5)
                if self.drag and self.drag[0] != 'create':
                    scope = self.scope
                    label = {'single': 'One event', 'group': 'All events', 'future': 'This and following'}[scope]
                    text(p, rect.adjusted(5, 0, -5, 0), label, GREEN, 8, True)
        self.paint_time_feedback(p)
        self.paint_copy_badge(p)
        p.end()

    def paint_event(self, p, rect, event, number, preview=False):
        color = QColor(self.calendars[event.calendar_id].color) if event.calendar_id in self.calendars else GREEN
        day_width = (self.width() - self.gutter) / max(1, len(self.days))
        day_index = int((rect.center().x() - self.gutter) / day_width)
        numbered = not preview and self.show_numbers and self.navigation_day == day_index
        target = numbered and self.navigation_digits.isascii() and self.navigation_digits.isdigit() and int(self.navigation_digits) == number
        selected = not preview and (event.id in self.selected_ids or event.id == self.selected_id)
        fill = QColor(color)
        fill.setAlpha(34 if preview else 70 if selected else 95 if target else 38 if event.candidate else 52)
        p.setBrush(fill)
        p.setPen(QPen(GREEN if target else SELECTED_EVENT_BORDER if selected else color, 3 if target or selected else 2 if preview else 1,
                      Qt.PenStyle.DashLine if preview or event.candidate else Qt.PenStyle.SolidLine))
        p.drawRoundedRect(rect, 5, 5)
        if selected:
            p.fillRect(QRectF(rect.left() + 3, rect.top() + 5, 3, max(3, rect.height() - 10)), SELECTED_EVENT_BORDER)
        p.save()
        p.setClipRect(rect.adjusted(4, 1, -3, -1))
        point_event = event.start.timestamp() == event.end.timestamp()
        scale = display_scale(self)
        title = (self.preferences.format_time(event.start.astimezone(local_zone())) + ' · ' if point_event else '') + event.title
        text(p, QRectF(rect.x() + 8, rect.y() + 1, rect.width() - (40 if numbered else 12), min(21 * scale, rect.height() - 2)), title, SELECTED_EVENT_BORDER if selected else color.darker(145), 9, True)
        if rect.height() > 39 * scale:
            value = self.preferences.format_time(event.start.astimezone(local_zone())) + '–' + self.preferences.format_time(event.end.astimezone(local_zone()))
            if event.candidate:
                value += ' · option'
            text(p, QRectF(rect.x() + 8, rect.y() + 23 * scale, rect.width() - 12, 18 * scale), value, SELECTED_EVENT_BORDER if selected else color.darker(120), 8)
        if rect.height() > 64 * scale and event.location:
            text(p, QRectF(rect.x() + 8, rect.y() + 43 * scale, rect.width() - 12, 18 * scale), event.location, color.darker(120), 8)
        p.restore()
        if numbered:
            size = min(22, max(12, rect.height() - 4))
            badge(p, QRectF(rect.right() - 26, rect.top() + 3, 22, size), str(number), target)

    def hit(self, pos):
        return next(((r, e) for r, e, _ in reversed(self.hits) if r.contains(pos)), None)

    def edit_hit(self, point, modifiers):
        hit = self.hit(point)
        if hit is None:
            return False
        item = hit[1]
        self.selected_day = self.day_at(point)
        if item.id not in self.selected_ids and item.id != self.selected_id:
            self.emit_selection(item, Qt.KeyboardModifier.NoModifier)
        self.edit_requested.emit(item.id)
        self.edit_scoped_requested.emit(item.id, modifiers)
        self.update()
        return True

    def day_at(self, point):
        if not self.days or point.x() < self.gutter or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        return self.days[self.position(point)[0]]

    def select_day_at(self, point):
        day = self.day_at(point)
        if day is not None:
            self.set_selected_day(day)
            self.day_selected.emit(day)
        return day

    def placement_target(self, point):
        day = self.day_at(point)
        if day is None:
            return None
        if not self.fixed_all_day and point.y() < self.time_top:
            return at(day)
        return at(day) + timedelta(minutes=self.position(point)[1])

    def emit_selection(self, item, modifiers):
        self.selected_id = item.id
        self.selected_scoped.emit(item.id, modifiers)
        self.selected.emit(item.id)

    def position(self, point, endpoint=False):
        width = (self.width() - self.gutter) / max(1, len(self.days))
        day = min(len(self.days) - 1, max(0, int((point.x() - self.gutter) / width)))
        minutes = min(self.end_hour * 60 if endpoint else self.end_hour * 60 - 15,
                      max(self.start_hour * 60, self.start_hour * 60 + round((point.y() - self.time_top) / self.hour_height * 4) * 15))
        return day, minutes

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.days:
            return
        point = event.position()
        self.hover_time = None
        self._drag_pointer = point
        day = self.day_at(point)
        if day is None:
            return
        self.setFocus()
        if self.placement_active:
            self.drag = self.ghost = self._deferred_selection = None
            self._placement_consumed = True
            self.placement_confirmed.emit(self.placement_target(point))
            return
        self._placement_consumed = False
        self.selected_day = day
        self.gesture_modifiers = event.modifiers()
        hit = self.hit(point)
        self._deferred_selection = None
        if hit:
            rect, item = hit
            self.gesture_started.emit(item.id)
            selected = item.id in self.selected_ids or item.id == self.selected_id
            if selected and (event.modifiers() & Qt.KeyboardModifier.ControlModifier or len(self.selected_ids) > 1):
                self._deferred_selection = (item, event.modifiers())
            else:
                self.emit_selection(item, event.modifiers())
            if not item.all_day:
                resize = point.y() >= rect.bottom() - 7 and not event.modifiers() & Qt.KeyboardModifier.ControlModifier
                self.drag = ('resize' if resize else 'move', item, point, self.position(point))
        else:
            self.day_selected.emit(day)
            if point.y() >= self.time_top:
                self.hover_time = self.position(point)
                self.drag = ('create', None, point, self.hover_time)
        self.update()

    def mouseMoveEvent(self, event):
        point = event.position()
        self._drag_pointer = point
        if self.placement_active:
            self.hover_time = None
            target = self.placement_target(point)
            if target is not None:
                self.placement_moved.emit(target)
            return
        self.gesture_modifiers = event.modifiers()
        if not self.drag:
            hit = self.hit(point)
            local = hit[1].start.astimezone(local_zone()) if hit else None
            day = self.day_at(point)
            hover = self.position(point) if not hit and day is not None and point.y() >= self.time_top else None
            if hover != self.hover_time:
                self.hover_time = hover
                self.update()
            if hit:
                self.setToolTip(hit[1].title + '\n' + self.preferences.format_date(local, weekday=True) + ' · '
                                + ('All day' if hit[1].all_day else self.preferences.format_time(local)) + '\n'
                                + hit[1].location + '\nDouble-click to edit.')
            elif day is not None:
                self.setToolTip('Double-click to add an all-day event.' if point.y() < self.time_top else
                                'Click to select this day. Double-click to add an event, or drag to choose its duration.')
            else:
                self.setToolTip('')
            self.setCursor(Qt.CursorShape.SizeVerCursor if hit and point.y() > hit[0].bottom() - 7 and not hit[1].all_day else Qt.CursorShape.ArrowCursor)
            return
        if (point - self.drag[2]).manhattanLength() < 5:
            if self.drag[0] == 'create' and self.ghost is None:
                day = self.day_at(point)
                self.hover_time = self.position(point) if day is not None and point.y() >= self.time_top and not self.hit(point) else None
                self.update()
            return
        kind, item, origin, (old_day, old_min) = self.drag
        day, minute = self.position(point, endpoint=True)
        if kind == 'create':
            begin = min(minute, old_min)
            self.ghost = (old_day, begin, max(begin + 15, minute, old_min))
        elif kind == 'resize':
            local = item.start.astimezone(local_zone())
            start_min = local.hour * 60 + local.minute
            self.ghost = (old_day, start_min, max(start_min + 15, minute))
        else:
            local = item.start.astimezone(local_zone())
            duration = (item.end.astimezone(timezone.utc) - item.start.astimezone(timezone.utc)).total_seconds() / 60
            original_min = local.hour * 60 + local.minute + local.second / 60 + local.microsecond / 60_000_000
            start_min = min(max(0, original_min + minute - old_min), max(0, 1440 - duration))
            self.ghost = (day, start_min, start_min + duration)
        if kind != 'create' and self.ghost:
            target_day, begin, finish = self.ghost
            start = item.start if kind == 'resize' else at(self.days[target_day]) + timedelta(minutes=begin)
            end = at(self.days[target_day]) + timedelta(minutes=finish)
            self.drag_preview_requested.emit(item.id, start, end, event.modifiers())
        self.update()

    def leaveEvent(self, event):
        self.hover_time = None
        self.setToolTip('')
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        drag, ghost = self.drag, self.ghost
        self.drag = self.ghost = None
        self._drag_pointer = None
        self.hover_time = None
        deferred, self._deferred_selection = self._deferred_selection, None
        if drag:
            kind, item, _, (day, minute) = drag
            if ghost:
                day, minute, finish = ghost
                start = at(self.days[day]) + timedelta(minutes=minute)
                end = at(self.days[day]) + timedelta(minutes=finish)
                if kind == 'create':
                    self.create_requested.emit(start, end)
                elif kind == 'resize':
                    self.move_requested.emit(item.id, item.start, end)
                    self.move_scoped_requested.emit(item, item.start, end, event.modifiers())
                else:
                    self.move_requested.emit(item.id, start, end)
                    self.move_scoped_requested.emit(item, start, end, event.modifiers())
        if deferred and not ghost:
            self.emit_selection(*deferred)
        if drag and drag[0] == 'create' and not ghost and not self.placement_active and not self._placement_consumed:
            point = event.position()
            if self.day_at(point) is not None and point.y() >= self.time_top and not self.hit(point):
                self.hover_time = self.position(point)
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.drag = self.ghost = None
        self._deferred_selection = None
        self._drag_pointer = None
        self.hover_time = None
        point = event.position()
        if self._placement_consumed:
            self._placement_consumed = False
            return
        if self.placement_active:
            target = self.placement_target(point)
            if target is not None:
                self.placement_confirmed.emit(target)
            return
        day = self.day_at(point)
        if day is None:
            return
        self.setFocus()
        if self.edit_hit(point, event.modifiers()):
            return
        self.selected_day = day
        self.day_selected.emit(day)
        if point.y() < self.time_top:
            self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))
        else:
            _, minute = self.position(point)
            start = at(day) + timedelta(minutes=minute)
            self.create_requested.emit(start, start + timedelta(hours=1))
        self.update()

    def contextMenuEvent(self, event):
        if self.placement_active:
            return
        day = self.day_at(event.pos())
        if day is not None:
            self.selected_day = day
            self.setFocus()
        hit = self.hit(event.pos())
        if hit:
            if hit[1].id not in self.selected_ids and hit[1].id != self.selected_id:
                self.emit_selection(hit[1], Qt.KeyboardModifier.NoModifier)
            self.update()
            self.context_requested.emit(hit[1].id, event.globalPos())
        elif day is not None:
            self.day_selected.emit(day)


class MonthCanvas(QWidget):
    create_requested = Signal(object, object)
    create_all_day_requested = Signal(object, object)
    day_selected = Signal(object)
    open_day = Signal(object)
    edit_requested = Signal(str)
    edit_scoped_requested = Signal(str, object)
    selected = Signal(str)
    selected_scoped = Signal(str, object)
    gesture_started = Signal(str)
    drag_preview_requested = Signal(str, object, object, object)
    move_requested = Signal(str, object, object)
    move_scoped_requested = Signal(object, object, object, object)
    placement_moved = Signal(object)
    placement_confirmed = Signal(object)
    context_requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.start = now().date()
        self.month = self.start.month
        self.events, self.calendars, self.hits = [], {}, []
        self.selected_id = None
        self.selected_ids = set()
        self.selected_day = None
        self.scope = 'single'
        self.preview_events = []
        self.placement_active = False
        self._placement_consumed = False
        self._deferred_selection = None
        self.drag = self.ghost = None
        self.gesture_modifiers = Qt.KeyboardModifier.NoModifier
        self._drag_pointer = None
        self.preferences = Preferences()
        self.setMinimumHeight(440)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.setMinimumHeight(math.ceil(440 * display_scale(self)))
        self.update()

    @property
    def header_height(self):
        return math.ceil(28 * display_scale(self))

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    set_selection = CalendarCanvas.set_selection
    set_scope = CalendarCanvas.set_scope
    emit_selection = CalendarCanvas.emit_selection
    edit_hit = CalendarCanvas.edit_hit
    set_drag_modifiers = CalendarCanvas.set_drag_modifiers
    copy_badge_rect = CalendarCanvas.copy_badge_rect
    paint_copy_badge = CalendarCanvas.paint_copy_badge

    def set_preview(self, events):
        self.preview_events = list(events)
        self.update()

    def set_data(self, start, month, events, calendars):
        self.start, self.month, self.events = start, month, events
        self.calendars = {c.id: c for c in calendars}
        self.update()

    def _paint_month_event(self, painter, rect, item, *, preview=False, before=False, after=False):
        color = QColor(self.calendars[item.calendar_id].color) if item.calendar_id in self.calendars else GREEN
        selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
        fill = QColor(color)
        fill.setAlpha(45 if preview else 58 if selected else 40)
        painter.setPen(QPen(SELECTED_EVENT_BORDER if selected else color, 3 if selected else 2 if preview else 1,
                            Qt.PenStyle.DashLine if preview or item.candidate else Qt.PenStyle.SolidLine)
                       if selected or preview or item.all_day else Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3, 3)
        if selected:
            painter.fillRect(QRectF(rect.left() + 3, rect.top() + 4, 3, rect.height() - 8), SELECTED_EVENT_BORDER)
        ink = SELECTED_EVENT_BORDER if selected else color.darker(140)
        if before:
            text(painter, QRectF(rect.left() + 7, rect.top(), 12, rect.height()), '‹', ink, 10, True)
        if after:
            text(painter, QRectF(rect.right() - 18, rect.top(), 12, rect.height()), '›', ink, 10, True)
        label = ('' if item.all_day else self.preferences.format_time(item.start.astimezone(local_zone())) + ' ') + item.title
        label_rect = rect.adjusted(21 if before else 8 if selected else 5, 0, -21 if after else -3, 0)
        painter.save()
        painter.setClipRect(label_rect)
        text(painter, label_rect, label, ink, 8, selected or preview)
        painter.restore()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        self.hits = []
        scale = display_scale(self)
        header, heading, lane, event_height = self.header_height, 32 * scale, 24 * scale, 21 * scale
        w, h = self.width() / 7, (self.height() - header) / 6
        capacity = max(0, int((h - 37 * scale) / lane))
        for index in range(7):
            name = self.preferences.weekday_name(self.start + timedelta(days=index)).upper()
            text(p, QRectF(index * w + 12, 0, w - 15, 25 * scale), name, MUTED, 8, True)
        # Finish every day background before drawing any bar across its neighbors.
        for index in range(42):
            day = self.start + timedelta(days=index)
            x, y = (index % 7) * w, header + (index // 7) * h
            rect = QRectF(x, y, w, h)
            if day.month != self.month:
                p.fillRect(rect, QColor('#f0f3ec'))
            if day == now().date():
                p.fillRect(rect, QColor('#edf2e7'))
            p.setPen(QPen(LINE, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
            text(p, QRectF(x + 10, y + 3 * scale, w - 18, 25 * scale), str(day.day),
                 GREEN if day == now().date() else INK, 10, True)
        previews = self.preview_events or ([self.ghost] if self.ghost is not None else [])
        for week in range(6):
            first = self.start + timedelta(days=week * 7)
            y = header + week * h
            timed = [[item for item in self.events if not item.all_day
                      and overlaps(item, at(first + timedelta(days=index)), at(first + timedelta(days=index + 1)))]
                     for index in range(7)]
            reserve = {index for index, daily in enumerate(timed)
                       if any(item.id in self.selected_ids or item.id == self.selected_id for item in daily)}
            spans = week_spans(self.events, first, capacity=capacity, selected=self.selected_ids,
                               primary=self.selected_id, reserve_timed=reserve)
            used, shown = [set() for _ in range(7)], [0] * 7
            for item, lane_index, start, end, _number, before, after in spans:
                rect = QRectF(start * w + 5, y + heading + lane_index * lane, (end - start) * w - 10, event_height)
                self._paint_month_event(p, rect, item, before=before, after=after)
                self.hits.append((rect, item))
                for index in range(start, end):
                    used[index].add(lane_index)
                    shown[index] += 1
            for index in range(7):
                day = first + timedelta(days=index)
                x = index * w
                free = [lane for lane in range(capacity) if lane not in used[index]]
                all_day_count = sum(item.all_day and item.start.date() <= day < item.end.date() for item in self.events)
                daily = timed[index]
                hidden_all_day = all_day_count - shown[index]
                slots = len(free)
                count = slots if len(daily) + hidden_all_day <= slots else max(0, slots - 1)
                visible = daily[:count]
                selected = [item for item in daily if item.id in self.selected_ids or item.id == self.selected_id]
                for item in selected:
                    if item in visible or not slots:
                        continue
                    replace_at = next((n for n in reversed(range(len(visible))) if visible[n] not in selected), None)
                    if replace_at is not None:
                        visible[replace_at] = item
                    elif len(visible) < slots:
                        visible.append(item)
                for lane_index, item in zip(free, visible):
                    rect = QRectF(x + 5, y + heading + lane_index * lane, w - 10, event_height)
                    self._paint_month_event(p, rect, item)
                    self.hits.append((rect, item))
                hidden = hidden_all_day + len(daily) - len(visible)
                header_count = hidden and len(visible) >= slots
                if hidden and not header_count:
                    lane_index = free[len(visible)]
                    text(p, QRectF(x + 10, y + heading + lane_index * lane, w - 15, event_height), f'+{hidden} more', MUTED, 8)
                if header_count:
                    text(p, QRectF(x + 35, y + 5 * scale, max(0, w - 57), 20 * scale), f'+{hidden}', MUTED, 7)
                text(p, QRectF(x + 32, y + 5 * scale, w - 40, 20 * scale), '+ all day' if w >= 90 and not header_count else '+', MUTED, 7,
                     align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            preview_spans = week_spans(previews, first, capacity=capacity)
            preview_used = [set() for _ in range(7)]
            for item, lane_index, start, end, _number, before, after in preview_spans:
                rect = QRectF(start * w + 5, y + heading + lane_index * lane, (end - start) * w - 10, event_height)
                self._paint_month_event(p, rect, item, preview=True, before=before, after=after)
                for index in range(start, end):
                    preview_used[index].add(lane_index)
            for index in range(7):
                day = first + timedelta(days=index)
                daily = [item for item in previews if not item.all_day and overlaps(item, at(day), at(day + timedelta(days=1)))]
                free = [lane for lane in range(capacity) if lane not in preview_used[index]]
                for lane_index, item in zip(free, daily):
                    rect = QRectF(index * w + 5, y + heading + lane_index * lane, w - 10, event_height)
                    self._paint_month_event(p, rect, item, preview=True)
        self.paint_copy_badge(p)
        p.end()

    def hit(self, point):
        return next(((rect, item) for rect, item in reversed(self.hits) if rect.contains(point)), None)

    def day_at(self, point):
        if point.y() < self.header_height or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        index = min(5, max(0, int((point.y() - self.header_height) / ((self.height() - self.header_height) / 6)))) * 7 + min(6, max(0, int(point.x() / (self.width() / 7))))
        return self.start + timedelta(days=index)

    def select_day_at(self, point):
        day = self.day_at(point)
        if day is not None:
            self.set_selected_day(day)
            self.day_selected.emit(day)
        return day

    def placement_target(self, point):
        day = self.day_at(point)
        return at(day, 9) if day is not None else None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.day_at(event.position())
        if day is None:
            return
        self.setFocus()
        self.gesture_modifiers = event.modifiers()
        self._drag_pointer = event.position()
        if self.placement_active:
            self.drag = self.ghost = self._deferred_selection = None
            self._placement_consumed = True
            self.placement_confirmed.emit(self.placement_target(event.position()))
            return
        self._placement_consumed = False
        self.selected_day = day
        self._deferred_selection = None
        hit = self.hit(event.position())
        if hit:
            item = hit[1]
            self.gesture_started.emit(item.id)
            selected = item.id in self.selected_ids or item.id == self.selected_id
            if selected and (event.modifiers() & Qt.KeyboardModifier.ControlModifier or len(self.selected_ids) > 1):
                self._deferred_selection = (item, event.modifiers())
            else:
                self.emit_selection(item, event.modifiers())
            self.drag = (item, event.position(), day)
            self.update()
        else:
            self.day_selected.emit(day)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position()
        self.drag = self.ghost = None
        self._deferred_selection = None
        if self._placement_consumed:
            self._placement_consumed = False
            return
        if self.placement_active:
            day = self.day_at(point)
            if day is not None:
                self.placement_confirmed.emit(self.placement_target(point))
            return
        day = self.day_at(point)
        if day is None:
            return
        self.setFocus()
        if self.edit_hit(point, event.modifiers()):
            return
        self.selected_day = day
        self.day_selected.emit(day)
        height = (self.height() - self.header_height) / 6
        if (point.y() - self.header_height) % height < 30 * display_scale(self):
            self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))
        else:
            start = at(day, 9)
            self.create_requested.emit(start, start + timedelta(hours=1))

    def mouseMoveEvent(self, event):
        point = event.position()
        self._drag_pointer = point
        self.gesture_modifiers = event.modifiers()
        day = self.day_at(point)
        if day is None:
            self.setToolTip('')
            return
        if self.placement_active:
            self.placement_moved.emit(self.placement_target(point))
            return
        if self.drag and (point - self.drag[1]).manhattanLength() >= 5:
            item, _, origin = self.drag
            shift = timedelta(days=(day - origin).days)
            start = item.start if item.all_day else item.start.astimezone(local_zone())
            end = item.end if item.all_day else item.end.astimezone(local_zone())
            self.ghost = replace(item, start=start + shift, end=end + shift)
            self.drag_preview_requested.emit(item.id, self.ghost.start, self.ghost.end, event.modifiers())
            self.update()
            return
        hit = self.hit(point)
        if hit:
            self.setToolTip(hit[1].title + '\nDouble-click to edit.')
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        height = (self.height() - self.header_height) / 6
        all_day = (point.y() - self.header_height) % height < 30 * display_scale(self)
        self.setToolTip(self.preferences.format_date(day, weekday=True) + '\n'
                        + ('Double-click the day heading to create an all-day event.' if all_day else
                           'Click to select this day. Double-click to create a new event at ' + self.preferences.format_time(time(9)) + '.'))
        self.setCursor(Qt.CursorShape.PointingHandCursor if all_day else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        drag, ghost = self.drag, self.ghost
        deferred = self._deferred_selection
        self.drag = self.ghost = self._deferred_selection = None
        self._drag_pointer = None
        if drag and ghost:
            self.move_requested.emit(drag[0].id, ghost.start, ghost.end)
            self.move_scoped_requested.emit(drag[0], ghost.start, ghost.end, event.modifiers())
        elif deferred:
            self.emit_selection(*deferred)
        self.update()

    def contextMenuEvent(self, event):
        if self.placement_active:
            return
        day = self.day_at(event.pos())
        if day is not None:
            self.selected_day = day
            self.setFocus()
        hit = next(((r, e) for r, e in self.hits if r.contains(event.pos())), None)
        if hit:
            if hit[1].id not in self.selected_ids and hit[1].id != self.selected_id:
                self.emit_selection(hit[1], Qt.KeyboardModifier.NoModifier)
            self.update()
            self.context_requested.emit(hit[1].id, event.globalPos())
        elif day is not None:
            self.day_selected.emit(day)


class AllDayCanvas(MonthCanvas):
    """Fixed week lanes, with one connected bar per all-day occurrence."""
    gutter = 62
    lane_height = 27

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days = []
        self.setMinimumHeight(0)
        self.setFixedHeight(35)

    def _lanes(self, events):
        if not self.days:
            return []
        return week_spans(events, self.days[0], len(self.days))

    def set_preferences(self, preferences):
        self.preferences = preferences
        scale = display_scale(self)
        self.gutter = math.ceil(62 * scale)
        self.lane_height = math.ceil(27 * scale)
        self._resize_lanes()
        self.update()

    def _resize_lanes(self):
        lanes = self._lanes(self.events)
        count = max((entry[1] + 1 for entry in lanes), default=1)
        self.setFixedHeight(count * self.lane_height + math.ceil(8 * display_scale(self)))

    def set_data(self, days, events, calendars):
        self.days, self.events = list(days), list(events)
        self.calendars = {calendar.id: calendar for calendar in calendars}
        self._resize_lanes()
        self.update()

    def day_at(self, point):
        if not self.days or point.x() < self.gutter or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        width = (self.width() - self.gutter) / len(self.days)
        if width <= 0:
            return None
        index = min(len(self.days) - 1, max(0, int((point.x() - self.gutter) / width)))
        return self.days[index]

    def placement_target(self, point):
        day = self.day_at(point)
        return at(day) if day is not None else None

    def rects(self, events=None):
        width = (self.width() - self.gutter) / max(1, len(self.days))
        pad = 4 * display_scale(self)
        return [(QRectF(self.gutter + start * width + 3, pad + lane * self.lane_height,
                        (end - start) * width - 6, self.lane_height - pad), item, number)
                for item, lane, start, end, number, _left, _right in self._lanes(self.events if events is None else events)]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        if not self.days:
            p.end()
            return
        width = (self.width() - self.gutter) / len(self.days)
        p.setPen(QPen(LINE, 1))
        for index in range(len(self.days) + 1):
            x = self.gutter + index * width
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        scale = display_scale(self)
        text(p, QRectF(3, 3, 55 * scale, 24 * scale), 'ALL DAY', MUTED, 7)
        self.hits = []
        for rect, item, number in self.rects():
            self._paint_bar(p, rect, item)
            self.hits.append((rect, item))
        previews = self.preview_events or ([self.ghost] if self.ghost is not None else [])
        pad = 4 * scale
        slots = max(1, (self.height() - 2 * pad) // self.lane_height)
        for rect, item, _number in self.rects(previews):
            # Preview lanes never resize the fixed surface under the pointer.
            rect.moveTop(pad + (int((rect.top() - pad) / self.lane_height) % slots) * self.lane_height)
            self._paint_bar(p, rect, item, preview=True)
        self.paint_copy_badge(p)
        p.end()

    def _paint_bar(self, painter, rect, item, preview=False):
        color = QColor(self.calendars[item.calendar_id].color) if item.calendar_id in self.calendars else GREEN
        selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
        fill = QColor(color)
        fill.setAlpha(36 if preview else 70 if selected else 45)
        painter.setPen(QPen(SELECTED_EVENT_BORDER if selected else color, 3 if selected else 2 if preview else 1,
                            Qt.PenStyle.DashLine if preview or item.candidate else Qt.PenStyle.SolidLine))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 4, 4)
        if selected:
            painter.fillRect(QRectF(rect.left() + 3, rect.top() + 4, 3, rect.height() - 8), SELECTED_EVENT_BORDER)
        first, last = self.days[0], self.days[-1] + timedelta(days=1)
        before, after = item.start.date() < first, item.end.date() > last
        ink = SELECTED_EVENT_BORDER if selected else color.darker(145)
        if before:
            text(painter, QRectF(rect.left() + 7, rect.top(), 12, rect.height()), '‹', ink, 10, True)
        if after:
            text(painter, QRectF(rect.right() - 18, rect.top(), 12, rect.height()), '›', ink, 10, True)
        label = rect.adjusted(21 if before else 8, 0, -21 if after else -6, 0)
        painter.save()
        painter.setClipRect(label)
        text(painter, label, item.title, ink, 8, selected)
        painter.restore()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.drag = self.ghost = self._deferred_selection = None
        if self._placement_consumed:
            self._placement_consumed = False
            return
        day = self.day_at(event.position())
        if day is None:
            return
        if self.placement_active:
            self.placement_confirmed.emit(at(day))
            return
        self.setFocus()
        if self.edit_hit(event.position(), event.modifiers()):
            return
        self.selected_day = day
        self.day_selected.emit(day)
        self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))

    def mouseMoveEvent(self, event):
        if self.drag or self.placement_active:
            super().mouseMoveEvent(event)
        else:
            hit = self.hit(event.position())
            self.setToolTip((hit[1].title + '\nDrag to move; Ctrl-drag to copy. Double-click to edit.') if hit else
                            'Double-click to add an all-day event.')
