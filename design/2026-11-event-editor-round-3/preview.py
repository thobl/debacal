"""Compact occurrence preview for the third native event-editor study.

The owner supplies concrete temporary events. Preview gestures request edits;
they never change the primary event or the owner's values on their own.
"""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from debacal.calendar_appearance import weekend_fill, weekend_text
from debacal.calendar_widgets import WEEKDAY_SIZE, display_scale, text
from debacal.event_style import event_colors, paint_event_card
from debacal.icons import icon
from debacal.theme import scheme_for
from debacal.timeutil import local_zone


def _local_start(item):
    return item.start if item.all_day else item.start.astimezone(local_zone())


def _local_end(item):
    return item.end if item.all_day else item.end.astimezone(local_zone())


def _first_icon(color):
    """Use a navigation glyph for the primary jump, distinct from Today."""
    pixels = QPixmap(40, 40)
    pixels.setDevicePixelRatio(2)
    pixels.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixels)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QPointF(5, 5), QPointF(5, 15))
    painter.drawLine(QPointF(13, 5), QPointF(8, 10))
    painter.drawLine(QPointF(8, 10), QPointF(13, 15))
    painter.end()
    return QIcon(pixels)


class EventPreview(QWidget):
    """A fixed-height month viewport with separate occurrence navigation.

    ``refresh`` retains the displayed month. ``show_event`` reveals an event
    without asking the owner to select it again, so selection callbacks can use
    that method safely. All geometry on ``grid`` is local to that child widget.
    """
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.preferences = owner.preferences
        self.current_month = (_local_start(owner.primary).date() if owner.entries else date.today()).replace(day=1)
        self._by_day = {}
        self._week_heights = []
        self._pending_reveal = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_pending)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(335)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(5)

        months = QHBoxLayout()
        months.setContentsMargins(0, 0, 0, 0)
        months.setSpacing(4)
        self.month_previous_button = self._button('previous', 'Previous month', lambda: self.navigate_month(-1))
        self.month_next_button = self._button('next', 'Next month', lambda: self.navigate_month(1))
        self.month_label = QLabel()
        self.month_label.setObjectName('sidebarSectionHeading')
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        months.addWidget(self.month_previous_button)
        months.addWidget(self.month_label, 1)
        months.addWidget(self.month_next_button)
        outer.addLayout(months)

        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.verticalScrollBar().rangeChanged.connect(self._reserve_scrollbar)
        self.grid = _OccurrenceMonth(self)
        self.scroll.setWidget(self.grid)
        outer.addWidget(self.scroll)

        navigation = QHBoxLayout()
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.setSpacing(4)
        self.first_button = self._button('first', 'Primary event', self.show_primary)
        self.previous_occurrence_button = self._button('previous', 'Previous occurrence', lambda: self.navigate_occurrence(-1))
        self.next_occurrence_button = self._button('next', 'Next occurrence', lambda: self.navigate_occurrence(1))
        self.position_label = QLabel()
        self.position_label.setProperty('muted', True)
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_label.setAccessibleName('Current occurrence and total count')
        navigation.addWidget(self.first_button)
        navigation.addWidget(self.previous_occurrence_button)
        navigation.addWidget(self.position_label, 1)
        navigation.addWidget(self.next_occurrence_button)
        outer.addLayout(navigation)
        self.refresh()

    def _button(self, name, title, action):
        button = QPushButton()
        button.setProperty('iconOnly', True)
        button.setProperty('previewIconName', name)
        button.setToolTip(title)
        button.setAccessibleName(title)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False: action())
        return button

    @property
    def grid_start(self):
        offset = (self.current_month.weekday() - self.preferences.first_weekday) % 7
        return date.fromordinal(max(1, self.current_month.toordinal() - offset))

    def day_at_offset(self, index):
        ordinal = self.grid_start.toordinal() + index
        return date.fromordinal(ordinal) if ordinal <= date.max.toordinal() else None

    @property
    def ordered_entries(self):
        return sorted(self.owner.entries, key=lambda item: (item.start.timestamp(), item.id))

    @property
    def selected_entry(self):
        identifier = getattr(self.owner, 'selected_id', None)
        return next((item for item in self.owner.entries if item.id == identifier),
                    next((item for item in self.owner.entries if item.id == 'primary'), None))

    def events_on(self, day):
        return self._by_day.get(day, [])

    def refresh(self):
        self.preferences = self.owner.preferences
        scale = self.preferences.font_scale / 100
        colors = scheme_for(self.preferences)
        self._by_day = {}
        first = self.grid_start.toordinal()
        last = min(date.max.toordinal(), first + 41)
        for item in self.ordered_entries:
            begin, finish = _local_start(item), _local_end(item)
            end_day = finish.date() if finish <= begin else (finish - timedelta(microseconds=1)).date()
            for ordinal in range(max(first, begin.date().toordinal()), min(last, end_day.toordinal()) + 1):
                self._by_day.setdefault(date.fromordinal(ordinal), []).append(item)
        self._week_heights = []
        for week in range(6):
            maximum = max((len(self.events_on(self.day_at_offset(week * 7 + index))) for index in range(7)), default=0)
            self._week_heights.append(round(max(40, 20 + 16 * max(1, maximum)) * scale))
        self.grid.setFixedHeight(round(24 * scale) + sum(self._week_heights))
        self.scroll.setFixedHeight(round(266 * scale))
        self.month_label.setText(self.preferences.format_month(self.current_month))
        self.month_previous_button.setEnabled(self.current_month > date(1, 1, 1))
        self.month_next_button.setEnabled(self.current_month < date(9999, 12, 1))
        for button in self.findChildren(QPushButton):
            name = button.property('previewIconName')
            if name:
                button.setIcon(_first_icon(colors.accent) if name == 'first' else icon(name, colors.accent))
                button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
                button.setFixedSize(round(30 * scale), round(30 * scale))
        selected = self.selected_entry
        ordered = self.ordered_entries
        index = ordered.index(selected) if selected in ordered else -1
        self.first_button.setEnabled(any(item.id == 'primary' for item in ordered))
        self.previous_occurrence_button.setEnabled(index > 0)
        self.next_occurrence_button.setEnabled(0 <= index < len(ordered) - 1)
        if selected:
            when = _local_start(selected)
            clock = '' if selected.all_day else ' · ' + self.preferences.format_time(when)
            self.position_label.setText(f'{index + 1}/{len(ordered)} · {when.day} '
                                        + self.preferences.month_name(when.month, short=True) + clock)
            self.position_label.setToolTip(('Primary event\n' if selected.id == 'primary' else '')
                                           + self.preferences.format_date(when.date(), weekday=True) + clock)
        else:
            self.position_label.setText('0 occurrences')
            self.position_label.setToolTip('')
        self._reserve_scrollbar(self.scroll.verticalScrollBar().minimum(), self.scroll.verticalScrollBar().maximum())
        self.grid.update()
        self.updateGeometry()

    def _reserve_scrollbar(self, minimum, maximum):
        # Preserve day-column width when a crowded week makes scrolling useful.
        reserve = round(8 * self.preferences.font_scale / 100) if maximum <= minimum else 0
        self.scroll.setViewportMargins(0, 0, reserve, 0)

    def show_month(self, day):
        self.current_month = day.replace(day=1)
        self.refresh()

    def navigate_month(self, delta):
        value = self.current_month.year * 12 + self.current_month.month - 1 + delta
        year, month = divmod(value, 12)
        if 1 <= year <= 9999:
            self.show_month(date(year, month + 1, 1))

    def show_event(self, identifier):
        item = next((entry for entry in self.owner.entries if entry.id == identifier), None)
        if item is not None:
            self.show_month(_local_start(item).date())
            self._pending_reveal = identifier
            self._reveal_timer.start(0)

    def _reveal_pending(self):
        item = next((entry for entry in self.owner.entries if entry.id == self._pending_reveal), None)
        if item is None:
            return
        day = _local_start(item).date()
        rect = next((rect for rect, event, _remove in self.grid.chip_rects(day) if event.id == item.id),
                    self.grid.cell_rect(day))
        self.scroll.ensureVisible(round(rect.center().x()), round(rect.center().y()), 0, round(rect.height() / 2 + 5))

    def _activate(self, identifier):
        self.owner.select_occurrence(identifier)
        self.show_event(identifier)

    def show_primary(self):
        if any(item.id == 'primary' for item in self.owner.entries):
            self._activate('primary')

    def navigate_occurrence(self, delta):
        ordered = self.ordered_entries
        selected = self.selected_entry
        if selected not in ordered:
            return
        index = ordered.index(selected) + delta
        if 0 <= index < len(ordered):
            self._activate(ordered[index].id)


