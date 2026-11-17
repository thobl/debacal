"""A disposable editor draft placed through the ordinary workspace surfaces."""
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, time, timedelta

from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt

from .calendar_widgets import at
from .date_time_fields import validate_datetime
from .event_range_index import EventRangeIndex
from .recurrence import overlaps, split_occurrence_id
from .shortcuts import DEFAULT_DIALOG_BINDINGS, shortcut_label
from .year_projection import day_interval


def source_id(event):
    return event.series_id or event.id


class OccurrenceOverlay(Sequence):
    """Add a small draft to the current year without expanding stored calendars."""
    def __init__(self, base, events, hidden_sources=()):
        self.base, self.extra, self.hidden = base, tuple(events), frozenset(hidden_sources)
        self.zone, self.first_day = base.zone, base.first_day
        self._extra = {event.id: event for event in self.extra}
        removed = base.for_sources(self.hidden) if self.hidden else ()
        self._count = len(base) - len(removed) + len(self.extra)
        self.known_events = tuple(event for event in base.known_events if source_id(event) not in self.hidden) + self.extra
        self.summaries = [dict(value) for value in base.summaries]
        for values, delta in ((removed, -1), (self.extra, 1)):
            for event in values:
                interval = day_interval(event.start, event.end, event.all_day,
                                        self.first_day.toordinal(), len(self.summaries), self.zone)
                if interval is None:
                    continue
                for day in range(*interval):
                    summary = self.summaries[day]
                    count = summary.get(event.calendar_id, (0, ''))[0] + delta
                    if count > 0:
                        summary[event.calendar_id] = count, ''
                    else:
                        summary.pop(event.calendar_id, None)

    def __len__(self):
        return self._count

    def __iter__(self):
        yield from (event for event in self.base if source_id(event) not in self.hidden)
        yield from self.extra

    def __getitem__(self, index):
        return list(self)[index]

    def event_by_id(self, identity):
        if identity in self._extra:
            return self._extra[identity]
        parts = split_occurrence_id(identity)
        return None if (parts[0] if parts else identity) in self.hidden else self.base.event_by_id(identity)

    def selected_events(self, identities):
        return [event for identity in identities if (event := self.event_by_id(identity)) is not None]

    def events_on_day(self, day, *, limit=100):
        if limit <= 0:
            return []
        start = datetime.combine(day, time.min, self.zone)
        end = (datetime.combine(day + timedelta(days=1), time.min, self.zone)
               if day.toordinal() < datetime.max.toordinal() else datetime.max.replace(tzinfo=self.zone))
        values = self.base.events_on_day(day, limit=limit, exclude_sources=self.hidden)
        values += [event for event in self.extra if overlaps(event, start, end)]
        return sorted(values, key=lambda event: (event.start.timestamp(), event.end.timestamp(), event.id))[:limit]

    def without_calendars(self, identities):
        return type(self)(self.base.without_calendars(identities),
                          [event for event in self.extra if event.calendar_id not in identities], self.hidden)


