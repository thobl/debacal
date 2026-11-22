"""Saved iCalendar alarms and their occurrence-relative trigger times."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
import re
from typing import Literal

from .event_duration import parse_duration_spec


@dataclass(frozen=True, slots=True)
class Alarm:
    key: str
    action: str
    trigger: str | None
    related: Literal['START', 'END'] = 'START'
    repeat: int = 0
    repeat_interval: str | None = None
    uid: str | None = None
    description: str = ''
    acknowledged: datetime | None = None
    raw_ics: str = ''
    issue: str | None = None

    def to_dict(self) -> dict:
        value = {field.name: getattr(self, field.name) for field in fields(self)}
        if self.acknowledged is not None:
            value['acknowledged'] = self.acknowledged.isoformat()
        return value

    @classmethod
    def from_dict(cls, value: dict) -> Alarm:
        value = dict(value)
        if isinstance(value.get('acknowledged'), str):
            value['acknowledged'] = datetime.fromisoformat(value['acknowledged'])
        return cls(**value)


AlarmEdit = tuple[Alarm, ...] | None


def signed_duration(value: str) -> tuple[int, int]:
    """Keep nominal days and elapsed seconds separate, including their sign."""
    if not value or not re.match(r'^[+-]?P', value.upper()):
        raise ValueError('Invalid alarm duration.')
    days, seconds = parse_duration_spec(value.upper().lstrip('+-'))
    return (-days, -seconds) if value.startswith('-') else (days, seconds)


def utc_stamp(value: str) -> datetime:
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', value.upper()):
        raise ValueError('An absolute alarm date-time must be UTC.')
    # RFC 5545 permits 60 for a leap second on the wire.
    clock = value[:13] + '59Z' if value[13:15] == '60' else value.upper()
    return datetime.strptime(clock, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)


def _advance(bound: datetime, days: int, seconds: int) -> datetime:
    if bound.tzinfo is None or bound.utcoffset() is None:
        raise ValueError('An alarm needs a timezone-aware occurrence bound.')
    if days:
        bound = (bound.replace(tzinfo=None) + timedelta(days=days)).replace(tzinfo=bound.tzinfo, fold=0)
    return bound.astimezone(timezone.utc) + timedelta(seconds=seconds)


def alarm_trigger(alarm: Alarm, start: datetime, end: datetime, *, repeat_index: int = 0) -> datetime:
    """Resolve one trigger, applying nominal offsets in the occurrence's zone."""
    if alarm.issue or not alarm.trigger:
        raise ValueError(alarm.issue or 'An alarm needs a trigger.')
    if not 0 <= repeat_index <= alarm.repeat:
        raise ValueError('Alarm repeat index is outside its finite count.')
    if alarm.related not in {'START', 'END'}:
        raise ValueError('An alarm must be relative to START or END.')
    bound = end if alarm.related == 'END' else start
    if alarm.trigger.upper().startswith(('P', '-P', '+P')):
        zone = bound.tzinfo
        instant = _advance(bound, *signed_duration(alarm.trigger))
    else:
        zone = timezone.utc
        instant = utc_stamp(alarm.trigger)
    if repeat_index:
        days, seconds = signed_duration(alarm.repeat_interval or '')
        if days < 0 or seconds < 0 or not (days or seconds):
            raise ValueError('Alarm repeats require a positive interval.')
        if not days:
            return instant + timedelta(seconds=seconds * repeat_index)
        # Each repeat is measured from the preceding firing. For mixed nominal
        # days/clock intervals an intervening DST gap can change the next clock.
        for _ in range(repeat_index):
            instant = _advance(instant.astimezone(zone), days, seconds)
    return instant


def alarm_semantic(alarms: tuple[Alarm, ...]) -> tuple:
    """Compare saved properties (including extensions), independent of local keys."""
    from .alarm_ics import semantic_properties
    return tuple(sorted((semantic_properties(alarm) for alarm in alarms), key=repr))
