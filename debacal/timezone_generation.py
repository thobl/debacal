"""Verify generated observance onsets against their timezone's UTC rules.

The generator in some icalendar releases finds forward changes in local-clock
space and emits the first post-gap clock as DTSTART/RDATE. RFC 5545 expresses
those onsets in TZOFFSETFROM instead. Correct only freshly generated data and
only when the native timezone proves the exact transition instant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo


class _BoundedZone(tzinfo):
    """Bound the maintained generator's search to its requested local dates.

    Its search loop handles OverflowError as the end of available timezone
    data. Without a bound, finer searches continue to year 9999 after the
    final transition even when the requested export ends centuries earlier.
    """
    def __init__(self, native, last_date):
        self.native, self.last_date = native, last_date

    def _local(self, value):
        if value is None:
            return None
        if value.date() >= self.last_date:
            raise OverflowError('Outside generated timezone coverage.')
        return value.replace(tzinfo=self.native)

    def utcoffset(self, value):
        return self.native.utcoffset(self._local(value))

    def dst(self, value):
        return self.native.dst(self._local(value))

    def tzname(self, value):
        return self.native.tzname(self._local(value))

    def fromutc(self, value):
        if value.tzinfo is not self:
            raise ValueError('fromutc requires this timezone on its input.')
        local = self.native.fromutc(value.replace(tzinfo=self.native))
        self._local(local)
        return local.replace(tzinfo=self)


def legacy_generated_timezone(zone, identifier, first_date, last_date):
    """Reproduce old generated source solely for exact-signature recognition."""
    from icalendar import Timezone
    return Timezone.from_tzinfo(zone, tzid=identifier, first_date=first_date, last_date=last_date)


def generate_verified_timezone(zone, identifier, first_date, last_date):
    """Generate native IANA rules with a bounded, daily transition search.

    Daily steps capture short IANA observances such as historical Ramadan
    suspensions that upstream's 64-day jumps can miss. This relies on native
    IANA rules not making two offset changes within one day; imported explicit
    VTIMEZONE definitions are never generated or modified by this function.
    """
    from icalendar import Timezone

    class BoundedTimezone(Timezone):
        _from_tzinfo_skip_search = (timedelta(days=1), timedelta(hours=1),
                                    timedelta(minutes=1), timedelta(seconds=1))

    component = BoundedTimezone.from_tzinfo(_BoundedZone(zone, last_date), tzid=identifier,
                                           first_date=first_date, last_date=last_date)
    return normalize_generated_onsets(component, zone)


def normalize_generated_onsets(component, zone):
    """Repair proven generator mistakes without guessing or changing imports."""
    def verified(clock, before, after):
        if not isinstance(clock, datetime) or clock.tzinfo is not None:
            raise ValueError('Generated timezone onsets must be local DATE-TIME values.')
        if before == after:
            return clock  # Initial/fixed-offset coverage does not describe a change.
        # The first candidate is the declared RFC onset. The second accounts
        # for a generator locating the change at its post-transition clock.
        for adjustment in (timedelta(0), after - before):
            try:
                instant = (clock - before - adjustment).replace(tzinfo=timezone.utc)
                previous = (instant - timedelta(microseconds=1)).astimezone(zone).utcoffset()
                following = instant.astimezone(zone).utcoffset()
            except OverflowError:
                continue
            if previous == before and following == after:
                return clock - adjustment
        raise ValueError('Generated timezone transition disagrees with its timezone rules. '
                         'Its calendar data cannot be exported safely.')

    for observance in component.subcomponents:
        if observance.name not in {'STANDARD', 'DAYLIGHT'}:
            continue
        # Component.decoded/add are available throughout supported icalendar
        # 6/7; convenience attributes such as .DTSTART appeared later.
        before, after = observance.decoded('TZOFFSETFROM'), observance.decoded('TZOFFSETTO')
        start = verified(observance.decoded('DTSTART'), before, after)
        del observance['DTSTART']
        observance.add('DTSTART', start)
        values = observance.get('RDATE', [])
        values = values if isinstance(values, list) else [values]
        if values:
            dates = [verified(item.dt, before, after) for value in values for item in value.dts]
            del observance['RDATE']
            observance.add('RDATE', dates)
    return component
