"""Whole-value date/time and duration input shared by event editors and pickers."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
import warnings

from dateutil.parser import UnknownTimezoneWarning, parse as parse_date
from PySide6.QtCore import QDate, QDateTime, QEvent, QLocale, QTime, QTimeZone, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from .buttons import icon_button
from .preferences import Preferences
from .timeutil import now


def validate_datetime(value):
    """Reject impossible local clocks without normalizing an event silently."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Choose a date and time with a timezone.')
    returned = value.astimezone(timezone.utc).astimezone(value.tzinfo)
    if returned.replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError('This local time does not exist because the clocks change. Choose another time.')
    return value


def _day_first(preferences):
    if preferences.date_format == 'us':
        return False
    if preferences.date_format in ('european', 'iso'):
        return True
    pattern = preferences.qt_date_pattern().lower()
    return pattern.find('d') < pattern.find('m')


def _translated_months(text, preferences):
    # Date names may be typed in either supported language, independently of
    # the regional numeric format. Word boundaries avoid changing other text.
    for locale in (preferences.qt_locale(), QLocale('de_DE')):
        for month in range(1, 13):
            english = QLocale('en_GB').monthName(month, QLocale.FormatType.LongFormat)
            for form in (QLocale.FormatType.LongFormat, QLocale.FormatType.ShortFormat):
                name = locale.monthName(month, form).rstrip('.')
                if name:
                    text = re.sub(r'(?<!\w)' + re.escape(name) + r'\.?(?!\w)', english, text, flags=re.I)
    return text


def parse_datetime(text, preferences=None, reference=None, *, all_day=False):
    """Accept padded/unpadded numeric dates, ISO and localized month names.

    Ambiguous slash dates follow the chosen regional format. A dot is always
    day-first and a leading four-digit year is always year-first. Omitted date
    parts and sub-minute precision inherit the reference value.
    """
    preferences = preferences or Preferences()
    reference = reference or now()
    if not isinstance(reference, datetime) or reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError('Choose a date and time with a timezone.')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Enter a date.' if all_day else 'Enter a date and time.')
    value = _translated_months(text.strip(), preferences)
    # Accept the optional separator often used in written European dates.
    value = re.sub(r'(?<=\d)\s*,\s*(?=\d{1,2}:)', ' ', value)
    leading_year = bool(re.match(r'\d{4}[-/.]', value))
    dotted = bool(re.match(r'\d{1,2}\.\d{1,2}\.', value))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', UnknownTimezoneWarning)
            parsed = parse_date(value, default=reference.replace(tzinfo=None),
                                dayfirst=dotted or (_day_first(preferences) and not leading_year),
                                yearfirst=leading_year)
    except (ValueError, OverflowError, TypeError, UnknownTimezoneWarning) as exc:
        example = preferences.format_date(reference.date())
        if not all_day:
            example += ' ' + preferences.format_time(reference)
        raise ValueError('Use a date' + ('' if all_day else ' and time') + ' such as ' + example + '.') from exc
    if all_day:
        return validate_datetime(datetime.combine(parsed.date(), time.min, reference.tzinfo))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=reference.tzinfo, fold=reference.fold)
    else:
        parsed = parsed.astimezone(reference.tzinfo)
    return validate_datetime(parsed)


def parse_duration(text, *, all_day=False):
    """Parse a positive duration, using days for bare all-day values."""
    if isinstance(text, timedelta):
        value = text
    else:
        if not isinstance(text, str):
            raise ValueError('Enter a duration such as 45min or 1h 30min.')
        normalized = text.strip().lower().replace(',', '.')
        clock = re.fullmatch(r'(\d+):(\d{1,2})(?::(\d{1,2}))?', normalized)
        try:
            if clock:
                if int(clock[2]) > 59 or (clock[3] and int(clock[3]) > 59):
                    raise ValueError
                value = timedelta(hours=int(clock[1]), minutes=int(clock[2]), seconds=int(clock[3] or 0))
            else:
                normalized = re.sub(r'\s+', '', normalized)
                if re.fullmatch(r'\d+(?:\.\d+)?', normalized):
                    normalized += 'd' if all_day else 'min'
                unit_values = {'d': 86400, 'day': 86400, 'days': 86400,
                               'h': 3600, 'hr': 3600, 'hour': 3600, 'hours': 3600,
                               'm': 60, 'min': 60, 'mins': 60, 'minute': 60, 'minutes': 60,
                               's': 1, 'sec': 1, 'secs': 1}
                pieces = list(re.finditer(r'(\d+(?:\.\d+)?)([a-z]+)', normalized))
                if not pieces or ''.join(piece[0] for piece in pieces) != normalized:
                    raise ValueError
                seconds = sum(Decimal(piece[1]) * unit_values[piece[2]] for piece in pieces)
                microseconds = seconds * 1_000_000
                if microseconds != microseconds.to_integral_value():
                    raise ValueError
                value = timedelta(microseconds=int(microseconds))
        except (ValueError, KeyError, InvalidOperation, OverflowError) as exc:
            raise ValueError('Enter a duration such as 45min, 1h 30min or 2d.') from exc
    if value <= timedelta(0):
        raise ValueError('The duration must be positive.')
    if all_day and value % timedelta(days=1):
        raise ValueError('All-day durations must contain whole days.')
    return value


