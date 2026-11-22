"""A paste remains a prepared snapshot until the user places it."""
from dataclasses import dataclass, field, replace
from datetime import datetime

from .event_clipboard import EventClipboard, relocated_event
from .models import Event
from .recurrence import expand_events
from .timeutil import local_zone


def initial_paste_target(clipboard: EventClipboard, origin, fallback_day):
    source = clipboard.reference.start
    if not clipboard.reference.all_day:
        source = source.astimezone(local_zone())
    copied_origin = getattr(clipboard, 'view_origin', None)
    if copied_origin is not None:
        day = source.date() + (origin - copied_origin)
    else:
        day = fallback_day
    return source.replace(year=day.year, month=day.month, day=day.day)


@dataclass
class PastePlacement:
    clipboard: EventClipboard
    calendar_id: str
    target: datetime
    scope: str = 'group'
    moved: bool = False
    events: list[Event] = field(default_factory=list)
    error: str = ''

    def __post_init__(self):
        try:
            self._geometry = self.clipboard.placement_geometry(self.scope)
            self.update(self.target)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self._geometry = ()
            self.error = str(exc)

    def update(self, target, *, preserve_calendars=False):
        self.target = target
        try:
            if not self._geometry:
                return
            if target.tzinfo is None or target.utcoffset() is None:
                raise ValueError("The paste target must include a timezone.")
            self.events = [replace(relocated_event(event, self.clipboard.reference, target),
                                   calendar_id=event.calendar_id if preserve_calendars else self.calendar_id)
                           for event in self._geometry]
            self.error = ''
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.events = []
            self.error = str(exc)

    def visible(self, start, end):
        if self.error:
            return []
        return expand_events(self.events, start, end)
