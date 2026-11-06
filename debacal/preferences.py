"""Persisted display preferences, using the OS locale unless overridden."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from PySide6.QtCore import QDate, QLocale, QTime


SETTINGS_KEY = 'display_preferences'


def _system_locale():
    return QLocale.system()


@dataclass(frozen=True, slots=True)
class Preferences:
    """Weekdays use Python's convention: Monday=0 through Sunday=6."""
    start_weekday: int | str = 'system'
    date_format: str = 'system'
    time_format: str = 'system'
    conflict_notifications: str = 'in_app'
    auto_sync_enabled: bool = True
    sync_interval_minutes: int = 1

    def __post_init__(self):
        if self.start_weekday != 'system' and (
            not isinstance(self.start_weekday, int) or isinstance(self.start_weekday, bool)
            or not 0 <= self.start_weekday <= 6
        ):
            raise ValueError('Week start must be system or a weekday from 0 (Monday) to 6 (Sunday).')
        if self.date_format not in ('system', 'european', 'iso', 'us'):
            raise ValueError('Unknown date format.')
        if self.time_format not in ('system', '24h', '12h'):
            raise ValueError('Unknown time format.')
        if self.conflict_notifications not in ('in_app', 'desktop', 'both'):
            raise ValueError('Unknown conflict notification preference.')
        if not isinstance(self.auto_sync_enabled, bool):
            raise ValueError('Automatic syncing must be enabled or disabled.')
        if (not isinstance(self.sync_interval_minutes, int) or isinstance(self.sync_interval_minutes, bool)
                or not 1 <= self.sync_interval_minutes <= 1440):
            raise ValueError('Sync interval must be between 1 and 1440 minutes.')

    @property
    def first_weekday(self) -> int:
        return _system_locale().firstDayOfWeek().value - 1 if self.start_weekday == 'system' else self.start_weekday

    def week_start(self, value: date) -> date:
        return value - timedelta(days=(value.weekday() - self.first_weekday) % 7)

    def qt_date_pattern(self) -> str:
        return {
            'european': 'dd.MM.yyyy', 'iso': 'yyyy-MM-dd', 'us': 'MM/dd/yyyy',
        }.get(self.date_format, _system_locale().dateFormat(QLocale.FormatType.ShortFormat))

    def qt_time_pattern(self) -> str:
        return {'24h': 'HH:mm', '12h': 'h:mm AP'}.get(
            self.time_format, _system_locale().timeFormat(QLocale.FormatType.ShortFormat))

    def qt_datetime_pattern(self) -> str:
        return self.qt_date_pattern() + '   ' + self.qt_time_pattern()

    def format_date(self, value: date, *, weekday: bool = False) -> str:
        formatted = _system_locale().toString(QDate(value.year, value.month, value.day), self.qt_date_pattern())
        return self.weekday_name(value) + ', ' + formatted if weekday else formatted

    def format_time(self, value: time | datetime) -> str:
        return _system_locale().toString(QTime(value.hour, value.minute, value.second), self.qt_time_pattern())

    def weekday_name(self, value: date | int, *, short: bool = True) -> str:
        day = value.weekday() if isinstance(value, date) else value
        if not 0 <= day <= 6:
            raise ValueError('Weekday must be between 0 and 6.')
        form = QLocale.FormatType.ShortFormat if short else QLocale.FormatType.LongFormat
        return _system_locale().dayName(day + 1, form)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Any) -> Preferences:
        if not isinstance(values, dict):
            return cls()
        # Settings from older versions or manual edits must not prevent startup.
        valid = {}
        for field in ('start_weekday', 'date_format', 'time_format', 'conflict_notifications', 'auto_sync_enabled', 'sync_interval_minutes'):
            if field not in values:
                continue
            try:
                cls(**{field: values[field]})
            except (TypeError, ValueError):
                continue
            valid[field] = values[field]
        return cls(**valid)

    @classmethod
    def load(cls, store) -> Preferences:
        return cls.from_dict(store.get_setting(SETTINGS_KEY, {}))

    def save(self, store) -> None:
        store.set_setting(SETTINGS_KEY, self.to_dict())


def format_date(value: date, preferences: Preferences | None = None, *, weekday: bool = False) -> str:
    return (preferences or Preferences()).format_date(value, weekday=weekday)


def format_time(value: time | datetime, preferences: Preferences | None = None) -> str:
    return (preferences or Preferences()).format_time(value)
