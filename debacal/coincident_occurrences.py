"""Standard detached identities for compact occurrence multiplicities.

An iCalendar recurrence set cannot repeat one RDATE. Extra simultaneous
occurrences therefore receive distinct nominal slots and standard detached
ranges. Actual times, nominal identities, and complete exception content stay
separate throughout text edits and transport.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from .models import Event
from .event_duration import end_for
from .recurrence import (RecurrenceError, event_zone, key_datetime, occurrence,
                         recurrence_key, recurrence_set)


def _contains(event, key, rules=None):
    when = key_datetime(key, event)
    value = (rules if rules is not None else recurrence_set(event)).before(when, inc=True)
    return value is not None and recurrence_key(value, event.all_day, floating_time=event.floating_time) == recurrence_key(when, event.all_day, floating_time=event.floating_time)


def multiplicities(event):
    """Finite multiplicities beyond the ordinary one-event recurrence set."""
    active = {key: Event.from_dict(payload) for key, payload in event.recurrence_overrides.items()
              if key not in event.excluded_dates}
    counts = Counter(recurrence_key(item.start, event.all_day, floating_time=event.floating_time) for item in active.values())
    if not counts:
        return Counter()
    base = replace(event, recurrence_overrides={}, excluded_dates=tuple(set(event.excluded_dates) | set(active)))
    rules = recurrence_set(base)
    result = Counter()
    for key, count in counts.items():
        extra = count + int(_contains(base, key, rules)) - 1
        if extra:
            result[key] = extra
    return result


def logical_occurrence_start(master, key):
    """Following-order position of an independently added detached occurrence.

    RRULE instances keep their nominal order after a move. An off-rule RDATE
    addition has its own actual position, including a synthetic duplicate slot.
    Same-time copies form one following boundary irrespective of nominal ID.
    """
    nominal = key_datetime(key, master)
    if key not in master.recurrence_dates or key not in master.recurrence_overrides:
        return nominal
    if nominal.timestamp() >= master.start.timestamp():
        bare = replace(master, recurrence_dates=(), excluded_dates=(), recurrence_overrides={})
        if _contains(bare, key):
            return nominal
    item = Event.from_dict(master.recurrence_overrides[key])
    return (datetime.combine(item.start.date(), time.min, event_zone(master)) if master.all_day
            else item.start.replace(tzinfo=event_zone(master)) if master.floating_time
            else item.start.astimezone(event_zone(master)))


def _shifted(value, source, candidate, shift):
    zone = event_zone(candidate)
    local = (datetime.combine(value.date(), time.min, zone) if candidate.all_day
             else value.replace(tzinfo=zone) if candidate.floating_time
             else value.astimezone(zone))
    result = (local.replace(tzinfo=None) + shift).replace(tzinfo=zone, fold=local.fold)
    if not candidate.all_day and not candidate.floating_time and result.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != result.replace(tzinfo=None):
        raise RecurrenceError('Moving this occurrence would use a nonexistent local time. Choose another time.')
    return result


def _end_at(item, start):
    return end_for(item, start)


def apply_occurrence_counts(candidate, source, extra_starts=(), *, shift=None, ordinary_first=True):
    """Attach existing payloads and materialize the requested extra copies.

    The candidate initially describes one ordinary event per actual instant.
    Retained detached identities reserve their own nominal slots. Any baseline
    event they replace is recovered at another nominal slot when still wanted.
    Thus collisions never overwrite an unrelated occurrence or its content.
    """
    shift = timedelta() if shift is None else shift
    candidate = replace(candidate, recurrence_overrides={})
    rules = recurrence_set(candidate)
    source_base = replace(source, recurrence_overrides={},
                          excluded_dates=tuple(set(source.excluded_dates) | set(source.recurrence_overrides)))
    source_rules = recurrence_set(source_base)
    extras = Counter(recurrence_key(value, candidate.all_day, floating_time=candidate.floating_time) for value in extra_starts)
    membership, source_membership = {}, {}
    def contains(key):
        if key not in membership:
            membership[key] = _contains(candidate, key, rules)
        return membership[key]
    def wanted(key):
        return int(contains(key)) + extras[key]
    def original_ordinary(key):
        if key not in source_membership:
            source_membership[key] = _contains(source_base, key, source_rules)
        return source_membership[key]

    overrides, retained = {}, Counter()
    reference = recurrence_key(source.start, source.all_day, floating_time=source.floating_time)
    for key, payload in sorted(source.recurrence_overrides.items(), key=lambda pair: (pair[0] != reference, pair[0])):
        if key in source.excluded_dates:
            continue
        item = Event.from_dict(payload)
        if item.all_day != source.all_day:
            raise RecurrenceError('A compact sequence cannot mix timed and all-day occurrences.')
        start = _shifted(item.start, source, candidate, shift)
        end = _end_at(item, start)
        actual = recurrence_key(start, candidate.all_day, floating_time=candidate.floating_time)
        original_actual = recurrence_key(item.start, source.all_day, floating_time=source.floating_time)
        ordinary_priority = int(ordinary_first and original_ordinary(original_actual))
        if retained[actual] >= max(0, wanted(actual) - ordinary_priority):
            continue
        nominal = recurrence_key(_shifted(key_datetime(key, source), source, candidate, shift), candidate.all_day, floating_time=candidate.floating_time)
        if nominal in overrides:
            raise RecurrenceError('Two retained occurrences have the same nominal identity.')
        if (nominal == key and start.isoformat() == item.start.isoformat()
                and end.isoformat() == item.end.isoformat() and candidate.all_day == item.all_day):
            overrides[nominal] = deepcopy(payload)
        else:
            overrides[nominal] = replace(item, start=start, end=end, all_day=candidate.all_day,
                                          recurrence_id=nominal).to_dict()
        retained[actual] += 1

    additions, exclusions = set(candidate.recurrence_dates), set(candidate.excluded_dates)
    reserved = set(overrides)
    for nominal in reserved:
        if not contains(nominal):
            additions.add(nominal)
        exclusions.discard(nominal)
    affected = set(extras) | set(retained) | reserved
    counts = Counter(retained)
    for actual in affected:
        if contains(actual) and actual not in reserved:
            counts[actual] += 1
        if counts[actual] > wanted(actual):
            # The retained detached occurrence already represents this start.
            # Suppress its otherwise redundant ordinary recurrence slot.
            exclusions.add(actual)
            counts[actual] -= 1

    used = reserved | set(candidate.recurrence_dates) | set(candidate.excluded_dates) | {recurrence_key(candidate.start, candidate.all_day, floating_time=candidate.floating_time)}
    step = timedelta(days=1) if candidate.all_day else timedelta(seconds=1)
    seed = (datetime.combine(candidate.start.date(), time.min, event_zone(candidate)) if candidate.all_day
            else candidate.start.astimezone(event_zone(candidate)).replace(microsecond=0))
    nominal_cursor, direction = seed, -1
    def allocate():
        nonlocal nominal_cursor, direction
        # DTSTART is a lower bound for a rule. Earlier unused whole-second
        # slots do not require walking an infinite future rule to prove absence.
        for _ in range(10000):
            try:
                nominal_cursor += direction * step
            except OverflowError:
                if direction < 0:
                    direction, nominal_cursor = 1, seed
                    continue
                break
            key = recurrence_key(nominal_cursor, candidate.all_day, floating_time=candidate.floating_time)
            if key not in used and not contains(key):
                used.add(key)
                return key
        raise RecurrenceError('No separate calendar identity is available for another coincident occurrence near this reference date.')

    for actual in sorted(affected):
        for _ in range(max(0, wanted(actual) - counts[actual])):
            nominal = allocate()
            start = key_datetime(actual, candidate)
            item = occurrence(candidate, key_datetime(nominal, candidate), nominal)
            item = replace(item, start=start, end=_end_at(candidate, start), recurrence_id=nominal)
            overrides[nominal] = item.to_dict()
            additions.add(nominal)
            exclusions.discard(nominal)
    return replace(candidate, recurrence_dates=tuple(additions), excluded_dates=tuple(exclusions),
                   recurrence_overrides=overrides)


def reanchor_compiled_multiset(source, old_compiled, new_compiled, start, end, key_mapping):
    """Reevaluate finite prefix copies while retaining a symbolic ongoing tail."""
    old_starts, new_starts = old_compiled.prefix_starts, new_compiled.prefix_starts
    if len(old_starts) != len(new_starts):
        raise RecurrenceError('The moved definition changes its number of prefix occurrences.')
    zone = event_zone(source)
    start, end = ((start.replace(tzinfo=zone), end.replace(tzinfo=zone)) if source.floating_time
                  else (start.astimezone(zone), end.astimezone(zone)))
    shift = start.replace(tzinfo=None) - source.start.astimezone(zone).replace(tzinfo=None)
    candidate = replace(source, start=start, end=end, recurrence_rule=new_compiled.recurrence_rule,
                        recurrence_dates=new_compiled.recurrence_dates, excluded_dates=new_compiled.excluded_dates,
                        recurrence_overrides={})
    queues = defaultdict(list)
    for key, payload in source.recurrence_overrides.items():
        if key not in source.excluded_dates:
            item = Event.from_dict(payload)
            queues[recurrence_key(item.start, source.all_day, floating_time=source.floating_time)].append((key, item, payload))
    rules = recurrence_set(source)
    for actual in set(recurrence_key(value, source.all_day, floating_time=source.floating_time) for value in old_starts):
        if actual not in source.recurrence_overrides and _contains(source, actual, rules):
            queues[actual].append((actual, occurrence(source, key_datetime(actual, source), actual), None))
    for queue in queues.values():
        queue.sort(key=lambda value: (value[0] != recurrence_key(source.start, source.all_day, floating_time=source.floating_time), value[0]))
    overrides, assigned = {}, set()
    def retain(key, item, actual, payload):
        nominal = recurrence_key(_shifted(key_datetime(key, source), source, candidate, shift), source.all_day, floating_time=source.floating_time)
        if nominal in overrides:
            raise RecurrenceError('Moving this series would merge two nominal identities.')
        actual = actual.replace(tzinfo=zone) if source.floating_time else actual.astimezone(zone)
        if payload is not None or recurrence_key(actual, source.all_day, floating_time=source.floating_time) != nominal:
            overrides[nominal] = replace(item, start=actual, end=_end_at(item, actual),
                                          recurrence_id=nominal).to_dict()
        assigned.add(key)
    for old, new in zip(old_starts, new_starts):
        queue = queues[recurrence_key(old, source.all_day, floating_time=source.floating_time)]
        if not queue:
            raise RecurrenceError('A moved prefix occurrence has no corresponding calendar identity.')
        key, item, payload = queue.pop(0)
        retain(key, item, new, payload)
    for key, payload in source.recurrence_overrides.items():
        if key in assigned or key in source.excluded_dates:
            continue
        item = Event.from_dict(payload)
        mapped = key_mapping.get(key)
        actual = (key_datetime(mapped, candidate) if mapped is not None
                  else _shifted(item.start, source, candidate, shift))
        retain(key, item, actual, payload)
    identity_source = replace(candidate, recurrence_overrides=overrides)
    return apply_occurrence_counts(candidate, identity_source, new_compiled.duplicate_starts,
                                   ordinary_first=False)
