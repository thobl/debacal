"""Resolve event-scoped iCalendar timezone rules without guessing aliases.

VTIMEZONE names are identifiers, not necessarily IANA database keys. Definitions
travel with their resource and override database rules when present. Two
resources can use the same TZID for different rules, so caches include the text.
"""
from __future__ import annotations

from collections.abc import Mapping
from bisect import bisect_left, bisect_right
from datetime import tzinfo
from functools import lru_cache
import re
from threading import RLock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .recurrence_budget import BoundedRule


MAX_DEFINITION_BYTES = 256 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_CONTEXT_ZONES = 64
MAX_OBSERVANCES = 256
MAX_CACHED_DEFINITION_BYTES = 64 * 1024
MAX_TRANSITION_PERIODS = 20_000
MAX_IDENTIFIER_LENGTH = 1024


class TimezoneError(ValueError):
    """A timezone cannot be interpreted safely; retain its original resource."""


def _validate_identifier(identifier):
    if (not isinstance(identifier, str) or not identifier or len(identifier) > MAX_IDENTIFIER_LENGTH
            or any(character in identifier for character in '\r\n\x00')):
        raise TimezoneError('A timezone identifier must be nonempty text of at most 1,024 characters '
                            'without line breaks or NUL characters.')


class _TransitionIndex:
    """Seek cached transitions in logarithmic time, extending them lazily.

    Dateutil's rruleset.before scans all cached history on each call. A timezone
    declared from year 1601 otherwise repeats that work for every visible event.
    The shared count bounds retained transitions across all observances.
    """
    def __init__(self, rules, identifier, budget):
        self.iterator, self.identifier, self.budget = iter(rules), identifier, budget
        self.values, self.exhausted, self.failure = [], False, None
        self.lock = RLock()

    def before(self, value, inc=False):
        with self.lock:
            if self.failure is not None:
                raise self.failure.with_traceback(None)
            try:
                while not self.exhausted and (not self.values or self.values[-1] <= value):
                    try:
                        following = next(self.iterator)
                    except StopIteration:
                        self.exhausted = True
                        break
                    self.budget[0] -= 1
                    if self.budget[0] < 0:
                        raise _transition_error(self.identifier)('transition count')
                    self.values.append(following)
            except (ValueError, TypeError, OverflowError) as error:
                # A stopped generator must not make a later retry look like a
                # successfully exhausted rule with an incorrect final offset.
                self.failure = error
                raise
            position = bisect_right(self.values, value) if inc else bisect_left(self.values, value)
            return self.values[position - 1] if position else None


class _ResourceTimezone(tzinfo):
    """Apply RFC 5545's pre-gap offset to explicit, resource-local rules.

    Dateutil's tzical backend assigns the *new* offset to a missing clock.
    iCalendar instead defines it using the offset before the gap. Valid clocks
    and repeated-hour folds still use exactly the supplied observances.
    """
    def __init__(self, identifier, backend):
        self.identifier, self.backend = identifier, backend
        self._wall_state = lru_cache(maxsize=512)(self._uncached_wall_state)

    def __deepcopy__(self, memo):
        # Timezone rules are immutable; authoring drafts can share their lookup
        # caches just as they share ZoneInfo objects.
        return self

    def __str__(self):
        return self.identifier

    def __repr__(self):
        return f'ResourceTimezone({self.identifier!r})'

    def _uncached_wall_state(self, wall, fold):
        local = wall.replace(tzinfo=self.backend, fold=fold)
        offset = self.backend.utcoffset(local)
        if offset is not None:
            try:
                previous = self.backend.fromutc((wall - offset).replace(tzinfo=self.backend))
            except OverflowError:
                # A valid year-1/year-9999 local clock may have no representable
                # UTC datetime; its declared offset still remains available.
                previous = local
            if previous.replace(tzinfo=None) < wall:
                # The backend interpreted a missing clock with the new offset,
                # taking its instant back to the valid side before the gap.
                local = previous
        return self.backend.utcoffset(local), self.backend.dst(local), self.backend.tzname(local)

    def _state(self, value):
        return self._wall_state(value.replace(tzinfo=None, fold=0), value.fold)

    def utcoffset(self, value):
        return self._state(value)[0] if value is not None else self.backend.utcoffset(None)

    def dst(self, value):
        return self._state(value)[1] if value is not None else self.backend.dst(None)

    def tzname(self, value):
        return self._state(value)[2] if value is not None else self.backend.tzname(None)

    def fromutc(self, value):
        if value.tzinfo is not self:
            raise ValueError('fromutc requires this timezone on its input.')
        return self.backend.fromutc(value.replace(tzinfo=self.backend)).replace(tzinfo=self)


def validate_definitions(definitions) -> dict[str, str]:
    """Return an owned map after checking resource-size and text boundaries."""
    if not isinstance(definitions, Mapping):
        raise TimezoneError('Timezone definitions must map TZID names to VTIMEZONE text.')
    if len(definitions) > MAX_CONTEXT_ZONES:
        raise TimezoneError(f'The event contains more than {MAX_CONTEXT_ZONES} timezone definitions.')
    copied, total = {}, 0
    for identifier, definition in definitions.items():
        _validate_identifier(identifier)
        if not isinstance(definition, str):
            raise TimezoneError(f'Timezone {identifier!r} needs VTIMEZONE text.')
        size = len(definition.encode('utf-8'))
        if size > MAX_DEFINITION_BYTES:
            raise TimezoneError(f'Timezone {identifier!r} exceeds the 256 KiB definition limit.')
        total += size
        if total > MAX_CONTEXT_BYTES:
            raise TimezoneError('The event exceeds the 1 MiB timezone definition limit.')
        copied[identifier] = definition
    return copied


