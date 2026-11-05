"""Offline iCalendar subset used when optional calendar packages are unavailable.

It preserves ordinary VEVENT masters and detached recurrence exceptions. The
icalendar package handles richer input syntax when installed. Custom timezone
definitions that cannot map to an IANA zone fail explicitly.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

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


def components(data):
    lines = unfolded(data)
    if "BEGIN:VCALENDAR" not in lines or "END:VCALENDAR" not in lines:
        raise ValueError("Expected a complete VCALENDAR document.")
    result, current, nested = [], None, 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            if current is not None:
                raise ValueError("Unexpected nested VEVENT.")
            current, nested = {}, 0
        elif line == "END:VEVENT":
            if current is None:
                raise ValueError("Unexpected END:VEVENT.")
            result.append(current)
            current = None
        elif current is not None:
            if line.startswith("BEGIN:"):
                nested += 1
            elif line.startswith("END:"):
                nested -= 1
            elif not nested and line:
                name, sep, value = line.partition(":")
                if not sep:
                    raise ValueError("Malformed iCalendar property.")
                key, *params = name.split(";")
                parameters = {}
                for item in params:
                    pkey, _, pvalue = item.partition("=")
                    parameters[pkey.upper()] = pvalue.strip('"')
                current.setdefault(key.upper(), []).append((parameters, value))
    if current is not None:
        raise ValueError("Unterminated VEVENT.")
    return result


def text(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def escaped(value):
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def prop(component, key, default=""):
    return text(component[key][0][1]) if key in component else default


def dt(value, params, tz):
    try:
        if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
            return datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time.min, tzinfo=tz), True
        zone = timezone.utc if value.endswith("Z") else tz
        if "TZID" in params:
            try:
                zone = ZoneInfo(params["TZID"])
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Timezone {params['TZID']} requires the full icalendar backend.") from exc
        fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
        return datetime.strptime(value.rstrip("Z"), fmt).replace(tzinfo=zone), False
    except ValueError as exc:
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
              f"LOCATION:{escaped(event.location)}", f"X-LEBACAL-ID:{escaped(event.id)}", f"X-LEBACAL-CALENDAR:{escaped(event.calendar_id)}"]
    if event.group_id:
        result.append(f"X-LEBACAL-GROUP:{escaped(event.group_id)}")
    if event.candidate:
        result.append("X-LEBACAL-CANDIDATE:TRUE")
    return [*result, "END:VEVENT"]


def folded(lines):
    output = []
    for line in lines:
        part, size = "", 0
        for char in line:
            length = len(char.encode("utf8"))
            if size + length > 75:
                output.append(part)
                part, size = " ", 1
            part += char
            size += length
        output.append(part)
    return ("\r\n".join(output) + "\r\n").encode()


def export(events):
    from .ics_resources import export as export_resources
    return export_resources(events)


def patch(data, event):
    from .ics_resources import patch as patch_resource
    return patch_resource(data, event)
