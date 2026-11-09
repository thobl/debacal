"""Verified derived end-range exceptions at the iCalendar boundary.

The local Cut policy stays symbolic. Other clients receive ordinary detached
VEVENT ranges; an unchanged Debacal round trip recognizes and discards only
those derived copies. Every uncertainty preserves the standard calendar data.
"""
from collections import OrderedDict
from dataclasses import replace
from datetime import timezone
import hashlib
import json
from threading import Lock

from . import ics_fallback as fallback


POLICY = 'X-DEBACAL-RANGE-POLICY'
SIGNATURE = 'X-DEBACAL-RANGE-SIGNATURE'
GENERATED = 'X-DEBACAL-RANGE-GENERATED'
PROPERTIES = frozenset((POLICY, SIGNATURE, GENERATED))
MAX_GENERATED_RANGES = 100000
_CACHE = OrderedDict()
_CACHE_LOCK = Lock()
_GENERATED_FIELDS = frozenset((
    'UID', 'DTSTART', 'DTEND', 'DURATION', 'RECURRENCE-ID', 'SUMMARY',
    'DESCRIPTION', 'LOCATION', 'DTSTAMP', 'SEQUENCE', 'CREATED', 'LAST-MODIFIED',
    'X-DEBACAL-ID', 'X-DEBACAL-CALENDAR', 'X-DEBACAL-GROUP', 'X-DEBACAL-CANDIDATE', GENERATED,
))


def signature(event):
    """Hash normalized scheduling and shared-content fields, never local IDs."""
    from .recurrence import canonical_rule, normalized_rule
    def stamp(value):
        return (value.date().isoformat() if event.all_day else
                value.replace(tzinfo=None).isoformat() if event.floating_time else
                value.astimezone(timezone.utc).isoformat())
    value = {
        'version': 1, 'policy': 'cut', 'uid': event.ical_uid,
        'start': stamp(event.start), 'end': stamp(event.end), 'all_day': event.all_day,
        'zone': '' if event.all_day else event.timezone_id,
        'rule': normalized_rule(canonical_rule(event.recurrence_rule, event), event),
        'add': sorted(event.recurrence_dates), 'omit': sorted(event.excluded_dates),
        'title': event.title, 'description': event.description, 'location': event.location,
        'candidate': event.candidate, 'group': event.group_id or '',
    }
    if event.duration_spec:
        value['duration_spec'] = event.duration_spec
    if event.floating_time:
        value['floating_time'] = True
    if not event.all_day and event.timezone_id in event.timezone_definitions:
        # Equal current offsets do not imply equal future transitions. Include
        # resource rules so the range cache cannot leak across reused TZIDs.
        value['timezone_definition'] = fallback.unfolded(event.timezone_definitions[event.timezone_id])
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def expected_ranges(event):
    """Bounded cache of scheduling triples, with no detached Event payloads."""
    from .range_policies import iter_cut_ranges
    fingerprint = signature(event)
    with _CACHE_LOCK:
        cached = _CACHE.get(fingerprint)
        if cached is not None:
            _check_budget(len(cached))
            _CACHE.move_to_end(fingerprint)
            return cached
    # Explicit exceptions are handled separately by both import and export;
    # the signature deliberately does not depend on their private payloads.
    bare = replace(event, recurrence_overrides={}, range_policy='cut')
    result = {}
    for key, start, end in iter_cut_ranges(bare):
        result[key] = (start, end)
        _check_budget(len(result))
    with _CACHE_LOCK:
        _CACHE[fingerprint] = result
        if len(_CACHE) > 12:
            _CACHE.popitem(last=False)
    return result


def _check_budget(size):
    if size > MAX_GENERATED_RANGES:
        raise ValueError('Cut needs more than 100,000 calendar range exceptions. '
                         'Shorten the recurrence or choose Skip or Shift before exporting or syncing.')


def generated_ranges(event):
    if getattr(event, 'range_policy', '') != 'cut':
        return ()
    return ((key, *bounds) for key, bounds in expected_ranges(event).items()
            if key not in event.recurrence_overrides and key not in event.excluded_dates)


def safe_generated_block(block):
    """Unknown fields or nested alarms are a manual edit, never disposable."""
    for line in block[1:-1]:
        field = line.partition(':')[0].split(';', 1)[0].upper()
        if field not in _GENERATED_FIELDS:
            return False
    return True


def matches_generated(component, event, key, expected, *, decode, tz, fingerprint=None):
    """Check a tagged range without constructing a large override payload."""
    if fallback.prop(component, GENERATED) != (fingerprint or signature(event)) or key not in expected:
        return False
    start, end = expected[key]
    try:
        actual_start = decode(component, 'DTSTART', tz)[0]
        actual_end = decode(component, 'DTEND', tz)[0]
    except (KeyError, IndexError, ValueError, TypeError, OverflowError):
        return False
    from datetime import datetime
    if event.all_day:
        if isinstance(actual_start, datetime) or isinstance(actual_end, datetime):
            return False
        same_range = (actual_start, actual_end) == (start.date(), end.date())
    else:
        if not isinstance(actual_start, datetime) or not isinstance(actual_end, datetime):
            return False
        if event.floating_time:
            def floating_property(name):
                parameters, raw = component.get(name, [({}, '')])[0]
                return bool(raw and not parameters.get('TZID') and parameters.get('VALUE') != 'DATE'
                            and not raw.upper().endswith('Z'))
            # Decoders attach the current viewer zone for display. Verify the
            # authored value type, then compare clocks without that attachment.
            same_range = (floating_property('DTSTART') and floating_property('DTEND')
                          and (actual_start.replace(tzinfo=None), actual_end.replace(tzinfo=None))
                              == (start.replace(tzinfo=None), end.replace(tzinfo=None)))
        else:
            def instant(value):
                return (value if value.tzinfo else value.replace(tzinfo=tz)).timestamp()
            same_range = (instant(actual_start), instant(actual_end)) == (start.timestamp(), end.timestamp())
    return (same_range and fallback.prop(component, 'STATUS').upper() != 'CANCELLED'
            and all(fallback.prop(component, name, value) == value for name, value in (
                ('SUMMARY', event.title), ('DESCRIPTION', event.description), ('LOCATION', event.location)))
            and fallback.prop(component, 'X-DEBACAL-CANDIDATE', str(event.candidate)).upper()
                in ({'TRUE'} if event.candidate else {'FALSE', ''})
            and fallback.prop(component, 'X-DEBACAL-GROUP', event.group_id or '') == (event.group_id or ''))
