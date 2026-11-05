"""Calendar search results with bounded, lazy occurrence pages."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import calendar
from heapq import merge
from itertools import chain, islice

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QStyle, QStyleOptionViewItem,
    QStyledItemDelegate, QTreeWidget, QTreeWidgetItem,
)

from .models import Event
from .recurrence import occurrence, recurrence_key, recurrence_set
from .timeutil import local_zone, now


SUBTITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
COLOR_ROLE = SUBTITLE_ROLE + 1
MASTER_ROLE = SUBTITLE_ROLE + 2
MORE_ROLE = SUBTITLE_ROLE + 3
EVENT_ROLE = SUBTITLE_ROLE + 4
PAGE_SIZE = 100
MAX_PAGES = 10
CONTEXT_SCAN_LIMIT = 5000


def impossible_monthday_filter(rule):
    """Prove one narrow calendar intersection empty without expanding a rule.

    The Gregorian 400-year cycle includes a leap February; year 2000 therefore
    supplies each month's maximum possible length. A positive or negative
    month-day outside every selected month's length can never produce a date.
    """
    parts = dict(piece.split('=', 1) for piece in rule.upper().split(';') if '=' in piece)
    if 'BYMONTH' not in parts or 'BYMONTHDAY' not in parts:
        return False
    try:
        months = [int(value) for value in parts['BYMONTH'].split(',')]
        days = [int(value) for value in parts['BYMONTHDAY'].split(',')]
    except ValueError:
        return False
    if not months or not days or any(month < 1 or month > 12 for month in months):
        return False
    return not any(1 <= abs(day) <= calendar.monthrange(2000, month)[1]
                   for month in months for day in days)


class _ResultItem(QTreeWidgetItem):
    """The one-column tree also accepts the old list item's data(role) API."""
    def data(self, *args):
        return super().data(0, args[0]) if len(args) == 1 else super().data(*args)

    def setData(self, *args):
        return super().setData(0, args[0], args[1]) if len(args) == 2 else super().setData(*args)

    def text(self, column=0):
        return super().text(column)


class _ResultDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option, index).width(), 45)

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        title = styled.text
        styled.text, styled.icon = '', QIcon()
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setClipRect(option.rect)
        left = option.rect.left() + 5
        color = QColor(index.data(COLOR_ROLE) or '#788373')
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(left, option.rect.top() + 9, 4, 26, 2, 2)
        left += 12
        width = max(0, option.rect.right() - left - 6)
        normal = QFont(option.font)
        bold = QFont(normal)
        bold.setBold(True)
        if normal.pixelSize() > 0:
            normal.setPixelSize(max(9, normal.pixelSize() - 1))
        elif normal.pointSizeF() > 0:
            normal.setPointSizeF(max(7, normal.pointSizeF() - 1))
        painter.setFont(bold)
        painter.setPen(QColor('#2c4835') if selected else styled.palette.color(QPalette.ColorRole.Text))
        painter.drawText(left, option.rect.top() + 18, painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width))
        painter.setFont(normal)
        painter.setPen(QColor('#52664b') if selected else QColor('#788373'))
        subtitle = index.data(SUBTITLE_ROLE) or ''
        painter.drawText(left, option.rect.top() + 35, painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, width))
        painter.restore()


