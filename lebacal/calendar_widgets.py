"""Paint-only calendar surfaces: visible events are cheap; gestures commit on release."""
from dataclasses import replace
from datetime import datetime, time, timedelta
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


def text(painter, rect, value, color=INK, size=10, bold=False, align=None):
    painter.setPen(color)
    font = QFont('Sans Serif', size)
    font.setBold(bold)
    painter.setFont(font)
    painter.drawText(rect, align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), value)


def badge(painter, rect, value, selected=False):
    painter.setPen(QPen(GREEN if selected else QColor('#bccbb4'), 1))
    painter.setBrush(GREEN if selected else QColor('#f8fbf3'))
    painter.drawRoundedRect(rect, 4, 4)
    text(painter, rect, value, QColor('#ffffff') if selected else GREEN, 8, True,
         Qt.AlignmentFlag.AlignCenter)


class DayHeader(QWidget):
    day_selected = Signal(object)
    create_all_day_requested = Signal(object, object)
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
        self.setFixedHeight(84)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Click to select a day. Double-click to create an all-day event.')

    def set_preferences(self, preferences):
        self.preferences = preferences
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
                self.set_selected_day(day)
                self.day_selected.emit(day)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            day = self.day_at(event.position())
            if day is not None:
                self.setFocus()
                self.set_selected_day(day)
                self.day_selected.emit(day)
                self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        if not self.days:
            return
        w = (self.width() - self.gutter) / len(self.days)
        for i, day in enumerate(self.days):
            x = self.gutter + i * w
            today = day == now().date()
            selected_day = self.navigation_active and i == self.navigation_day
            if day == self.selected_day:
                p.setPen(QPen(SELECTED_DAY_BORDER, 1.5))
                p.setBrush(SELECTED_DAY)
                p.drawRoundedRect(QRectF(x + 3, 3, w - 6, self.height() - 6), 7, 7)
            if today or selected_day:
                p.setPen(QPen(GREEN, 2) if selected_day else QPen(Qt.PenStyle.NoPen))
                p.setBrush(QColor('#dceace') if selected_day else QColor('#e6eedf'))
                p.drawRoundedRect(QRectF(x + 8, 5, w - 16, 54), 7, 7)
            text(p, QRectF(x + 14, 8, w - 22, 20), self.preferences.weekday_name(day).upper(), GREEN if today or selected_day else MUTED, 9, True)
            text(p, QRectF(x + 14, 27, w - 22, 28), str(day.day), GREEN if today else INK, 18, True)
            if self.navigation_active:
                badge(p, QRectF(x + w - 34, 11, 21, 21), str(i + 1), selected_day)
            p.setPen(QPen(LINE, 1))
            p.drawLine(QPointF(x + 8, self.all_day_top), QPointF(x + w - 8, self.all_day_top))
            text(p, QRectF(x + 8, self.all_day_top + 1, w - 16, 20), '+ All day', GREEN, 8,
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
    context_requested = Signal(str, object)

    hour_height = 64
    gutter = 62
    all_day_height = 44

    def __init__(self, parent=None):
        super().__init__(parent)
        self.days, self.events, self.calendars = [], [], {}
        self.selected_id = None
        self.selected_day = None
        self.hits = []
        self.drag = None
        self.ghost = None
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
        self.calendars = {c.id: c for c in calendars}
        self.update()

    def set_preferences(self, preferences):
        self.preferences = preferences
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

    def rects(self):
        if not self.days:
            return []
        width = (self.width() - self.gutter) / len(self.days)
        result = []
        for index, day in enumerate(self.days):
            start, end = at(day), at(day + timedelta(days=1))
            daily = [e for e in self.events if overlaps(e, start, end)]
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
                    rect = QRectF(x + 3 + slot * width / n, 4, width / n - 6, 33)
                else:
                    placement = placements[event.id]
                    local_start = max(event.start.astimezone(local_zone()), start)
                    local_end = min(event.end.astimezone(local_zone()), end)
                    top = (local_start - start).total_seconds() / 3600 * self.hour_height
                    bottom = (local_end - start).total_seconds() / 3600 * self.hour_height
                    rect = QRectF(x + placement.column * width / placement.columns + 3,
                                  self.all_day_height + top + 1,
                                  width / placement.columns - 6,
                                  max(18 if event.start.timestamp() == event.end.timestamp() else 14, bottom - top - 2))
                result.append((rect, event, day_numbers[event.id]))
        return result

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        if not self.days:
            return
        w = (self.width() - self.gutter) / len(self.days)
        for i, day in enumerate(self.days):
            if day.weekday() >= 5:
                p.fillRect(QRectF(self.gutter + i * w, 0, w, self.height()), QColor('#f4f6f0'))
            if day == self.selected_day:
                p.fillRect(QRectF(self.gutter + i * w, 0, w, self.height()), SELECTED_DAY)
            if self.show_numbers and i == self.navigation_day:
                p.fillRect(QRectF(self.gutter + i * w, 0, w, self.height()), QColor('#eef4e7'))
        p.setPen(QPen(LINE, 1))
        for i in range(len(self.days) + 1):
            x = self.gutter + i * w
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
        if self.selected_day in self.days:
            index = self.days.index(self.selected_day)
            p.setPen(QPen(SELECTED_DAY_BORDER, 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(self.gutter + index * w + 1, 1, w - 2, self.height() - 2))
        text(p, QRectF(4, 4, 56, 28), 'ALL DAY', MUTED, 7)
        p.setPen(QPen(LINE, 1))
        p.drawLine(0, self.all_day_height, self.width(), self.all_day_height)
        for hour in range(24):
            y = self.all_day_height + hour * self.hour_height
            p.setPen(QPen(LINE, 1))
            p.drawLine(self.gutter, y, self.width(), y)
            if hour:
                label = self.preferences.format_time(time(hour))
                text(p, QRectF(2, y - 11, 54, 22), label, MUTED, 7 if len(label) > 5 else 8,
                     align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            p.setPen(QPen(QColor('#edf0e9'), 1, Qt.PenStyle.DotLine))
            p.drawLine(self.gutter, y + self.hour_height // 2, self.width(), y + self.hour_height // 2)
        self.hits = self.rects()
        for rect, event, number in self.hits:
            self.paint_event(p, rect, event, number)
        current = now()
        if current.date() in self.days:
            i = self.days.index(current.date())
            y = self.all_day_height + (current.hour + current.minute / 60) * self.hour_height
            p.setPen(QPen(QColor('#c16f55'), 1.5))
            p.drawLine(QPointF(self.gutter + i * w, y), QPointF(self.gutter + (i + 1) * w, y))
            p.setBrush(QColor('#c16f55'))
            p.drawEllipse(QPointF(self.gutter + i * w, y), 3, 3)
        if self.ghost:
            day, start_min, end_min = self.ghost
            if 0 <= day < len(self.days):
                rect = QRectF(self.gutter + day * w + 3, self.all_day_height + start_min / 60 * self.hour_height,
                              w - 6, max(12, (end_min - start_min) / 60 * self.hour_height))
                p.setPen(QPen(GREEN, 2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(80, 130, 90, 45))
                p.drawRoundedRect(rect, 5, 5)
                if self.drag and self.drag[0] != 'create':
                    scope = scope_from_modifiers(self.gesture_modifiers, self.scope_modifiers)
                    label = {'single': 'One event', 'group': 'All events', 'future': 'This and following'}[scope]
                    text(p, rect.adjusted(5, 0, -5, 0), label, GREEN, 8, True)
        p.end()

    def paint_event(self, p, rect, event, number):
        color = QColor(self.calendars[event.calendar_id].color) if event.calendar_id in self.calendars else GREEN
        day_width = (self.width() - self.gutter) / max(1, len(self.days))
        day_index = int((rect.center().x() - self.gutter) / day_width)
        numbered = self.show_numbers and self.navigation_day == day_index
        target = numbered and self.navigation_digits.isascii() and self.navigation_digits.isdigit() and int(self.navigation_digits) == number
        selected = event.id == self.selected_id
        fill = QColor(SELECTED_EVENT if selected else color)
        if not selected:
            fill.setAlpha(95 if target else (38 if event.candidate else 52))
        p.setBrush(fill)
        p.setPen(QPen(GREEN if target else SELECTED_EVENT_BORDER if selected else color, 3 if target or selected else 1,
                      Qt.PenStyle.DashLine if event.candidate else Qt.PenStyle.SolidLine))
        p.drawRoundedRect(rect, 5, 5)
        if selected:
            p.fillRect(QRectF(rect.left() + 3, rect.top() + 5, 3, max(3, rect.height() - 10)), SELECTED_EVENT_BORDER)
        p.save()
        p.setClipRect(rect.adjusted(4, 1, -3, -1))
        point_event = event.start.timestamp() == event.end.timestamp()
        title = (self.preferences.format_time(event.start.astimezone(local_zone())) + ' · ' if point_event else '') + event.title
        text(p, QRectF(rect.x() + 8, rect.y() + 1, rect.width() - (40 if numbered else 12), min(21, rect.height() - 2)), title, SELECTED_EVENT_BORDER if selected else color.darker(145), 9, True)
        if rect.height() > 39:
            value = self.preferences.format_time(event.start.astimezone(local_zone())) + '–' + self.preferences.format_time(event.end.astimezone(local_zone()))
            if event.candidate:
                value += ' · option'
            text(p, QRectF(rect.x() + 8, rect.y() + 23, rect.width() - 12, 18), value, color.darker(120), 8)
        if rect.height() > 64 and event.location:
            text(p, QRectF(rect.x() + 8, rect.y() + 43, rect.width() - 12, 18), event.location, color.darker(120), 8)
        p.restore()
        if numbered:
            size = min(22, max(12, rect.height() - 4))
            badge(p, QRectF(rect.right() - 26, rect.top() + 3, 22, size), str(number), target)

    def hit(self, pos):
        return next(((r, e) for r, e, _ in reversed(self.hits) if r.contains(pos)), None)

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

    def position(self, point, endpoint=False):
        width = (self.width() - self.gutter) / max(1, len(self.days))
        day = min(len(self.days) - 1, max(0, int((point.x() - self.gutter) / width)))
        minutes = min(1440 if endpoint else 1425,
                      max(0, round((point.y() - self.all_day_height) / self.hour_height * 4) * 15))
        return day, minutes

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.days:
            return
        point = event.position()
        if self.select_day_at(point) is None:
            return
        self.gesture_modifiers = event.modifiers()
        hit = self.hit(point)
        self.setFocus()
        if hit:
            rect, item = hit
            self.selected_id = item.id
            self.selected.emit(item.id)
            if not item.all_day:
                self.drag = ('resize' if point.y() >= rect.bottom() - 7 else 'move', item, point, self.position(point))
        elif point.x() >= self.gutter and point.y() >= self.all_day_height:
            self.drag = ('create', None, point, self.position(point))
        self.update()

    def mouseMoveEvent(self, event):
        point = event.position()
        self.gesture_modifiers = event.modifiers()
        if not self.drag:
            hit = self.hit(point)
            local = hit[1].start.astimezone(local_zone()) if hit else None
            day = self.day_at(point)
            if hit:
                self.setToolTip(hit[1].title + '\n' + self.preferences.format_date(local, weekday=True) + ' · '
                                + ('All day' if hit[1].all_day else self.preferences.format_time(local)) + '\n'
                                + hit[1].location + '\nDouble-click to create a new event.')
            elif day is not None:
                self.setToolTip('Double-click to add an all-day event.' if point.y() < self.all_day_height else
                                'Click to select this day. Double-click to add an event, or drag to choose its duration.')
            else:
                self.setToolTip('')
            self.setCursor(Qt.CursorShape.SizeVerCursor if hit and point.y() > hit[0].bottom() - 7 and not hit[1].all_day else Qt.CursorShape.ArrowCursor)
            return
        if (point - self.drag[2]).manhattanLength() < 5:
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
            duration = (item.end.timestamp() - item.start.timestamp()) / 60
            start_min = min(max(0, local.hour * 60 + local.minute + minute - old_min), max(0, 1440 - duration))
            self.ghost = (day, start_min, start_min + duration)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        drag, ghost = self.drag, self.ghost
        self.drag = self.ghost = None
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
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.drag = self.ghost = None
        point = event.position()
        day = self.select_day_at(point)
        if day is None:
            return
        self.setFocus()
        if point.y() < self.all_day_height:
            self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))
        else:
            _, minute = self.position(point)
            start = at(day) + timedelta(minutes=minute)
            self.create_requested.emit(start, start + timedelta(hours=1))
        self.update()

    def contextMenuEvent(self, event):
        if self.select_day_at(event.pos()) is not None:
            self.setFocus()
        hit = self.hit(event.pos())
        if hit:
            self.selected_id = hit[1].id
            self.selected.emit(hit[1].id)
            self.update()
            self.context_requested.emit(hit[1].id, event.globalPos())


class MonthCanvas(QWidget):
    create_requested = Signal(object, object)
    create_all_day_requested = Signal(object, object)
    day_selected = Signal(object)
    open_day = Signal(object)
    edit_requested = Signal(str)
    edit_scoped_requested = Signal(str, object)
    selected = Signal(str)
    context_requested = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.start = now().date()
        self.month = self.start.month
        self.events, self.calendars, self.hits = [], {}, []
        self.selected_id = None
        self.selected_day = None
        self.preferences = Preferences()
        self.setMinimumHeight(440)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.update()

    def set_selected_day(self, day):
        self.selected_day = day
        self.update()

    def set_data(self, start, month, events, calendars):
        self.start, self.month, self.events = start, month, events
        self.calendars = {c.id: c for c in calendars}
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), BG)
        self.hits = []
        w, h = self.width() / 7, (self.height() - 28) / 6
        for i in range(7):
            name = self.preferences.weekday_name(self.start + timedelta(days=i)).upper()
            text(p, QRectF(i * w + 12, 0, w - 15, 25), name, MUTED, 8, True)
        for i in range(42):
            day = self.start + timedelta(days=i)
            x, y = (i % 7) * w, 28 + (i // 7) * h
            rect = QRectF(x, y, w, h)
            if day.month != self.month:
                p.fillRect(rect, QColor('#f0f3ec'))
            if day == now().date():
                p.fillRect(rect, QColor('#edf2e7'))
            if day == self.selected_day:
                p.fillRect(rect, SELECTED_DAY)
            p.setPen(QPen(LINE, 1))
            # Event pills set a fill brush. Reset it before drawing the next
            # day border, otherwise that calendar's tint fills every later day.
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
            if day == self.selected_day:
                p.setPen(QPen(SELECTED_DAY_BORDER, 1.5))
                p.drawRect(rect.adjusted(1, 1, -1, -1))
            text(p, QRectF(x + 10, y + 3, w - 18, 25), str(day.day), GREEN if day == now().date() else INK, 10, True)
            text(p, QRectF(x + 32, y + 5, w - 40, 20), '+ all day' if w >= 90 else '+', MUTED, 7,
                 align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            daily = [e for e in self.events if overlaps(e, at(day), at(day + timedelta(days=1)))]
            capacity = max(0, int((h - 37) / 24))
            visible = daily[:capacity if len(daily) <= capacity else max(0, capacity - 1)]
            selected = next((item for item in daily if item.id == self.selected_id), None)
            if selected is not None and selected not in visible and capacity:
                if visible:
                    visible[-1] = selected
                else:
                    visible = [selected]
            for n, item in enumerate(visible):
                er = QRectF(x + 5, y + 32 + n * 24, w - 10, 21)
                color = QColor(self.calendars[item.calendar_id].color)
                selected = item.id == self.selected_id
                p.setPen(QPen(SELECTED_EVENT_BORDER, 3) if selected else Qt.PenStyle.NoPen)
                fill = QColor(SELECTED_EVENT if selected else color)
                if not selected:
                    fill.setAlpha(40)
                p.setBrush(fill)
                p.drawRoundedRect(er, 3, 3)
                label = ('' if item.all_day else self.preferences.format_time(item.start.astimezone(local_zone())) + ' ') + item.title
                if selected:
                    p.fillRect(QRectF(er.left() + 3, er.top() + 4, 3, er.height() - 8), SELECTED_EVENT_BORDER)
                text(p, er.adjusted(8 if selected else 5, 0, -3, 0), label, SELECTED_EVENT_BORDER if selected else color.darker(140), 8, selected)
                self.hits.append((er, item))
            if len(daily) > len(visible) and len(visible) < capacity:
                text(p, QRectF(x + 10, y + 32 + len(visible) * 24, w - 15, 21), f'+{len(daily) - len(visible)} more', MUTED, 8)
        p.end()

    def day_at(self, point):
        if point.y() < 28 or not self.rect().contains(point.toPoint() if hasattr(point, 'toPoint') else point):
            return None
        index = min(5, max(0, int((point.y() - 28) / ((self.height() - 28) / 6)))) * 7 + min(6, max(0, int(point.x() / (self.width() / 7))))
        return self.start + timedelta(days=index)

    def select_day_at(self, point):
        day = self.day_at(point)
        if day is not None:
            self.set_selected_day(day)
            self.day_selected.emit(day)
        return day

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.select_day_at(event.position()) is None:
            return
        self.setFocus()
        hit = next(((r, e) for r, e in self.hits if r.contains(event.position())), None)
        if hit:
            self.selected_id = hit[1].id
            self.selected.emit(hit[1].id)
            self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position()
        day = self.select_day_at(point)
        if day is None:
            return
        self.setFocus()
        height = (self.height() - 28) / 6
        if (point.y() - 28) % height < 30:
            self.create_all_day_requested.emit(at(day), at(day + timedelta(days=1)))
        else:
            start = at(day, 9)
            self.create_requested.emit(start, start + timedelta(hours=1))

    def mouseMoveEvent(self, event):
        point = event.position()
        day = self.day_at(point)
        if day is None:
            self.setToolTip('')
            return
        height = (self.height() - 28) / 6
        all_day = (point.y() - 28) % height < 30
        self.setToolTip(self.preferences.format_date(day, weekday=True) + '\n'
                        + ('Double-click the day heading to create an all-day event.' if all_day else
                           'Click to select this day. Double-click to create a new event at 09:00.'))
        self.setCursor(Qt.CursorShape.PointingHandCursor if all_day else Qt.CursorShape.ArrowCursor)

    def contextMenuEvent(self, event):
        if self.select_day_at(event.pos()) is not None:
            self.setFocus()
        hit = next(((r, e) for r, e in self.hits if r.contains(event.pos())), None)
        if hit:
            self.selected_id = hit[1].id
            self.selected.emit(hit[1].id)
            self.update()
            self.context_requested.emit(hit[1].id, event.globalPos())
