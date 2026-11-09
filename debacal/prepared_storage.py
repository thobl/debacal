"""Versioned primitive storage for already validated query representations.

Only current source data is represented here: no viewed interval or query
answer is retained. Loaders rebuild numeric rule state and frozen timezone
tables without parsing RRULE/ICS strings or generating occurrences. The Store
owns transaction/version checks and the durable container around these values.
"""
from __future__ import annotations

from array import array
import base64
from dataclasses import MISSING, fields
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import sys
from types import MethodType
from zoneinfo import ZoneInfo

import dateutil
from dateutil.rrule import rrule, weekday

from .models import Event, copy_event
from .prepared_recurrence import PreparedSeries, _CalendarSeek, _HourlySeek, _StaticSeek, _effective_arguments
from .timezones import (
    MAX_OBSERVANCES, MAX_TRANSITION_PERIODS, TimezoneError, _ResourceTimezone,
    _TransitionIndex, _find_indexed_component, register_prepared_timezone,
)


FORMAT_VERSION = 1
ENGINE_VERSION = dateutil.__version__
_DAY_US = 86_400_000_000
_WALL_ORIGIN = datetime.min
_WALL_UNIT = timedelta(microseconds=1)
_ARRAY_CODES = frozenset({'I', 'Q', 'q', 'd'})
_RULE_FIELDS = (
    '_dtstart', '_freq', '_interval', '_count', '_original_rule', '_until', '_wkst',
    '_bysetpos', '_bymonth', '_byyearday', '_byeaster', '_bymonthday',
    '_bynmonthday', '_byweekno', '_byweekday', '_bynweekday', '_byhour',
    '_byminute', '_bysecond', '_timeset',
)
_SEEK_TYPES = {'static': _StaticSeek, 'calendar': _CalendarSeek, 'hourly': _HourlySeek}
_SEEK_FIELDS = {
    'static': ('start', 'frequency', 'interval', 'count', 'multiplicity', 'partial',
               'pattern', 'prefix', 'unit'),
    'calendar': ('start', 'frequency', 'monthly', 'interval', 'count', 'cyclic',
                 'week_offset', 'origin', 'step', 'periods', 'prefix', 'partial',
                 'complete_cycle'),
    'hourly': ('start', 'count', 'interval', 'origin', 'multiplicity', 'partial',
               'days', 'prefix'),
}
_EVENT_FIELDS = tuple(item for item in fields(Event) if item.name != '_query_projection')
_EVENT_NAMES = frozenset(item.name for item in _EVENT_FIELDS)
_EVENT_DEFAULTS = {item.name: (item.default if item.default is not MISSING else item.default_factory())
                   for item in _EVENT_FIELDS if item.default is not MISSING or item.default_factory is not MISSING}