class _OccurrenceMonth(QWidget):
    def __init__(self, preview):
        super().__init__(preview)
        self.preview = preview
        self.focus_day = preview.current_month
        self.focus_entry = None
        self.keyboard_focus = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('Occurrence preview')

    @property
    def header_height(self):
        return round(24 * display_scale(self.preview))

    def cell_rect(self, day):
        index = (day - self.preview.grid_start).days
        if not 0 <= index < 42:
            return QRectF()
        week, column = divmod(index, 7)
        width = self.width() / 7
        return QRectF(column * width, self.header_height + sum(self.preview._week_heights[:week]),
                      width, self.preview._week_heights[week])

    def chip_rects(self, day):
        cell, scale = self.cell_rect(day), display_scale(self.preview)
        if cell.isEmpty():
            return []
        results = []
        for index, event in enumerate(self.preview.events_on(day)):
            rect = QRectF(cell.left() + 3 * scale, cell.top() + 19 * scale + index * 16 * scale,
                          max(1, cell.width() - 6 * scale), 14 * scale)
            remove = (QRectF(rect.right() - 16 * scale, rect.top(), 16 * scale, rect.height())
                      if event.id != 'primary' else QRectF())
            results.append((rect, event, remove))
        return results

    def day_at(self, point):
        if point.y() < self.header_height or point.x() < 0 or point.x() >= self.width():
            return None
        top = self.header_height
        for week, height in enumerate(self.preview._week_heights):
            if top <= point.y() < top + height:
                return self.preview.day_at_offset(week * 7 + min(6, int(point.x() / (self.width() / 7))))
            top += height
        return None

    def paintEvent(self, _event):
        preview, owner = self.preview, self.preview.owner
        preferences = preview.preferences
        scheme = scheme_for(preferences)
        scale = display_scale(preview)
        style = event_colors(owner.source.color, preferences)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(scheme.background))
        width = self.width() / 7
        for index in range(7):
            day = preview.day_at_offset(index)
            if day is None:
                continue
            text(painter, QRectF(index * width, 0, width, self.header_height), preferences.weekday_name(day).upper(),
                 QColor(weekend_text(day, preferences, scheme, scheme.muted)), WEEKDAY_SIZE, True,
                 Qt.AlignmentFlag.AlignCenter)
        for index in range(42):
            day = preview.day_at_offset(index)
            if day is None:
                continue
            rect = self.cell_rect(day)
            outside = day.month != preview.current_month.month
            if outside:
                painter.fillRect(rect, QColor(scheme.outside_month))
            shade = weekend_fill(day, preferences, scheme)
            if shade is not None:
                painter.fillRect(rect, QColor(shade))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.line), .7 * scale))
            painter.drawRect(rect)
            ink = scheme.muted if outside else scheme.ink
            primary_day = any(item.id == 'primary' for item in preview.events_on(day))
            text(painter, QRectF(rect.left() + 5 * scale, rect.top(), rect.width() - 10 * scale, 18 * scale),
                 str(day.day), QColor(weekend_text(day, preferences, scheme, ink)), 8, primary_day)
            for chip, item, remove in self.chip_rects(day):
                selected = item.id == getattr(owner, 'selected_id', None)
                paint_event_card(painter, chip, style, selected=selected, scale=scale, radius=2)
                left = 8 * scale if selected else 4 * scale
                if item.id == 'primary':
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(style.outline))
                    center = QPointF(chip.left() + 5 * scale, chip.center().y() - scale)
                    painter.drawEllipse(center, 2 * scale, 2 * scale)
                    painter.setPen(QPen(QColor(style.outline), scale))
                    painter.drawLine(center, QPointF(center.x(), center.y() + 5 * scale))
                    left = 11 * scale
                begin, end = _local_start(item), _local_end(item)
                if item.all_day:
                    caption = '◇'
                elif begin.date() == day:
                    caption = preferences.format_time(begin) + (' →' if end.date() > day else '')
                else:
                    caption = '→ ' + preferences.format_time(end) if end.date() == day else '→'
                caption_rect = chip.adjusted(left, 0, -17 * scale if not remove.isEmpty() else -2 * scale, 0)
                painter.save()
                painter.setClipRect(caption_rect)
                text(painter, caption_rect, caption, QColor(style.ink), 7, selected)
                painter.restore()
                if not remove.isEmpty():
                    center, half = remove.center(), 2.5 * scale
                    painter.setPen(QPen(QColor(style.muted), scale))
                    painter.drawLine(QPointF(center.x() - half, center.y() - half), QPointF(center.x() + half, center.y() + half))
                    painter.drawLine(QPointF(center.x() - half, center.y() + half), QPointF(center.x() + half, center.y() - half))
            if self.hasFocus() and self.keyboard_focus and day == self.focus_day:
                painter.setPen(QPen(QColor(scheme.focus), scale, Qt.PenStyle.DotLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(2, 2, -2, -2))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.day_at(event.position())
        if day is None:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.keyboard_focus = False
        self.focus_day, self.focus_entry = day, None
        for rect, item, remove in self.chip_rects(day):
            if not remove.isEmpty() and remove.contains(event.position()):
                self.preview.owner.remove_occurrence(item.id)
                return
            if rect.contains(event.position()):
                self.focus_entry = item.id
                self.preview.owner.select_occurrence(item.id)
                self.update()
                return
        self.preview.owner.date_toggled(day)

    def mouseMoveEvent(self, event):
        day = self.day_at(event.position())
        if day is None:
            self.setToolTip('')
            return
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        for rect, item, remove in self.chip_rects(day):
            if remove.contains(event.position()):
                self.setToolTip('Remove ' + self.preview.preferences.format_time(_local_start(item)))
                return
            if rect.contains(event.position()):
                begin, finish = _local_start(item), _local_end(item)
                when = self.preview.preferences.format_date(begin.date(), weekday=True)
                if not item.all_day:
                    when += ' · ' + self.preview.preferences.format_time(begin) + '–' + self.preview.preferences.format_time(finish)
                if finish.date() != begin.date():
                    when += ' · ' + self.preview.preferences.format_date(finish.date())
                self.setToolTip(('Primary event\n' if item.id == 'primary' else '') + when)
                return
        self.setToolTip(self.preview.preferences.format_date(day, weekday=True))

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.day_at(event.position())
        if day is None:
            return
        edit = getattr(self.preview.owner, 'edit_occurrence', None)
        if edit is not None:
            for rect, item, remove in self.chip_rects(day):
                if rect.contains(event.position()) and not remove.contains(event.position()):
                    edit(item.id)
                    return

    def keyPressEvent(self, event):
        self.keyboard_focus = True
        changes = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
        if event.key() in changes:
            ordinal = self.focus_day.toordinal() + changes[event.key()]
            if 1 <= ordinal <= date.max.toordinal():
                self.focus_day = date.fromordinal(ordinal)
                self.focus_entry = None
                if self.focus_day.replace(day=1) != self.preview.current_month:
                    self.preview.show_month(self.focus_day)
                self.scroll_to_focus()
                self.update()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.focus_entry:
                self.preview.owner.select_occurrence(self.focus_entry)
            else:
                self.preview.owner.date_toggled(self.focus_day)
        elif event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.focus_entry not in (None, 'primary'):
            self.preview.owner.remove_occurrence(self.focus_entry)
            self.focus_entry = None
        else:
            super().keyPressEvent(event)

    def scroll_to_focus(self):
        rect = self.cell_rect(self.focus_day)
        self.preview.scroll.ensureVisible(round(rect.center().x()), round(rect.center().y()), 0, round(rect.height() / 2))

    def focusInEvent(self, event):
        self.keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                                Qt.FocusReason.ShortcutFocusReason)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self.update()
        super().focusOutEvent(event)
