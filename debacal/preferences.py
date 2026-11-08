"""Persisted display preferences, using the OS locale unless overridden."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta
from typing import Any

from PySide6.QtCore import QDate, QLocale, QTime

from .theme import SCHEMES, CUSTOM_COLORS, valid_rgb


SETTINGS_KEY = 'display_preferences'
WEEK_NUMBERS_DEFAULT_KEY = 'week_numbers_default_version'


def _system_locale():
    return QLocale.system()


def _system_language_locale() -> QLocale:
    # QLocale.system() combines OS categories: its UI language can be English
    # while LC_TIME supplies German month/day names. Reconstruct a regular
    # locale from the preferred UI language to keep names separate from formats.
    system = _system_locale()
    for language in system.uiLanguages():
        locale = QLocale(language)
        if locale.language() != QLocale.Language.C or language in ('C', 'POSIX'):
            return locale
    return QLocale(system.name())


@dataclass(frozen=True, slots=True)
class Preferences:
    """Weekdays use Python's convention: Monday=0 through Sunday=6."""
    start_weekday: int | str = 'system'
    date_format: str = 'system'
    time_format: str = 'system'
    conflict_notifications: str = 'in_app'
    auto_sync_enabled: bool = True
    sync_interval_minutes: int = 1
    locale_name: str = 'system'
    font_scale: int = 100
    show_week_numbers: bool = True
    visible_start_hour: int = 8
    visible_end_hour: int = 18
    color_scheme: str = 'neutral'
    agenda_style: str = 'timeline'
    control_style: str = 'underlined'
    appearance_mode: str = 'light'
    custom_accent: str = CUSTOM_COLORS['custom_accent']
    custom_surface: str = CUSTOM_COLORS['custom_surface']
    custom_weekend: str = CUSTOM_COLORS['custom_weekend']
    weekend_style: str = 'text'
    show_sidebar_title: bool = True
    sidebar_title: str = 'debacal'
    duration_presets_minutes: tuple[int, ...] = (30, 45, 60)

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
        if self.locale_name not in ('system', 'en_GB', 'de_DE'):
            raise ValueError('Choose the system, English, or German locale for dates.')
        if type(self.font_scale) is not int or not 80 <= self.font_scale <= 140:
            raise ValueError('Text size must be between 80 and 140 percent.')
        if type(self.show_week_numbers) is not bool:
            raise ValueError('Week numbers must be enabled or disabled.')
        if (type(self.visible_start_hour) is not int or type(self.visible_end_hour) is not int
                or not 0 <= self.visible_start_hour < self.visible_end_hour <= 24):
            raise ValueError('Visible hours must start before they end, between 00:00 and 24:00.')
        if not isinstance(self.color_scheme, str) or self.color_scheme not in (*SCHEMES, 'custom'):
            raise ValueError('Choose a predefined or custom color palette.')
        if not isinstance(self.agenda_style, str) or self.agenda_style not in ('cards', 'grouped', 'timeline'):
            raise ValueError('Choose event cards, cards grouped by day, or a day timeline.')
        if not isinstance(self.control_style, str) or self.control_style not in ('underlined', 'filled'):
            raise ValueError('Choose underlined or filled button groups.')
        if self.appearance_mode not in ('light', 'dark'):
            raise ValueError('Choose a light or dark appearance.')
        for field in CUSTOM_COLORS:
            if not valid_rgb(getattr(self, field)):
                raise ValueError('Use a six-digit RGB color such as #475f52.')
        if self.weekend_style not in ('text', 'background', 'none'):
            raise ValueError('Choose text, background, or no weekend highlighting.')
        if type(self.show_sidebar_title) is not bool:
            raise ValueError('The sidebar title must be shown or hidden.')
        if (not isinstance(self.sidebar_title, str) or not self.sidebar_title.strip()
                or len(self.sidebar_title) > 120 or '\n' in self.sidebar_title or '\r' in self.sidebar_title):
            raise ValueError('Choose a sidebar title of 1 to 120 characters on one line.')
        presets = self.duration_presets_minutes
        if (not isinstance(presets, (tuple, list)) or not 1 <= len(presets) <= 6
                or any(type(value) is not int or not 1 <= value <= 10080 for value in presets)
                or len(set(presets)) != len(presets)):
            raise ValueError('Choose one to six different duration buttons between 1 minute and 7 days.')
        object.__setattr__(self, 'duration_presets_minutes', tuple(presets))

    def qt_locale(self) -> QLocale:
        """Locale for date names, independent of regional formatting preferences."""
        return _system_language_locale() if self.locale_name == 'system' else QLocale(self.locale_name)

    @property
    def first_weekday(self) -> int:
        return _system_locale().firstDayOfWeek().value - 1 if self.start_weekday == 'system' else self.start_weekday

    def week_start(self, value: date) -> date:
        return value - timedelta(days=(value.weekday() - self.first_weekday) % 7)

    def week_number(self, value: date) -> int:
        # ISO week numbers belong to Thursdays. Use the Thursday in the visible
        # week even when its first day is configured as Sunday or Saturday.
        start = value.toordinal() - (value.weekday() - self.first_weekday) % 7
        thursday = start + (3 - self.first_weekday) % 7
        return date.fromordinal(min(date.max.toordinal(), max(1, thursday))).isocalendar().week

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
        formatted = self.qt_locale().toString(QDate(value.year, value.month, value.day), self.qt_date_pattern())
        return self.weekday_name(value) + ', ' + formatted if weekday else formatted

    def format_time(self, value: time | datetime) -> str:
        return _system_locale().toString(QTime(value.hour, value.minute, value.second), self.qt_time_pattern())

    def weekday_name(self, value: date | int, *, short: bool = True) -> str:
        day = value.weekday() if isinstance(value, date) else value
        if not 0 <= day <= 6:
            raise ValueError('Weekday must be between 0 and 6.')
        form = QLocale.FormatType.ShortFormat if short else QLocale.FormatType.LongFormat
        return self.qt_locale().dayName(day + 1, form)

    def month_name(self, value: date | int, *, short: bool = False) -> str:
        month = value.month if isinstance(value, date) else value
        if type(month) is not int or not 1 <= month <= 12:
            raise ValueError('Month must be between 1 and 12.')
        form = QLocale.FormatType.ShortFormat if short else QLocale.FormatType.LongFormat
        return self.qt_locale().standaloneMonthName(month, form)

    def format_month(self, value: date, *, year: bool = True) -> str:
        return self.qt_locale().toString(QDate(value.year, value.month, 1), 'MMMM yyyy' if year else 'MMMM')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Any) -> Preferences:
        if not isinstance(values, dict):
            return cls()
        # Settings from older versions or manual edits must not prevent startup.
        valid = {}
        for field in (item.name for item in fields(cls) if item.name not in ('visible_start_hour', 'visible_end_hour')):
            if field not in values:
                continue
            try:
                cls(**{field: values[field]})
            except (TypeError, ValueError):
                continue
            valid[field] = values[field]
        start, end = values.get('visible_start_hour', 8), values.get('visible_end_hour', 18)
        start = start if type(start) is int and 0 <= start <= 23 else 8
        end = end if type(end) is int and 1 <= end <= 24 else 18
        if start < end:
            valid.update(visible_start_hour=start, visible_end_hour=end)
        return cls(**valid)

    @classmethod
    def load(cls, store) -> Preferences:
        with store._transaction():
            values = store.get_setting(SETTINGS_KEY, {})
            if store.get_setting(WEEK_NUMBERS_DEFAULT_KEY, 0) != 1:
                # Older versions saved the previous false default with every
                # preferences change. Enable the new default once; subsequent
                # choices, including switching it off again, remain explicit.
                values = dict(values) if isinstance(values, dict) else {}
                values['show_week_numbers'] = True
                store.set_setting(SETTINGS_KEY, values)
                store.set_setting(WEEK_NUMBERS_DEFAULT_KEY, 1)
            return cls.from_dict(values)

    def save(self, store) -> None:
        with store._transaction():
            store.set_setting(SETTINGS_KEY, self.to_dict())
            store.set_setting(WEEK_NUMBERS_DEFAULT_KEY, 1)


def format_date(value: date, preferences: Preferences | None = None, *, weekday: bool = False) -> str:
    return (preferences or Preferences()).format_date(value, weekday=weekday)


def format_time(value: time | datetime, preferences: Preferences | None = None) -> str:
    return (preferences or Preferences()).format_time(value)