class SearchPanel(QTreeWidget):
    """One parent per matching resource; dated instances load on expansion.

    Pages contain at most 100 rows, with ten pages per resource. Seeking the
    current view inspects at most 5,000 nominal starts; very old dense series
    fall back to their beginning rather than scanning an unbounded history.
    """
    result_selected = Signal(str, object)
    activated = Signal(str)

    def __init__(self, store, preferences, parent=None):
        super().__init__(parent)
        self.store, self.preferences = store, preferences
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setItemDelegate(_ResultDelegate(self))
        self.setIndentation(15)
        self._index = None
        self._query = None
        self._range = None
        self._context_changed = False
        self._expanded = set()
        self._pages = {}
        self._states = {}
        self._masters = {}
        self._calendars = {}
        self._events_by_id = {}
        self._prefetched = {}
        self._context_budget = CONTEXT_SCAN_LIMIT
        self._updating = False
        self._mouse_selecting = False
        self.currentItemChanged.connect(self._current_changed)
        self.itemExpanded.connect(self._expanded_item)
        self.itemCollapsed.connect(self._collapsed_item)
        self.itemActivated.connect(self._activate_item)

    @staticmethod
    def event_id(item):
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def invalidate(self):
        self._index = None

    def set_range(self, start, end):
        if end <= start:
            raise ValueError('The search context range must end after its start.')
        self._context_changed = self._context_changed or self._range != (start, end)
        self._range = (start, end)

    def _visible_rows(self):
        def walk(item):
            yield item
            if item.isExpanded():
                for offset in range(item.childCount()):
                    yield from walk(item.child(offset))
        return [item for index in range(self.topLevelItemCount()) for item in walk(self.topLevelItem(index))]

    def count(self):
        return len(self._visible_rows())

    def row_count(self):
        return self.count()

    def item(self, row):
        rows = self._visible_rows()
        return rows[row] if 0 <= row < len(rows) else None

    def currentRow(self):
        rows = self._visible_rows()
        return rows.index(self.currentItem()) if self.currentItem() in rows else -1

    def setCurrentRow(self, row):
        item = self.item(row)
        if item is not None:
            if item is self.currentItem():
                self._current_changed(item, item)
            else:
                self.setCurrentItem(item)
        else:
            self.setCurrentItem(None)

    def cycle(self, direction):
        rows = [item for item in self._visible_rows() if self.event_id(item)]
        if not rows:
            return None
        current = self.currentItem()
        index = rows.index(current) if current in rows else (-1 if direction > 0 else 0)
        item = rows[(index + direction) % len(rows)]
        if item is current:
            self._current_changed(item, item)
        else:
            self.setCurrentItem(item)
        self.scrollToItem(item)
        return self.event_id(item)

    def set_selection(self, identifiers):
        identifiers = set(identifiers)
        blocked = self.blockSignals(True)
        try:
            for item in self._all_rows():
                item.setSelected(bool(self.event_id(item) in identifiers))
        finally:
            self.blockSignals(blocked)

    def _all_rows(self):
        def walk(item):
            yield item
            for index in range(item.childCount()):
                yield from walk(item.child(index))
        return [item for index in range(self.topLevelItemCount()) for item in walk(self.topLevelItem(index))]

    def selected_ids(self):
        return {self.event_id(item) for item in self.selectedItems() if self.event_id(item)}

    def selection_targets(self):
        """Return loaded row identities and their actual selection payloads."""
        return [(self.event_id(item), item.data(EVENT_ROLE)) for item in self._all_rows()
                if self.event_id(item) and item.data(EVENT_ROLE) is not None]

    def result_event(self, event_id):
        """Resolve a parent or child without another database expansion."""
        return self._events_by_id.get(event_id)

    def addItem(self, item):
        if not isinstance(item, _ResultItem):
            title = item if isinstance(item, str) else item.text()
            converted = _ResultItem([title])
            if not isinstance(item, str):
                converted.setData(Qt.ItemDataRole.UserRole, item.data(Qt.ItemDataRole.UserRole))
            item = converted
        self.addTopLevelItem(item)

    def _searchable(self, event):
        return ' '.join((event.title, event.description, event.location)).casefold()

    def populate(self, query):
        query = query.strip().casefold()
        if query == self._query and self._index is not None and not self._context_changed:
            return self.count()
        selected = self.selected_ids()
        current_id = self.event_id(self.currentItem())
        if self._index is None and query:
            self._calendars = {calendar.id: calendar for calendar in self.store.list_calendars()}
            self._index = []
            for master in self.store.list_events():
                overrides = [Event.from_dict(payload) for key, payload in master.recurrence_overrides.items()
                             if key not in master.excluded_dates]
                self._index.append((master, self._searchable(master), overrides))
        blocked = self.blockSignals(True)
        self._updating = True
        try:
            super().clear()
            self._states.clear()
            self._masters.clear()
            self._events_by_id.clear()
            self._prefetched.clear()
            self._context_budget = CONTEXT_SCAN_LIMIT
            self._query = query
            self._context_changed = False
            if not query:
                return 0
            matches = 0
            for master, searchable, overrides in self._index:
                matching_overrides = [event for event in overrides if query in self._searchable(event)]
                if query not in searchable and not matching_overrides:
                    continue
                if matches >= 200:
                    self._placeholder(None, 'First 200 results', 'Refine your search to see more.')
                    break
                matches += 1
                item = self._event_item(master)
                self._masters[master.id] = master
                if query not in searchable:
                    matching_overrides.sort(key=lambda event: event.start.astimezone(timezone.utc))
                    item.setData(Qt.ItemDataRole.UserRole, matching_overrides[0].id)
                    item.setData(SUBTITLE_ROLE, 'Matching dated occurrence · expand to inspect')
                    item.setData(MORE_ROLE, matching_overrides)
                    representative = matching_overrides[0]
                elif master.recurring:
                    try:
                        iterator = self._occurrences(master)
                        representative = next(iterator, None)
                        self._prefetched[master.id] = chain((representative,), iterator) if representative else iter(())
                    except (ValueError, OverflowError):
                        representative = None
                else:
                    representative = master
                item.setData(EVENT_ROLE, representative)
                if representative is not None:
                    self._events_by_id[self.event_id(item)] = representative
                    self._events_by_id[master.id] = representative
                    item.setData(SUBTITLE_ROLE, self._when(representative)
                                 + (' · repeating' if master.recurring else ''))
                else:
                    self._events_by_id.pop(self.event_id(item), None)
                self.addTopLevelItem(item)
                if master.recurring:
                    item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                    if master.id in self._expanded:
                        for _ in range(self._pages.get(master.id, 1)):
                            self._load_page(item, remember=False)
                        item.setExpanded(True)
            if not matches:
                self._placeholder(None, 'No matching events', 'Try another title, place, or note.')
            for item in self._all_rows():
                if self.event_id(item) in selected:
                    item.setSelected(True)
                if current_id and self.event_id(item) == current_id:
                    self.setCurrentItem(item)
            return self.count()
        finally:
            self._updating = False
            self.blockSignals(blocked)

    def _event_item(self, event):
        item = _ResultItem([event.title])
        item.setData(Qt.ItemDataRole.UserRole, event.id)
        item.setData(EVENT_ROLE, event)
        self._events_by_id[event.id] = event
        item.setData(MASTER_ROLE, event.series_id or event.id)
        subtitle = self._when(event)
        if event.recurring:
            subtitle += ' · repeating'
        item.setData(SUBTITLE_ROLE, subtitle)
        calendar = self._calendars.get(event.calendar_id)
        item.setData(COLOR_ROLE, calendar.color if calendar else '#788373')
        item.setToolTip(0, calendar.name if calendar else '')
        return item

    def _when(self, event):
        local = event.start if event.all_day else event.start.astimezone(local_zone())
        return self.preferences.format_date(local.date(), weekday=True) + (
            ' · all day' if event.all_day else ' · ' + self.preferences.format_time(local))

    def _placeholder(self, parent, title, subtitle='', *, more=False):
        item = _ResultItem([title])
        item.setData(SUBTITLE_ROLE, subtitle)
        item.setData(MORE_ROLE, more)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        if not more:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _occurrences(self, master):
        if impossible_monthday_filter(master.recurrence_rule):
            # DTSTART/RDATE still belong to the standard resource. Only the
            # provably empty generator is omitted from this private search copy.
            master = replace(master, recurrence_rule='')
        rules = recurrence_set(master)
        iterator = iter(rules)
        floor = self._range[0] if self._range else now()
        found = None
        for _ in range(min(CONTEXT_SCAN_LIMIT, self._context_budget)):
            self._context_budget -= 1
            candidate = next(iterator, None)
            if candidate is None:
                break
            if candidate.timestamp() >= floor.timestamp():
                found = candidate
                break
        if found is None:
            iterator, floor = iter(rules), None
        else:
            iterator = chain((found,), iterator)
        def normal():
            for start in iterator:
                key = recurrence_key(start, master.all_day)
                if key in master.recurrence_overrides:
                    continue
                if start.astimezone(timezone.utc).astimezone(start.tzinfo).replace(tzinfo=None) != start.replace(tzinfo=None):
                    continue
                yield occurrence(master, start, key)
        detached = [Event.from_dict(payload) for key, payload in master.recurrence_overrides.items()
                    if key not in master.excluded_dates]
        if floor is not None:
            detached = [event for event in detached if event.start.timestamp() >= floor.timestamp()]
        detached.sort(key=lambda event: event.start.astimezone(timezone.utc))
        return merge(normal(), detached, key=lambda event: event.start.astimezone(timezone.utc))

    def _load_page(self, item, *, remember=True):
        master_id = item.data(MASTER_ROLE)
        if master_id not in self._masters:
            return
        state = self._states.get(master_id)
        if state is None:
            matching = item.data(MORE_ROLE)
            try:
                if isinstance(matching, list):
                    iterator = iter(sorted(matching, key=lambda event: event.start.timestamp()))
                elif master_id in self._prefetched:
                    iterator = self._prefetched.pop(master_id)
                else:
                    self._context_budget = CONTEXT_SCAN_LIMIT
                    iterator = self._occurrences(self._masters[master_id])
            except (ValueError, OverflowError):
                self._placeholder(item, 'Dates unavailable', 'This event needs review before its dates can be shown.')
                return
            state = self._states[master_id] = {'iterator': iterator, 'pages': 0, 'finished': False}
        if state['finished'] or state['pages'] >= MAX_PAGES:
            return
        for index in range(item.childCount() - 1, -1, -1):
            if item.child(index).data(MORE_ROLE) is True:
                item.takeChild(index)
        try:
            values = list(islice(state['iterator'], PAGE_SIZE + 1))
        except (ValueError, OverflowError):
            self._placeholder(item, 'Dates unavailable', 'This event needs review before more dates can be shown.')
            state['finished'] = True
            return
        for event in values[:PAGE_SIZE]:
            item.addChild(self._event_item(event))
        state['pages'] += 1
        state['finished'] = len(values) <= PAGE_SIZE
        if not state['finished']:
            state['iterator'] = chain(values[PAGE_SIZE:], state['iterator'])
            if state['pages'] < MAX_PAGES:
                self._placeholder(item, 'More dates…', f'{state["pages"] * PAGE_SIZE} dates shown', more=True)
            else:
                self._placeholder(item, '1,000 dates shown', 'Choose another calendar range to inspect later dates.')
        if remember:
            self._pages[master_id] = state['pages']

    def load_more(self, item):
        """Load another bounded page for a parent or its More dates row."""
        if item is not None:
            self._load_page(item.parent() if item.data(MORE_ROLE) is True else item)

    def _expanded_item(self, item):
        if self._updating:
            return
        master_id = item.data(MASTER_ROLE)
        self._expanded.add(master_id)
        if item.childCount() == 0:
            self._load_page(item)

    def _collapsed_item(self, item):
        self._expanded.discard(item.data(MASTER_ROLE))

    def _current_changed(self, current, _previous):
        if not self._updating and not self._mouse_selecting and self.event_id(current):
            self.result_selected.emit(self.event_id(current), QApplication.keyboardModifiers())

    def _activate_item(self, item, _column):
        if item.data(MORE_ROLE) is True:
            self._load_page(item.parent())
        elif identifier := self.event_id(item):
            self.activated.emit(identifier)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(MORE_ROLE) is True:
            self._load_page(item.parent())
            event.accept()
            return
        self._mouse_selecting = True
        try:
            super().mousePressEvent(event)
        finally:
            self._mouse_selecting = False
        if identifier := self.event_id(item):
            self.result_selected.emit(identifier, event.modifiers())
