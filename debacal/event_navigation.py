"""Find neighbouring event days without expanding whole recurring calendars."""
from datetime import datetime, time, timedelta, timezone

from .recurrence import key_datetime, occurrence, recurrence_key, recurrence_set


def event_day(event, zone):
    return event.start.date() if event.all_day else event.start.astimezone(zone).date()


def adjacent_event_day(resources, day, direction, zone, matches=lambda event, master: True):
    """Find the next/previous populated day, including moved exceptions.

    Each recurring resource contributes its nearest nominal occurrence and its
    finite detached exceptions. A filter that excludes a series' shared fields
    only needs to inspect exceptions, rather than searching an endless stream.
    """
    if direction not in (-1, 1):
        raise ValueError('Choose the previous or next event day.')
    best = None

    def consider(event, master=None):
        nonlocal best
        candidate = event_day(event, zone)
        if (candidate - day).days * direction <= 0 or not matches(event, master):
            return False
        if best is None or (candidate - best).days * direction < 0:
            best = candidate
        return True

    for master in resources:
        if not master.recurring:
            consider(master)
            continue
        try:
            for key in master.recurrence_overrides:
                if key not in master.excluded_dates:
                    consider(occurrence(master, key_datetime(key, master), key), master)
            if not matches(master, master):
                continue
            boundary = datetime.combine(day + timedelta(days=1 if direction > 0 else 0), time.min, zone)
            if master.all_day:
                # DATE events float; their nominal midnight may be over a day
                # away from the display timezone's midnight across the dateline.
                boundary += timedelta(days=-2 if direction > 0 else 2)
            rules = recurrence_set(master, after=boundary if direction > 0 else None)
            utc_boundary = boundary.astimezone(timezone.utc)
            nominal = (rules.xafter(utc_boundary, inc=True) if direction > 0 else _previous(rules, utc_boundary))
            for index, value in enumerate(nominal):
                if index >= 10000 + len(master.recurrence_overrides):
                    break
                key = recurrence_key(value, master.all_day)
                if key in master.recurrence_overrides:
                    continue
                if consider(occurrence(master, value, key), master):
                    break
        except (ValueError, TypeError, OverflowError, KeyError):
            # An unrelated malformed resource must not block event navigation.
            continue
    return best


def _previous(rules, boundary):
    value = rules.before(boundary)
    while value is not None:
        yield value
        value = rules.before(value)