class CalendarOccurrencePicker(QObject):
    """Suspend a modal form while its unsaved series is edited on the calendar."""
    def __init__(self, window, dialog):
        super().__init__(window)
        self.window, self.dialog = window, dialog
        self.loop = QEventLoop(self)
        self.cancelled = False
        self.selected_id = None
        self.preview = []
        self.events = {}
        self.hidden_sources = set()
        if dialog.original:
            self.hidden_sources.add(dialog.original.series_id or dialog.original.id)
        self.rebuild()

    def rebuild(self):
        self.master = self.dialog.calendar_occurrence_master()
        self.index = EventRangeIndex()
        if self.master is not None:
            self.index.upsert(self.master)
        if self.dialog.series is not None and self.dialog.scope.currentData() == 'future':
            from .series_operations import reidentify_series, updated_future
            before, _after = updated_future(self.dialog.series, self.dialog.original,
                                            self.dialog.build_events()[0], self.dialog.schedule_replacement)
            if before is not None:
                identity = 'occurrence-context:' + before.id
                self.index.upsert(reidentify_series(before, identity, identity))

    def changed(self):
        self.rebuild()
        self.preview = []
        self.window._surface_data_key = None
        self.window.refresh(presentation_only=True)

    def render_events(self, base, days, calendars):
        start, end = at(days[0]), at(days[-1] + timedelta(days=1))
        issues = []
        extra, _failures = self.index.query(start, end, issues=issues)
        self.events = {event.id: event for event in extra if source_id(event) == self.master.id} if self.master else {}
        for surface in self.window.day_surfaces:
            surface.set_occurrence_picking(self.events)
        if hasattr(base, 'summaries'):
            values = OccurrenceOverlay(base, extra, self.hidden_sources)
        else:
            values = [event for event in base if source_id(event) not in self.hidden_sources] + extra
        colors = [replace(calendar, visible=True) if self.master and calendar.id == self.master.calendar_id else calendar
                  for calendar in calendars]
        return values, colors

    def event_by_id(self, identity):
        return self.events.get(identity)

    def select(self, identity):
        self.selected_id = identity if identity in self.events else None
        self.apply_selection()

    def apply_selection(self):
        selected = {self.selected_id} if self.selected_id in self.events else set()
        for surface in self.window.event_surfaces:
            surface.selected_id = self.selected_id
            surface.set_selection(selected)
            if hasattr(surface, 'set_primary_selection'):
                surface.set_primary_selection(selected)
            surface.set_scope('single')
        self.window.export_selection_button.setEnabled(False)

    def target_range(self, target, source=None):
        if self.master is None or target is None:
            return None
        if self.master.all_day:
            target = at(target.date())
        elif source is not self.window.canvas:
            primary = self.dialog.value(self.dialog.start)
            target = primary.replace(year=target.year, month=target.month, day=target.day)
        span = self.dialog.occurrence_range_at(target)
        for endpoint in span:
            validate_datetime(endpoint, all_day=self.master.all_day, floating_time=self.master.floating_time)
        return span

    def hover(self, target, source=None):
        self.preview = []
        try:
            span = self.target_range(target, source)
            if span is not None:
                start, end = span
                self.preview = [replace(self.master, id='occurrence-preview', start=start, end=end,
                                        recurrence_rule='', recurrence_dates=(), excluded_dates=(),
                                        recurrence_overrides={}, range_policy='', raw_ics='')]
        except (ValueError, OverflowError):
            pass  # Invalid local clocks have no placeable preview.
        self.window.render_placement()

    def add(self, target, source=None, end=None):
        try:
            span = self.target_range(target, source)
        except (ValueError, OverflowError) as error:
            self.window.report_error(str(error), title='Cannot add occurrence')
            return
        if span is None:
            return
        start, default_end = span
        # A timed drag defines a range only for a timed prototype. A date-only
        # target keeps its clock and duration; all-day ranges use midnight ends.
        if end is not None and not ((source is self.window.canvas and not self.master.all_day)
                                    or (self.master.all_day and end.time() == time.min)):
            end = None
        if not self.dialog.add_calendar_occurrence(start, end or default_end):
            self.window.report_error(self.dialog.error.text(), title='Cannot add occurrence')

    def move(self, event, start, end):
        if event is None or event.id not in self.events:
            return
        if not self.dialog.edit_calendar_occurrence(event, start, end):
            self.window.report_error(self.dialog.error.text(), title='Cannot change occurrence')

    def drag_preview(self, identity, start, end):
        event = self.event_by_id(identity)
        self.preview = [replace(event, start=start, end=end)] if event is not None else []
        self.window.render_placement()

    def finish(self):
        self.loop.quit()

    def abort(self, *_args):
        self.cancelled = True
        self.finish()

    def eventFilter(self, obj, event):
        if (obj is self.dialog and event.type() == QEvent.Type.WindowStateChange
                and not self.dialog.isMinimized()):
            self.finish()
        return False

    def run(self):
        window, dialog = self.window, self.dialog
        window.cancel_paste()
        saved_modality, saved_state = dialog.windowModality(), dialog.windowState()
        saved_flags = dialog.windowFlags()
        native_window = dialog.windowHandle()
        transient_parent = native_window.transientParent() if native_window else None
        blocked = {'new_event', 'undo', 'redo', 'history', 'preferences', 'export_selection'}
        controls = [(button, button.isEnabled()) for button, title, binding in window.command_buttons
                    if binding in blocked or title in ('Import calendar file', 'Export calendar file', 'Add calendar')]
        controls.append((window.selection_control, window.selection_control.isEnabled()))
        window.occurrence_picker = self
        dialog.draft_changed.connect(self.changed)
        dialog.finished.connect(self.abort)
        dialog.installEventFilter(self)
        try:
            for control, _enabled in controls:
                control.setEnabled(False)
            keys = ' / '.join(value for action in ('dialog_close', 'dialog_accept')
                              if (value := shortcut_label(window.bindings.get(action, DEFAULT_DIALOG_BINDINGS[action]))))
            window.finish_occurrences_button.setToolTip('Return to event editor' + (' · ' + keys if keys else ''))
            window.occurrence_bar.show()
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            # Window managers can restore minimized dialogs with their parent.
            # Temporarily use an independently minimizable native window while
            # preserving QObject ownership and restoring the exact dialog flags.
            dialog.setWindowFlags((saved_flags & ~Qt.WindowType.WindowType_Mask)
                                  | Qt.WindowType.Window | Qt.WindowType.WindowMinimizeButtonHint)
            native_window = dialog.windowHandle()
            if native_window is not None:
                native_window.setTransientParent(None)
            dialog.showMinimized()
            window._surface_data_key = None
            window.refresh(presentation_only=True)
            window.raise_()
            window.activateWindow()
            window.active_canvas().setFocus()
            self.loop.exec()
        finally:
            dialog.removeEventFilter(self)
            dialog.draft_changed.disconnect(self.changed)
            dialog.finished.disconnect(self.abort)
            dialog.hide()
            dialog.setWindowState(saved_state & ~Qt.WindowState.WindowMinimized)
            dialog.setWindowModality(saved_modality)
            dialog.setWindowFlags(saved_flags)
            native_window = dialog.windowHandle()
            if native_window is not None:
                native_window.setTransientParent(transient_parent)
            window.occurrence_picker = None
            window.occurrence_bar.hide()
            for control, enabled in controls:
                control.setEnabled(enabled)
            for surface in window.day_surfaces:
                surface.set_occurrence_picking(None)
            window._surface_data_key = None
            if not window._closed:
                window.refresh(presentation_only=True)
            self.deleteLater()
        return not self.cancelled