def _transition_error(identifier):
    def error(_detail):
        return TimezoneError(f'Timezone {identifier!r} has transition rules that exceed the computation limit. '
                             'Its original definition has been retained.')
    return error


def _bound_transition_rules(zone, identifier):
    """Keep dateutil's timezone calculations from scanning dense ancient rules.

    The declared python-dateutil 2.x backend represents each observance as an
    rruleset. Guard its rule iterators, retaining dateutil's transition/cache
    semantics and ordinary historic yearly rules through the supported years.
    """
    observances = getattr(zone, '_comps', None)
    if observances is None:
        # Future icalendar backends may return an independently constructed
        # native zone with bounded lookup rather than dateutil observances.
        return zone
    if len(observances) > MAX_OBSERVANCES:
        raise TimezoneError(f'Timezone {identifier!r} exceeds the {MAX_OBSERVANCES} observance limit.')
    error = _transition_error(identifier)
    budget = [MAX_TRANSITION_PERIODS]
    per_observance = MAX_TRANSITION_PERIODS // max(1, len(observances))
    for observance in observances:
        rules = observance.rrule
        for name in ('_rrule', '_exrule'):
            values = getattr(rules, name, None)
            if values is None:
                raise TimezoneError('The timezone backend cannot enforce transition computation limits.')
            setattr(rules, name, [BoundedRule(rule, error, calendar_periods=True,
                                            limit=per_observance) for rule in values])
        observance.rrule = _TransitionIndex(rules, identifier, budget)
    return zone


def _explicit_timezone(identifier: str, definition: str) -> tzinfo:
    try:
        from icalendar import Component, Timezone
        from icalendar.timezone import TZP
    except ImportError:
        raise TimezoneError(f'Timezone {identifier!r} needs the icalendar dependency to read its VTIMEZONE rules.') from None
    try:
        # Parsing VTIMEZONE itself eagerly seeds icalendar's unbounded global
        # TZID cache. Parse its properties/observances as a generic component,
        # then give them back their normal class before scoped construction.
        # Bytes always mean content, never a filename accepted by newer APIs.
        content = definition.encode('utf-8').replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        unfolded = re.sub(rb'\n[ \t]', b'', content)
        temporary = re.sub(rb'(?im)^(BEGIN|END):VTIMEZONE[ \t]*$',
                           lambda match: match[1].upper() + b':X-DEBACAL-SCOPED-TIMEZONE',
                           unfolded)
        parsed = Component.from_ical(temporary)
        if parsed.name != 'X-DEBACAL-SCOPED-TIMEZONE' or str(parsed.get('TZID', '')) != identifier:
            raise TimezoneError(f'Timezone {identifier!r} has no matching VTIMEZONE definition.')
        component = Timezone()
        component.update(parsed)
        component.subcomponents = parsed.subcomponents
        # RFC TZID is already TEXT; dateutil's tzical reader rejects otherwise
        # valid redundant VALUE=TEXT parameters. Normalize only the temporary
        # component passed to that reader, retaining the source definition.
        component['TZID'] = str(component['TZID'])
        if len(component.subcomponents) > MAX_OBSERVANCES:
            raise TimezoneError(f'Timezone {identifier!r} exceeds the {MAX_OBSERVANCES} observance limit.')
        # Do not let an IANA/Windows lookup or a previous resource's cached
        # timezone replace the explicit rules in this resource.
        # The provider's public construction API exists throughout supported
        # icalendar 6/7. Unlike to_tz(), it never performs identifier lookup or
        # consults the library's shared TZID cache.
        zone = TZP('zoneinfo').create_timezone(component)
        if not isinstance(zone, tzinfo):
            raise TimezoneError(f'Timezone {identifier!r} did not define usable timezone rules.')
        return _ResourceTimezone(identifier, _bound_transition_rules(zone, identifier))
    except TimezoneError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
        raise TimezoneError(f'Timezone {identifier!r} has an invalid or unsupported VTIMEZONE definition.') from None


@lru_cache(maxsize=64)
def _cached_explicit_timezone(identifier: str, definition: str) -> tzinfo:
    return _explicit_timezone(identifier, definition)


def resolve_timezone(identifier: str, definitions: Mapping[str, str] | None = None) -> tzinfo:
    """Resolve a name within its resource's explicit timezone context first."""
    _validate_identifier(identifier)
    definition = definitions.get(identifier) if definitions is not None else None
    if definition is not None:
        # Validate direct resolver callers too; models validate the whole map.
        validate_definitions({identifier: definition})
        if len(definition.encode('utf-8')) <= MAX_CACHED_DEFINITION_BYTES:
            return _cached_explicit_timezone(identifier, definition)
        return _explicit_timezone(identifier, definition)
    try:
        return ZoneInfo(identifier)
    except (ValueError, TypeError, OSError, ZoneInfoNotFoundError):
        raise TimezoneError(f'Timezone {identifier!r} is not available in the timezone database '
                             'and has no VTIMEZONE definition in this resource.') from None


def event_timezone(event) -> tzinfo:
    """The scheduling zone used for recurrence, nominal duration and editing."""
    if event.timezone_id:
        return resolve_timezone(event.timezone_id, event.timezone_definitions)
    return event.start.tzinfo


def timezone_identifier(zone: tzinfo) -> str:
    """Return a persisted TZID for database or resource-defined timezone rules."""
    return getattr(zone, 'identifier', '') or getattr(zone, 'key', '')
