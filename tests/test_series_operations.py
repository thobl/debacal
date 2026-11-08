"""Pure following-occurrence partitions preserve the original schedule set."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from zoneinfo import ZoneInfo

from debacal.models import Event
from debacal.ics import export_ics, parse_ics
from debacal.ics_resources import _blocks, _decoded, _key, _uid_of
from debacal.ics_fallback import components, folded, prop
from debacal.event_clipboard import EventClipboard
from debacal.patterns import expand_sequence
from debacal.recurrence import (
    RecurrenceError, base_schedule_starts, expand_series, occurrence,
    recurrence_key, schedule_fields,
)
from debacal.series_operations import (
    clone_event, detach_event, partition_series, reidentify_series, updated_future,
)
from debacal.store import Store


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 10, 19, 9, tzinfo=ZONE)
LOWER = datetime(2025, 1, 1, tzinfo=ZONE)
UPPER = datetime(2028, 1, 1, tzinfo=ZONE)


def master(**values):
    fields = dict(id='series', calendar_id='local', ical_uid='server-uid', title='Meeting',
                  start=START, end=START + timedelta(hours=1), recurrence_rule='FREQ=WEEKLY;COUNT=5')
    fields.update(values)
    return Event(**fields)


def instances(source):
    return expand_series(source, LOWER, UPPER)


def signatures(source):
    if source is None:
        return set()
    return {(event.recurrence_id or recurrence_key(event.start, event.all_day),
             event.start.astimezone(timezone.utc), event.end.astimezone(timezone.utc),
             event.title, event.description, event.location, event.all_day, event.candidate)
            for event in instances(source)}


def detached(source, index, *, shift=timedelta(0), title='Exception'):
    initial = base_schedule_starts(source)[index]
    event = occurrence(source, initial)
    return replace(event, start=event.start + shift, end=event.end + shift, title=title)


def metadata_source(source, *, override_alarm=False):
    header, blocks = _blocks(export_ics([source]))
    result = []
    for block in blocks:
        if block[0] != 'BEGIN:VEVENT':
            result.append(block)
            continue
        exception = any(line.startswith('RECURRENCE-ID') for line in block)
        if exception:
            additions = ['X-SERVER-GROUP:specific value', 'X-SERVER-DETACHED:retained override']
            if override_alarm:
                additions += ['BEGIN:VALARM', 'UID:specific-alarm', 'ACTION:DISPLAY',
                              'TRIGGER:-PT5M', 'DESCRIPTION:Specific reminder', 'END:VALARM']
        else:
            additions = ['X-SERVER-INHERITED:retained parent', 'X-SERVER-GROUP:base first',
                         'X-SERVER-GROUP:base second']
            for name, trigger in [('first', '-PT15M'), ('second', '-PT60M')]:
                additions += ['BEGIN:VALARM', 'UID:parent-alarm-' + name, 'ACTION:DISPLAY',
                              'TRIGGER:' + trigger, 'DESCRIPTION:Parent reminder', 'END:VALARM']
        result.append([*block[:-1], *additions, 'END:VEVENT'])
    raw = folded(['BEGIN:VCALENDAR', *header, 'X-SERVER-HEADER:retained header',
                  *(line for block in result for line in block), 'END:VCALENDAR'])
    return parse_ics(raw, source.calendar_id)[0]


def metadata_with_cancellation():
    source = master()
    exception = detached(source, 2, shift=timedelta(hours=3))
    cancelled = detached(source, 3)
    source = metadata_source(replace(source, recurrence_overrides={
        exception.recurrence_id: exception.to_dict(), cancelled.recurrence_id: cancelled.to_dict(),
    }), override_alarm=True)
    header, blocks = _blocks(source.raw_ics)
    result = []
    for block in blocks:
        if block[0] == 'BEGIN:VEVENT':
            component = components(folded(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
            if ('RECURRENCE-ID' in component and
                    _key(_decoded(component, 'RECURRENCE-ID', ZONE)[0], ZONE) == cancelled.recurrence_id):
                block = [*block[:-1], 'STATUS:CANCELLED', 'X-SERVER-CANCELLATION:retained cancellation', 'END:VEVENT']
        result.append(block)
    return parse_ics(folded(['BEGIN:VCALENDAR', *header,
                            *(line for block in result for line in block), 'END:VCALENDAR']), 'local')[0]


class SeriesPartitionTests(unittest.TestCase):
    def assert_partition(self, original, before, after):
        self.assertEqual(signatures(original), signatures(before) | signatures(after))
        self.assertFalse(signatures(before) & signatures(after))

    def test_count_is_partitioned_without_flattening(self):
        source = master()
        before, after = partition_series(source, instances(source)[2])
        self.assertIn('COUNT=2', before.recurrence_rule)
        self.assertIn('COUNT=3', after.recurrence_rule)
        self.assertEqual(before.id, source.id)
        self.assertEqual(after.id, source.id)  # Storage assigns the new tail ID.
        self.assert_partition(source, before, after)

    def test_count_counts_rule_members_before_exclusions(self):
        source = master()
        starts = base_schedule_starts(source)
        source = replace(source, excluded_dates=(recurrence_key(starts[0]), recurrence_key(starts[3])))
        selected = instances(source)[-1]
        before, after = partition_series(source, selected)
        self.assertIn('COUNT=4', before.recurrence_rule)
        self.assertIn('COUNT=1', after.recurrence_rule)
        self.assertEqual(len(before.excluded_dates), 2)
        self.assert_partition(source, before, after)

    def test_first_visible_occurrence_does_not_leave_an_empty_head(self):
        source = master()
        starts = base_schedule_starts(source)
        source = replace(source, excluded_dates=tuple(recurrence_key(value) for value in starts[:2]))
        before, after = partition_series(source, instances(source)[0])
        self.assertIsNone(before)
        self.assertIn('COUNT=3', after.recurrence_rule)
        self.assert_partition(source, before, after)

    def test_moved_exceptions_are_partitioned_by_original_keys(self):
        source = master()
        past = detached(source, 0, shift=timedelta(days=70), title='Past nominal event moved later')
        pivot = detached(source, 2, shift=timedelta(days=-30), title='Selected event moved earlier')
        source = replace(source, recurrence_overrides={past.recurrence_id: past.to_dict(), pivot.recurrence_id: pivot.to_dict()})
        selected = next(value for value in instances(source) if value.recurrence_id == pivot.recurrence_id)
        before, after = partition_series(source, selected)
        self.assertIn(past.recurrence_id, before.recurrence_overrides)
        self.assertNotIn(pivot.recurrence_id, before.recurrence_overrides)
        self.assertIn(pivot.recurrence_id, after.recurrence_overrides)
        self.assertEqual(after.start, base_schedule_starts(source)[2])
        self.assert_partition(source, before, after)

    def test_infinite_rule_stays_infinite_and_local_clock_survives_dst(self):
        source = master(recurrence_rule='FREQ=WEEKLY;BYDAY=MO')
        before, after = partition_series(source, instances(source)[1])
        self.assertIn('UNTIL=', before.recurrence_rule)
        self.assertNotIn('COUNT=', after.recurrence_rule)
        self.assertNotIn('UNTIL=', after.recurrence_rule)
        self.assertEqual(after.start.hour, 9)
        self.assertNotEqual(before.start.utcoffset(), after.start.utcoffset())
        self.assert_partition(source, before, after)

    def test_all_day_until_remains_an_exclusive_partition_of_dates(self):
        start = datetime(2026, 10, 20, tzinfo=ZONE)
        source = master(start=start, end=start + timedelta(days=1), all_day=True,
                        recurrence_rule='FREQ=DAILY;UNTIL=20261025')
        before, after = partition_series(source, instances(source)[3])
        self.assertIn('UNTIL=20261022', before.recurrence_rule)
        self.assertIn('UNTIL=20261025', after.recurrence_rule)
        self.assertTrue(all(len(value.recurrence_id) == 10 for value in instances(after)))
        self.assert_partition(source, before, after)

    def test_off_rule_rdate_pivot_does_not_reset_fortnightly_phase(self):
        extra = START + timedelta(days=6)
        source = master(recurrence_rule='FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;COUNT=4',
                        recurrence_dates=(extra.isoformat(),))
        selected = next(value for value in instances(source) if value.start == extra)
        before, after = partition_series(source, selected)
        self.assertEqual(after.start, START + timedelta(weeks=2))
        self.assertIn(recurrence_key(extra), after.recurrence_dates)
        self.assertIn('COUNT=3', after.recurrence_rule)
        self.assert_partition(source, before, after)

    def test_off_rule_dtstart_and_extra_date_are_both_preserved(self):
        start = START + timedelta(days=1)
        extra = start + timedelta(days=14)
        source = master(start=start, end=start + timedelta(hours=1),
                        recurrence_rule='FREQ=WEEKLY;BYDAY=MO;COUNT=3', recurrence_dates=(extra.isoformat(),))
        selected = next(value for value in instances(source) if value.start == extra)
        before, after = partition_series(source, selected)
        self.assertEqual(before.start, start)
        self.assertIn(recurrence_key(extra), after.recurrence_dates)
        self.assert_partition(source, before, after)

    def test_rule_ending_before_pivot_is_not_extended_in_head(self):
        extra = START + timedelta(days=40)
        source = master(recurrence_rule='FREQ=DAILY;UNTIL=20261021T070000Z', recurrence_dates=(extra.isoformat(),))
        before, after = partition_series(source, instances(source)[-1])
        self.assertIn('UNTIL=20261021T070000Z', before.recurrence_rule)
        self.assertEqual(after.recurrence_rule, '')
        self.assert_partition(source, before, after)

    def test_rdates_before_fixed_anchor_partition_without_reordering_history(self):
        source = master(recurrence_rule='', recurrence_dates=(
            (START - timedelta(days=10)).isoformat(), (START - timedelta(days=5)).isoformat(),
            (START + timedelta(days=10)).isoformat()))
        selected = instances(source)[1]
        before, after = partition_series(source, selected)
        self.assertEqual(before.start, START - timedelta(days=10))
        self.assertEqual(after.start, START - timedelta(days=5))
        self.assertIn(recurrence_key(START), after.recurrence_dates)
        self.assert_partition(source, before, after)

    def test_head_keeps_original_fixed_anchor_when_earlier_rdates_exist(self):
        source = master(recurrence_rule='', recurrence_dates=(
            (START - timedelta(days=10)).isoformat(), (START + timedelta(days=10)).isoformat()))
        before, after = partition_series(source, instances(source)[-1])
        self.assertEqual(before.start, source.start)
        self.assert_partition(source, before, after)

    def test_finite_custom_text_is_rebuilt_for_each_partition(self):
        starts = expand_sequence('+0d 12*+2w +4w 3*+2w', START)
        source = master(**schedule_fields(starts), schedule_expression='+0d 12*+2w +4w 3*+2w',
                        expression_anchor=START.date().isoformat(), schedule_syntax=2)
        before, after = partition_series(source, instances(source)[7])
        for part in (before, after):
            self.assertEqual(part.schedule_syntax, 2)
            actual = expand_sequence(part.schedule_expression, part.start)
            self.assertEqual({value.astimezone(timezone.utc) for value in actual},
                             {value.astimezone(timezone.utc) for value in base_schedule_starts(part)})
        self.assert_partition(source, before, after)

    def test_relative_future_projection_rebases_unchanged_full_count(self):
        source = master()
        selected = instances(source)[2]
        edited = replace(selected, title='New title', start=selected.start + timedelta(hours=2),
                         end=selected.end + timedelta(hours=2), recurrence_rule=source.recurrence_rule)
        before, after = updated_future(source, selected, edited)
        self.assertIn('COUNT=3', after.recurrence_rule)
        self.assertEqual({value.title for value in instances(before)}, {'Meeting'})
        self.assertEqual({value.title for value in instances(after)}, {'New title'})
        self.assertTrue(all(value.start.hour == 11 for value in instances(after)))

    def test_absolute_replacement_is_not_rebased_as_an_unchanged_original_count(self):
        source = master()
        selected = instances(source)[2]
        edited = replace(selected, recurrence_rule=source.recurrence_rule)
        before, after = updated_future(source, selected, edited, replace_schedule=True)
        self.assertIn('COUNT=5', after.recurrence_rule)
        self.assertEqual(len(instances(before)), 2)
        self.assertEqual(len(instances(after)), 5)

    def test_multiple_times_per_day_preserve_rule_phase(self):
        source = master(recurrence_rule='FREQ=DAILY;BYHOUR=9,11;COUNT=8')
        before, after = partition_series(source, instances(source)[1])
        self.assertEqual(after.start.hour, 11)
        self.assert_partition(source, before, after)

    def test_bounded_or_unverifiable_splits_fail_actionably(self):
        source = master(start=datetime(1970, 1, 1, 9, tzinfo=ZONE),
                        end=datetime(1970, 1, 1, 10, tzinfo=ZONE), recurrence_rule='FREQ=DAILY')
        selected = occurrence(source, datetime(2026, 11, 1, 9, tzinfo=ZONE))
        with self.assertRaisesRegex(RecurrenceError, '10,000'):
            partition_series(source, selected)
        complex_rule = master(recurrence_rule='FREQ=YEARLY;BYWEEKNO=1;BYDAY=MO;COUNT=4')
        with self.assertRaisesRegex(RecurrenceError, 'week-number'):
            partition_series(complex_rule, instances(complex_rule)[1])


class SeriesCopyTests(unittest.TestCase):
    def assert_ordinary_copy(self, source, selected, snapshot):
        original = source.to_dict()
        self.assertEqual(snapshot.id, selected.id)
        self.assertEqual(snapshot.ical_uid, source.ical_uid)
        self.assertFalse(snapshot.recurring)
        self.assertEqual(snapshot.series_id, '')
        self.assertEqual(snapshot.recurrence_id, '')
        copied = clone_event(snapshot, new_id='copy-id', new_uid='copy-uid', calendar_id='destination')
        serialized = export_ics([copied]).decode()
        loaded = parse_ics(serialized, 'destination')
        self.assertEqual(len(loaded), 1)
        actual = loaded[0]
        self.assertFalse(actual.recurring)
        self.assertEqual(actual.ical_uid, 'copy-uid')
        for field in ('start', 'end', 'title', 'description', 'location', 'all_day'):
            self.assertEqual(getattr(actual, field), getattr(selected, field))
        _, blocks = _blocks(serialized)
        self.assertEqual([_uid_of(block) for block in blocks if block[0] == 'BEGIN:VEVENT'], ['copy-uid'])
        self.assertNotIn('RECURRENCE-ID', serialized)
        self.assertNotIn('RRULE:', '\n'.join(line for block in blocks if block[0] == 'BEGIN:VEVENT' for line in block))
        self.assertEqual(source.to_dict(), original)
        return serialized

    def test_detach_ordinary_event_keeps_raw_metadata_directly(self):
        source = metadata_source(master(recurrence_rule=''))
        snapshot = detach_event(source, source)
        self.assertEqual(snapshot.raw_ics, source.raw_ics)
        serialized = self.assert_ordinary_copy(source, source, snapshot)
        self.assertEqual(serialized.count('BEGIN:VALARM'), 2)
        self.assertIn('X-SERVER-HEADER:retained header', serialized)

    def test_detach_generated_occurrence_inherits_parent_alarms_and_metadata(self):
        source = metadata_source(master())
        original = source.to_dict()
        selected = instances(source)[2]
        snapshot = detach_event(source, selected)
        serialized = self.assert_ordinary_copy(source, selected, snapshot)
        self.assertEqual(serialized.count('BEGIN:VALARM'), 2)
        self.assertIn('TRIGGER:-PT15M', serialized)
        self.assertIn('TRIGGER:-PT60M', serialized)
        self.assertIn('X-SERVER-INHERITED:retained parent', serialized)
        self.assertIn('X-SERVER-HEADER:retained header', serialized)
        self.assertIn('BEGIN:VTIMEZONE', serialized)
        self.assertEqual(source.to_dict(), original)

    def test_detach_specific_alarm_replaces_whole_parent_alarm_group(self):
        source = master()
        exception = replace(detached(source, 1, shift=timedelta(hours=3)),
                            end=START + timedelta(weeks=1, hours=5),
                            description='Specific notes', location='Specific room')
        source = metadata_source(replace(source, recurrence_overrides={exception.recurrence_id: exception.to_dict()}),
                                 override_alarm=True)
        original = source.to_dict()
        selected = next(value for value in instances(source) if value.recurrence_id == exception.recurrence_id)
        snapshot = detach_event(source, selected)
        serialized = self.assert_ordinary_copy(source, selected, snapshot)
        self.assertEqual(serialized.count('BEGIN:VALARM'), 1)
        self.assertIn('UID:specific-alarm', serialized)
        self.assertIn('TRIGGER:-PT5M', serialized)
        self.assertNotIn('parent-alarm', serialized)
        self.assertNotIn('TRIGGER:-PT15M', serialized)
        self.assertIn('X-SERVER-INHERITED:retained parent', serialized)
        self.assertIn('X-SERVER-GROUP:specific value', serialized)
        self.assertNotIn('X-SERVER-GROUP:base', serialized)
        self.assertIn('X-SERVER-DETACHED:retained override', serialized)
        self.assertIn('X-SERVER-HEADER:retained header', serialized)
        self.assertEqual(source.to_dict(), original)

    def test_detach_override_without_alarms_inherits_parent_alarm_group(self):
        source = master()
        exception = detached(source, 1, shift=timedelta(hours=3))
        source = metadata_source(replace(source, recurrence_overrides={exception.recurrence_id: exception.to_dict()}))
        selected = next(value for value in instances(source) if value.recurrence_id == exception.recurrence_id)
        serialized = self.assert_ordinary_copy(source, selected, detach_event(source, selected))
        self.assertEqual(serialized.count('BEGIN:VALARM'), 2)
        self.assertIn('X-SERVER-DETACHED:retained override', serialized)

    def test_detach_restored_occurrence_does_not_reuse_cancelled_template(self):
        source = master()
        exception = detached(source, 1)
        source = metadata_source(replace(source, recurrence_overrides={exception.recurrence_id: exception.to_dict()}))
        header, blocks = _blocks(source.raw_ics)
        rewritten = []
        for block in blocks:
            if block[0] == 'BEGIN:VEVENT' and any(line.startswith('RECURRENCE-ID') for line in block):
                block = [*block[:-1], 'STATUS:CANCELLED', 'END:VEVENT']
            rewritten.append(block)
        cancelled = parse_ics(folded(['BEGIN:VCALENDAR', *header,
                                     *(line for block in rewritten for line in block), 'END:VCALENDAR']), 'local')[0]
        restored = replace(cancelled, excluded_dates=())
        selected = instances(restored)[1]
        serialized = self.assert_ordinary_copy(restored, selected, detach_event(restored, selected))
        self.assertNotIn('STATUS:CANCELLED', serialized)
        self.assertEqual(serialized.count('BEGIN:VALARM'), 2)

    def test_detach_without_parent_raw_uses_actual_instance_fields(self):
        source = master()
        selected = instances(source)[2]
        snapshot = detach_event(source, selected)
        self.assertEqual(snapshot.raw_ics, '')
        self.assert_ordinary_copy(source, selected, snapshot)

    def test_reidentify_remaps_every_detached_identity_without_mutating_source(self):
        source = master()
        exception = detached(source, 1, shift=timedelta(hours=3))
        source = replace(source, recurrence_overrides={exception.recurrence_id: exception.to_dict()})
        source = replace(source, raw_ics=export_ics([source]).decode())
        original = source.to_dict()
        copied = reidentify_series(source, 'copy', 'new-uid')
        payload = copied.recurrence_overrides[exception.recurrence_id]
        self.assertEqual(copied.id, 'copy')
        self.assertEqual(copied.ical_uid, 'new-uid')
        self.assertEqual(payload['series_id'], 'copy')
        self.assertEqual(payload['ical_uid'], 'new-uid')
        self.assertTrue(payload['id'].startswith('copy::occurrence::'))
        self.assertEqual(payload['recurrence_id'], exception.recurrence_id)
        self.assertEqual(source.to_dict(), original)

    def test_following_copy_preserves_alarms_metadata_and_rewrites_cancelled_uid(self):
        raw = '''BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Example//Metadata//EN\r
X-SERVER-CALENDAR:retained header\r
BEGIN:VTIMEZONE\r
TZID:Europe/Berlin\r
BEGIN:STANDARD\r
DTSTART:19701025T030000\r
TZOFFSETFROM:+0200\r
TZOFFSETTO:+0100\r
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r
END:STANDARD\r
BEGIN:DAYLIGHT\r
DTSTART:19700329T020000\r
TZOFFSETFROM:+0100\r
TZOFFSETTO:+0200\r
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r
END:DAYLIGHT\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:old-resource\r
DTSTAMP:20261001T000000Z\r
DTSTART;TZID=Europe/Berlin:20261019T090000\r
DTEND;TZID=Europe/Berlin:20261019T100000\r
RRULE:FREQ=WEEKLY;COUNT=5\r
SUMMARY:Metadata example\r
X-SERVER-METADATA:retained master\r
BEGIN:VALARM\r
UID:independent-alarm-id\r
ACTION:DISPLAY\r
TRIGGER:-PT15M\r
DESCRIPTION:Retained reminder\r
END:VALARM\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old-resource\r
RECURRENCE-ID;TZID=Europe/Berlin:20261102T090000\r
DTSTAMP:20261001T000000Z\r
DTSTART;TZID=Europe/Berlin:20261102T110000\r
DTEND;TZID=Europe/Berlin:20261102T120000\r
SUMMARY:Detached example\r
X-SERVER-DETACHED:retained override\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old-resource\r
RECURRENCE-ID;TZID=Europe/Berlin:20261109T090000\r
DTSTAMP:20261001T000000Z\r
STATUS:CANCELLED\r
X-SERVER-CANCELLATION:retained cancellation\r
END:VEVENT\r
END:VCALENDAR\r
'''
        source = parse_ics(raw, 'local')[0]
        original = source.to_dict()
        selected = instances(source)[1]
        before, after = partition_series(source, selected)
        copied = reidentify_series(after, 'new-resource-id', 'new-resource-uid')
        serialized = export_ics([copied]).decode()
        self.assertIn('BEGIN:VALARM', serialized)
        self.assertIn('UID:independent-alarm-id', serialized)
        self.assertIn('TRIGGER:-PT15M', serialized)
        self.assertIn('X-SERVER-CALENDAR:retained header', serialized)
        self.assertIn('X-SERVER-METADATA:retained master', serialized)
        self.assertIn('X-SERVER-DETACHED:retained override', serialized)
        self.assertIn('X-SERVER-CANCELLATION:retained cancellation', serialized)
        self.assertIn('BEGIN:VTIMEZONE', serialized)
        _, blocks = _blocks(serialized)
        event_blocks = [block for block in blocks if block[0] == 'BEGIN:VEVENT']
        self.assertEqual(len(event_blocks), 3)
        self.assertEqual({_uid_of(block) for block in event_blocks}, {'new-resource-uid'})
        loaded = parse_ics(serialized, 'local')
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].ical_uid, 'new-resource-uid')
        self.assertEqual(signatures(copied), signatures(loaded[0]))
        self.assertEqual(source.to_dict(), original)
        self.assertEqual(before.ical_uid, source.ical_uid)
        self.assertIn('UID:old-resource', before.raw_ics)

    def test_reidentification_drops_unrelated_event_resources_without_rewriting_alarm_uid(self):
        source = master(recurrence_rule='')
        foreign = replace(source, id='foreign', ical_uid='unrelated-resource')
        source = replace(source, raw_ics=export_ics([source, foreign]).decode())
        copied = reidentify_series(source, 'new-id', 'new-uid')
        _, blocks = _blocks(copied.raw_ics)
        self.assertEqual([_uid_of(block) for block in blocks if block[0] == 'BEGIN:VEVENT'], ['new-uid'])

    def test_malformed_or_wrong_resource_metadata_is_not_silently_discarded(self):
        with self.assertRaisesRegex(RecurrenceError, 'malformed'):
            reidentify_series(replace(master(), raw_ics='broken source'), 'new-id', 'new-uid')
        other = replace(master(), ical_uid='unrelated-resource')
        with self.assertRaisesRegex(RecurrenceError, 'another event'):
            reidentify_series(replace(master(), raw_ics=export_ics([other]).decode()), 'new-id', 'new-uid')

    def test_copying_one_displayed_instance_does_not_copy_its_parent_schedule(self):
        source = master(group_id='old-choices', read_only=True)
        selected = instances(source)[2]
        copied = clone_event(selected, new_id='copy', new_uid='new-uid', calendar_id='destination')
        self.assertFalse(copied.recurring)
        self.assertFalse(copied.read_only)
        self.assertIsNone(copied.group_id)
        self.assertEqual(copied.series_id, '')
        self.assertEqual(copied.calendar_id, 'destination')
        self.assertEqual(copied.start, selected.start)
        self.assertEqual(copied.end, selected.end)

    def test_clone_master_detached_members_leave_read_only_source_group(self):
        source = master(group_id='old-group', read_only=True)
        exception = detached(source, 1, shift=timedelta(hours=3))
        source = replace(source, recurrence_overrides={exception.recurrence_id: exception.to_dict()})
        copied = clone_event(source, new_id='copy', new_uid='new-uid', calendar_id='destination')
        payload = copied.recurrence_overrides[exception.recurrence_id]
        self.assertFalse(payload['read_only'])
        self.assertIsNone(payload['group_id'])
        self.assertEqual(payload['calendar_id'], 'destination')


class RawTemplateProjectionTests(unittest.TestCase):
    def assert_shifted_metadata(self, event):
        serialized = export_ics([event]).decode()
        for retained in ('UID:parent-alarm-first', 'UID:specific-alarm',
                         'X-SERVER-INHERITED:retained parent', 'X-SERVER-DETACHED:retained override',
                         'X-SERVER-CANCELLATION:retained cancellation', 'X-SERVER-HEADER:retained header',
                         'BEGIN:VTIMEZONE'):
            self.assertIn(retained, serialized)
        loaded = parse_ics(serialized, event.calendar_id)[0]
        self.assertEqual(signatures(event), signatures(loaded))
        self.assertEqual(event.excluded_dates, loaded.excluded_dates)
        self.assertEqual(set(event.recurrence_overrides), set(loaded.recurrence_overrides))
        _, blocks = _blocks(serialized)
        for block in blocks:
            if block[0] != 'BEGIN:VEVENT':
                continue
            component = components(folded(['BEGIN:VCALENDAR', *block, 'END:VCALENDAR']))[0]
            if 'RECURRENCE-ID' not in component:
                continue
            key = _key(_decoded(component, 'RECURRENCE-ID', ZONE)[0], ZONE)
            if prop(component, 'STATUS') == 'CANCELLED':
                self.assertIn(key, event.excluded_dates)
            else:
                self.assertIn(key, event.recurrence_overrides)

    def test_shifted_group_and_future_copies_keep_detached_and_cancellation_templates(self):
        source = metadata_with_cancellation()
        original = source.to_dict()
        selected = instances(source)[1]
        for scope in ('group', 'future'):
            with self.subTest(scope=scope):
                clipboard = EventClipboard.capture([source], selected, scope)
                copied = clipboard.instantiate(scope, selected.start + timedelta(days=7), 'destination')[0]
                self.assertNotEqual(copied.ical_uid, source.ical_uid)
                self.assert_shifted_metadata(copied)
        self.assertEqual(source.to_dict(), original)

    def test_saved_group_and_future_moves_keep_templates_and_past_unchanged(self):
        source = metadata_with_cancellation()
        original = source.to_dict()
        for scope, index in (('group', 1), ('future', 1), ('future', 0)):
            with self.subTest(scope=scope, index=index), TemporaryDirectory() as folder, Store(Path(folder) / 'db') as store:
                store.apply('Create', [source])
                selected = store.query_occurrences(LOWER, UPPER)[index]
                edited = replace(selected, start=selected.start + timedelta(days=7), end=selected.end + timedelta(days=7),
                                 recurrence_rule=source.recurrence_rule, recurrence_dates=source.recurrence_dates,
                                 excluded_dates=source.excluded_dates, recurrence_overrides=source.recurrence_overrides)
                if scope == 'group':
                    store.edit_series(selected, edited)
                    projected = store.get_event(source.id)
                else:
                    before, _ = partition_series(source, selected)
                    store.edit_future(selected, edited)
                    if before is not None:
                        self.assertEqual(store.get_event(source.id).to_dict(), before.to_dict())
                        projected = next(event for event in store.list_events() if event.id != source.id)
                    else:
                        self.assertEqual(len(store.list_events()), 1)
                        projected = store.get_event(source.id)
                self.assert_shifted_metadata(projected)
        self.assertEqual(source.to_dict(), original)

    def test_invalid_explicit_dst_move_rejects_copy_and_rolls_back_saved_scopes(self):
        start = datetime(2027, 3, 25, 1, 30, tzinfo=ZONE)
        extra = datetime(2027, 3, 27, 2, 30, tzinfo=ZONE)
        source = master(start=start, end=start + timedelta(hours=1), recurrence_rule='',
                        recurrence_dates=(recurrence_key(extra),))
        selected = instances(source)[0]
        clipboard = EventClipboard.capture([source], selected, 'group')
        with self.assertRaisesRegex(RecurrenceError, 'does not exist'):
            clipboard.instantiate('group', selected.start + timedelta(days=1), 'destination')
        for scope in ('group', 'future'):
            with self.subTest(scope=scope), TemporaryDirectory() as folder, Store(Path(folder) / 'db') as store:
                store.apply('Create', [source])
                original = store.list_events()
                history = store.history()
                pending = store.pending_changes()
                selected = store.query_occurrences(LOWER, UPPER)[0]
                edited = replace(selected, start=selected.start + timedelta(days=1), end=selected.end + timedelta(days=1),
                                 recurrence_dates=source.recurrence_dates)
                with self.assertRaisesRegex(RecurrenceError, 'does not exist'):
                    (store.edit_series if scope == 'group' else store.edit_future)(selected, edited)
                self.assertEqual(store.list_events(), original)
                self.assertEqual(store.history(), history)
                self.assertEqual(store.pending_changes(), pending)

    def test_invalid_master_and_detached_endpoints_are_rejected_before_normalization(self):
        from debacal.recurrence import updated_series
        for kind in ('master-start', 'master-end', 'override-start', 'exclusion'):
            with self.subTest(kind=kind):
                start = datetime(2027, 3, 25, 1, 30, tzinfo=ZONE)
                source = master(start=start, end=start + timedelta(minutes=30))
                if kind.startswith('master'):
                    start = datetime(2027, 3, 27, 2 if kind == 'master-start' else 1, 30, tzinfo=ZONE)
                    source = replace(source, start=start, end=start + timedelta(hours=1))
                elif kind.startswith('override'):
                    scheduled = datetime(2027, 4, 1, 1, 30, tzinfo=ZONE)
                    exception = occurrence(source, scheduled)
                    override_start = datetime(2027, 3, 27, 2 if kind == 'override-start' else 1, 30, tzinfo=ZONE)
                    exception = replace(exception, start=override_start, end=override_start + timedelta(hours=1))
                    source = replace(source, recurrence_overrides={exception.recurrence_id: exception.to_dict()})
                else:
                    source = replace(source, excluded_dates=(recurrence_key(datetime(2027, 3, 27, 2, 30, tzinfo=ZONE)),))
                edited = replace(source, start=source.start + timedelta(days=1), end=source.end + timedelta(days=1))
                with self.assertRaisesRegex(RecurrenceError, 'does not exist'):
                    updated_series(source, source, edited)


if __name__ == '__main__':
    unittest.main()