class PreparedStateError(ValueError):
    """Derived state needs rebuilding from the retained canonical source."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _wall_us(value):
    return ((value.toordinal() - 1) * _DAY_US
            + ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000
            + value.microsecond)


def _from_wall(value, zone=None, fold=0):
    if type(value) is not int or type(fold) is not int or fold not in (0, 1):
        raise PreparedStateError('Invalid prepared date coordinates.')
    # Timedelta's integer arithmetic reconstructs all supported years exactly
    # and avoids repeated Python date/clock division for every loaded endpoint.
    return (_WALL_ORIGIN + _WALL_UNIT * value).replace(tzinfo=zone, fold=fold)


def _pack(values):
    if values.typecode not in _ARRAY_CODES:
        raise PreparedStateError('Unsupported prepared numeric array.')
    output = values
    if sys.byteorder != 'little':
        output = array(values.typecode, values)
        output.byteswap()
    return [values.typecode, base64.b64encode(output.tobytes()).decode('ascii')]


def _unpack(value):
    code, payload = value
    if code not in _ARRAY_CODES or not isinstance(payload, str):
        raise PreparedStateError('Invalid prepared numeric array.')
    result = array(code)
    result.frombytes(base64.b64decode(payload, validate=True))
    if sys.byteorder != 'little':
        result.byteswap()
    return result


class StateWriter:
    """Encode primitives and collect shared zones/definition text by identity."""

    def __init__(self, shared_sink=None):
        self.zones = {}
        self.definitions = {}
        self._zone_refs = {}
        self._shared_sink = shared_sink
        self.references = set()

    def reset_references(self):
        self.references.clear()

    def definition(self, text):
        reference = hashlib.sha256(text.encode('utf-8')).hexdigest()
        self.references.add(('definition', reference))
        if reference not in self.definitions:
            self.definitions[reference] = text
            if self._shared_sink is not None:
                self._shared_sink('definition', reference, text)
        return reference

    def zone(self, zone):
        if zone is None:
            return None
        known = self._zone_refs.get(id(zone))
        if known is not None and known[0] is zone:
            self.references.add(('zone', known[1]))
            return known[1]
        if zone is timezone.utc:
            return 'utc'
        if isinstance(zone, ZoneInfo):
            return 'iana:' + zone.key
        state = self._timezone(zone)
        reference = 'zone:' + _digest(state)
        self.references.add(('zone', reference))
        if reference not in self.zones:
            self.zones[reference] = state
            if self._shared_sink is not None:
                self._shared_sink('zone', reference, state)
        self._zone_refs[id(zone)] = zone, reference
        return reference

    def datetime(self, value):
        return [_wall_us(value), value.fold, self.zone(value.tzinfo)]

    def _timezone(self, zone):
        identifier = getattr(zone, 'identifier', '')
        backend = getattr(zone, 'backend', None)
        if isinstance(zone, _ResourceTimezone) and isinstance(backend, ZoneInfo):
            return {'v': FORMAT_VERSION, 'kind': 'alias', 'id': identifier, 'key': backend.key}
        components = getattr(backend, '_comps', None)
        if components is not None:
            values = []
            for component in components:
                index = component.rrule
                if not isinstance(index, _TransitionIndex):
                    raise PreparedStateError('The timezone has no prepared transition table.')
                failure = (None if index.failure is None else
                           [type(index.failure).__name__, str(index.failure)])
                values.append({'from': self.value(component.tzoffsetfrom),
                               'to': self.value(component.tzoffsetto), 'dst': component.isdst,
                               'name': component.tzname,
                               'transitions': _pack(array('q', (_wall_us(value) for value in index.values))),
                               'failure': failure,
                               'failure_from': _wall_us(index.failure_from) if index.failure_from is not None else None,
                               'inclusive': index.inclusive_failure})
            return {'v': FORMAT_VERSION, 'kind': 'scoped', 'id': identifier, 'components': values}
        offset = zone.utcoffset(None)
        if offset is None:
            raise PreparedStateError('The timezone cannot be represented as prepared data.')
        return {'v': FORMAT_VERSION, 'kind': 'offset', 'offset': self.value(offset),
                'name': zone.tzname(None)}

    def definitions_state(self, definitions):
        return [[identifier, self.definition(text)] for identifier, text in sorted(definitions.items())]

    def _payload(self, value, omit_transport):
        if not isinstance(value, dict):
            return self.value(value)
        result = {}
        for name, item in value.items():
            if name == 'raw_ics' and omit_transport:
                continue
            if name == '_query_projection':
                continue
            if name == 'timezone_definitions' and isinstance(item, dict):
                result[name] = {'@': 'definitions', 'v': self.definitions_state(item)}
            elif isinstance(item, dict):
                result[name] = self._payload(item, omit_transport)
            else:
                result[name] = self.value(item)
        return {'@': 'map', 'v': result}

    def event(self, event, omit_transport=True, base=None):
        values = {}
        for item in _EVENT_FIELDS:
            name = item.name
            if name in {'start', 'end', 'timezone_definitions'} or omit_transport and name == 'raw_ics':
                continue
            value = getattr(event, name)
            previous = getattr(base, name) if base is not None else _EVENT_DEFAULTS.get(name, MISSING)
            if value == previous:
                continue
            values[name] = (self._payload(value, omit_transport) if name == 'recurrence_overrides'
                            else self.value(value))
        start = ([_wall_us(event.start), 0, None] if event.floating_time else self.datetime(event.start))
        end = ([_wall_us(event.end), 0, None] if event.floating_time else self.datetime(event.end))
        state = {'v': FORMAT_VERSION, 'f': values, 's': start,
                 'e': end,
                 'q': bool(omit_transport or getattr(event, '_query_projection', False))}
        if base is None or event.timezone_definitions != base.timezone_definitions:
            state['d'] = self.definitions_state(event.timezone_definitions)
        return state

    def value(self, value):
        if value is None or type(value) in (str, bool, int, float):
            return value
        if isinstance(value, datetime):
            return {'@': 'datetime', 'v': self.datetime(value)}
        if isinstance(value, timedelta):
            return {'@': 'duration', 'v': [value.days, value.seconds, value.microseconds]}
        if isinstance(value, time):
            return {'@': 'time', 'v': [((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond,
                                      value.fold, self.zone(value.tzinfo)]}
        if isinstance(value, weekday):
            return {'@': 'weekday', 'v': [value.weekday, value.n]}
        if isinstance(value, array):
            return {'@': 'array', 'v': _pack(value)}
        if isinstance(value, dict):
            return {'@': 'map', 'v': {name: self.value(item) for name, item in value.items()}}
        if isinstance(value, (tuple, list, set, frozenset)):
            kind = ('tuple' if isinstance(value, tuple) else 'set' if isinstance(value, set)
                    else 'frozen' if isinstance(value, frozenset) else 'list')
            items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
            return {'@': kind, 'v': [self.value(item) for item in items]}
        raise PreparedStateError(f'Unsupported prepared value: {type(value).__name__}.')

    def series(self, prepared):
        rule = None if prepared.rule is None else {
            name: self.value(getattr(prepared.rule, name)) for name in _RULE_FIELDS}
        seek = None
        if prepared.seek is not None:
            kind = next((kind for kind, cls in _SEEK_TYPES.items() if isinstance(prepared.seek, cls)), None)
            if kind is None:
                raise PreparedStateError('Unsupported prepared recurrence plan.')
            seek = {'kind': kind, 'f': {name: self.value(getattr(prepared.seek, name))
                                       for name in _SEEK_FIELDS[kind] if hasattr(prepared.seek, name)}}
        return {'v': FORMAT_VERSION, 'engine': ENGINE_VERSION, 'rule': rule, 'seek': seek,
                'floating': prepared.floating, 'zone': self.zone(prepared.zone),
                'event_zone': self.zone(prepared.event_zone),
                'duration': self.value(prepared.duration), 'nominal': self.value(prepared.nominal_duration),
                'excluded': list(prepared.excluded), 'overrides': list(prepared.override_keys),
                'added': list(prepared.added_keys), 'periods': prepared.calendar_periods,
                'finite': self.value(prepared._finite_values),
                'finite_starts': _pack(array('d', prepared._finite_starts)),
                'explicit': [self.event(item, base=prepared.master) for item in prepared.explicit_occurrences],
                'explicit_starts': _pack(array('d', prepared._explicit_starts)),
                'explicit_ends': _pack(array('d', prepared._explicit_ends)),
                'cut': prepared.cut, 'bounds': self.value(prepared.rule_bounds)}


class StateReader:
    """Restore validated primitive representations with shared definition data."""

    def __init__(self, zone_loader=None, definition_loader=None):
        self._zone_loader = zone_loader or {}
        self._definition_loader = definition_loader or {}
        self._zones = {'utc': timezone.utc}
        self._definitions = {}
        self._definition_maps = {}
        self._registered_definitions = set()

    def retain_references(self, live_zone_refs, live_def_refs):
        """Release catalog entries no current source uses, retaining live data."""
        zones, definitions = set(live_zone_refs), set(live_def_refs)
        for reference in tuple(self._zones):
            if reference.startswith('zone:') and reference not in zones:
                del self._zones[reference]
        for reference in tuple(self._definitions):
            if reference not in definitions:
                del self._definitions[reference]
        for references in tuple(self._definition_maps):
            if any(reference not in definitions for _identifier, reference in references):
                del self._definition_maps[references]
        retained_texts = set(self._definitions.values())
        self._registered_definitions = {key for key in self._registered_definitions if key[1] in retained_texts}

    @staticmethod
    def _load(loader, reference):
        return loader(reference) if callable(loader) else loader[reference]

    def zone(self, reference):
        if reference is None:
            return None
        if reference in self._zones:
            return self._zones[reference]
        if reference.startswith('iana:'):
            result = ZoneInfo(reference[5:])
        else:
            state = self._load(self._zone_loader, reference)
            if state.get('v') != FORMAT_VERSION:
                raise PreparedStateError('Unsupported prepared timezone version.')
            kind = state['kind']
            if kind == 'alias':
                result = _ResourceTimezone(state['id'], ZoneInfo(state['key']))
            elif kind == 'offset':
                offset = self.value(state['offset'])
                result = timezone(offset, state['name']) if state.get('name') is not None else timezone(offset)
            elif kind == 'scoped':
                result = self._scoped_timezone(state)
            else:
                raise PreparedStateError('Unsupported prepared timezone kind.')
        self._zones[reference] = result
        return result

    def _scoped_timezone(self, state):
        from dateutil.tz.tz import _tzicalvtz, _tzicalvtzcomp
        if not 0 < len(state['components']) <= MAX_OBSERVANCES:
            raise PreparedStateError('Invalid prepared timezone observances.')
        components, total = [], 0
        failures = {'TimezoneError': TimezoneError, 'ValueError': ValueError,
                    'TypeError': TypeError, 'OverflowError': OverflowError}
        for item in state['components']:
            index = object.__new__(_TransitionIndex)
            coordinates = _unpack(item['transitions'])
            total += len(coordinates)
            if total > MAX_TRANSITION_PERIODS:
                raise PreparedStateError('Prepared timezone exceeds its transition limit.')
            index.values = tuple(_from_wall(value) for value in coordinates)
            failure = item['failure']
            if failure is not None and failure[0] not in failures:
                raise PreparedStateError('Invalid prepared timezone diagnostic.')
            index.failure = failures[failure[0]](failure[1]) if failure is not None else None
            index.failure_from = _from_wall(item['failure_from']) if item['failure_from'] is not None else None
            index.inclusive_failure = item['inclusive']
            component = _tzicalvtzcomp(self.value(item['from']).total_seconds(),
                                      self.value(item['to']).total_seconds(), item['dst'], item['name'], index)
            components.append(component)
        backend = _tzicalvtz(state['id'], components)
        backend._find_comp = MethodType(_find_indexed_component, backend)
        return _ResourceTimezone(state['id'], backend)

    def datetime(self, state):
        value, fold, reference = state
        return _from_wall(value, self.zone(reference), fold)

    def definitions_state(self, state):
        key = tuple(tuple(item) for item in state)
        if key not in self._definition_maps:
            result = {}
            for identifier, reference in key:
                if reference not in self._definitions:
                    self._definitions[reference] = self._load(self._definition_loader, reference)
                result[identifier] = self._definitions[reference]
            self._definition_maps[key] = result
        return self._definition_maps[key]

    def event(self, state, base=None):
        if state.get('v') != FORMAT_VERSION:
            raise PreparedStateError('Unsupported prepared event version.')
        if base is None:
            if not {'id', 'calendar_id', 'title'}.issubset(state['f']):
                raise PreparedStateError('Prepared event lacks its required fields.')
            result = object.__new__(Event)
            for item in _EVENT_FIELDS:
                if item.name in _EVENT_DEFAULTS:
                    default = _EVENT_DEFAULTS[item.name]
                    object.__setattr__(result, item.name, default.copy() if isinstance(default, dict) else default)
        else:
            result = copy_event(base)
        for name, value in state['f'].items():
            if name not in _EVENT_NAMES:
                raise PreparedStateError('Invalid prepared event field.')
            object.__setattr__(result, name, self.value(value))
        if result.floating_time:
            # Floating values carry civil clocks, not the writer's timezone or
            # fold. Decode them just like canonical floating JSON on this host.
            from .timeutil import local_zone
            zone = local_zone()
            object.__setattr__(result, 'start', _from_wall(state['s'][0], zone))
            object.__setattr__(result, 'end', _from_wall(state['e'][0], zone))
        else:
            object.__setattr__(result, 'start', self.datetime(state['s']))
            object.__setattr__(result, 'end', self.datetime(state['e']))
        if 'd' in state:
            object.__setattr__(result, 'timezone_definitions', self.definitions_state(state['d']))
        if state.get('q'):
            object.__setattr__(result, 'raw_ics', '')
        if hasattr(Event, '_query_projection'):
            object.__setattr__(result, '_query_projection', bool(state.get('q')))
        identifier = result.timezone_id
        zone = result.start.tzinfo
        if (identifier and isinstance(zone, _ResourceTimezone) and zone.identifier == identifier
                and (definition := result.timezone_definitions.get(identifier)) is not None):
            key = identifier, definition
            if key not in self._registered_definitions:
                shared = register_prepared_timezone(identifier, definition, zone)
                if shared is not zone:
                    # Multiple active Stores should own the same immutable
                    # rules. Otherwise one Store closing could remove the weak
                    # registry entry while another still needs equivalent data.
                    for reference, current in self._zones.items():
                        if current is zone:
                            self._zones[reference] = shared
                    object.__setattr__(result, 'start', result.start.replace(tzinfo=shared))
                    if result.end.tzinfo is zone:
                        object.__setattr__(result, 'end', result.end.replace(tzinfo=shared))
                self._registered_definitions.add(key)
        return result

    def value(self, state):
        if state is None or type(state) in (str, bool, int, float):
            return state
        kind, value = state['@'], state['v']
        if kind == 'datetime':
            return self.datetime(value)
        if kind == 'duration':
            return timedelta(days=value[0], seconds=value[1], microseconds=value[2])
        if kind == 'time':
            micros, fold, reference = value
            seconds, micros = divmod(micros, 1_000_000)
            hour, seconds = divmod(seconds, 3600)
            minute, second = divmod(seconds, 60)
            return time(hour, minute, second, micros, tzinfo=self.zone(reference), fold=fold)
        if kind == 'weekday':
            return weekday(*value)
        if kind == 'array':
            return _unpack(value)
        if kind == 'definitions':
            return self.definitions_state(value)
        if kind == 'map':
            return {name: self.value(item) for name, item in value.items()}
        constructors = {'tuple': tuple, 'list': list, 'set': set, 'frozen': frozenset}
        if kind in constructors:
            return constructors[kind](self.value(item) for item in value)
        raise PreparedStateError('Unsupported prepared primitive kind.')

    def series(self, master, state, *, explicit_occurrences=None):
        if state.get('v') != FORMAT_VERSION or state.get('engine') != ENGINE_VERSION:
            raise PreparedStateError('Prepared recurrence engine version changed.')
        prepared = object.__new__(PreparedSeries)
        prepared.master = master
        prepared.floating = state['floating']
        prepared.zone = self.zone(state['zone'])
        if master.floating_time:
            from .timeutil import local_zone
            prepared.event_zone = local_zone()
        else:
            prepared.event_zone = self.zone(state['event_zone'])
        # Only the original range is used from generation_master by queries.
        generation = copy_event(master)
        if prepared.floating:
            object.__setattr__(generation, 'start', master.start.replace(tzinfo=timezone.utc, fold=0))
            object.__setattr__(generation, 'end', master.end.replace(tzinfo=timezone.utc, fold=0))
        prepared.generation_master = generation
        prepared.duration = self.value(state['duration'])
        prepared.nominal_duration = self.value(state['nominal'])
        prepared.excluded = frozenset(state['excluded'])
        prepared.override_keys = frozenset(state['overrides'])
        prepared.added_keys = frozenset(state['added'])
        prepared.calendar_periods = state['periods']
        prepared.rule, prepared.effective, prepared.seek = None, None, None
        if state['rule'] is not None:
            if set(state['rule']) != set(_RULE_FIELDS):
                raise PreparedStateError('Invalid prepared rule fields.')
            rule = object.__new__(rrule)
            rule.__dict__.update({name: self.value(value) for name, value in state['rule'].items()})
            rule._tzinfo = rule._dtstart.tzinfo
            rule._cache, rule._cache_complete, rule._len = None, False, None
            prepared.rule = rule
            prepared.effective = _effective_arguments(rule)
        seek_state = state['seek']
        if seek_state is not None:
            kind = seek_state['kind']
            if kind not in _SEEK_TYPES:
                raise PreparedStateError('Unsupported prepared recurrence plan.')
            seek = object.__new__(_SEEK_TYPES[kind])
            for name, value in seek_state['f'].items():
                if name not in _SEEK_FIELDS[kind]:
                    raise PreparedStateError('Invalid prepared recurrence plan field.')
                setattr(seek, name, self.value(value))
            if kind != 'static':
                seek.rule = prepared.rule
            if kind == 'calendar':
                seek.times = tuple(prepared.rule._timeset)
            prepared.seek = seek
        prepared._finite_values = self.value(state['finite'])
        prepared._finite_starts = _unpack(state['finite_starts'])
        prepared.explicit_occurrences = (tuple(explicit_occurrences) if explicit_occurrences is not None else
                                        tuple(self.event(value, base=master) for value in state['explicit']))
        prepared._explicit_starts = _unpack(state['explicit_starts'])
        prepared._explicit_ends = _unpack(state['explicit_ends'])
        prepared.cut = state['cut']
        prepared.rule_bounds = self.value(state['bounds'])
        return prepared