def format_duration(value, *, all_day=False):
    value = parse_duration(value, all_day=all_day)
    if all_day:
        return str(value.days) + (' day' if value.days == 1 else ' days')
    microseconds = (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    days, rest = divmod(microseconds, 86_400_000_000)
    hours, rest = divmod(rest, 3_600_000_000)
    minutes, rest = divmod(rest, 60_000_000)
    seconds = (format(Decimal(rest) / 1_000_000, 'f').rstrip('0').rstrip('.')
               if rest % 1_000_000 else str(rest // 1_000_000))
    return ' '.join(part for part in (f'{days}d' if days else '', f'{hours}h' if hours else '',
                                       f'{minutes}min' if minutes else '', f'{seconds}s' if rest else '') if part)


class DateTimeField(QWidget):
    """A whole date/time value with an explicit, shared-picker affordance."""
    dateTimeChanged = Signal(QDateTime)
    dateChanged = Signal(QDate)
    editingFinished = Signal()
    picker_requested = Signal()

    def __init__(self, preferences=None, parent=None):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self._value = now()
        self._all_day = False
        self._display_format = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.edit = QLineEdit(self)
        self.button = icon_button('day', 'Choose date and time', preferences=self.preferences, parent=self.edit, size=26)
        self.button.setObjectName('dateTimePickerButton')
        self.button.clicked.connect(self.picker_requested.emit)
        self.edit.editingFinished.connect(self.editingFinished.emit)
        self.edit.textChanged.connect(lambda _text: self.edit.setProperty('invalid', False))
        layout.addWidget(self.edit, 1)
        self.edit.installEventFilter(self)
        self._place_picker()
        self.setFocusProxy(self.edit)
        self._format()

    def _place_picker(self):
        scale = self.preferences.font_scale / 100
        inset = max(2, round(2 * scale))
        height = max(1, min(round(28 * scale), self.edit.height() - 2 * inset))
        width = max(round(22 * scale), min(round(28 * scale), height + round(4 * scale)))
        self.button.setFixedSize(width, height)
        self.button.move(self.edit.width() - width - inset, (self.edit.height() - height) // 2)
        self.edit.setTextMargins(0, 0, width + inset, 0)
        self.button.raise_()

    def eventFilter(self, watched, event):
        if watched is self.edit and event.type() in (QEvent.Type.Resize, QEvent.Type.StyleChange):
            self._place_picker()
        return super().eventFilter(watched, event)

    def _format(self):
        if self._display_format:
            caption = self.preferences.qt_locale().toString(self.dateTime(), self._display_format)
        else:
            caption = self.preferences.format_date(self._value.date())
            if not self._all_day:
                caption += ' ' + self.preferences.format_time(self._value)
        self.edit.setText(caption)

    def set_value(self, value):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Choose a date and time with a timezone.')
        changed = (value.timestamp(), str(value.tzinfo), value.fold) != (
            self._value.timestamp(), str(self._value.tzinfo), self._value.fold)
        previous_date = self._value.date()
        self._value = value
        self._format()
        if changed:
            self.dateTimeChanged.emit(self.dateTime())
            if previous_date != value.date():
                self.dateChanged.emit(self.date())

    def value(self):
        # Returning an unchanged value must retain imported precision/fold.
        previous = self.edit.text()
        if self._display_format:
            formatted = self.preferences.qt_locale().toString(self.dateTime(), self._display_format)
        else:
            formatted = self.preferences.format_date(self._value.date())
            if not self._all_day:
                formatted += ' ' + self.preferences.format_time(self._value)
        if previous.strip() == formatted:
            value = self._value.replace(hour=0, minute=0, second=0, microsecond=0) if self._all_day else self._value
            return validate_datetime(value)
        return parse_datetime(previous, self.preferences, self._value, all_day=self._all_day)

    def commit(self):
        self.set_value(self.value())
        return self._value

    def dateTime(self):
        value = self._value.replace(hour=0, minute=0, second=0, microsecond=0) if self._all_day else self._value
        return QDateTime(QDate(value.year, value.month, value.day),
                         QTime(value.hour, value.minute, value.second, value.microsecond // 1000), QTimeZone.utc())

    def setDateTime(self, value):
        if isinstance(value, datetime):
            return self.set_value(value if value.tzinfo else value.replace(tzinfo=self._value.tzinfo))
        date, clock = value.date(), value.time()
        candidate = self._value.replace(year=date.year(), month=date.month(), day=date.day(), hour=clock.hour(),
                                        minute=clock.minute(), second=clock.second(), microsecond=clock.msec() * 1000)
        if candidate.microsecond == self._value.microsecond // 1000 * 1000:
            candidate = candidate.replace(microsecond=self._value.microsecond)
        self.set_value(candidate)

    def date(self):
        value = self._value.date()
        return QDate(value.year, value.month, value.day)

    def setDate(self, value):
        day = value.toPython() if isinstance(value, QDate) else value
        self.set_value(self._value.replace(year=day.year, month=day.month, day=day.day))

    def setDisplayFormat(self, pattern):
        self._display_format = pattern
        self._format()

    def displayFormat(self):
        return self._display_format or (self.preferences.qt_date_pattern() if self._all_day else self.preferences.qt_datetime_pattern())

    def setAllDay(self, enabled):
        self._all_day = bool(enabled)
        self._display_format = None
        caption = 'Choose date' if enabled else 'Choose date and time'
        self.button.setToolTip(caption)
        self.button.setAccessibleName(caption)
        self._format()

    def isAllDay(self):
        return self._all_day

    def text(self):
        return self.edit.text()

    def setText(self, text):
        self.edit.setText(text)

    def lineEdit(self):
        return self.edit

    def selectAll(self):
        self.edit.selectAll()

    def interpretText(self):
        return self.commit()
