"""Paint-only calendar surfaces: visible events are cheap; gestures commit on release."""
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from bisect import bisect_left, bisect_right
from time import monotonic
from html import escape
from functools import lru_cache
from types import SimpleNamespace
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QFontMetricsF
from PySide6.QtWidgets import QWidget, QMenu, QApplication

from .layout import layout_events
from .recurrence import overlaps
from .preferences import Preferences
from .timeutil import local_zone, now
from .event_scope import normalize_scope_modifiers, scope_from_modifiers
from .theme import scheme_for
from .event_style import event_colors, source_outline
from .calendar_appearance import weekend_text, weekend_fill

# Default aliases remain available to standalone painter callers. Live surfaces
# resolve their own preferences through paint_colors rather than mutating these.
BG = QColor('#ffffff')
LINE = QColor('#e0e5e8')
INK = QColor('#27313a')
MUTED = QColor('#596570')
GREEN = QColor('#475f52')
CONFIRM_TOOLTIP = 'Confirm this slot and remove the other choices. Undo restores them.'
WEEKDAY_SIZE = 9
AXIS_SIZE = 8
ALL_DAY_SIZE = 7
WEEK_CAPTION_SIZE = WEEKDAY_SIZE
DAY_NUMBER_SIZE = 17
WEEK_NUMBER_SIZE = DAY_NUMBER_SIZE


@lru_cache(maxsize=64)
def calendar_gutter(preferences):
    """One compact axis for every day column, including longer 12-hour times."""
    scale = preferences.font_scale / 100
    font = QFont('Sans Serif')
    font.setPointSizeF(AXIS_SIZE * scale)
    metrics = QFontMetricsF(font)
    width = max(metrics.horizontalAdvance(preferences.format_time(time(hour))) for hour in range(24))
    return math.ceil(max(44 * scale, width + 12 * scale))


def axis_label_rect(gutter, top, height, scale):
    return QRectF(4 * scale, top, gutter - 10 * scale, height)


def weekday_label_rect(left, width, scale):
    return QRectF(left + 11, 8 * scale, width - 20, 20 * scale)


def day_number_rect(left, width, scale):
    return QRectF(left + 11, 28 * scale, width - 20, math.ceil(28 * scale))


def paint_week_caption(painter, gutter, scale, color):
    """Keep the shared week-axis caption fixed while switching views."""
    text(painter, QRectF(0, 8 * scale, gutter, 20 * scale), 'WEEK', color, WEEK_CAPTION_SIZE, True,
         align=Qt.AlignmentFlag.AlignCenter)


@lru_cache(maxsize=128)
def _scheme_colors(scheme):
    return SimpleNamespace(**{key: QColor(getattr(scheme, key)) for key in (
        'background', 'panel', 'ink', 'muted', 'accent', 'weekend', 'line',
        'control_surface', 'selection', 'selection_edge', 'now', 'outside_month', 'weekend_background', 'on_accent')})


def paint_colors(widget):
    """Reuse paint tokens while each surface keeps its own scheme."""
    return _scheme_colors(scheme_for(getattr(widget, 'preview', widget)))


def draw_column_grid(painter, gutter, width, days, height, color):
    column = (width - gutter) / max(1, len(days))
    painter.setPen(QPen(color, 1))
    for index in range(len(days) + 1):
        x = gutter + index * column
        painter.drawLine(QPointF(x, 0), QPointF(x, height))


def at(day, hour=0, minute=0):
    return datetime.combine(day, time(hour, minute), local_zone())


def event_tooltip(event, preferences, calendar=None):
    """Full event details without gesture instructions or interpreted markup."""
    start = event.start if event.all_day else event.start.astimezone(local_zone())
    end = event.end if event.all_day else event.end.astimezone(local_zone())
    when = preferences.format_date(start.date(), weekday=True)
    if event.all_day:
        last = end.date() - timedelta(days=1)
        if last > start.date():
            when += ' — ' + preferences.format_date(last, weekday=True)
        when += ' · All day'
    else:
        when += ' · ' + preferences.format_time(start)
        if end.timestamp() != start.timestamp():
            when += ('–' if end.date() == start.date() else
                     ' — ' + preferences.format_date(end.date(), weekday=True) + ' · ')
            when += preferences.format_time(end)
    lines = [event.title, when]
    if calendar is not None:
        lines.append(calendar.name)
    lines.extend(value for value in (event.location, event.description) if value)
    return '<qt>' + '<br>'.join(escape(line).replace('\n', '<br>') for line in lines) + '</qt>'


def bucket_days(events, days, zone, date_events=False):
    """Index visible day membership once, retaining the supplied event order."""
    bounds = [(datetime.combine(day, time.min, zone), datetime.combine(day + timedelta(days=1), time.min, zone))
              for day in days]
    starts = [begin.timestamp() for begin, _end in bounds]
    ends = [end.timestamp() for _begin, end in bounds]
    buckets = [[] for _day in days]
    ordered = all(left < right for left, right in zip(starts, starts[1:]))
    for item in events:
        if date_events and item.all_day:
            for index, day in enumerate(days):
                if item.start.date() <= day < item.end.date():
                    buckets[index].append(item)
            continue
        begin, end = item.start.timestamp(), item.end.timestamp()
        if ordered:
            first = bisect_right(starts, begin) - 1 if begin == end else bisect_right(ends, begin)
            stop = first + 1 if begin == end else bisect_left(starts, end)
            indexes = range(max(0, first), min(len(days), stop))
        else:
            indexes = range(len(days))
        for index in indexes:
            if (starts[index] <= begin < ends[index] if begin == end else begin < ends[index] and end > starts[index]):
                buckets[index].append(item)
    return bounds, buckets


def display_scale(widget):
    preferences = getattr(widget, 'preferences', None)
    if preferences is None:
        preferences = getattr(getattr(widget, 'preview', None), 'preferences', None)
    return getattr(preferences, 'font_scale', 100) / 100


def text(painter, rect, value, color=None, size=10, bold=False, align=None):
    if rect.width() <= 0 or rect.height() <= 0:
        return
    painter.setPen(color if color is not None else paint_colors(painter.device()).ink)
    font = QFont('Sans Serif')
    font.setPointSizeF(size * display_scale(painter.device()))
    font.setBold(bold)
    painter.setFont(font)
    painter.drawText(rect, align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), value)


