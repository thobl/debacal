"""Versioned authoring text remains faithful to canonical RRULE/RDATE storage."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from debacal.models import Calendar, Event
from debacal.patterns import expand_sequence, serialize_sequence
from debacal.recurrence import RecurrenceError, base_schedule_starts, expand_event, recurrence_set, schedule_fields
from debacal.store import Store
from debacal.schedule_authoring import displayed_starts, expression_matches


ZONE = ZoneInfo('Europe/Berlin')
ANCHOR = datetime(2026, 11, 4, 10, tzinfo=ZONE)
LITERAL = '  +0d 3*+2w    +3w 2*(+2w)  '


def custom_event(expression=LITERAL, *, candidate=False, calendar_id='local', anchor=ANCHOR):
    starts = expand_sequence(expression, anchor)
    return Event('custom', calendar_id, 'Seminar', starts[0], starts[0] + timedelta(hours=1),
                 **schedule_fields(starts), schedule_expression=expression,
                 expression_anchor=anchor.date().isoformat(), schedule_syntax=2, candidate=candidate)




def instants(values):
    return {value.timestamp() for value in values}


class ExpressionTests(unittest.TestCase):
    def assert_authoring_matches(self, event):
        self.assertTrue(expression_matches(event, event.schedule_expression))

    def test_literal_anchor_and_version_survive_restart_history_and_outbox(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.sqlite3'
            with Store(path) as store:
                store.save_calendar(Calendar('remote', 'Remote', '#678956', kind='caldav', url='https://example.test/cal/'))
                master = custom_event(calendar_id='remote')
                store.apply('Create custom schedule', [master])
                self.assertEqual(store.pending_changes()[0]['payload']['schedule_expression'], LITERAL)
                self.assertEqual(store.pending_changes()[0]['payload']['schedule_syntax'], 2)
            with Store(path) as store:
                self.assertEqual(store.get_event(master.id).schedule_expression, LITERAL)
                self.assertEqual(store.get_event(master.id).expression_anchor, ANCHOR.date().isoformat())
                store.undo()
                self.assertIsNone(store.get_event(master.id))
                store.redo()
                self.assertEqual(store.get_event(master.id), master)

    def test_relative_only_literal_survives_title_time_and_day_moves(self):
        with Store(':memory:') as store:
            master = custom_event()
            original = base_schedule_starts(master)
            store.apply('Create', [master])
            moved = replace(master, title='New seminar title', start=master.start + timedelta(days=1, hours=2),
                            end=master.end + timedelta(days=1, hours=2))
            store.edit_series(master, moved)
            updated = store.get_event(master.id)
            self.assertEqual(updated.schedule_expression, LITERAL)
            self.assertEqual(updated.expression_anchor, '2026-11-05')
            self.assertEqual(updated.schedule_syntax, 2)
            self.assertEqual(instants(base_schedule_starts(updated)), instants([value + timedelta(days=1, hours=2) for value in original]))
            self.assert_authoring_matches(updated)

    def test_time_only_and_absolute_steps_rewrite_without_losing_different_times(self):
        with Store(':memory:') as store:
            master = custom_event('+0d 14:30 08:15-8.11.2026 12:05-15.11.2026')
            original = base_schedule_starts(master)
            store.apply('Create', [master])
            store.edit_series(master, replace(master, start=master.start + timedelta(days=1, hours=2),
                                              end=master.end + timedelta(days=1, hours=2)))
            updated = store.get_event(master.id)
            self.assertNotEqual(updated.schedule_expression, master.schedule_expression)
            self.assertEqual(instants(base_schedule_starts(updated)), instants([value + timedelta(days=1, hours=2) for value in original]))
            self.assert_authoring_matches(updated)
            actual = expand_event(updated, min(original), max(original) + timedelta(days=4))
            self.assertTrue(all(event.end - event.start == timedelta(hours=1) for event in actual))

    def test_explicit_earlier_dates_never_replace_fixed_initial_anchor(self):
        with Store(':memory:') as store:
            master = custom_event('+0d 08:30-1.10.2026 +1d 12:00')
            self.assertEqual(master.start, ANCHOR)
            self.assertEqual(master.recurrence_rule, '')
            store.apply('Create', [master])
            store.edit_series(master, replace(master, start=master.start + timedelta(days=1), end=master.end + timedelta(days=1)))
            updated = store.get_event(master.id)
            self.assertEqual(updated.start, ANCHOR + timedelta(days=1))
            self.assert_authoring_matches(updated)

    def test_count_rule_custom_series_preserves_all_authoring_steps_when_moved(self):
        with Store(':memory:') as store:
            master = custom_event('  +0d 4*+2w  ')
            self.assertIn('FREQ=WEEKLY', master.recurrence_rule)
            self.assertEqual(master.recurrence_dates, ())
            store.apply('Create', [master])
            store.edit_series(master, replace(master, start=master.start + timedelta(days=1), end=master.end + timedelta(days=1)))
            updated = store.get_event(master.id)
            self.assertEqual(updated.schedule_expression, '  +0d 4*+2w  ')
            self.assertEqual(len(base_schedule_starts(updated)), 5)
            self.assert_authoring_matches(updated)

    def test_new_literal_absolute_schedule_replacement_is_authoritative(self):
        with Store(':memory:') as store:
            master = custom_event()
            store.apply('Create', [master])
            replacement = custom_event('+0d +1d +2d', anchor=ANCHOR + timedelta(days=5))
            store.edit_series(master, replacement, replace_schedule=True)
            updated = store.get_event(master.id)
            self.assertEqual(updated.schedule_expression, '+0d +1d +2d')
            self.assertEqual(updated.start, replacement.start)
            self.assertEqual(updated.schedule_syntax, 2)
            self.assert_authoring_matches(updated)

    def test_single_override_keeps_parent_language_and_clears_its_own_authoring(self):
        with Store(':memory:') as store:
            master = custom_event()
            store.apply('Create', [master])
            selected = store.query_occurrences(master.start, master.start + timedelta(days=60))[1]
            store.edit_occurrence(selected, replace(selected, title='Only one seminar'))
            updated = store.get_event(master.id)
            self.assertEqual(updated.schedule_expression, LITERAL)
            self.assertEqual(updated.schedule_syntax, 2)
            override = updated.recurrence_overrides[selected.recurrence_id]
            self.assertEqual((override['schedule_expression'], override['expression_anchor'], override['schedule_syntax']), ('', '', 1))

    def test_confirm_and_convert_to_once_clear_authoring_and_undo_restores_it(self):
        with Store(':memory:') as store:
            master = custom_event(candidate=True)
            store.apply('Create', [master])
            selected = store.query_occurrences(master.start, master.start + timedelta(days=60))[1]
            store.confirm_candidate(selected)
            confirmed = store.get_event(master.id)
            self.assertEqual((confirmed.schedule_expression, confirmed.expression_anchor, confirmed.schedule_syntax), ('', '', 1))
            store.undo()
            self.assertEqual(store.get_event(master.id), master)
            store.edit_series(master, replace(master, recurrence_dates=(), recurrence_rule='', schedule_expression='', expression_anchor=''))
            ordinary = store.get_event(master.id)
            self.assertFalse(ordinary.recurring)
            self.assertEqual((ordinary.schedule_expression, ordinary.expression_anchor, ordinary.schedule_syntax), ('', '', 1))



    def test_one_date_authored_series_and_old_index_migration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.sqlite3'
            with Store(path) as store:
                master = custom_event('10:00-4.11.2026')
                self.assertEqual(master.recurrence_dates, ())
                self.assertTrue(master.recurring)
                store.apply('Create one-date custom schedule', [master])
                store.connection.execute('UPDATE events SET recurring=0 WHERE id=?', (master.id,))
                store.connection.commit()
            with Store(path) as store:
                occurrences = store.query_occurrences(master.start, master.end)
                self.assertEqual(len(occurrences), 1)
                self.assertEqual(occurrences[0].series_id, master.id)
                changed = replace(master, start=master.start + timedelta(days=1), end=master.end + timedelta(days=1))
                store.edit_series(master, changed)
                self.assert_authoring_matches(store.get_event(master.id))

    def test_exclusions_and_detached_overrides_keep_identity_while_text_describes_actual_starts(self):
        with Store(':memory:') as store:
            master = custom_event('+0d +2w +2w +3w')
            store.apply('Create', [master])
            original = store.query_occurrences(master.start, master.start + timedelta(days=80))
            store.delete_occurrence(original[1])
            store.edit_occurrence(original[2], replace(original[2], start=original[2].start + timedelta(hours=2), end=original[2].end + timedelta(hours=2)))
            master = store.get_event(master.id)
            store.edit_series(master, replace(master, start=master.start + timedelta(days=1), end=master.end + timedelta(days=1)))
            updated = store.get_event(master.id)
            self.assertEqual(len(updated.excluded_dates), 1)
            self.assertEqual(len(updated.recurrence_overrides), 1)
            self.assertEqual(len(base_schedule_starts(updated)), 4)
            self.assert_authoring_matches(updated)

    def test_month_clamp_move_reevaluates_relative_program_from_the_new_primary(self):
        with Store(':memory:') as store:
            master = custom_event('+0d 3*+1m!', anchor=datetime(2027, 1, 31, 10, tzinfo=ZONE))
            store.apply('Create', [master])
            store.edit_series(master, replace(master, start=master.start + timedelta(days=1), end=master.end + timedelta(days=1)))
            updated = store.get_event(master.id)
            self.assertEqual(updated.schedule_expression, master.schedule_expression)
            self.assertEqual(instants(base_schedule_starts(updated)),
                             instants(expand_sequence(master.schedule_expression, updated.start)))
            self.assert_authoring_matches(updated)


class ScheduleSynthesisTests(unittest.TestCase):
    def test_regular_sequences_synthesize_exact_standard_count_rules(self):
        for expression, frequency in [('+0d 4*+1d', 'DAILY'), ('+0d 4*+2w', 'WEEKLY'), ('+0d 4*+1m', 'MONTHLY'),
                                      ('+0d 3*+1y', 'YEARLY')]:
            with self.subTest(expression=expression):
                starts = expand_sequence(expression, ANCHOR)
                fields = schedule_fields(starts)
                self.assertIn('FREQ=' + frequency, fields['recurrence_rule'])
                event = Event('rule', 'local', 'Rule', starts[0], starts[0] + timedelta(hours=1), **fields)
                self.assertEqual(instants(base_schedule_starts(event)), instants(starts))

    def test_relative_clock_rules_have_exact_finite_counts_and_expand_through_midnight(self):
        anchor = ANCHOR.replace(hour=23, minute=30)
        for minutes, expected in [(120, 'FREQ=HOURLY;INTERVAL=2;COUNT=5'),
                                  (45, 'FREQ=MINUTELY;INTERVAL=45;COUNT=5')]:
            with self.subTest(minutes=minutes):
                starts = [anchor + index * timedelta(minutes=minutes) for index in range(5)]
                fields = schedule_fields(starts)
                self.assertEqual(fields, {'recurrence_rule': expected, 'recurrence_dates': ()})
                event = Event('rule', 'local', 'Rule', anchor, anchor + timedelta(minutes=15), **fields)
                expanded = expand_event(event, anchor - timedelta(hours=1), anchor + timedelta(days=2))
                self.assertEqual([value.start.timestamp() for value in expanded], [value.timestamp() for value in starts])

    def test_clock_rule_synthesis_certifies_dst_and_preserves_ambiguous_folds(self):
        spring = datetime(2027, 3, 28, 1, 30, tzinfo=ZONE)
        for anchor, minutes, frequency in [(spring, 120, 'HOURLY'),
                                            (spring, 90, 'MINUTELY'),
                                            (datetime(2026, 10, 25, 1, 30, tzinfo=ZONE), 60, 'HOURLY')]:
            with self.subTest(minutes=minutes, anchor=anchor):
                starts = [anchor + index * timedelta(minutes=minutes) for index in range(4)]
                fields = schedule_fields(starts)
                self.assertIn('FREQ=' + frequency, fields['recurrence_rule'])
                event = Event('dst', 'local', 'DST', anchor, anchor + timedelta(minutes=10), **fields)
                self.assertEqual(instants(base_schedule_starts(event)), instants(starts))
                expanded = expand_event(event, anchor - timedelta(hours=1), starts[-1] + timedelta(hours=1))
                self.assertEqual(instants(value.start for value in expanded), instants(starts))
        first = datetime(2026, 10, 25, 1, 30, tzinfo=ZONE, fold=1)
        starts = [(first + index * timedelta(hours=1)).replace(fold=first.fold) for index in range(4)]
        fields = schedule_fields(starts)
        self.assertFalse(fields['recurrence_rule'])
        event = Event('fold', 'local', 'Fold', first, first + timedelta(minutes=10), **fields)
        self.assertEqual(instants(base_schedule_starts(event)), instants(starts))

    def test_clock_rules_do_not_replace_calendar_units_or_round_imported_precision(self):
        for hours, frequency in [(24, 'DAILY'), (168, 'WEEKLY')]:
            starts = [ANCHOR + index * timedelta(hours=hours) for index in range(4)]
            self.assertIn('FREQ=' + frequency, schedule_fields(starts)['recurrence_rule'])
        precise = ANCHOR.replace(microsecond=123)
        starts = [precise + index * timedelta(minutes=15) for index in range(5)]
        fields = schedule_fields(starts)
        self.assertFalse(fields['recurrence_rule'])
        event = Event('precise', 'local', 'Precise', precise, precise + timedelta(minutes=10), **fields)
        self.assertEqual(instants(base_schedule_starts(event)), instants(starts))

    def test_unbounded_or_filtered_minutely_rules_remain_explicitly_rejected(self):
        for rule in ['FREQ=MINUTELY', 'FREQ=MINUTELY;UNTIL=20990101T000000Z',
                     'FREQ=MINUTELY;COUNT=10001', 'FREQ=MINUTELY;COUNT=5;BYMONTH=12']:
            with self.subTest(rule=rule), self.assertRaisesRegex(RecurrenceError, 'finite count'):
                recurrence_set(Event('dense', 'local', 'Dense', ANCHOR, ANCHOR + timedelta(minutes=10), recurrence_rule=rule))

    def test_irregular_different_times_and_earlier_dates_use_exact_rdates(self):
        for expression in ['+0d 14:30 +2d 09:00', '+0d 08:30-1.10.2026 +1d', '+0d +1w +2w +3w']:
            starts = expand_sequence(expression, ANCHOR)
            fields = schedule_fields(starts)
            self.assertEqual(fields['recurrence_rule'], '')
            event = Event('dates', 'local', 'Dates', starts[0], starts[0] + timedelta(minutes=45), **fields)
            self.assertEqual(event.start, ANCHOR)
            self.assertEqual(instants(base_schedule_starts(event)), instants(starts))

    def test_different_folds_do_not_get_synthesized_as_same_clock_rule(self):
        first = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=0)
        second = first.replace(fold=1)
        fields = schedule_fields([first, second])
        self.assertEqual(fields['recurrence_rule'], '')
        self.assertEqual(len(fields['recurrence_dates']), 1)

    def test_exported_standard_schedule_matches_independent_icalendar_expansion(self):
        try:
            import icalendar
            import recurring_ical_events
        except ImportError:
            self.skipTest('Independent optional recurrence library is unavailable')
        from debacal.ics import export_ics, parse_ics
        cases = [(LITERAL, ANCHOR), ('+0d 4*+2w', ANCHOR), ('+0d 4*+1m', ANCHOR),
                 ('+0d 14:30 08:15-8.11.2026 12:05-15.11.2026', ANCHOR),
                 ('+0d 08:30-1.10.2026 +1d 12:00', ANCHOR),
                 ('+0d 3*+1m!', datetime(2027, 1, 31, 10, tzinfo=ZONE))]
        for expression, anchor in cases:
            with self.subTest(expression=expression):
                event = custom_event(expression, anchor=anchor)
                expected = expand_sequence(expression, anchor)
                data = export_ics([event])
                calendar = icalendar.Calendar.from_ical(data)
                master_component = calendar.walk('VEVENT')[0]
                self.assertEqual(master_component.decoded('DTSTART').timestamp(), anchor.timestamp())
                independent = recurring_ical_events.of(calendar).between(min(expected) - timedelta(days=1), max(expected) + timedelta(days=1))
                self.assertEqual(instants(component.decoded('DTSTART') for component in independent), instants(expected))
                restored = parse_ics(data, 'local')[0]
                self.assertEqual(restored.schedule_expression, expression)
                self.assertEqual(restored.schedule_syntax, 2)
                self.assertEqual(restored.timezone_id, event.timezone_id)
                self.assertEqual(instants(base_schedule_starts(restored)), instants(expected))


class CalendarMetadataTests(unittest.TestCase):
    def test_server_color_can_follow_server_or_keep_manual_override(self):
        original = Calendar('remote', 'Remote', '#112233', server_color='#445566')
        self.assertEqual(original.color, '#445566')
        custom = replace(original, use_server_color=False, color='#aabbcc')
        refreshed = replace(custom, server_color='#cc3344')
        self.assertEqual(refreshed.color, '#aabbcc')
        self.assertEqual(replace(refreshed, use_server_color=True).color, '#cc3344')
        legacy = Calendar.from_dict({'id': 'old', 'name': 'Old', 'color': '#112233'})
        self.assertEqual(legacy.color, '#112233')
        self.assertEqual(legacy.server_color, '')

    def test_default_calendar_is_persisted_and_falls_back_from_read_only(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.sqlite3'
            with Store(path) as store:
                remote = Calendar('remote', 'Remote', '#112233', kind='caldav')
                store.save_calendar(remote)
                self.assertEqual(store.get_default_calendar().id, 'local')
                store.set_default_calendar(remote.id)
            with Store(path) as store:
                self.assertEqual(store.get_default_calendar().id, 'remote')
                store.save_calendar(replace(remote, read_only=True))
                self.assertEqual(store.get_default_calendar().id, 'local')
                with self.assertRaises(ValueError):
                    store.set_default_calendar('remote')
                with self.assertRaises(ValueError):
                    store.set_default_calendar('missing')
                store.save_calendar(replace(store.get_calendar('local'), read_only=True))
                self.assertIsNone(store.get_default_calendar())

    def test_quarantine_preserves_raw_data_without_changing_last_good_event(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'calendar.sqlite3'
            raw = b'BEGIN:VCALENDAR\r\ninvalid\xff\r\nEND:VCALENDAR\r\n'
            with Store(path) as store:
                master = custom_event()
                store.apply('Create', [master])
                store.set_quarantined_resource('local', 'bad-resource', raw, 'Invalid resource')
                store.set_quarantined_resource('local', 'bad-resource', raw, 'Updated reason')
                self.assertEqual(len(store.list_quarantined_resources()), 1)
                self.assertEqual(store.get_event(master.id), master)
                self.assertEqual(len(store.history()), 1)
            with Store(path) as store:
                record = store.list_quarantined_resources('local')[0]
                self.assertEqual(record['raw_ics'], raw)
                self.assertEqual(record['reason'], 'Updated reason')
                store.delete_quarantined_resource('local', 'bad-resource')
                self.assertEqual(store.list_quarantined_resources(), [])

    def test_invalid_cached_rule_does_not_hide_healthy_events(self):
        with Store(':memory:') as store:
            healthy = replace(custom_event(), id='healthy', recurrence_dates=(), recurrence_rule='',
                              schedule_expression='', expression_anchor='', schedule_syntax=1)
            store.apply('Create', [healthy])
            bad = replace(healthy, id='bad', ical_uid='bad-uid', title='Private title', recurrence_rule='FREQ=INVALID', raw_ics='original raw')
            store.connection.execute('INSERT INTO events(id,calendar_id,start,end,data,recurring) VALUES (?,?,?,?,?,?)',
                                     (bad.id, bad.calendar_id, bad.start.timestamp(), bad.end.timestamp(), json.dumps(bad.to_dict()), True))
            store.connection.commit()
            self.assertEqual(store.query_occurrences(healthy.start, healthy.end), [healthy])
            record = store.list_quarantined_resources()[0]
            self.assertEqual(record['resource_id'], hashlib.sha256(b'bad-uid').hexdigest()[:12])
            self.assertEqual(record['raw_ics'], 'original raw')
            self.assertNotIn('Private title', record['reason'])
