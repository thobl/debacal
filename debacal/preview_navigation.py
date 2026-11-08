"""On-demand occurrence navigation, independent of the visible preview month."""
from datetime import datetime, time, timedelta, timezone
from itertools import islice

from .recurrence import canonical_rule, event_zone, expand_series, key_datetime, normalized_rule, occurrence, occurrence_id, recurrence_key, recurrence_set


NAVIGATION_LIMIT = 10000
POSITION_LIMIT = 2000


class PreviewNavigation:
    def __init__(self, masters):
        self.masters = []
        self._signature = None
        self._finite = None
        self._positions = None
        self._positions_checked = False
        self._known_positions = {}
        self.rebind(masters)

    def rebind(self, masters):
        """Retain cheap position data across title and duration changes."""
        masters = list(masters)
        signature = tuple((item.id, item.start.isoformat(), item.all_day, item.timezone_id,
                           item.recurrence_rule, item.recurrence_dates, item.excluded_dates,
                           tuple(sorted((key, value.get('start')) for key, value in item.recurrence_overrides.items())))
                          for item in masters)
        self._finite = None
        if signature != self._signature:
            self._positions = None
            self._positions_checked = False
            self._known_positions = {}
        self._signature, self.masters = signature, masters

    def _position_budget(self, master):
        if not master.recurrence_rule:
            return True
        fields = normalized_rule(master.recurrence_rule, master)
        if 'COUNT' in fields:
            return int(fields['COUNT']) <= POSITION_LIMIT
        if 'UNTIL' not in fields:
            return False
        canonical = dict(part.split('=', 1) for part in canonical_rule(master.recurrence_rule, master).split(';'))
        boundary = canonical['UNTIL']
        until = (datetime.strptime(boundary, '%Y%m%d').replace(tzinfo=event_zone(master)) if len(boundary) == 8 else
                 datetime.strptime(boundary, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(event_zone(master)))
        first = master.start.astimezone(event_zone(master))
        seconds = max(0, (until.replace(tzinfo=None) - first.replace(tzinfo=None)).total_seconds())
        units = {'SECONDLY': 1, 'MINUTELY': 60, 'HOURLY': 3600, 'DAILY': 86400,
                 'WEEKLY': 604800, 'MONTHLY': 28 * 86400, 'YEARLY': 365 * 86400}
        return seconds / (units.get(fields.get('FREQ'), 1) * int(fields.get('INTERVAL', 1))) <= POSITION_LIMIT

    def _position_records(self):
        if self._positions_checked:
            return self._positions
        self._positions_checked = True
        if not self.bounded or any(not self._position_budget(item) for item in self.masters):
            return None
        positions = {}
        for master in self.masters:
            if not master.recurring:
                positions[master.id] = master.start.timestamp()
                continue
            if not master.recurrence_rule:
                keys = {recurrence_key(master.start, master.all_day), *master.recurrence_dates,
                        *master.recurrence_overrides} - set(master.excluded_dates)
            else:
                keys = set()
                for index, value in enumerate(islice(recurrence_set(master), POSITION_LIMIT + 1)):
                    if index == POSITION_LIMIT:
                        return None
                    keys.add(recurrence_key(value, master.all_day))
                keys.update(set(master.recurrence_overrides) - set(master.excluded_dates))
            for key in keys:
                nominal = key_datetime(key, master)
                if nominal.astimezone(timezone.utc).astimezone(nominal.tzinfo).replace(tzinfo=None) != nominal.replace(tzinfo=None):
                    continue
                actual = (datetime.fromisoformat(master.recurrence_overrides[key]['start'])
                          if key in master.recurrence_overrides else nominal)
                positions[occurrence_id(master.id, key)] = actual.timestamp()
        self._positions = sorted((timestamp, key) for key, timestamp in positions.items())
        return self._positions

    def position(self, reference):
        """Return a one-based chronological position and exact total when cheap.

        Large/complex or infinite rules never enumerate their history for a
        label. An unknown ordinal/total is represented by None. Adjacent jumps
        retain a known ordinal as the user navigates an unbounded series.
        """
        if self._finite is not None:
            records = [(item.start.timestamp(), item.id) for item in self._finite]
        else:
            records = self._position_records()
        if records is not None:
            index = next((index + 1 for index, (_stamp, key) in enumerate(records) if key == reference.id), None)
            return index, len(records)
        if reference.id in self._known_positions:
            return self._known_positions[reference.id], None
        if len(self.masters) == 1:
            master = self.masters[0]
            if (not master.recurrence_dates and not master.excluded_dates and not master.recurrence_overrides
                    and reference.start.timestamp() == master.start.timestamp()):
                self._known_positions[reference.id] = 1
                return 1, None
        return None, None

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
        result = min(candidates, key=lambda item: (item.start.timestamp(), item.id)) if direction > 0 else max(
            candidates, key=lambda item: (item.start.timestamp(), item.id))
        if reference.id in self._known_positions:
            self._known_positions[result.id] = self._known_positions[reference.id] + direction
        return result
