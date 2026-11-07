"""Native date and time surfaces for the second event-editor study.

These widgets own only their display and in-progress pointer gestures. The
prototype controller decides how a requested edit changes its temporary events.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from debacal.calendar_appearance import weekend_text
from debacal.calendar_widgets import calendar_gutter, display_scale, text
from debacal.event_style import event_colors, paint_event_card
from debacal.theme import scheme_for
from debacal.timeutil import local_zone


def _local(value):
    return value.astimezone(local_zone())


class MonthPicker(QWidget):
    """A date-only picker that reveals occurrence chips only for a series.

    Public geometry helpers return coordinates local to ``grid``. Use that
    child as the target when exercising these helpers with QTest.
    """
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.preferences = owner.preferences
        initial = owner.selected_day if owner.entries else date.today()
        self.current_month = initial.replace(day=1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self.previous_button = QPushButton('‹')
        self.next_button = QPushButton('›')
        self.previous_button.setToolTip('Previous month')
        self.next_button.setToolTip('Next month')
        self.month_label = QLabel()
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for button in (self.previous_button, self.next_button):
            button.setFixedSize(28, 28)
            button.setAutoDefault(False)
            button.setStyleSheet('padding: 0; border-color: transparent;')
        self.previous_button.clicked.connect(lambda: self.navigate(-1))
        self.next_button.clicked.connect(lambda: self.navigate(1))
        bar.addWidget(self.previous_button)
        bar.addWidget(self.month_label, 1)
        bar.addWidget(self.next_button)
        outer.addLayout(bar)
        self.grid = _MonthGrid(self)
        outer.addWidget(self.grid)
        self.refresh()

    @property
    def grid_start(self):
        return self.preferences.week_start(self.current_month)

    def refresh(self):
        self.preferences = self.owner.preferences
        self.grid.preferences = self.preferences
        scale = self.preferences.font_scale / 100
        if self.owner.series_enabled:
            heights = []
            for week in range(6):
                count = max(len(self.grid.events_on(self.grid_start + timedelta(days=week * 7 + offset)))
                            for offset in range(7))
                heights.append((19 + 16 * max(2, min(6, count))) * scale)
            self.grid.row_heights = heights
        else:
            self.grid.row_heights = [33 * scale] * 6
        self.grid.setFixedHeight(round(self.grid.header_height + sum(self.grid.row_heights)))
        self.month_label.setText(self.preferences.format_month(self.current_month))
        self.previous_button.setEnabled(self.current_month > date(1, 1, 1))
        self.next_button.setEnabled(self.current_month < date(9999, 12, 1))
        self.grid.update()
        self.updateGeometry()

    def show_date(self, day):
        self.current_month = day.replace(day=1)
        self.grid.focus_day = day
        self.refresh()

    def navigate(self, delta):
        value = self.current_month.year * 12 + self.current_month.month - 1 + delta
        year, month = divmod(value, 12)
        if 1 <= year <= 9999:
            self.current_month = date(year, month + 1, 1)
            self.refresh()

    def cell_rect(self, day):
        return self.grid.cell_rect(day)

    def chip_rects(self, day):
        return self.grid.chip_rects(day)


class _MonthGrid(QWidget):
    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.preferences = picker.preferences
        self.focus_day = picker.owner.selected_day if picker.owner.entries else picker.current_month
        self.focus_entry = None
        self._keyboard_focus = False
        self.row_heights = [33 * display_scale(self)] * 6
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('Event date')

    @property
    def owner(self):
        return self.picker.owner

    @property
    def header_height(self):
        return 24 * display_scale(self)

    def cell_rect(self, day):
        index = (day - self.picker.grid_start).days
        if not 0 <= index < 42:
            return QRectF()
        row = index // 7
        width = self.width() / 7
        return QRectF((index % 7) * width, self.header_height + sum(self.row_heights[:row]),
                      width, self.row_heights[row])

    def day_at(self, point):
        if point.y() < self.header_height or not self.rect().contains(point.toPoint()):
            return None
        width = self.width() / 7
        offset = point.y() - self.header_height
        row = 0
        while row < 5 and offset >= self.row_heights[row]:
            offset -= self.row_heights[row]
            row += 1
        index = row * 7 + min(6, int(point.x() / width))
        return self.picker.grid_start + timedelta(days=index)

    def events_on(self, day):
        return sorted((item for item in self.owner.entries if _local(item.start).date() == day),
                      key=lambda item: item.start.timestamp())

    def chip_rects(self, day):
        if not self.owner.series_enabled:
            return []
        cell, scale = self.cell_rect(day), display_scale(self)
        if cell.isEmpty():
            return []
        capacity = max(1, round((cell.height() - 19 * scale) / (16 * scale)))
        result = []
        for index, item in enumerate(self.events_on(day)[:capacity]):
            rect = QRectF(cell.left() + 3 * scale, cell.top() + 19 * scale + index * 16 * scale,
                          cell.width() - 6 * scale, 14 * scale)
            remove = (QRectF(rect.right() - 13 * scale, rect.top(), 13 * scale, rect.height())
                      if item.id != 'first' else QRectF())
            result.append((rect, item, remove))
        return result

    def overflow_rect(self, day):
        if not self.owner.series_enabled or len(self.events_on(day)) <= len(self.chip_rects(day)):
            return QRectF()
        cell, scale = self.cell_rect(day), display_scale(self)
        return QRectF(cell.right() - 35 * scale, cell.top(), 33 * scale, 16 * scale)

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scheme = scheme_for(self.preferences)
        style = event_colors(self.owner.source.color, self.preferences)
        scale = display_scale(self)
        painter.fillRect(self.rect(), QColor(scheme.background))
        width = self.width() / 7
        for index in range(7):
            day = self.picker.grid_start + timedelta(days=index)
            text(painter, QRectF(index * width, 0, width, self.header_height),
                 self.preferences.weekday_name(day).upper(),
                 QColor(weekend_text(day, self.preferences, scheme, scheme.muted)),
                 8, True, Qt.AlignmentFlag.AlignCenter)
        for index in range(42):
            day = self.picker.grid_start + timedelta(days=index)
            rect = self.cell_rect(day)
            outside = day.month != self.picker.current_month.month
            items = self.events_on(day) if self.owner.series_enabled else []
            selected = day == self.owner.selected_day
            if self.owner.series_enabled:
                if items:
                    painter.fillRect(rect.adjusted(1, 1, -1, -1), QColor(style.fill))
                painter.setPen(QPen(QColor(scheme.line), .7))
                painter.drawLine(rect.bottomLeft(), rect.bottomRight())
                number_rect = QRectF(rect.left() + 5 * scale, rect.top(), 25 * scale, 17 * scale)
                ink = scheme.muted if outside else scheme.ink
                text(painter, number_rect, str(day.day),
                     QColor(weekend_text(day, self.preferences, scheme, ink)), 8, selected)
                chips = self.chip_rects(day)
                for chip, item, remove in chips:
                    active = item.id == self.owner.selected_id
                    paint_event_card(painter, chip, style, selected=active, scale=scale, radius=2 * scale)
                    left = 5 * scale
                    if item.id == 'first':
                        painter.setBrush(QColor(style.outline))
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.drawEllipse(QPointF(chip.left() + 4 * scale, chip.center().y()), 1.5 * scale, 1.5 * scale)
                        left = 8 * scale
                    label_rect = chip.adjusted(left, 0, -15 * scale if not remove.isEmpty() else -2 * scale, 0)
                    painter.save()
                    painter.setClipRect(label_rect)
                    text(painter, label_rect, self.preferences.format_time(_local(item.start)), QColor(style.ink), 6.5, active)
                    painter.restore()
                    if not remove.isEmpty():
                        center, half = remove.center(), 2.5 * scale
                        painter.setPen(QPen(QColor(style.muted), max(.9, scale)))
                        painter.drawLine(QPointF(center.x() - half, center.y() - half), QPointF(center.x() + half, center.y() + half))
                        painter.drawLine(QPointF(center.x() - half, center.y() + half), QPointF(center.x() + half, center.y() - half))
                overflow = self.overflow_rect(day)
                if not overflow.isEmpty():
                    text(painter, overflow, str(len(items) - len(chips)) + ' more', QColor(scheme.muted), 6.5,
                         align=Qt.AlignmentFlag.AlignRight)
            else:
                number_rect = QRectF(rect.center().x() - 13 * scale, rect.center().y() - 13 * scale,
                                     26 * scale, 26 * scale)
                if selected:
                    painter.setPen(QPen(QColor(style.outline), scale))
                    painter.setBrush(QColor(style.fill))
                    painter.drawRoundedRect(number_rect, 4 * scale, 4 * scale)
                ink = scheme.muted if outside else scheme.ink
                text(painter, number_rect, str(day.day),
                     QColor(weekend_text(day, self.preferences, scheme, ink)), 10, selected,
                     Qt.AlignmentFlag.AlignCenter)
            if self.hasFocus() and self._keyboard_focus and day == self.focus_day:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(scheme.focus), scale, Qt.PenStyle.DotLine))
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.day_at(event.position())
        if day is None:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._keyboard_focus = False
        self.focus_day, self.focus_entry = day, None
        for rect, item, remove in self.chip_rects(day):
            if not remove.isEmpty() and remove.contains(event.position()):
                self.owner.remove_entry(item.id)
                return
            if rect.contains(event.position()):
                self.focus_entry = item.id
                self.owner.select_entry(item.id)
                self.update()
                return
        if self.overflow_rect(day).contains(event.position()):
            menu = QMenu(self)
            visible = len(self.chip_rects(day))
            for item in self.events_on(day)[visible:]:
                caption = self.preferences.format_time(_local(item.start))
                if item.id == 'first':
                    action = menu.addAction(caption + ' · First event')
                    action.triggered.connect(lambda _=False: self.owner.select_entry('first'))
                else:
                    action = menu.addAction('Remove ' + caption)
                    action.triggered.connect(lambda _=False, identifier=item.id: self.owner.remove_entry(identifier))
            menu.exec(event.globalPosition().toPoint())
            return
        self.owner.date_clicked(day)

    def mouseMoveEvent(self, event):
        day = self.day_at(event.position())
        if day is None:
            self.setToolTip('')
            return
        lines = [self.preferences.format_date(day, weekday=True)]
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        for rect, item, remove in self.chip_rects(day):
            if remove.contains(event.position()):
                self.setToolTip('Remove ' + self.preferences.format_time(_local(item.start)))
                return
            if rect.contains(event.position()):
                self.setToolTip(self.preferences.format_time(_local(item.start)) + '–' +
                                self.preferences.format_time(_local(item.end)))
                return
        for item in self.events_on(day):
            lines.append(self.preferences.format_time(_local(item.start)))
        self.setToolTip('\n'.join(lines))

    def keyPressEvent(self, event):
        self._keyboard_focus = True
        shifts = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
        if event.key() in shifts:
            try:
                self.focus_day += timedelta(days=shifts[event.key()])
            except OverflowError:
                return
            self.focus_entry = None
            if self.focus_day.month != self.picker.current_month.month or self.focus_day.year != self.picker.current_month.year:
                self.picker.show_date(self.focus_day)
            self.update()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.focus_entry:
                self.owner.select_entry(self.focus_entry)
            else:
                self.owner.date_clicked(self.focus_day)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.focus_entry and self.focus_entry != 'first':
            self.owner.remove_entry(self.focus_entry)
            self.focus_entry = None
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                                   Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()


class TimeStrip(QWidget):
    """One narrow, scroll-free time axis with a duration band and direct gestures.

    ``band_rects`` returns ``(rectangle, Event, resize_handle)`` triples in this
    widget's coordinates. Empty handles identify other occurrences. ``y_for``
    and ``minute_at`` expose the pointer mapping for native renderer checks.
    """
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.preferences = owner.preferences
        self.gutter = calendar_gutter(self.preferences)
        self.first_minute, self.last_minute = 8 * 60, 18 * 60
        self._drag = None
        self._draft = None
        self._hover = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(260)
        self.setAccessibleName('Starting time and duration')
        self.refresh()

    def sizeHint(self):
        scale = self.preferences.font_scale / 100
        return QSize(round(150 * scale), round(300 * scale))

    def minimumSizeHint(self):
        return QSize(round(130 * self.preferences.font_scale / 100), 240)

    def visible_entries(self):
        if not self.owner.entries:
            return []
        selected = self.owner.selected
        if not self.owner.series_enabled:
            return [selected]
        return [item for item in self.owner.entries if _local(item.start).date() == self.owner.selected_day]

    def refresh(self):
        self.preferences = self.owner.preferences
        self.gutter = calendar_gutter(self.preferences)
        if not self._drag:
            first, last = self.preferences.visible_start_hour, self.preferences.visible_end_hour
            for item in self.visible_entries():
                begin, finish = _local(item.start), _local(item.end)
                first = min(first, begin.hour)
                finish_minute = (finish.replace(tzinfo=None) - datetime.combine(begin.date(), time.min)).total_seconds() / 60
                last = max(last, min(24, math.ceil(finish_minute / 60)))
            self.first_minute, self.last_minute = first * 60, max(first + 1, last) * 60
        self.updateGeometry()
        self.update()

    def y_for(self, minute):
        if isinstance(minute, datetime):
            local = _local(minute)
            minute = local.hour * 60 + local.minute + local.second / 60
        return 10 + (minute - self.first_minute) / max(1, self.last_minute - self.first_minute) * max(1, self.height() - 20)

    minute_y = y_for

    def minute_at(self, point, *, endpoint=False):
        y = point.y() if hasattr(point, 'y') else point
        minute = self.first_minute + (y - 10) / max(1, self.height() - 20) * (self.last_minute - self.first_minute)
        latest = self.last_minute if endpoint else self.last_minute - 15
        return max(self.first_minute, min(latest, round(minute / 15) * 15))

    def time_at(self, minute):
        if hasattr(minute, 'y'):
            minute = self.minute_at(minute)
        zone = self.owner.selected.start.tzinfo if self.owner.entries else local_zone()
        return datetime.combine(self.owner.selected_day, time.min, zone) + timedelta(minutes=minute)

    def _rect_for(self, start, end):
        midnight = datetime.combine(self.owner.selected_day, time.min)
        begin = (_local(start).replace(tzinfo=None) - midnight).total_seconds() / 60
        finish = (_local(end).replace(tzinfo=None) - midnight).total_seconds() / 60
        top, bottom = self.y_for(max(self.first_minute, begin)), self.y_for(min(self.last_minute, finish))
        if bottom < 10 or top > self.height() - 10:
            return QRectF()
        scale = display_scale(self)
        return QRectF(self.gutter + 14 * scale, max(10, top),
                      min(36 * scale, max(25, self.width() - self.gutter - 24 * scale)), max(18, bottom - top))

    def band_rects(self):
        result = []
        items = sorted(self.visible_entries(), key=lambda item: item.id == self.owner.selected_id)
        for item in items:
            rect = self._rect_for(item.start, item.end)
            if rect.isEmpty():
                continue
            handle = (QRectF(rect.left(), rect.bottom() - 7, rect.width(), 7)
                      if item.id == self.owner.selected_id else QRectF())
            result.append((rect, item, handle))
        return result

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = display_scale(self)
        scheme = scheme_for(self.preferences)
        painter.fillRect(self.rect(), QColor(scheme.background))
        painter.setPen(QPen(QColor(scheme.line), scale))
        painter.drawLine(QPointF(self.gutter, 10), QPointF(self.gutter, self.height() - 10))
        step = 60 if self.height() / max(1, (self.last_minute - self.first_minute) / 60) >= 20 else 120
        for minute in range(self.first_minute, self.last_minute + 1, 30):
            y = self.y_for(minute)
            painter.setPen(QPen(QColor(scheme.line), scale))
            painter.drawLine(QPointF(self.gutter - (5 if minute % 60 == 0 else 3), y), QPointF(self.gutter, y))
            if minute % step == 0:
                value = self.time_at(minute)
                text(painter, QRectF(0, y - 9 * scale, self.gutter - 8, 18 * scale),
                     self.preferences.format_time(value), QColor(scheme.muted), 7.5,
                     align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        style = event_colors(self.owner.source.color, self.preferences)
        for rect, item, handle in self.band_rects():
            if self._draft and item.id == self.owner.selected_id:
                continue
            selected = item.id == self.owner.selected_id
            painter.save()
            if not selected:
                painter.setOpacity(.36)
            painter.setPen(QPen(QColor(style.outline), 1.3 * scale if selected else scale))
            painter.setBrush(QColor(style.fill))
            painter.drawRoundedRect(rect, 3 * scale, 3 * scale)
            if selected:
                painter.setPen(QPen(QColor(style.outline), scale))
                painter.drawLine(QPointF(handle.center().x() - 7 * scale, handle.center().y()),
                                 QPointF(handle.center().x() + 7 * scale, handle.center().y()))
            painter.restore()
        if self._draft:
            rect = self._rect_for(*self._draft)
            painter.setPen(QPen(QColor(style.outline), 1.5 * scale))
            painter.setBrush(QColor(style.fill))
            painter.drawRoundedRect(rect, 3 * scale, 3 * scale)
        if self._hover is not None and not self._drag:
            y = self.y_for(self._hover)
            painter.setPen(QPen(QColor(scheme.muted), .7 * scale, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(self.gutter + 2, y), QPointF(self.width() - 5, y))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        point = event.position()
        self._hover = None
        minute = self.minute_at(point, endpoint=True)
        for rect, item, handle in reversed(self.band_rects()):
            if rect.contains(point):
                if item.id != self.owner.selected_id:
                    self.owner.select_entry(item.id)
                    handle = QRectF()
                kind = 'resize' if not handle.isEmpty() and handle.contains(point) else 'move'
                self._drag = kind, item.id, point, minute, item.start, item.end
                return
        start = self.time_at(self.minute_at(point))
        self._drag = 'create', None, point, self.minute_at(point), start, start + timedelta(minutes=15)

    def mouseMoveEvent(self, event):
        point = event.position()
        if not self._drag:
            self._hover = self.minute_at(point)
            self.setToolTip(self.preferences.format_time(self.time_at(self._hover)))
            self.setCursor(Qt.CursorShape.CrossCursor)
            for rect, item, handle in reversed(self.band_rects()):
                if rect.contains(point):
                    self._hover = None
                    self.setCursor(Qt.CursorShape.SizeVerCursor if handle.contains(point) else Qt.CursorShape.OpenHandCursor)
                    self.setToolTip(self.preferences.format_time(_local(item.start)) + '–' + self.preferences.format_time(_local(item.end)))
                    break
            self.update()
            return
        kind, identifier, origin, previous, start, end = self._drag
        if (point - origin).manhattanLength() < 4:
            return
        minute = self.minute_at(point, endpoint=True)
        if kind == 'create':
            low, high = min(previous, minute), max(previous, minute)
            self._draft = self.time_at(low), self.time_at(max(low + 15, high))
        elif kind == 'resize':
            self._draft = start, max(start + timedelta(minutes=15), self.time_at(minute))
        else:
            distance = (point.y() - origin.y()) / max(1, self.height() - 20) * (self.last_minute - self.first_minute)
            delta = timedelta(minutes=round(distance / 15) * 15)
            self._draft = start + delta, end + delta
        begin, finish = self._draft
        self.setToolTip(self.preferences.format_time(_local(begin)) + '–' + self.preferences.format_time(_local(finish)))
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        drag, draft = self._drag, self._draft
        self._drag = self._draft = None
        kind, identifier, _origin, minute, _start, _end = drag
        if draft:
            if kind == 'create':
                self.owner.interval_chosen(*draft)
            else:
                self.owner.move_entry(identifier, *draft)
        elif kind == 'create':
            self.owner.time_clicked(self.time_at(minute))
        self.refresh()

    def leaveEvent(self, event):
        if not self._drag:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            amount = 60 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 15
            shift = timedelta(minutes=amount * (-1 if event.key() == Qt.Key.Key_Up else 1))
            selected = self.owner.selected
            self.owner.move_entry(selected.id, selected.start + shift, selected.end + shift)
            event.accept()
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.owner.selected_id != 'first':
            self.owner.remove_entry(self.owner.selected_id)
            event.accept()
        else:
            super().keyPressEvent(event)
