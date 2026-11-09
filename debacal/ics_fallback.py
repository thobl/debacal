"""Offline iCalendar subset used when optional calendar packages are unavailable.

It preserves ordinary VEVENT masters and detached recurrence exceptions. The
icalendar package handles richer input syntax when installed. Date values use
resource-scoped VTIMEZONE definitions or the IANA timezone database.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re

from .models import Event


def unfolded(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    logical = re.sub(r"(?:\r\n|\r|\n)[ \t]", "", data).replace("\r\n", "\n").replace("\r", "\n")
    # Unicode line/paragraph separators are valid TEXT content, including
    # whitespace in a custom sequence. Only physical ICS newlines split lines.
    lines = logical.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return lines


def _unquoted_parts(value, delimiter):
    parts, start, quoted, escaped_quote = [], 0, False, False
    for index, character in enumerate(value):
        if character == '"' and not escaped_quote:
            quoted = not quoted
        if character == delimiter and not quoted:
            parts.append(value[start:index])
            start = index + 1
        escaped_quote = character == '\\' and not escaped_quote
    parts.append(value[start:])
    return parts


def content_line(line):
    """Read a content line without splitting quoted parameter delimiters."""
    if '"' not in line:
        name, separator, value = line.partition(':')
        if not separator:
            raise ValueError('Malformed iCalendar property.')
        key, *parameters = name.split(';')
    else:
        parts = _unquoted_parts(line, ':')
        if len(parts) < 2:
            raise ValueError('Malformed iCalendar property.')
        name, value = parts[0], ':'.join(parts[1:])
        key, *parameters = _unquoted_parts(name, ';')
    params = {}
    for item in parameters:
        pkey, separator, pvalue = item.partition('=')
        if not separator:
            raise ValueError('Malformed iCalendar parameter.')
        if pvalue.startswith('"') and pvalue.endswith('"'):
            pvalue = pvalue[1:-1]
        pvalue = re.sub(r"\^(\^|[nN]|')", lambda match:
                        '^' if match[1] == '^' else '"' if match[1] == "'" else '\n', pvalue)
        params[pkey.upper()] = pvalue.upper() if pkey.upper() == 'VALUE' else pvalue
    return key.upper(), params, value


def parameter(value):
    """Encode a parameter value, including RFC 6868 quotes/newlines."""
    value = str(value).replace('^', '^^').replace('\r\n', '\n').replace('\r', '\n')
    value = value.replace('\n', '^n').replace('"', "^'")
    return '"' + value + '"' if any(character in value for character in ':;,') else value


def components(data):
    lines = unfolded(data)
    markers = {line.upper() for line in lines}
    if "BEGIN:VCALENDAR" not in markers or "END:VCALENDAR" not in markers:
        raise ValueError("Expected a complete VCALENDAR document.")
    result, current, nested = [], None, 0
    for line in lines:
        if not line:
            continue
        key, parameters, value = content_line(line)
        if key == 'BEGIN' and value.upper() == 'VEVENT':
            if current is not None:
                raise ValueError("Unexpected nested VEVENT.")
            current, nested = {}, 0
        elif key == 'END' and value.upper() == 'VEVENT':
            if current is None:
                raise ValueError("Unexpected END:VEVENT.")
            result.append(current)
            current = None
        elif current is not None:
            if key == 'BEGIN':
                nested += 1
            elif key == 'END':
                nested -= 1
            elif not nested and line:
                current.setdefault(key, []).append((parameters, value))
    if current is not None:
        raise ValueError("Unterminated VEVENT.")
    return result


def text(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def escaped(value):
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def prop(component, key, default=""):
    return text(component[key][0][1]) if key in component else default


def dt(value, params, tz, definitions=None):
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time.min, tzinfo=tz), True
        clock = value.upper()
        zone = timezone.utc if clock.endswith("Z") else tz
        if "TZID" in params and not clock.endswith("Z"):
            from .timezones import resolve_timezone
            zone = resolve_timezone(params["TZID"], definitions)
        fmt = "%Y%m%dT%H%M%S" if len(clock.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
        return datetime.strptime(clock.rstrip("Z"), fmt).replace(tzinfo=zone), False
    except ValueError as exc:
        from .timezones import TimezoneError
        if isinstance(exc, TimezoneError):
            raise
        raise ValueError(f"Invalid iCalendar date/time {value!r}: {exc}") from exc


def duration(value):
    match = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match:
        raise ValueError(f"Unsupported duration: {value}")
    w, d, h, m, s = (int(v or 0) for v in match.groups())
    return timedelta(weeks=w, days=d, hours=h, minutes=m, seconds=s)


def parse(data, calendar_id, window_start=None, window_end=None, *, limit=10000, read_only_recurring=False):
    from .ics_resources import parse as parse_resources
    return parse_resources(data, calendar_id, window_start, window_end, limit=limit, use_full=False)


def event_lines(event):
    if event.all_day:
        dates = [f"DTSTART;VALUE=DATE:{event.start:%Y%m%d}", f"DTEND;VALUE=DATE:{event.end:%Y%m%d}"]
    else:
        dates = [f"DTSTART:{event.start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"]
        if event.end.timestamp() > event.start.timestamp():
            dates.append(f"DTEND:{event.end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}")
    result = ["BEGIN:VEVENT", f"UID:{escaped(event.id)}", f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}",
              f"SUMMARY:{escaped(event.title)}", *dates, f"DESCRIPTION:{escaped(event.description)}",
              f"LOCATION:{escaped(event.location)}", f"X-DEBACAL-ID:{escaped(event.id)}", f"X-DEBACAL-CALENDAR:{escaped(event.calendar_id)}"]
    if event.group_id:
        result.append(f"X-DEBACAL-GROUP:{escaped(event.group_id)}")
    if event.candidate:
        result.append("X-DEBACAL-CANDIDATE:TRUE")
    return [*result, "END:VEVENT"]


def folded(lines):
    output = []
    for line in lines:
        raw = line.encode('utf-8')
        if len(raw) <= 75:
            output.append(raw)
            continue
        offset = 0
        while offset < len(raw):
            prefix = b' ' if offset else b''
            end = min(offset + 75 - len(prefix), len(raw))
            # Never split a UTF-8 code point. The continuation's leading space
            # is part of the RFC 5545 limit of 75 octets per physical line.
            while end < len(raw) and raw[end] & 0xc0 == 0x80:
                end -= 1
            output.append(prefix + raw[offset:end])
            offset = end
    return b'\r\n'.join(output) + b'\r\n'


def export(events):
    from .ics_resources import export as export_resources
    return export_resources(events)


def patch(data, event):
    from .ics_resources import patch as patch_resource
    return patch_resource(data, event)
