"""Profile-local reminder choices and reusable saved-alarm templates."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, TYPE_CHECKING, get_args

from .alarm_series import copied_alarms
from .alarms import Alarm, signed_duration
from .ui_state import REMINDER_SETTINGS_KEY, read_ui_setting

if TYPE_CHECKING:
    from .models import Event
    from .store import Store


ImportReminderMode = Literal['keep', 'remove', 'add_missing', 'replace']


def relative_templates(alarms: tuple[Alarm, ...]) -> tuple[Alarm, ...]:
    alarms = tuple(alarms)
    for alarm in alarms:
        if not isinstance(alarm, Alarm):
            raise ValueError('Reminder templates must contain relative alarms.')
        try:
            signed_duration(alarm.trigger or '')
        except ValueError as error:
            raise ValueError('Reminder templates require a valid relative trigger.') from error
    return alarms


def instantiate_reminder_templates(alarms: tuple[Alarm, ...], *, calendar_id: str,
                                   event_uid: str, recurrence_id: str = '') -> tuple[Alarm, ...]:
    """Assign reusable alarms to a destination component with stable identities."""
    if not alarms:
        return ()
    component = json.dumps(('debacal-reminder-template', calendar_id, event_uid, recurrence_id))
    return copied_alarms(alarms, component)


@dataclass(frozen=True, slots=True)
class CalendarReminderPolicy:
    mode: Literal['event', 'mute', 'replace'] = 'event'
    alarms: tuple[Alarm, ...] = ()

    def __post_init__(self):
        if self.mode not in ('event', 'mute', 'replace'):
            raise ValueError('Unknown calendar reminder mode.')
        object.__setattr__(self, 'alarms', relative_templates(self.alarms))


@dataclass(frozen=True, slots=True)
class ReminderPreferences:
    enabled: bool = False
    delivery: Literal['desktop', 'in_app', 'both'] = 'both'
    sound: Literal['default', 'silent'] = 'default'
    missed: Literal['upcoming', 'all', 'none'] = 'upcoming'
    close_behavior: Literal['quit', 'tray'] = 'quit'
    new_timed: tuple[Alarm, ...] = ()
    new_all_day: tuple[Alarm, ...] = ()
    import_mode: ImportReminderMode = 'keep'
    import_alarms: tuple[Alarm, ...] = ()
    calendars: Mapping[str, CalendarReminderPolicy] = field(default_factory=dict)

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError('Reminder delivery must be enabled or disabled.')
        if self.delivery not in ('desktop', 'in_app', 'both'):
            raise ValueError('Unknown reminder delivery mode.')
        if self.sound not in ('default', 'silent'):
            raise ValueError('Unknown reminder sound mode.')
        if self.missed not in ('upcoming', 'all', 'none'):
            raise ValueError('Unknown missed reminder policy.')
        if self.close_behavior not in ('quit', 'tray'):
            raise ValueError('Unknown close behavior.')
        if self.import_mode not in get_args(ImportReminderMode):
            raise ValueError('Unknown import reminder mode.')
        for name in ('new_timed', 'new_all_day', 'import_alarms'):
            object.__setattr__(self, name, relative_templates(getattr(self, name)))
        calendars = dict(self.calendars)
        if any(not isinstance(key, str) or not isinstance(policy, CalendarReminderPolicy)
               for key, policy in calendars.items()):
            raise ValueError('Calendar reminder policies must be keyed by calendar ID.')
        object.__setattr__(self, 'calendars', MappingProxyType(calendars))

    def alarms_for_new_event(self, event: Event) -> tuple[Alarm, ...]:
        """Instantiate the configured list once a new event has its own identity."""
        alarms = self.new_all_day if event.all_day else self.new_timed
        return instantiate_reminder_templates(alarms, calendar_id=event.calendar_id, event_uid=event.ical_uid)


@dataclass(frozen=True, slots=True)
class ReminderPolicySnapshot:
    preferences: ReminderPreferences = field(default_factory=ReminderPreferences)
    generation: int = 0
    eligible_since: datetime | None = None
    calendar_eligible_since: Mapping[str, datetime] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'calendar_eligible_since', MappingProxyType(dict(self.calendar_eligible_since)))

    def _to_dict(self) -> dict:
        value = self.preferences
        return {
            'preferences': {
                'enabled': value.enabled,
                'delivery': value.delivery,
                'sound': value.sound,
                'missed': value.missed,
                'close_behavior': value.close_behavior,
                'new_timed': [alarm.to_dict() for alarm in value.new_timed],
                'new_all_day': [alarm.to_dict() for alarm in value.new_all_day],
                'import_mode': value.import_mode,
                'import_alarms': [alarm.to_dict() for alarm in value.import_alarms],
                'calendars': {key: {'mode': policy.mode, 'alarms': [alarm.to_dict() for alarm in policy.alarms]}
                              for key, policy in value.calendars.items()},
            },
            'generation': self.generation,
            'eligible_since': self.eligible_since.isoformat() if self.eligible_since is not None else None,
            'calendar_eligible_since': {key: time.isoformat() for key, time in self.calendar_eligible_since.items()},
        }

    @classmethod
    def _from_dict(cls, payload: dict | None) -> ReminderPolicySnapshot:
        if payload is None:
            return cls()
        values = dict(payload['preferences'])
        for name in ('new_timed', 'new_all_day', 'import_alarms'):
            values[name] = tuple(Alarm.from_dict(alarm) for alarm in values[name])
        values['calendars'] = {key: CalendarReminderPolicy(policy['mode'], tuple(Alarm.from_dict(alarm)
                                                                            for alarm in policy['alarms']))
                               for key, policy in values['calendars'].items()}
        boundary = payload['eligible_since']
        return cls(ReminderPreferences(**values), payload['generation'],
                   datetime.fromisoformat(boundary) if boundary is not None else None,
                   {key: datetime.fromisoformat(time) for key, time in payload['calendar_eligible_since'].items()})


def load_reminder_policy(store: Store) -> ReminderPolicySnapshot:
    """Read the local preferences and delivery boundaries in one snapshot."""
    return ReminderPolicySnapshot._from_dict(store.get_setting(REMINDER_SETTINGS_KEY))


def read_reminder_policy(database: str | Path) -> ReminderPolicySnapshot:
    """Read the persisted snapshot with no connection to the canonical database."""
    _, value = read_ui_setting(database, REMINDER_SETTINGS_KEY)
    return ReminderPolicySnapshot._from_dict(value)


def load_reminder_preferences(store: Store) -> ReminderPreferences:
    return load_reminder_policy(store).preferences


def save_reminder_preferences(store: Store, value: ReminderPreferences, *, now: datetime) -> None:
    if now.utcoffset() is None:
        raise ValueError('Reminder eligibility needs a timezone-aware time.')
    now = now.astimezone(timezone.utc)
    with store.ui_transaction():
        snapshot = load_reminder_policy(store)
        previous = snapshot.preferences
        if value == previous:
            return
        boundary = now if value.enabled and not previous.enabled else snapshot.eligible_since
        calendars = dict(snapshot.calendar_eligible_since)
        for key, policy in previous.calendars.items():
            current = value.calendars.get(key)
            if policy.mode == 'mute' and (current is None or current.mode != 'mute'):
                calendars[key] = now
        updated = replace(snapshot, preferences=value, generation=snapshot.generation + 1,
                          eligible_since=boundary, calendar_eligible_since=calendars)
        store.set_setting(REMINDER_SETTINGS_KEY, updated._to_dict())
