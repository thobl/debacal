"""Separate RFC 5545 streams without sharing calendar-level timezone scope."""
from __future__ import annotations


def calendar_documents(lines):
    """Yield complete VCALENDAR documents, rejecting ambiguous boundaries.

    A stream can contain multiple calendars (RFC 5545 section 3.4). Definitions
    belong to their own envelope, even if another calendar reuses a TZID. Do
    not flatten them into one calendar or recover a damaged nesting by guessing
    where a timezone definition ends.
    """
    current = None
    for line in lines:
        marker = line.upper()
        if marker == 'BEGIN:VCALENDAR':
            if current is not None:
                raise ValueError('Nested or unclosed VCALENDAR in calendar stream.')
            current = [line]
        elif marker == 'END:VCALENDAR':
            if current is None:
                raise ValueError('Unexpected END:VCALENDAR in calendar stream.')
            current.append(line)
            yield current
            current = None
        elif current is not None:
            current.append(line)
        elif line.strip():
            raise ValueError('Unexpected data outside VCALENDAR in calendar stream.')
    if current is not None:
        raise ValueError('Unclosed VCALENDAR in calendar stream.')
