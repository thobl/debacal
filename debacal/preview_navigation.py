"""On-demand occurrence navigation, independent of the visible preview month."""
from datetime import datetime, time, timedelta, timezone
from itertools import islice

from .recurrence import canonical_rule, event_zone, expand_series, key_datetime, normalized_rule, occurrence, recurrence_key, recurrence_set


NAVIGATION_LIMIT = 10000


class PreviewNavigation:
    def __init__(self, masters):
        self.masters = list(masters)
        self._finite = None

    @property
    def bounded(self):
        return all(not item.recurrence_rule or {'COUNT', 'UNTIL'} & normalized_rule(item.recurrence_rule, item).keys()
                   for item in self.masters)

    def explicit_count(self):
        """Count RDATE-only schedules without constructing offscreen events."""
        if any(master.recurrence_rule for master in self.masters):
            return None
        identifiers = set()
        for master in self.masters:
            if not master.recurring:
                identifiers.add(master.id)
                continue
            keys = {recurrence_key(master.start, master.all_day), *master.recurrence_dates,
                    *master.recurrence_overrides} - set(master.excluded_dates)
            for key in keys:
                value = key_datetime(key, master)
                if value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) == value.replace(tzinfo=None):
                    identifiers.add((master.id, key))
        return len(identifiers)

    def finite_events(self):
        if self._finite is not None:
            return self._finite
        if not self.bounded:
            return None
        values = {}
        for master in self.masters:
            if not master.recurring:
                values[master.id] = master
                continue
            for index, generated in enumerate(islice(recurrence_set(master), NAVIGATION_LIMIT + 1)):
                if index == NAVIGATION_LIMIT:
                    raise ValueError('This complex series exceeds the 10,000-occurrence navigation limit.')
                if generated.astimezone(timezone.utc).astimezone(generated.tzinfo).replace(tzinfo=None) != generated.replace(tzinfo=None):
                    continue
                item = occurrence(master, generated)
                values[item.id] = item
            for key in master.recurrence_overrides:
                if key not in master.excluded_dates:
                    item = occurrence(master, key_datetime(key, master), key)
                    values[item.id] = item
        self._finite = sorted(values.values(), key=lambda item: (item.start.timestamp(), item.id))
        return self._finite

    def _simple_last(self, master):
        if not master.recurrence_rule:
            if not master.recurring:
                return master
            keys = {recurrence_key(master.start, master.all_day), *master.recurrence_dates,
                    *master.recurrence_overrides} - set(master.excluded_dates)
            latest = None
            for key in keys:
                value = key_datetime(key, master)
                if value.astimezone(timezone.utc).astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
                    continue
                if key in master.recurrence_overrides:
                    value = occurrence(master, value, key).start
                if latest is None or (value.timestamp(), key) > (latest[0].timestamp(), latest[1]):
                    latest = value, key
            return occurrence(master, key_datetime(latest[1], master), latest[1]) if latest else None
        fields = normalized_rule(master.recurrence_rule, master)
        frequency = fields.get('FREQ')
        if (not {'COUNT', 'UNTIL'} & fields.keys()
                or fields.keys() - {'FREQ', 'INTERVAL', 'COUNT', 'UNTIL', 'WKST', 'BYDAY'}
                or frequency not in ('DAILY', 'WEEKLY')):
            return None
        first = master.start.astimezone(event_zone(master))
        if 'BYDAY' in fields:
            weekday = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')[first.weekday()]
            if frequency != 'WEEKLY' or fields['BYDAY'] != weekday:
                return None
        unit = timedelta(days=7 if frequency == 'WEEKLY' else 1)
        interval = int(fields.get('INTERVAL', 1))
        count = int(fields['COUNT']) if 'COUNT' in fields else None
        if interval <= 0 or (count is not None and count <= 0):
            raise ValueError('Recurrence interval and count must be positive integers.')
        if 'UNTIL' in fields:
            normalized = dict(part.split('=', 1) for part in canonical_rule(master.recurrence_rule, master).split(';'))
            boundary = normalized['UNTIL']
            if len(boundary) == 8:
                until = datetime.combine(datetime.strptime(boundary, '%Y%m%d').date(), time.max, event_zone(master))
            else:
                until = datetime.strptime(boundary, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(event_zone(master))
            until_count = (until.replace(tzinfo=None) - first.replace(tzinfo=None)) // (unit * interval) + 1
            count = min(count, until_count) if count is not None else until_count
        # DTSTART remains a set member even when UNTIL precedes it.
        nominal = first + unit * (interval * max(0, count - 1))
        finish = nominal + timedelta(days=2)
        begin = nominal - unit * interval * min(max(1, count), len(master.excluded_dates) + 3)
        candidates = expand_series(master, begin, finish, limit=NAVIGATION_LIMIT)
        for key in (*master.recurrence_dates, *master.recurrence_overrides):
            if key not in master.excluded_dates:
                candidates.append(occurrence(master, key_datetime(key, master), key))
        return max(candidates, key=lambda item: item.start.timestamp(), default=None)

    def last(self):
        if not self.bounded or not self.masters:
            return None
        values = [self._simple_last(master) for master in self.masters]
        if all(item is not None for item in values):
            return max(values, key=lambda item: item.start.timestamp())
        values = self.finite_events()
        return values[-1] if values else None

    def adjacent(self, reference, direction):
        key = reference.start.timestamp(), reference.id
        if self._finite is not None:
            eligible = [item for item in self._finite if ((item.start.timestamp(), item.id) > key if direction > 0
                                                         else (item.start.timestamp(), item.id) < key)]
            return eligible[0] if direction > 0 and eligible else eligible[-1] if eligible else None
        candidates = []
        boundary = reference.start
        for master in self.masters:
            if not master.recurring:
                item_key = master.start.timestamp(), master.id
                if (item_key > key if direction > 0 else item_key < key):
                    candidates.append(master)
                continue
            for days in (32, 128, 512, 2048, 8192, 32768, 131072, 524288):
                try:
                    start = boundary if direction > 0 else boundary - timedelta(days=days)
                    end = boundary + timedelta(days=days) if direction > 0 else boundary + timedelta(microseconds=1)
                except OverflowError:
                    break
                found = expand_series(master, start, end, limit=NAVIGATION_LIMIT, truncate=direction > 0)
                found = sorted((item for item in found if ((item.start.timestamp(), item.id) > key if direction > 0
                                                          else (item.start.timestamp(), item.id) < key)),
                               key=lambda item: (item.start.timestamp(), item.id))
                if found:
                    candidates.append(found[0] if direction > 0 else found[-1])
                    break
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item.start.timestamp(), item.id)) if direction > 0 else max(
            candidates, key=lambda item: (item.start.timestamp(), item.id))
