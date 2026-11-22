"""Compact alarm and inbox labels for the editor and reminder list."""
from collections import defaultdict
from datetime import timedelta

from PySide6.QtCore import QTime

from .alarm_series import is_relative
from .alarms import signed_duration
from .date_time_fields import format_duration


def offset_magnitude(trigger):
    days, seconds = map(abs, signed_duration(trigger))
    parts = [f'{days}d'] if days else []
    if seconds:
        parts.append(format_duration(timedelta(seconds=seconds), elapsed=True))
    return ' '.join(parts) or '0min'


def _row_text(entry, preferences, zone, calendar):
    due = entry.reminder
    trigger = due.key.trigger_utc.astimezone(zone)
    start = (due.start or trigger).astimezone(zone)
    def time(value):
        if not value.second:
            return preferences.format_time(value)
        pattern = preferences.qt_time_pattern()
        if 's' not in pattern:
            pattern = pattern.replace('mm', 'mm:ss')
        return preferences.qt_locale().toString(QTime(value.hour, value.minute, value.second), pattern)
    def timestamp(value):
        return f'{preferences.format_date(value.date())} {time(value)}'
    alarm = due.alarm
    if is_relative(alarm):
        offset = offset_magnitude(alarm.trigger)
        when = 'before' if alarm.trigger.startswith('-') else 'after'
        rule = f'At {alarm.related.lower()}' if offset == '0min' else f'{offset} {when} {alarm.related.lower()}'
    else:
        rule = 'Fixed time'
    if alarm.repeat:
        rule += f' · {due.key.repeat_index + 1}/{alarm.repeat + 1}'
    if alarm.action != 'DISPLAY':
        rule += ' · ' + alarm.action.capitalize()
    alarm_time = time(trigger) if start.date() == trigger.date() else timestamp(trigger)
    if trigger.replace(fold=0).utcoffset() != trigger.replace(fold=1).utcoffset():
        alarm_time += ' ' + trigger.strftime('%z')
    status = 'Unread' if entry.unread and entry.status == 'delivered' else entry.status.capitalize()
    fields = [due.title + '\n' + rule, timestamp(start) + '\nAlarm ' + alarm_time, calendar, status]
    tips = [fields[0] + ('\n' + alarm.description if alarm.description else ''),
            'Event ' + timestamp(start) + '\nAlarm ' + timestamp(trigger) + ' ' + trigger.strftime('%z'),
            calendar, entry.status.capitalize() + (' · Unread' if entry.unread else ' · Read')]
    if alarm.repeat:
        tips[0] += f'\nRepeats {alarm.repeat} × every {offset_magnitude(alarm.repeat_interval)}'
    if entry.snoozed_until:
        until = timestamp(entry.snoozed_until.astimezone(zone))
        tips[3] = f'Snoozed until {until}' + (' · Unread' if entry.unread else ' · Read')
    return fields, tips


def inbox_rows(entries, preferences, zone, calendars):
    rows = []
    duplicates = defaultdict(list)
    for entry in entries:
        due = entry.reminder
        calendar = calendars.get(due.calendar_id)
        fields, tips = _row_text(entry, preferences, zone, calendar.name if calendar else due.calendar_id)
        rows.append((entry, calendar, fields, tips))
        duplicates[due.key.source_id, due.key.occurrence_key, *fields[:2]].append((fields, tips))
    for group in duplicates.values():
        if len(group) > 1:
            for index, (fields, tips) in enumerate(group, 1):
                prefix = f'Alarm {index} · '
                fields[0] = fields[0].replace('\n', '\n' + prefix, 1)
                tips[0] = tips[0].replace('\n', '\n' + prefix, 1)
    return rows
