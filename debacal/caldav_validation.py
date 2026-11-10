"""Small, conservative repairs and checks at the CalDAV upload boundary.

This is not an independent iCalendar parser. The event codec owns modeled
fields; these checks cover retained metadata that a strict storage server can
reject even when the event itself was imported successfully.
"""
from __future__ import annotations

import re

from . import ics_fallback as fallback


# RFC 5545 section 3.6.1: these retained event properties are optional
# singletons. Attendees, categories, attachments and extension properties may
# repeat and must never be collapsed by this transport check.
_EVENT_SINGLETONS = frozenset({
    'CLASS', 'CREATED', 'COLOR', 'GEO', 'LAST-MODIFIED', 'ORGANIZER',
    'PRIORITY', 'SEQUENCE', 'STATUS', 'TRANSP', 'URL',
})
_VALUE_TYPES = frozenset({
    'BINARY', 'BOOLEAN', 'CAL-ADDRESS', 'DATE', 'DATE-TIME', 'DURATION',
    'FLOAT', 'INTEGER', 'PERIOD', 'RECUR', 'TEXT', 'TIME', 'URI', 'UTC-OFFSET',
})


def normalize_header_tokens(header):
    """Remove invalid token whitespace without changing text/quoted values.

    Some producers write names such as ``REFRESH - INTERVAL; VALUE = DURATION``.
    The maintained icalendar parser accepts them, but retaining their spelling
    would make the edited event fail a strict server's lexical parser.
    """
    result = []
    for line in header:
        parts = fallback._unquoted_parts(line, ':')
        if len(parts) < 2:
            result.append(line)
            continue
        names = fallback._unquoted_parts(parts[0], ';')
        normalized = [re.sub(r'[ \t]+', '', names[0])]
        for parameter in names[1:]:
            name, separator, value = parameter.partition('=')
            name = re.sub(r'[ \t]+', '', name)
            # VALUE is an enumerated token, not display text. Recognize a
            # complete known value before trimming its unquoted padding.
            if name.upper() == 'VALUE' and value.strip().upper() in _VALUE_TYPES:
                value = value.strip()
            normalized.append(name + separator + value)
        result.append(';'.join(normalized) + ':' + ':'.join(parts[1:]))
    return result


def normalize_event_metadata(blocks):
    """Collapse exact singleton duplicates; require review of conflicting ones.

    Choosing among different organizers or statuses can change scheduling
    meaning. Report the field without putting its private values in a log.
    Nested alarms and all unknown/repeatable properties remain untouched.
    """
    normalized = []
    for block in blocks:
        if not block or block[0] != 'BEGIN:VEVENT':
            normalized.append(block)
            continue
        result, seen, depth = [], {}, 0
        for line in block:
            key, _separator, value = line.partition(':')
            if key.upper() == 'BEGIN':
                depth += 1
            elif key.upper() == 'END':
                depth -= 1
            name = key.split(';', 1)[0].upper()
            if depth == 1 and name in _EVENT_SINGLETONS:
                if name in seen:
                    if seen[name] == line:
                        continue
                    raise fallback.CalendarValueError(
                        f'Conflicting {name} properties in an event cannot be uploaded to CalDAV. '
                        'Review the original resource and keep the intended value; '
                        'the local event and its pending changes are retained.')
                seen[name] = line
            result.append(line)
        normalized.append(result)
    return normalized