def week_spans(events, first, days=7, *, capacity=None, selected=(), primary=None, reserve_timed=()):
    """Place whole DATE spans in shared lanes, optionally bounded by cell rows."""
    last = first + timedelta(days=days)
    visible = [item for item in events if item.all_day and item.start.date() < last and item.end.date() > first]
    visible.sort(key=lambda item: (0 if item.id == primary else 1 if item.id in selected else 2,
                                  max(first, item.start.date()), -(item.end.date() - item.start.date()).days, item.id))
    lanes, result = [], []
    reserved = sum(1 << index for index in reserve_timed)
    for item in visible:
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
        result.append((item, lane, start, end, item.start.date() < first, item.end.date() > last))
    return result


class DayHeader(QWidget):
    day_selected = Signal(object)
    week_requested = Signal(object)
    create_all_day_requested = Signal(object, object)
    placement_moved = Signal(object)
    placement_confirmed = Signal(object)
    gutter = 62
    all_day_top = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days = []
        self.preferences = Preferences()
        self.gutter = calendar_gutter(self.preferences)
        self.selected_day = None
        self.creation_calendar = None
        self.placement_active = False
        self._placement_consumed = False
        self.setFixedHeight(64)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Double-click to create an all-day event.')

    def set_preferences(self, preferences):
        self.preferences = preferences
        scale = display_scale(self)
        self.gutter = calendar_gutter(preferences)
        self.all_day_top = math.ceil(64 * scale)
        self.setFixedHeight(self.all_day_top)
        self.update()

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    def set_creation_calendar(self, calendar):
        self.creation_calendar = (calendar if calendar is not None and not calendar.read_only
                                  and calendar.kind != 'ics' else None)
        self.update()

    def creation_color(self):
        calendar = self.creation_calendar
        color = QColor(calendar.color) if calendar is not None else QColor(paint_colors(self).accent)
        return color if color.isValid() else QColor(paint_colors(self).accent)

    def day_at(self, point):
        if not self.days or point.x() < self.gutter or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        width = (self.width() - self.gutter) / len(self.days)
        if width <= 0:
            return None
        return self.days[min(len(self.days) - 1, int((point.x() - self.gutter) / width))]

    def week_rect(self):
        if not self.days or not self.preferences.show_week_numbers:
            return None
        return QRectF(4, 8 * display_scale(self), self.gutter - 8, 48 * display_scale(self))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            week = self.week_rect()
            if week is not None and week.contains(event.position()) and not self.placement_active:
                self.week_requested.emit(self.preferences.week_start(self.days[0]))
                return
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
            week = self.week_rect()
            if week is not None and week.contains(event.position()):
                return
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
        week = self.week_rect()
        self.setToolTip('Open week ' + str(self.preferences.week_number(self.days[0]))
                        if week is not None and week.contains(event.position()) else
                        'Double-click to create an all-day event.')
        day = self.day_at(event.position())
        if self.placement_active and day is not None:
            self.placement_moved.emit(at(day))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        p.fillRect(self.rect(), colors.background)
        if not self.days:
            return
        w = (self.width() - self.gutter) / len(self.days)
        scale = display_scale(self)
        week = self.week_rect()
        if week is not None:
            paint_week_caption(p, self.gutter, scale, colors.muted)
            text(p, QRectF(week.left(), 28 * scale, week.width(), math.ceil(28 * scale)),
                 str(self.preferences.week_number(self.days[0])), colors.accent, WEEK_NUMBER_SIZE, True,
                 Qt.AlignmentFlag.AlignCenter)
        for i, day in enumerate(self.days):
            x = self.gutter + i * w
            today = day == now().date()
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                p.fillRect(QRectF(x, 0, w, self.height()), shade)
            if today:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(colors.selection)
                p.drawRoundedRect(QRectF(x + 6, 5 * scale, w - 12, 52 * scale), 4, 4)
            day_color = weekend_text(day, self.preferences, colors,
                                     colors.accent if today else colors.muted)
            text(p, weekday_label_rect(x, w, scale), self.preferences.weekday_name(day).upper(), day_color, WEEKDAY_SIZE, True)
            text(p, day_number_rect(x, w, scale), str(day.day),
                 weekend_text(day, self.preferences, colors, colors.accent if today else colors.ink), DAY_NUMBER_SIZE, True)
        draw_column_grid(p, self.gutter, self.width(), self.days, self.height(), colors.line)
        p.setPen(QPen(colors.line, 1))
        p.drawLine(QPointF(self.gutter, self.height() - .5), QPointF(self.width(), self.height() - .5))
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
    confirm_requested = Signal(str)

    hour_height = 64
    gutter = 62
    all_day_height = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days, self.events, self.calendars = [], [], {}
        self.selected_id = None
        self.selected_ids = set()
        self.selected_day = None
        self.creation_calendar = None
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
        self.preferences = Preferences()
        self.gutter = calendar_gutter(self.preferences)
        self.scope_modifiers = normalize_scope_modifiers()
        self.gesture_modifiers = Qt.KeyboardModifier.NoModifier
        self.initialize_confirmation()
        # Keep a late-night point's minimum visual marker fully visible below
        # its true time, without extending the event itself into tomorrow.
        self.setMinimumHeight(self.hour_height * 24 + self.all_day_height + 20)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_data(self, days, events, calendars):
        self.cancel_confirmation()
        self.days, self.events = days, events
        self.hover_time = None
        self.calendars = {c.id: c for c in calendars}
        self.data_changed()
        self._refit()
        self.update()

    @property
    def time_top(self):
        return 0 if self.fixed_all_day else self.all_day_height

    def set_selection(self, ids):
        if set(ids) != self.selected_ids:
            self.cancel_confirmation()
            self._shape_cache.pop('month', None)
            self.confirm_hits = []
        self.selected_ids = set(ids)
        if self.selected_id not in self.selected_ids:
            self.selected_id = next(iter(sorted(self.selected_ids)), None)
        self.update()

    def set_scope(self, scope):
        self.scope = scope if scope in ('single', 'group', 'future') else 'single'
        self.update()

    set_creation_calendar = DayHeader.set_creation_calendar
    creation_color = DayHeader.creation_color

    def set_preview(self, events):
        self.preview_events = list(events)
        self._preview_version += 1
        if self.placement_active:
            self.cancel_confirmation()
            self.hover_time = None
        self.update()

    def initialize_confirmation(self):
        self.confirm_hits = []
        self._confirm_pressed = None
        self._recent_confirmation = None
        self._confirmation_release_consumed = False
        self._data_version = self._preview_version = 0
        self._bucket_cache = {}
        self._shape_cache = {}
        self._confirmable_events = {}
        self._hovered_event_id = None

    def data_changed(self):
        self._data_version += 1
        self._bucket_cache.clear()
        self.invalidate_geometry()
        self._confirmable_events = {}
        self._hovered_event_id = None
        for item in self.events:
            calendar = self.calendars.get(item.calendar_id)
            if (item.candidate and not item.read_only and calendar and not calendar.read_only
                    and calendar.kind != 'ics'):
                self._confirmable_events.setdefault(item.id, item)

    def invalidate_geometry(self):
        self._shape_cache.clear()
        self.hits = []
        self.confirm_hits = []

    def resizeEvent(self, event):
        if hasattr(self, '_shape_cache'):
            self.invalidate_geometry()
        QWidget.resizeEvent(self, event)

    def event_token(self, events):
        if events is None or events is self.events:
            return 'actual', self._data_version
        if events is self.preview_events:
            return 'preview', self._preview_version
        if self.ghost is not None and len(events) == 1 and events[0] is self.ghost:
            return 'ghost', id(self.ghost)
        return None

    def day_buckets(self, days, events=None, *, date_events=False):
        token = self.event_token(events)
        zone = local_zone()
        key = (token, tuple(days), zone, date_events)
        if token is not None:
            entry = self._bucket_cache.get(token[0])
            if entry is not None and entry[0] == key:
                return entry[1]
        result = bucket_days(self.events if events is None else events, days, zone, date_events)
        if token is not None:
            self._bucket_cache[token[0]] = key, result
        return result

    def cancel_confirmation(self):
        self._confirm_pressed = None
        self._confirmation_release_consumed = False
        if self.placement_active:
            self._recent_confirmation = None

    def confirmation_event(self, event_id):
        if self.placement_active:
            return None
        visible = event_id in self.selected_ids or event_id == self.selected_id or event_id == self._hovered_event_id
        return self._confirmable_events.get(event_id) if visible else None

    def confirmation_rect(self, rect, item, preview=False, trailing=0):
        if preview or self.drag or self.confirmation_event(item.id) is None:
            return None
        scale = display_scale(self)
        size = min(18 * scale, rect.height() - 4 * scale)
        if size < 9 * scale or rect.width() < size + (10 + trailing) * scale:
            return None
        return QRectF(rect.right() - (3 + trailing) * scale - size,
                      rect.top() + 2 * scale, size, size)

    def paint_confirmation(self, painter, rect, item):
        if rect is None:
            return
        self.confirm_hits.append((rect, item.id))
        colors = paint_colors(self)
        painter.setPen(QPen(colors.accent, 1.2))
        painter.setBrush(colors.panel)
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QPen(colors.accent, max(1.4, 1.5 * display_scale(self))))
        painter.drawLine(QPointF(rect.left() + rect.width() * .22, rect.top() + rect.height() * .52),
                         QPointF(rect.left() + rect.width() * .43, rect.top() + rect.height() * .72))
        painter.drawLine(QPointF(rect.left() + rect.width() * .43, rect.top() + rect.height() * .72),
                         QPointF(rect.left() + rect.width() * .80, rect.top() + rect.height() * .27))

    def confirmation_hit(self, point):
        return next(((rect, event_id) for rect, event_id in reversed(self.confirm_hits)
                     if rect.contains(point) and self.confirmation_event(event_id) is not None), None)

    def begin_confirmation(self, point):
        hit = self.confirmation_hit(point)
        if hit is None:
            return False
        rect, event_id = hit
        self._confirm_pressed = (event_id, QRectF(rect), QPointF(point), False)
        self.drag = self.ghost = self._deferred_selection = None
        self._drag_pointer = None
        if hasattr(self, 'hover_time'):
            self.hover_time = None
        self.selected_day = self.day_at(point)
        self.gesture_started.emit(event_id)
        self.setToolTip(CONFIRM_TOOLTIP)
        self.update()
        return True

    def confirmation_hover(self, point):
        hit = self.hit(point)
        hovered = hit[1].id if hit else None
        if hovered != self._hovered_event_id:
            self._hovered_event_id = hovered
            self.update()
        if self._confirm_pressed:
            event_id, rect, origin, cancelled = self._confirm_pressed
            cancelled = cancelled or (point - origin).manhattanLength() >= 5 or not rect.contains(point)
            self._confirm_pressed = event_id, rect, origin, cancelled
        if self._confirm_pressed or self.confirmation_hit(point):
            if hasattr(self, 'hover_time'):
                self.hover_time = None
            self.setToolTip(CONFIRM_TOOLTIP)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.update()
            return True
        return False

    def release_confirmation(self, event):
        if self._confirmation_release_consumed:
            self._confirmation_release_consumed = False
            return True
        pressed, self._confirm_pressed = self._confirm_pressed, None
        if pressed is None:
            return False
        event_id, rect, _origin, cancelled = pressed
        if (not cancelled and rect.contains(event.position()) and self.confirmation_event(event_id) is not None):
            self._recent_confirmation = (QPointF(event.globalPosition()),
                                         monotonic() + QApplication.doubleClickInterval() / 1000)
            self.confirm_requested.emit(event_id)
        self.update()
        return True

    def consume_recent_confirmation(self, event):
        recent = self._recent_confirmation
        if recent is None:
            return False
        position, deadline = recent
        delta = event.globalPosition() - position
        distance = QApplication.styleHints().mouseDoubleClickDistance()
        if self.placement_active or monotonic() > deadline or abs(delta.x()) > distance or abs(delta.y()) > distance:
            self._recent_confirmation = None
            return False
        self._confirmation_release_consumed = True
        self.drag = self.ghost = self._deferred_selection = None
        self._drag_pointer = None
        self.update()
        return True

    def hideEvent(self, event):
        self.cancel_confirmation()
        self._recent_confirmation = None
        self._hovered_event_id = None
        QWidget.hideEvent(self, event)

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
            colors = paint_colors(self)
            painter.setPen(QPen(colors.accent, 1.5))
            painter.setBrush(colors.panel)
            painter.drawRoundedRect(rect, 4, 4)
            text(painter, rect, '+', colors.accent, 13, True, Qt.AlignmentFlag.AlignCenter)

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
        colors = paint_colors(self)
        accent = self.creation_color() if creating else colors.muted
        style = event_colors(accent.name(), self, alpha=34)
        color = QColor(style.outline if creating else accent)
        color.setAlpha(210 if creating else 125)
        painter.setPen(QPen(color, 1.5 if creating else 1))
        painter.drawLine(QPointF(x + 3, y), QPointF(x + width - 3, y))
        label = self.preferences.format_time(time(int(minute // 60) % 24, int(minute % 60)))
        scale = display_scale(self)
        label_width = min(width - 12, max(46, len(label) * 7 + 10) * scale)
        rect = QRectF(x + 6, min(max(self.time_top + 2, y - 22 * scale), self.height() - 23 * scale), label_width, 20 * scale)
        painter.setPen(QPen(QColor(style.outline) if creating else colors.line, 1))
        painter.setBrush(QColor(style.fill) if creating else colors.background)
        painter.drawRoundedRect(rect, 3, 3)
        label_color = QColor(source_outline(accent, style.fill, 4.5)) if creating else accent
        text(painter, rect, label, label_color, 8, creating, Qt.AlignmentFlag.AlignCenter)

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
            zone = local_zone()
            for events in (self.events, self.preview_events):
                bounds, buckets = self.day_buckets(self.days, events)
                for (begin, finish), daily in zip(bounds, buckets):
                    for event in daily:
                        if event.all_day:
                            continue
                        start = max(event.start.astimezone(zone), begin)
                        end = min(event.end.astimezone(zone), finish)
                        start_min = (start - begin).total_seconds() / 60
                        end_min = (end - begin).total_seconds() / 60
                        if event.start.timestamp() == event.end.timestamp():
                            end_min = min(1440, start_min + 15)
                        first = min(first, max(0, math.floor(start_min / 60)))
                        last = max(last, min(24, math.ceil(end_min / 60)))
        previous = self.start_hour, self.end_hour, self.hour_height
        self.start_hour, self.end_hour = first, last
        hours = max(1, last - first)
        self.hour_height = max(24 * display_scale(self), (self._viewport_height - self.time_top - 20) / hours)
        if previous != (first, last, self.hour_height):
            self.invalidate_geometry()
        self.setMinimumHeight(math.ceil(self.time_top + hours * self.hour_height + 20))
        if previous != (first, last, self.hour_height):
            self.hits = self.rects()
        self.update()

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.invalidate_geometry()
        scale = display_scale(self)
        self.gutter = calendar_gutter(preferences)
        self.all_day_height = math.ceil(44 * scale)
        self._refit()
        self.update()

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    def set_scope_modifiers(self, settings):
        self.scope_modifiers = normalize_scope_modifiers(settings)
        self.update()

    def rects(self, events=None):
        if not self.days:
            return []
        token = self.event_token(events)
        zone = local_zone()
        scale = display_scale(self)
        key = (token, tuple(self.days), zone, self.width(), self.gutter, self.time_top,
               self.hour_height, self.start_hour, self.end_hour, scale, self.fixed_all_day)
        cache_tag = ('rects', token[0]) if token is not None else None
        cached = self._shape_cache.get(cache_tag) if cache_tag is not None else None
        if cached is not None and cached[0] == key:
            return [(QRectF(rect), item) for rect, item in cached[1]]
        width = (self.width() - self.gutter) / len(self.days)
        result = []
        bounds, buckets = self.day_buckets(self.days, events)
        for index, ((start, end), daily) in enumerate(zip(bounds, buckets)):
            if self.fixed_all_day:
                daily = [item for item in daily if not item.all_day]
            timed = [item for item in daily if not item.all_day]
            placements = {value.event_id: value for value in layout_events(timed, window_start=start, window_end=end)}
            allday = {item.id: slot for slot, item in enumerate(item for item in daily if item.all_day)}
            for item in daily:
                x = self.gutter + index * width
                if item.all_day:
                    slot, count = allday[item.id], max(1, len(allday))
                    rect = QRectF(x + 3 + slot * width / count, 4, width / count - 6, 33 * scale)
                else:
                    placement = placements[item.id]
                    local_start = max(item.start.astimezone(zone), start)
                    local_end = min(item.end.astimezone(zone), end)
                    top = ((local_start - start).total_seconds() / 3600 - self.start_hour) * self.hour_height
                    bottom = ((local_end - start).total_seconds() / 3600 - self.start_hour) * self.hour_height
                    rect = QRectF(x + placement.column * width / placement.columns + 3,
                                  self.time_top + top + 1, width / placement.columns - 6,
                                  max((18 if item.start.timestamp() == item.end.timestamp() else 14) * scale, bottom - top - 2))
                result.append((rect, item))
        if cache_tag is not None:
            self._shape_cache[cache_tag] = key, result
        return [(QRectF(rect), item) for rect, item in result]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        p.fillRect(self.rect(), colors.background)
        self.confirm_hits = []
        if not self.days:
            return
        w = (self.width() - self.gutter) / len(self.days)
        scale = display_scale(self)
        for i, day in enumerate(self.days):
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                p.fillRect(QRectF(self.gutter + i * w, 0, w, self.height()), shade)
        draw_column_grid(p, self.gutter, self.width(), self.days, self.height(), colors.line)
        if not self.fixed_all_day:
            text(p, axis_label_rect(self.gutter, 4 * scale, 28 * scale, scale), 'All day', colors.muted, ALL_DAY_SIZE,
                 align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            p.setPen(QPen(colors.line, 1))
            p.drawLine(0, self.time_top, self.width(), self.time_top)
        for hour in range(self.start_hour, self.end_hour + 1):
            y = self.time_top + (hour - self.start_hour) * self.hour_height
            p.setPen(QPen(colors.line, 1))
            p.drawLine(QPointF(self.gutter, y), QPointF(self.width(), y))
            if hour and hour < 24:
                label = self.preferences.format_time(time(hour))
                text(p, axis_label_rect(self.gutter, max(0, y - 11 * scale), 22 * scale, scale), label, colors.muted, AXIS_SIZE,
                     align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if hour < self.end_hour:
                p.setPen(QPen(colors.line, 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(self.gutter, y + self.hour_height / 2), QPointF(self.width(), y + self.hour_height / 2))
        current_point = None
        current = now()
        if current.date() in self.days and self.start_hour <= current.hour < self.end_hour:
            i = self.days.index(current.date())
            y = self.time_top + (current.hour + current.minute / 60 - self.start_hour) * self.hour_height
            p.setPen(QPen(colors.now, 1.5))
            p.drawLine(QPointF(self.gutter + i * w, y), QPointF(self.gutter + (i + 1) * w, y))
            current_point = QPointF(self.gutter + i * w, y)
        self.hits = self.rects()
        for rect, event in self.hits:
            self.paint_event(p, rect, event)
        for rect, event in self.rects(self.preview_events):
            self.paint_event(p, rect, event, preview=True)
        if current_point is not None:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colors.now)
            p.drawEllipse(current_point, 3, 3)
        if self.ghost and not self.preview_events:
            day, start_min, end_min = self.ghost
            if 0 <= day < len(self.days):
                rect = QRectF(self.gutter + day * w + 3, self.time_top + (start_min / 60 - self.start_hour) * self.hour_height,
                              w - 6, max(12, (end_min - start_min) / 60 * self.hour_height))
                creating = self.drag and self.drag[0] == 'create'
                item = self.drag[1] if self.drag and not creating else None
                calendar = self.calendars.get(item.calendar_id) if item is not None else None
                color = self.creation_color() if creating else QColor(calendar.color) if calendar else colors.accent
                style = event_colors(color.name(), self, alpha=45)
                p.setPen(QPen(QColor(style.outline), 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(style.fill))
                p.drawRoundedRect(rect, 5, 5)
                if self.drag and self.drag[0] != 'create':
                    scope = self.scope
                    label = {'single': 'One event', 'group': 'All events', 'future': 'This and following'}[scope]
                    text(p, rect.adjusted(5, 0, -5, 0), label, QColor(style.ink), 8, True)
        self.paint_time_feedback(p)
        self.paint_copy_badge(p)
        p.end()

    def paint_event(self, p, rect, event, preview=False):
        color = self.calendars[event.calendar_id].color if event.calendar_id in self.calendars else scheme_for(self).accent
        selected = not preview and (event.id in self.selected_ids or event.id == self.selected_id)
        confirm = self.confirmation_rect(rect, event, preview)
        scale = display_scale(self)
        style = event_colors(color, self, alpha=34 if preview else 38 if event.candidate else 52)
        edge = QColor(style.selected_outline if selected else style.outline)
        p.setBrush(QColor(style.fill))
        p.setPen(QPen(edge, 2 if selected or preview else 1,
                      Qt.PenStyle.DashLine if preview or event.candidate else Qt.PenStyle.SolidLine))
        p.drawRoundedRect(rect, 5, 5)
        if selected:
            p.fillRect(QRectF(rect.left() + 3, rect.top() + 5, 3, max(3, rect.height() - 10)), edge)
        # QPainter draws no text in nonpositive label rectangles. Dense
        # overlap columns still keep their frames, but need no font/layout work.
        if rect.width() > 12 and rect.height() > 2:
            p.save()
            p.setClipRect(rect.adjusted(4, 1, -3, -1))
            point_event = event.start.timestamp() == event.end.timestamp()
            scale = display_scale(self)
            title = (self.preferences.format_time(event.start.astimezone(local_zone())) + ' · ' if point_event else '') + event.title
            reserve = 12 + (confirm.width() + 6 * scale if confirm is not None else 0)
            text(p, QRectF(rect.x() + 8, rect.y() + 1, rect.width() - reserve, min(21 * scale, rect.height() - 2)), title, QColor(style.ink), 9, True)
            if rect.height() > 39 * scale:
                value = self.preferences.format_time(event.start.astimezone(local_zone())) + '–' + self.preferences.format_time(event.end.astimezone(local_zone()))
                if event.candidate:
                    value += ' · option'
                text(p, QRectF(rect.x() + 8, rect.y() + 23 * scale, rect.width() - 12, 18 * scale), value, QColor(style.muted), 8)
            if rect.height() > 64 * scale and event.location:
                text(p, QRectF(rect.x() + 8, rect.y() + 43 * scale, rect.width() - 12, 18 * scale), event.location, QColor(style.muted), 8)
            p.restore()
        self.paint_confirmation(p, confirm, event)

    def hit(self, pos):
        self.hits = self.rects()
        return next(((r, e) for r, e in reversed(self.hits) if r.contains(pos)), None)

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
        self.cancel_confirmation()
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
        if self.consume_recent_confirmation(event):
            return
        if self.begin_confirmation(point):
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
            if self.confirmation_hover(point):
                return
            hit = self.hit(point)
            day = self.day_at(point)
            hover = self.position(point) if not hit and day is not None and point.y() >= self.time_top else None
            if hover != self.hover_time:
                self.hover_time = hover
                self.update()
            if hit:
                self.setToolTip(event_tooltip(hit[1], self.preferences, self.calendars.get(hit[1].calendar_id)))
            elif day is not None:
                self.setToolTip('Double-click to add an all-day event.' if point.y() < self.time_top else
                                'Double-click to add an event, or drag to choose its duration.')
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
        self._hovered_event_id = None
        if hasattr(self, 'hover_time'):
            self.hover_time = None
        self.setToolTip('')
        self.update()
        QWidget.leaveEvent(self, event)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.release_confirmation(event):
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
        self.cancel_confirmation()
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
        if self.consume_recent_confirmation(event):
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
        self.cancel_confirmation()
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
    week_requested = Signal(object)
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
    confirm_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.start = now().date()
        self.month = self.start.month
        self.events, self.calendars, self.hits = [], {}, []
        self.selected_id = None
        self.selected_ids = set()
        self.selected_day = None
        self.creation_calendar = None
        self.scope = 'single'
        self.preview_events = []
        self.placement_active = False
        self._placement_consumed = False
        self._deferred_selection = None
        self.drag = self.ghost = None
        self.gesture_modifiers = Qt.KeyboardModifier.NoModifier
        self._drag_pointer = None
        self.preferences = Preferences()
        self.show_creation_hints = False
        self.initialize_confirmation()
        self.setMinimumHeight(440)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.invalidate_geometry()
        self.setMinimumHeight(math.ceil(440 * display_scale(self)))
        self.update()

    @property
    def header_height(self):
        return math.ceil(28 * display_scale(self))

    @property
    def week_gutter(self):
        return calendar_gutter(self.preferences)

    def week_rect(self, row):
        if not self.preferences.show_week_numbers or not 0 <= row < 6:
            return None
        height = (self.height() - self.header_height) / 6
        return QRectF(0, self.header_height + row * height, self.week_gutter, height)

    def week_at(self, point):
        if not self.preferences.show_week_numbers or not 0 <= point.x() < self.week_gutter or not self.header_height <= point.y() < self.height():
            return None
        row = int((point.y() - self.header_height) / ((self.height() - self.header_height) / 6))
        return self.start + timedelta(days=7 * row)

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    set_selection = CalendarCanvas.set_selection
    set_scope = CalendarCanvas.set_scope
    set_creation_calendar = CalendarCanvas.set_creation_calendar
    creation_color = CalendarCanvas.creation_color
    emit_selection = CalendarCanvas.emit_selection
    edit_hit = CalendarCanvas.edit_hit
    set_drag_modifiers = CalendarCanvas.set_drag_modifiers
    copy_badge_rect = CalendarCanvas.copy_badge_rect
    paint_copy_badge = CalendarCanvas.paint_copy_badge
    data_changed = CalendarCanvas.data_changed
    invalidate_geometry = CalendarCanvas.invalidate_geometry
    resizeEvent = CalendarCanvas.resizeEvent
    leaveEvent = CalendarCanvas.leaveEvent
    event_token = CalendarCanvas.event_token
    day_buckets = CalendarCanvas.day_buckets
    initialize_confirmation = CalendarCanvas.initialize_confirmation
    cancel_confirmation = CalendarCanvas.cancel_confirmation
    confirmation_event = CalendarCanvas.confirmation_event
    confirmation_rect = CalendarCanvas.confirmation_rect
    paint_confirmation = CalendarCanvas.paint_confirmation
    confirmation_hit = CalendarCanvas.confirmation_hit
    begin_confirmation = CalendarCanvas.begin_confirmation
    confirmation_hover = CalendarCanvas.confirmation_hover
    release_confirmation = CalendarCanvas.release_confirmation
    consume_recent_confirmation = CalendarCanvas.consume_recent_confirmation
    hideEvent = CalendarCanvas.hideEvent

    def set_preview(self, events):
        if self.placement_active:
            self.cancel_confirmation()
        self.preview_events = list(events)
        self._preview_version += 1
        self.update()

    def set_data(self, start, month, events, calendars):
        self.cancel_confirmation()
        self.start, self.month, self.events = start, month, events
        self.calendars = {c.id: c for c in calendars}
        self.data_changed()
        self.update()

    def _paint_month_event(self, painter, rect, item, *, preview=False, before=False, after=False):
        color = self.calendars[item.calendar_id].color if item.calendar_id in self.calendars else scheme_for(self).accent
        selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
        confirm = self.confirmation_rect(rect, item, preview, trailing=18 if after else 0)
        style = event_colors(color, self, alpha=45 if preview else 40)
        edge = QColor(style.selected_outline if selected else style.outline)
        painter.setPen(QPen(edge, 2 if selected or preview else 1,
                            Qt.PenStyle.DashLine if preview or item.candidate else Qt.PenStyle.SolidLine)
                       if selected or preview or item.all_day else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(style.fill))
        painter.drawRoundedRect(rect, 3, 3)
        if selected:
            painter.fillRect(QRectF(rect.left() + 3, rect.top() + 4, 3, rect.height() - 8), edge)
        ink = QColor(style.ink)
        if before:
            text(painter, QRectF(rect.left() + 7, rect.top(), 12, rect.height()), '‹', ink, 10, True)
        if after:
            text(painter, QRectF(rect.right() - 18, rect.top(), 12, rect.height()), '›', ink, 10, True)
        label = ('' if item.all_day else self.preferences.format_time(item.start.astimezone(local_zone())) + ' ') + item.title
        label_rect = rect.adjusted(21 if before else 8 if selected else 5, 0, -21 if after else -3, 0)
        if confirm is not None:
            label_rect.setRight(confirm.left() - 4 * display_scale(self))
        painter.save()
        painter.setClipRect(label_rect)
        text(painter, label_rect, label, ink, 8, selected or preview)
        painter.restore()
        self.paint_confirmation(painter, confirm, item)

    def month_row_metrics(self):
        scale = display_scale(self)
        return 32 * scale, 24 * scale, 21 * scale, 5 * scale

    def month_layout(self):
        previews = [self.ghost] if not self.preview_events and self.ghost is not None else self.preview_events
        key = (self._data_version, self._preview_version, id(self.ghost), self.start,
               self.width(), self.height(), display_scale(self), local_zone(),
               frozenset(self.selected_ids), self.selected_id, self.week_gutter, self.show_creation_hints)
        cached = self._shape_cache.get('month')
        if cached is not None and cached[0] == key:
            return cached[1]
        scale = display_scale(self)
        header = self.header_height
        heading, lane, event_height, bottom_padding = self.month_row_metrics()
        gutter = self.week_gutter
        width, height = (self.width() - gutter) / 7, (self.height() - header) / 6
        capacity = max(0, int((height - heading - bottom_padding) / lane))
        days = [self.start + timedelta(days=index) for index in range(42)]
        _bounds, buckets = self.day_buckets(days, date_events=True)
        _bounds, preview_buckets = self.day_buckets(days, previews, date_events=True)
        commands, hits = [], []
        selected_ids = self.selected_ids | {self.selected_id}
        for week in range(6):
            first = self.start + timedelta(days=week * 7)
            y = header + week * height
            week_buckets = buckets[week * 7:week * 7 + 7]
            timed = [[item for item in daily if not item.all_day] for daily in week_buckets]
            reserve = {index for index, daily in enumerate(timed)
                       if any(item.id in self.selected_ids or item.id == self.selected_id for item in daily)}
            spans = week_spans(self.events, first, capacity=capacity, selected=self.selected_ids,
                               primary=self.selected_id, reserve_timed=reserve)
            used, shown = [set() for _ in range(7)], [0] * 7
            for item, lane_index, start, end, before, after in spans:
                rect = QRectF(gutter + start * width + 5, y + heading + lane_index * lane, (end - start) * width - 10, event_height)
                commands.append(('event', rect, item, False, before, after))
                hits.append((rect, item))
                for index in range(start, end):
                    used[index].add(lane_index)
                    shown[index] += 1
            for index in range(7):
                x = gutter + index * width
                free = [slot for slot in range(capacity) if slot not in used[index]]
                all_day_count = sum(item.all_day for item in week_buckets[index])
                daily = timed[index]
                hidden_all_day = all_day_count - shown[index]
                slots = len(free)
                count = slots if len(daily) + hidden_all_day <= slots else max(0, slots - 1)
                visible = daily[:count]
                visible_ids = {item.id for item in visible}
                selected = [item for item in daily if item.id in selected_ids]
                for item in selected:
                    if item.id in visible_ids or not slots:
                        continue
                    replace_at = next((number for number in reversed(range(len(visible))) if visible[number].id not in selected_ids), None)
                    if replace_at is not None:
                        visible_ids.discard(visible[replace_at].id)
                        visible[replace_at] = item
                        visible_ids.add(item.id)
                    elif len(visible) < slots:
                        visible.append(item)
                        visible_ids.add(item.id)
                for lane_index, item in zip(free, visible):
                    rect = QRectF(x + 5, y + heading + lane_index * lane, width - 10, event_height)
                    commands.append(('event', rect, item, False, False, False))
                    hits.append((rect, item))
                hidden = hidden_all_day + len(daily) - len(visible)
                header_count = hidden and len(visible) >= slots
                if hidden and not header_count:
                    lane_index = free[len(visible)]
                    commands.append(('text', QRectF(x + 10, y + heading + lane_index * lane, width - 15, event_height), f'+{hidden} more', 8, None))
                if header_count:
                    commands.append(('text', QRectF(x + 35, y + 5 * scale, max(0, width - 57), 20 * scale), f'+{hidden}', 7, None))
                if self.show_creation_hints:
                    commands.append(('text', QRectF(x + 32, y + 5 * scale, width - 40, 20 * scale),
                                     '+ all day' if width >= 90 and not header_count else '+', 7,
                                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            preview_spans = week_spans(previews, first, capacity=capacity)
            preview_used = [set() for _ in range(7)]
            for item, lane_index, start, end, before, after in preview_spans:
                rect = QRectF(gutter + start * width + 5, y + heading + lane_index * lane, (end - start) * width - 10, event_height)
                commands.append(('event', rect, item, True, before, after))
                for index in range(start, end):
                    preview_used[index].add(lane_index)
            for index in range(7):
                daily = [item for item in preview_buckets[week * 7 + index] if not item.all_day]
                free = [slot for slot in range(capacity) if slot not in preview_used[index]]
                for lane_index, item in zip(free, daily):
                    rect = QRectF(gutter + index * width + 5, y + heading + lane_index * lane, width - 10, event_height)
                    commands.append(('event', rect, item, True, False, False))
        result = commands, hits
        self._shape_cache['month'] = key, result
        return result

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        p.fillRect(self.rect(), colors.background)
        self.confirm_hits = []
        scale = display_scale(self)
        gutter = self.week_gutter
        width, height = (self.width() - gutter) / 7, (self.height() - self.header_height) / 6
        day_label_height = min(25 * scale, self.month_row_metrics()[0] - 2 * scale)
        if self.preferences.show_week_numbers:
            paint_week_caption(p, gutter, scale, colors.muted)
            for row in range(6):
                rect = self.week_rect(row)
                text(p, QRectF(rect.left(), rect.top() + 3 * scale, rect.width(), 25 * scale),
                     str(self.preferences.week_number(self.start + timedelta(days=7 * row))), colors.accent, 8, True,
                     Qt.AlignmentFlag.AlignCenter)
        today = now().date()
        for index in range(7):
            day = self.start + timedelta(days=index)
            name = self.preferences.weekday_name(day).upper()
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                p.fillRect(QRectF(gutter + index * width, 0, width, self.header_height), shade)
            text(p, weekday_label_rect(gutter + index * width, width, scale), name,
                 weekend_text(day, self.preferences, colors, colors.muted), WEEKDAY_SIZE, True)
        # Finish every day background before drawing any bar across its neighbors.
        for index in range(42):
            day = self.start + timedelta(days=index)
            x, y = gutter + (index % 7) * width, self.header_height + (index // 7) * height
            rect = QRectF(x, y, width, height)
            if day.month != self.month:
                p.fillRect(rect, colors.outside_month)
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                p.fillRect(rect, shade)
            if day == today:
                if shade is not None:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(colors.selection)
                    p.drawRoundedRect(QRectF(x + 5 * scale, y + 2 * scale,
                                            max(0, min(28 * scale, width - 10 * scale)), 26 * scale),
                                      4 * scale, 4 * scale)
                else:
                    p.fillRect(rect, colors.selection)
            p.setPen(QPen(colors.line, 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
            text(p, QRectF(x + 10, y + 3 * scale, width - 18, day_label_height), str(day.day),
                 weekend_text(day, self.preferences, colors, colors.accent if day == today else colors.ink), 10, True)
        p.setPen(QPen(colors.line, 1))
        for index in range(8):
            x = gutter + index * width
            p.drawLine(QPointF(x, 0), QPointF(x, self.header_height))
        commands, hits = self.month_layout()
        self.hits = [(QRectF(rect), item) for rect, item in hits]
        for command in commands:
            if command[0] == 'event':
                _kind, rect, item, preview, before, after = command
                self._paint_month_event(p, rect, item, preview=preview, before=before, after=after)
            else:
                _kind, rect, label, size, align = command
                text(p, rect, label, colors.muted, size, align=align)
        self.paint_copy_badge(p)
        p.end()

    def hit(self, point):
        self.hits = [(QRectF(rect), item) for rect, item in self.month_layout()[1]]
        return next(((rect, item) for rect, item in reversed(self.hits) if rect.contains(point)), None)

    def day_at(self, point):
        if point.x() < self.week_gutter or point.y() < self.header_height or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        index = min(5, max(0, int((point.y() - self.header_height) / ((self.height() - self.header_height) / 6)))) * 7 + min(6, max(0, int((point.x() - self.week_gutter) / ((self.width() - self.week_gutter) / 7))))
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
        week = self.week_at(event.position())
        if week is not None and not self.placement_active:
            self.week_requested.emit(week)
            return
        self.cancel_confirmation()
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
        if self.consume_recent_confirmation(event):
            return
        if self.begin_confirmation(event.position()):
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
        self.cancel_confirmation()
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
        if self.consume_recent_confirmation(event):
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
        if not self.placement_active and not self.drag and self.confirmation_hover(point):
            return
        day = self.day_at(point)
        if day is None:
            week = self.week_at(point)
            self.setToolTip('Open week ' + str(self.preferences.week_number(week)) if week is not None else '')
            self.setCursor(Qt.CursorShape.PointingHandCursor if week is not None else Qt.CursorShape.ArrowCursor)
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
            self.setToolTip(event_tooltip(hit[1], self.preferences, self.calendars.get(hit[1].calendar_id)))
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        height = (self.height() - self.header_height) / 6
        all_day = (point.y() - self.header_height) % height < 30 * display_scale(self)
        self.setToolTip(self.preferences.format_date(day, weekday=True) + '\n'
                        + ('Double-click the day heading to create an all-day event.' if all_day else
                           'Double-click to create an event at ' + self.preferences.format_time(time(9)) + '.'))
        self.setCursor(Qt.CursorShape.PointingHandCursor if all_day else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.release_confirmation(event):
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
        self.cancel_confirmation()
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


class AllDayCanvas(MonthCanvas):
    """Fixed week lanes, with one connected bar per all-day occurrence."""
    gutter = 62
    lane_height = 27

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days = []
        self.gutter = calendar_gutter(self.preferences)
        self.setMinimumHeight(0)
        self.setFixedHeight(35)

    def _lanes(self, events):
        if not self.days:
            return []
        token = self.event_token(events)
        key = token, tuple(self.days)
        tag = ('lanes', token[0]) if token is not None else None
        cached = self._shape_cache.get(tag) if tag is not None else None
        if cached is not None and cached[0] == key:
            return cached[1]
        result = week_spans(events, self.days[0], len(self.days))
        if tag is not None:
            self._shape_cache[tag] = key, result
        return result

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.invalidate_geometry()
        scale = display_scale(self)
        self.gutter = calendar_gutter(preferences)
        self.lane_height = math.ceil(27 * scale)
        self._resize_lanes()
        self.update()

    def _resize_lanes(self):
        lanes = self._lanes(self.events)
        count = max((entry[1] + 1 for entry in lanes), default=1)
        self.setFixedHeight(count * self.lane_height + math.ceil(8 * display_scale(self)))

    def week_at(self, point):
        return None

    def set_data(self, days, events, calendars):
        self.cancel_confirmation()
        self.days, self.events = list(days), list(events)
        self.calendars = {calendar.id: calendar for calendar in calendars}
        self.data_changed()
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
                        (end - start) * width - 6, self.lane_height - pad), item)
                for item, lane, start, end, _left, _right in self._lanes(self.events if events is None else events)]

    def hit(self, point):
        self.hits = self.rects()
        return next(((rect, item) for rect, item in reversed(self.hits) if rect.contains(point)), None)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = paint_colors(self)
        p.fillRect(self.rect(), colors.background)
        if not self.days:
            p.end()
            return
        column = (self.width() - self.gutter) / len(self.days)
        for index, day in enumerate(self.days):
            shade = weekend_fill(day, self.preferences, colors)
            if shade is not None:
                p.fillRect(QRectF(self.gutter + index * column, 0, column, self.height()), shade)
        draw_column_grid(p, self.gutter, self.width(), self.days, self.height(), colors.line)
        p.setPen(QPen(colors.line, 1))
        p.drawLine(QPointF(self.gutter, self.height() - .5), QPointF(self.width(), self.height() - .5))
        scale = display_scale(self)
        text(p, axis_label_rect(self.gutter, 3 * scale, 24 * scale, scale), 'All day', colors.muted, ALL_DAY_SIZE,
             align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.hits = []
        self.confirm_hits = []
        for rect, item in self.rects():
            self._paint_bar(p, rect, item)
            self.hits.append((rect, item))
        previews = [self.ghost] if not self.preview_events and self.ghost is not None else self.preview_events
        pad = 4 * scale
        slots = max(1, (self.height() - 2 * pad) // self.lane_height)
        for rect, item in self.rects(previews):
            # Preview lanes never resize the fixed surface under the pointer.
            rect.moveTop(pad + (int((rect.top() - pad) / self.lane_height) % slots) * self.lane_height)
            self._paint_bar(p, rect, item, preview=True)
        self.paint_copy_badge(p)
        p.end()

    def _paint_bar(self, painter, rect, item, preview=False):
        color = self.calendars[item.calendar_id].color if item.calendar_id in self.calendars else scheme_for(self).accent
        selected = not preview and (item.id in self.selected_ids or item.id == self.selected_id)
        style = event_colors(color, self, alpha=36 if preview else 45)
        edge = QColor(style.selected_outline if selected else style.outline)
        painter.setPen(QPen(edge, 2 if selected or preview else 1,
                            Qt.PenStyle.DashLine if preview or item.candidate else Qt.PenStyle.SolidLine))
        painter.setBrush(QColor(style.fill))
        painter.drawRoundedRect(rect, 4, 4)
        if selected:
            painter.fillRect(QRectF(rect.left() + 3, rect.top() + 4, 3, rect.height() - 8), edge)
        first, last = self.days[0], self.days[-1] + timedelta(days=1)
        before, after = item.start.date() < first, item.end.date() > last
        confirm = self.confirmation_rect(rect, item, preview, trailing=18 if after else 0)
        ink = QColor(style.ink)
        if before:
            text(painter, QRectF(rect.left() + 7, rect.top(), 12, rect.height()), '‹', ink, 10, True)
        if after:
            text(painter, QRectF(rect.right() - 18, rect.top(), 12, rect.height()), '›', ink, 10, True)
        label = rect.adjusted(21 if before else 8, 0, -21 if after else -6, 0)
        if confirm is not None:
            label.setRight(confirm.left() - 4 * display_scale(self))
        painter.save()
        painter.setClipRect(label)
        text(painter, label, item.title, ink, 8, selected)
        painter.restore()
        self.paint_confirmation(painter, confirm, item)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.cancel_confirmation()
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
        if self.consume_recent_confirmation(event):
            return
        self.setFocus()
        if self.edit_hit(event.position(), event.modifiers()):
            return
        self.selected_day = day
        self.day_selected.emit(day)
        self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))

    def mouseMoveEvent(self, event):
        if self.drag or self.placement_active or self._confirm_pressed:
            super().mouseMoveEvent(event)
        else:
            if self.confirmation_hover(event.position()):
                return
            hit = self.hit(event.position())
            self.setToolTip(event_tooltip(hit[1], self.preferences, self.calendars.get(hit[1].calendar_id)) if hit else
                            'Double-click to add an all-day event.')
