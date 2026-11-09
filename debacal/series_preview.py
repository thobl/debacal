"""Visible-month occurrence preview with calendar and series navigation."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from .calendar_appearance import weekend_fill, weekend_text
from .calendar_widgets import WEEKDAY_SIZE, display_scale, text
from .event_style import event_colors
from .models import Event
from .preferences import Preferences
from .preview_navigation import PreviewNavigation
from .preview_controls import PreviewControls
from .recurrence import expand_events, key_datetime, occurrence, recurrence_key
from .theme import corner_radius, scheme_for
from .timeutil import local_zone, now


def _start(event):
    return event.start if event.all_day or event.floating_time else event.start.astimezone(local_zone())


def _end(event):
    return event.end if event.all_day or event.floating_time else event.end.astimezone(local_zone())


class SeriesPreview(QWidget):
    """Expand only the visible month and request edits from the owning editor.

    Primary event fields remain independent of preview selection. Occurrence
    navigation is resolved on demand, never by expanding an infinite schedule.
    """
    date_toggled = Signal(object)
    date_clicked = Signal(object)
    occurrence_selected = Signal(object)
    occurrence_edit_requested = Signal(object)
    occurrence_remove_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.preferences = Preferences()
        self.masters, self.visible_events = [], []
        self.calendar_colors = {}
        self.current_month = now().date().replace(day=1)
        self.grid_start = self.preferences.week_start(self.current_month)
        self.date_editing = False
        self.error = ''
        self.total_occurrences = None
        self.anchor_dates, self.added_dates, self.excluded_dates = set(), set(), set()
        self._primary = self._selected = None
        self._following_primary = True
        self._explicit_primary = self._initialized = self._laying_out = False
        self._by_day, self._week_heights = {}, []
        self._navigation = PreviewNavigation([])
        self._navigation_failure = ''
        self._reveal_id = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_selected)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)
        self.controls = PreviewControls(self.preferences, self)
        for name in ('previous_button', 'today_button', 'next_button', 'month_label', 'first_button',
                     'previous_occurrence_button', 'next_occurrence_button', 'last_button', 'position_label'):
            setattr(self, name, getattr(self.controls, name))
        self.controls.previous_month_requested.connect(lambda: self.navigate_month(-1))
        self.controls.next_month_requested.connect(lambda: self.navigate_month(1))
        self.controls.today_requested.connect(self.show_today)
        self.controls.first_requested.connect(self.show_first_date)
        self.controls.previous_requested.connect(lambda: self.navigate_occurrence(-1))
        self.controls.next_requested.connect(lambda: self.navigate_occurrence(1))
        self.controls.last_requested.connect(self.show_last_date)
        self.first_button.setToolTip('Primary event')
        self.first_button.setAccessibleName('Primary event')
        outer.addWidget(self.controls.month_bar)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._reserve_scrollbar)
        self.scroll.viewport().installEventFilter(self)
        self.grid = _PreviewGrid(self)
        self.scroll.setWidget(self.grid)
        outer.addWidget(self.scroll, 1)
        outer.addWidget(self.controls.occurrence_bar)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setProperty('error', True)
        self.error_label.hide()
        outer.addWidget(self.error_label)
        self.setMinimumHeight(310)
        self._rebuild()

    @property
    def primary(self):
        return self._primary

    @property
    def selected_event(self):
        return self._selected

    @property
    def selected_id(self):
        return self._selected.id if self._selected is not None else None

    def set_primary(self, event):
        followed_primary = self._following_primary or self._selected is None or self.is_primary(self._selected)
        self._explicit_primary = event is not None
        self._primary = event
        self.anchor_dates = {_start(event).date()} if event else set()
        if followed_primary:
            self._selected = self._default_selection()
        self._following_primary = followed_primary
        self._update_navigation()
        self.grid.update()

    def set_events(self, masters_or_events, preferences=None, calendars=None):
        previous = self._selected
        followed_primary = self._following_primary or previous is None or self.is_primary(previous)
        self.masters = list(masters_or_events)
        if preferences is not None:
            self.preferences = preferences
        if calendars is not None:
            self.calendar_colors = {calendar.id: calendar.color for calendar in calendars}
        if not self._explicit_primary:
            self._primary = self.masters[0] if self.masters else None
        if self.masters and not self._initialized:
            self.current_month = min(_start(event).date() for event in self.masters).replace(day=1)
            self._initialized = True
        self._navigation.rebind(self.masters)
        self._navigation_failure = ''
        self._rebuild()
        if previous:
            current = next((item for item in self.visible_events if item.id == previous.id), None)
            if current is None:
                current = next((item for item in self.visible_events
                                if item.start.timestamp() == previous.start.timestamp()
                                and item.calendar_id == previous.calendar_id), None)
            self._selected = current or self._default_selection()
        else:
            self._selected = self._default_selection()
        self._following_primary = followed_primary
        self._update_navigation()

    def enable_date_editing(self, enabled):
        self.date_editing = bool(enabled)
        self.grid.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.grid.update()

    def set_selected_occurrence(self, event_or_id):
        if isinstance(event_or_id, Event):
            self._selected = event_or_id
        elif event_or_id is None:
            self._selected = None
        else:
            self._selected = next((item for item in [*self.visible_events, *self.masters]
                                   if item.id == event_or_id), self._selected)
        self._following_primary = self._selected is None or self.is_primary(self._selected)
        self._update_navigation()
        self.grid.update()

    def is_primary(self, event):
        if self._primary is None:
            return False
        primary = self._primary
        root = primary.series_id or primary.id.split('::occurrence::', 1)[0]
        item_root = event.series_id or event.id.split('::occurrence::', 1)[0]
        same_source = item_root == root
        if not same_source and len(self.masters) == 1:
            same_source = item_root == self.masters[0].id and event.calendar_id == primary.calendar_id
        same_start = (_start(event).date() == _start(primary).date() if primary.all_day and event.all_day
                      else event.start.timestamp() == primary.start.timestamp())
        master = next((item for item in self.masters if item.id == item_root), None)
        same_nominal = (master is None or not master.recurring
                        or event.recurrence_id == self._primary_key(master))
        return same_source and same_start and same_nominal

    def _primary_key(self, master):
        """Resolve the prototype identity even when a rule starts after its RDATE."""
        first = recurrence_key(master.start, master.all_day, floating_time=master.floating_time)
        if self._primary is None:
            return first
        actual = self._primary.start.timestamp()
        key = self._primary.recurrence_id
        if key and (key in master.recurrence_dates or key in master.recurrence_overrides):
            candidate = occurrence(master, key_datetime(key, master), key)
            if candidate.start.timestamp() == actual:
                return key
        return first

    def _primary_occurrence(self):
        if self._primary is None:
            return None
        current = next((event for event in self.visible_events if self.is_primary(event)), None)
        if current:
            return current
        for master in self.masters:
            key = self._primary_key(master) if master.recurring else ''
            if key in master.excluded_dates:
                continue
            candidate = occurrence(master, key_datetime(key, master), key) if key else master
            if self.is_primary(candidate):
                return candidate
        return None

    def _default_selection(self):
        primary = self._primary_occurrence()
        if primary is not None:
            return primary
        try:
            first = self._navigation.first()
            if first is None:
                self.total_occurrences = 0
            return first
        except (ValueError, OverflowError) as error:
            self._navigation_failure = str(error)
            return None

    def navigate_month(self, delta):
        index = self.current_month.year * 12 + self.current_month.month - 1 + delta
        year, month = divmod(index, 12)
        if 1 <= year <= 9999:
            self.show_month(date(year, month + 1, 1))

    def show_month(self, day):
        self.current_month = day.replace(day=1)
        self._initialized = True
        self._rebuild()

    def show_today(self):
        self.show_month(now().date())

    def show_first_date(self):
        if self._primary is not None:
            self.show_month(_start(self._primary).date())
            self._activate(self._primary_occurrence())

    def show_event(self, event_or_id):
        event = event_or_id if isinstance(event_or_id, Event) else next(
            (item for item in [*self.visible_events, *self.masters] if item.id == event_or_id), self._selected)
        if event is not None:
            self.show_month(_start(event).date())
            self._reveal_id = event.id
            self._reveal_timer.start(0)

    def _reveal_selected(self):
        event = next((item for item in self.visible_events if item.id == self._reveal_id), None)
        if event is not None:
            rect = next((rect for rect, item, _remove in self.grid.chip_rects(_start(event).date()) if item.id == event.id),
                        self.grid.cell_rect(_start(event).date()))
            self.scroll.ensureVisible(round(rect.center().x()), round(rect.center().y()), 0, round(rect.height() / 2 + 5))

    def _activate(self, event):
        if event is not None:
            self.set_selected_occurrence(event)
            self.show_event(event)
            self.occurrence_selected.emit(event)

    def show_last_date(self):
        if not self._navigation.bounded or not self.masters:
            return
        try:
            self._activate(self._navigation.last())
        except (ValueError, OverflowError) as error:
            self._navigation_failure = str(error)
            self.last_button.setToolTip(str(error))
            self.last_button.setEnabled(False)

    def navigate_occurrence(self, direction):
        reference = self._selected or self._default_selection()
        if reference is not None:
            try:
                self._activate(self._navigation.adjacent(reference, direction))
            except (ValueError, OverflowError) as error:
                button = self.next_occurrence_button if direction > 0 else self.previous_occurrence_button
                button.setToolTip(str(error))

    def events_on(self, day):
        return self._by_day.get(day, [])

    def _rebuild(self):
        ordinal = self.current_month.toordinal() - (self.current_month.weekday() - self.preferences.first_weekday) % 7
        self.grid_start = date.fromordinal(max(1, ordinal))
        self.controls.set_preferences(self.preferences)
        self.controls.set_month(self.current_month)
        self.error, self.total_occurrences = '', None
        self.anchor_dates = {_start(self._primary).date()} if self._primary else set()
        self.added_dates, self.excluded_dates = set(), set()
        try:
            for master in self.masters:
                for key in master.recurrence_dates:
                    value = key_datetime(key, master)
                    self.added_dates.add(value.date() if master.all_day or master.floating_time else value.astimezone(local_zone()).date())
                for key in master.excluded_dates:
                    value = key_datetime(key, master)
                    self.excluded_dates.add(value.date() if master.all_day or master.floating_time else value.astimezone(local_zone()).date())
            last = date.fromordinal(min(date.max.toordinal(), self.grid_start.toordinal() + 42))
            begin = datetime.combine(self.grid_start, time.min, local_zone())
            end = datetime.combine(last, time.max if last == date.max else time.min, local_zone())
            self.visible_events = expand_events(self.masters, begin, end, limit=2000)
            self.total_occurrences = self._navigation.explicit_count()
        except (ValueError, OverflowError) as error:
            self.visible_events = []
            self.error = str(error)
        self._by_day = {}
        for item in self.visible_events:
            begin, finish = _start(item), _end(item)
            last_day = finish.date() if finish.timestamp() == begin.timestamp() else (finish - timedelta(microseconds=1)).date()
            first = max(self.grid_start.toordinal(), begin.date().toordinal())
            last = min(date.max.toordinal(), self.grid_start.toordinal() + 41, last_day.toordinal())
            for ordinal in range(first, last + 1):
                self._by_day.setdefault(date.fromordinal(ordinal), []).append(item)
        self.error_label.setText('Preview unavailable: ' + self.error if self.error else '')
        self.error_label.setVisible(bool(self.error))
        self.previous_button.setEnabled(self.current_month > date.min)
        self.next_button.setEnabled(self.current_month < date(9999, 12, 1))
        self._layout_grid()
        self._reserve_scrollbar(self.scroll.verticalScrollBar().minimum(), self.scroll.verticalScrollBar().maximum())
        self._update_navigation()
        self.grid.update()

    def _update_navigation(self):
        selected = self._selected or self._primary_occurrence()
        ready = bool(self.masters) and not self.error
        self.first_button.setEnabled(ready)
        if not ready:
            self.last_button.setEnabled(False)
            self.previous_occurrence_button.setEnabled(False)
            self.next_occurrence_button.setEnabled(False)
            self.controls.set_position(None)
            return
        self.last_button.setEnabled(ready and selected is not None and self._navigation.bounded and not self._navigation_failure)
        self.last_button.setToolTip(self._navigation_failure or ('Last occurrence' if self._navigation.bounded else 'This series has no last occurrence.'))
        self.previous_occurrence_button.setEnabled(ready and selected is not None)
        self.next_occurrence_button.setEnabled(ready and selected is not None)
        index, total = self._navigation.position(selected) if selected else (None, self.total_occurrences)
        if total is not None:
            self.total_occurrences = total
        self.controls.set_position(selected, index, total, unbounded=not self._navigation.bounded,
                                   display_zone=local_zone())
        if total is not None and total <= 1:
            self.previous_occurrence_button.setEnabled(False)
            self.next_occurrence_button.setEnabled(False)
        elif index is not None:
            self.previous_occurrence_button.setEnabled(ready and index > 1)
            if total is not None:
                self.next_occurrence_button.setEnabled(ready and index < total)
        values = self._navigation._finite
        if values is not None and selected:
            index = next((index for index, item in enumerate(values) if item.id == selected.id), -1)
            self.previous_occurrence_button.setEnabled(ready and index > 0)
            self.next_occurrence_button.setEnabled(ready and 0 <= index < len(values) - 1)

    def _layout_grid(self, height=None):
        if self._laying_out or not hasattr(self, 'grid'):
            return
        self._laying_out = True
        try:
            scale = display_scale(self)
            self.grid.header_height = round(23 * scale)
            available = self.scroll.viewport().height() if height is None else height
            base = max(40 * scale, (available - self.grid.header_height) / 6)
            self._week_heights = []
            for week in range(6):
                amounts = [len(self.events_on(self._day(week * 7 + day))) for day in range(7)]
                self._week_heights.append(max(base, (21 + 16 * max(amounts, default=0)) * scale))
            self.grid.setFixedHeight(round(self.grid.header_height + sum(self._week_heights)))
        finally:
            self._laying_out = False

    def _day(self, offset):
        ordinal = self.grid_start.toordinal() + offset
        return date.fromordinal(ordinal) if ordinal <= date.max.toordinal() else None

    def _reserve_scrollbar(self, minimum, maximum):
        reserve = max(0, self.scroll.verticalScrollBar().sizeHint().width()) if maximum <= minimum else 0
        self.scroll.setViewportMargins(0, 0, reserve, 0)

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._layout_grid(event.size().height())
            self._reserve_scrollbar(self.scroll.verticalScrollBar().minimum(), self.scroll.verticalScrollBar().maximum())
        return super().eventFilter(watched, event)


class _PreviewGrid(QWidget):
    def __init__(self, preview):
        super().__init__(preview)
        self.preview = preview
        self.header_height = 23
        self.focus_day = preview.current_month
        self.focus_entry = None
        self.keyboard_focus = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('Occurrence preview')

    def cell_rect(self, day):
        if day is None:
            return QRectF()
        offset = (day - self.preview.grid_start).days
        if not 0 <= offset < 42:
            return QRectF()
        week, column = divmod(offset, 7)
        width = self.width() / 7
        return QRectF(column * width, self.header_height + sum(self.preview._week_heights[:week]),
                      width, self.preview._week_heights[week])

    def date_at(self, point):
        if point.y() < self.header_height or point.x() < 0 or point.x() >= self.width():
            return None
        top = self.header_height
        for week, height in enumerate(self.preview._week_heights):
            if top <= point.y() < top + height:
                return self.preview._day(week * 7 + min(6, int(point.x() / (self.width() / 7))))
            top += height
        return None

    def chip_rects(self, day):
        cell = self.cell_rect(day)
        if cell.isEmpty():
            return []
        scale, values = display_scale(self.preview), []
        for index, item in enumerate(self.preview.events_on(day)):
            rect = QRectF(cell.left() + 3 * scale, cell.top() + 21 * scale + index * 16 * scale,
                          cell.width() - 6 * scale, 14 * scale)
            remove = QRectF(rect.right() - 13 * scale, rect.top(), 13 * scale, rect.height()) if self.preview.date_editing else QRectF()
            values.append((rect, item, remove))
        return values

    def paintEvent(self, _):
        preview = self.preview
        colors = scheme_for(preview.preferences)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(colors.background))
        scale = display_scale(preview)
        width = self.width() / 7
        for index in range(7):
            day = preview._day(index)
            if day is not None:
                shade = weekend_fill(day, preview.preferences, colors)
                if shade:
                    painter.fillRect(QRectF(index * width, 0, width, self.header_height), QColor(shade))
                text(painter, QRectF(index * width, 0, width, self.header_height),
                     preview.preferences.weekday_name(day).upper(),
                     QColor(weekend_text(day, preview.preferences, colors, colors.muted)), WEEKDAY_SIZE,
                     True, Qt.AlignmentFlag.AlignCenter)
        for offset in range(42):
            day = preview._day(offset)
            if day is None:
                continue
            rect, events = self.cell_rect(day), preview.events_on(day)
            outside = day.month != preview.current_month.month
            if outside:
                painter.fillRect(rect, QColor(colors.outside_month))
            shade = weekend_fill(day, preview.preferences, colors)
            if shade:
                painter.fillRect(rect, QColor(shade))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors.line), .8 * scale))
            painter.drawRect(rect)
            ordinary = colors.muted if outside else colors.accent if events else colors.ink
            text(painter, QRectF(rect.left() + 5 * scale, rect.top() + scale, rect.width() - 10 * scale, 19 * scale),
                 str(day.day), QColor(weekend_text(day, preview.preferences, colors, ordinary)), 9,
                 day in preview.anchor_dates)
            for card, item, remove in self.chip_rects(day):
                source = preview.calendar_colors.get(item.calendar_id, colors.accent)
                style = event_colors(source, preview, alpha=38 if item.candidate else 52)
                selected = item.id == preview.selected_id
                edge = QColor(style.selected_outline if selected else style.outline)
                painter.setPen(QPen(edge, 2 * scale if selected else scale,
                                     Qt.PenStyle.DashLine if item.candidate else Qt.PenStyle.SolidLine))
                painter.setBrush(QColor(style.fill))
                painter.drawRoundedRect(card, corner_radius(preview.preferences, 3) * scale, corner_radius(preview.preferences, 3) * scale)
                left = 4 * scale
                if preview.is_primary(item) and _start(item).date() == day:
                    painter.setPen(QPen(edge, scale))
                    painter.setBrush(edge)
                    center = QPointF(card.left() + 3 * scale, card.center().y() - scale)
                    painter.drawEllipse(center, 1.5 * scale, 1.5 * scale)
                    painter.drawLine(center, QPointF(center.x(), center.y() + 5 * scale))
                    left = 11 * scale
                begin, end = _start(item), _end(item)
                if item.all_day:
                    caption = '◇'
                elif begin.date() == day:
                    caption = preview.preferences.format_time(begin) + (' →' if end.date() > day else '')
                else:
                    caption = '→ ' + preview.preferences.format_time(end) if end.date() == day else '→'
                content = card.adjusted(left, 0, -14 * scale if not remove.isEmpty() else -2 * scale, 0)
                painter.save()
                painter.setClipRect(content)
                text(painter, content, caption, QColor(style.ink), 7, False,
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                painter.restore()
                if not remove.isEmpty():
                    center, half = remove.center(), 2.5 * scale
                    painter.setPen(QPen(QColor(style.muted), scale))
                    painter.drawLine(QPointF(center.x() - half, center.y() - half), QPointF(center.x() + half, center.y() + half))
                    painter.drawLine(QPointF(center.x() - half, center.y() + half), QPointF(center.x() + half, center.y() - half))
            if self.hasFocus() and self.keyboard_focus and day == self.focus_day:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(colors.focus), scale, Qt.PenStyle.DotLine))
                painter.drawRect(rect.adjusted(2, 2, -2, -2))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.date_at(event.position())
        if day is None:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.keyboard_focus = False
        self.focus_day, self.focus_entry = day, None
        self.preview.date_clicked.emit(day)
        for rect, item, remove in self.chip_rects(day):
            if not remove.isEmpty() and remove.contains(event.position()):
                self.preview.occurrence_remove_requested.emit(item)
                return
            if rect.contains(event.position()):
                self.focus_entry = item
                self.preview._activate(item)
                return
        if self.preview.date_editing:
            self.preview.date_toggled.emit(day)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            day = self.date_at(event.position())
            if day is not None:
                for rect, item, remove in self.chip_rects(day):
                    if rect.contains(event.position()) and not remove.contains(event.position()):
                        self.preview.occurrence_edit_requested.emit(item)
                        return

    def mouseMoveEvent(self, event):
        day = self.date_at(event.position())
        if day is None:
            self.setToolTip('')
            return
        preferences = self.preview.preferences
        for rect, item, remove in self.chip_rects(day):
            if remove.contains(event.position()):
                self.setToolTip('Remove ' + preferences.format_time(_start(item)))
                return
            if rect.contains(event.position()):
                begin, end = _start(item), _end(item)
                when = preferences.format_date(begin.date(), weekday=True)
                if not item.all_day:
                    when += ' · ' + preferences.format_time(begin) + '–' + preferences.format_time(end)
                if end.date() != begin.date():
                    when += ' · ' + preferences.format_date(end.date())
                self.setToolTip(('Primary event\n' if self.preview.is_primary(item) else '') + item.title + '\n' + when)
                return
        self.setToolTip(preferences.format_date(day, weekday=True))

    def keyPressEvent(self, event):
        self.keyboard_focus = True
        shifts = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
        if event.key() in shifts:
            ordinal = self.focus_day.toordinal() + shifts[event.key()]
            if 1 <= ordinal <= date.max.toordinal():
                self.focus_day = date.fromordinal(ordinal)
                self.focus_entry = None
                if self.focus_day.replace(day=1) != self.preview.current_month:
                    self.preview.show_month(self.focus_day)
                rect = self.cell_rect(self.focus_day)
                self.preview.scroll.ensureVisible(round(rect.center().x()), round(rect.center().y()), 0, 20)
                self.update()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.focus_entry:
                self.preview._activate(self.focus_entry)
            elif self.preview.date_editing:
                self.preview.date_toggled.emit(self.focus_day)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.focus_entry:
            if self.preview.date_editing:
                self.preview.occurrence_remove_requested.emit(self.focus_entry)
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event):
        self.keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                                Qt.FocusReason.ShortcutFocusReason)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.update()
        super().focusOutEvent(event)
