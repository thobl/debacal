from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from debacal.patterns import (
    PatternError, describe_pattern, expand_pattern, expand_sequence,
    reconstruct_pattern, reconstruct_sequence, serialize_sequence,
    shift_schedule_expression,
)


ZONE = ZoneInfo('Europe/Berlin')
ANCHOR = datetime(2026, 11, 7, 9, tzinfo=ZONE)


class SequenceTests(unittest.TestCase):
    def test_requested_example_includes_fixed_event_and_rolls_partial_dates(self):
        values = expand_sequence('+0d 13. +1w 18. 2. 3.2. +2w', ANCHOR)
        self.assertEqual([value.date() for value in values],
                         [date(2026, 11, 7), date(2026, 11, 13), date(2026, 11, 20),
                          date(2026, 12, 18), date(2027, 1, 2), date(2027, 2, 3), date(2027, 2, 17)])
        self.assertTrue(all(value.hour == 9 for value in values))
        self.assertEqual(values[0], ANCHOR)

    def test_relative_inherits_previous_clock_but_date_only_resets_fixed_clock(self):
        values = expand_sequence('+0d +1w 14:00 +1d 18. +1w', ANCHOR)
        self.assertEqual([(value.month, value.day, value.hour) for value in values],
                         [(11, 7, 9), (11, 14, 9), (11, 14, 14), (11, 15, 14), (11, 18, 9), (11, 25, 9)])

    def test_explicit_clock_date_prefix_and_time_only(self):
        values = expand_sequence('+0d 14:20-13. +1w 22. 15:45-3.2.', ANCHOR)
        self.assertEqual([(value.year, value.month, value.day, value.hour, value.minute) for value in values],
                         [(2026, 11, 7, 9, 0), (2026, 11, 13, 14, 20), (2026, 11, 20, 14, 20),
                          (2026, 11, 22, 9, 0), (2027, 2, 3, 15, 45)])
        values = expand_sequence('+0d 09:00 10:00', ANCHOR)
        self.assertEqual(values[1].date(), ANCHOR.date() + timedelta(days=1))
        self.assertEqual(values[2].date(), values[1].date())

    def test_omitted_components_choose_strictly_next_match(self):
        anchor = ANCHOR.replace(day=13)
        self.assertEqual(expand_sequence('+0d 13.', anchor)[1].date(), date(2026, 12, 13))
        self.assertEqual(expand_sequence('+0d 13.11.', anchor)[1].date(), date(2027, 11, 13))
        with self.assertRaises(PatternError):
            expand_sequence('13.11', anchor)
        self.assertEqual(expand_sequence('+0d 10:00-13.', anchor)[1].date(), date(2026, 11, 13))

    def test_fully_specified_dates_are_exact_even_before_cursor_or_fixed_event(self):
        values = expand_sequence('+0d 1.1.2026 +1w 2.', ANCHOR)
        self.assertEqual([value.date() for value in values],
                         [date(2026, 11, 7), date(2026, 1, 1), date(2026, 1, 8), date(2026, 2, 2)])
        self.assertEqual(expand_sequence('+0d 11:30-3.2.2025', ANCHOR)[1],
                         datetime(2025, 2, 3, 11, 30, tzinfo=ZONE))

    def test_duplicate_exact_dates_are_deduplicated_but_still_move_cursor(self):
        values = expand_sequence('+0d 1.1.2026 7.11.2026 +1w', ANCHOR)
        self.assertEqual([value.date() for value in values],
                         [date(2026, 11, 7), date(2026, 1, 1), date(2026, 11, 14)])
        self.assertEqual(expand_sequence('3*7.11.2026', ANCHOR), [ANCHOR])

    def test_units_and_stepwise_calendar_clipping(self):
        with self.assertRaises(PatternError):
            expand_sequence('+2', ANCHOR)
        self.assertEqual(expand_sequence('+0d +3d', ANCHOR)[1].date(), date(2026, 11, 10))
        january = datetime(2027, 1, 31, 9, tzinfo=ZONE)
        self.assertEqual([value.date() for value in expand_sequence('+0d 2*+1m!', january)],
                         [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)])
        self.assertEqual(expand_sequence('+0d +2m', january)[1].date(), date(2027, 3, 31))
        leap = datetime(2028, 2, 29, 9, tzinfo=ZONE)
        self.assertEqual(expand_sequence('+0d +1y!', leap)[1].date(), date(2029, 2, 28))




    def test_impossible_partial_dates_skip_only_matching_cycles(self):
        january = datetime(2027, 1, 31, 9, tzinfo=ZONE)
        self.assertEqual(expand_sequence('+0d 31.', january)[1].date(), date(2027, 3, 31))
        self.assertEqual(expand_sequence('+0d 29.2.', ANCHOR)[1].date(), date(2028, 2, 29))
        with self.assertRaisesRegex(PatternError, 'does not exist'):
            expand_sequence('31.4.', ANCHOR)

    def test_repeats_execute_atoms_and_nested_groups_against_new_cursor(self):
        self.assertEqual([value.date() for value in expand_sequence('+0d 3*+1w', ANCHOR)],
                         [date(2026, 11, 7), date(2026, 11, 14), date(2026, 11, 21), date(2026, 11, 28)])
        values = expand_sequence('+0d 2*(+1d, 2*(+1w +2d))', ANCHOR)
        self.assertEqual([(value.date() - ANCHOR.date()).days for value in values],
                         [0, 1, 8, 10, 17, 19, 20, 27, 29, 36, 38])
        values = expand_sequence('+0d 2*(8. 7.)', ANCHOR)
        self.assertEqual([value.date() for value in values],
                         [date(2026, 11, 7), date(2026, 11, 8), date(2026, 12, 7),
                          date(2026, 12, 8), date(2027, 1, 7)])

    def test_spaces_and_commas_can_be_mixed(self):
        values = expand_sequence(' +0d 2 * ( +1d , +1w ) , 13. ', ANCHOR)
        self.assertEqual([value.date() for value in values],
                         [date(2026, 11, 7), date(2026, 11, 8), date(2026, 11, 15),
                          date(2026, 11, 16), date(2026, 11, 23), date(2026, 12, 13)])

    def test_dst_relative_weeks_preserve_local_clock_not_utc_offset(self):
        start = datetime(2027, 3, 21, 9, tzinfo=ZONE)
        following = expand_sequence('+0d +1w', start)[1]
        self.assertEqual(following.hour, 9)
        self.assertNotEqual(start.utcoffset(), following.utcoffset())
        self.assertEqual(following.timestamp() - start.timestamp(), 167 * 3600)


    def test_nonexistent_dst_times_are_actionable_errors(self):
        for expression, anchor in [('+0d +1d', datetime(2027, 3, 27, 2, 30, tzinfo=ZONE)),
                                   ('+1m', datetime(2027, 2, 28, 2, 30, tzinfo=ZONE)),
                                   ('28.3.2027', datetime(2027, 1, 1, 2, 30, tzinfo=ZONE)),
                                   ('+0d 02:30', datetime(2027, 3, 27, 3, tzinfo=ZONE))]:
            with self.subTest(expression=expression), self.assertRaisesRegex(PatternError, 'clocks change'):
                expand_sequence(expression, anchor)
        after_gap = datetime(2027, 3, 28, 3, tzinfo=ZONE)
        self.assertEqual(expand_sequence('+0d 02:30', after_gap)[1].date(), date(2027, 3, 29))

    def test_ambiguous_time_fold_policy_is_preserved(self):
        start = datetime(2026, 10, 24, 2, 30, tzinfo=ZONE, fold=1)
        value = expand_sequence('+0d +1d', start)[1]
        self.assertEqual(value.fold, 1)
        self.assertEqual(value.utcoffset(), timedelta(hours=1))
        anchor = datetime(2026, 10, 24, 9, tzinfo=ZONE, fold=1)
        self.assertEqual(expand_sequence('+0d 02:30-25.10.2026', anchor)[1].fold, 1)

    def test_subminute_anchor_clock_is_retained_by_relative_and_date_only_steps(self):
        anchor = ANCHOR.replace(second=17, microsecond=123)
        values = expand_sequence('+0d +1w 18.', anchor)
        self.assertTrue(all((value.second, value.microsecond) == (17, 123) for value in values))

    def test_invalid_syntax_is_rejected_with_useful_errors(self):
        invalid = ['Mo X', '*+1', '2**+1', '+1+2', '0*+1', '+0', '-1', '+1hr', '+1.5', '+2', '+1h', '+60min', '1.1.2026.',
                   '+0h', '+0min', '+1mins', '+1minh', '+1.5h', '+1000000000min',
                   '24:00', '23:60', '3.13.', '31.4.', '0.', '32.', '1.1.0000', '1.1.10000',
                   '09:00-', '2026-11-10', '()', '(+1', '+1)', '+1,,+2', ',+1', '+1,', '2*()']
        for expression in invalid:
            with self.subTest(expression=expression), self.assertRaises(PatternError):
                expand_sequence(expression, ANCHOR)

    def test_limits_are_checked_before_executing_or_allocating_large_repeats(self):
        self.assertEqual(len(expand_sequence('+0d 499*+1d', ANCHOR)), 500)
        with patch('debacal.patterns._relative', side_effect=AssertionError('must not execute')):
            for expression in ['+0d 500*+1d', '999999999999999999*+1', '9999*(9999*(+1))',
                               '(' * 9 + '+0d +1w' + ')' * 9, '+1 ' * 3000]:
                with self.subTest(expression=expression), self.assertRaises(PatternError):
                    expand_sequence(expression, ANCHOR)
        with self.assertRaises(PatternError):
            expand_sequence('501*7.11.2026', ANCHOR)
        self.assertEqual(expand_sequence('+0d', ANCHOR, limit=1), [ANCHOR])
        with self.assertRaises(PatternError):
            expand_sequence('+0d +1w', ANCHOR, limit=1)

    def test_overflow_and_bad_anchor_are_pattern_errors(self):
        late = datetime(9999, 12, 31, 9, tzinfo=timezone.utc)
        for expression in ['+0d +1d', '+0d +1w', '+1m', '+0d +1y!', '+0d 31.', '31.12.']:
            with self.subTest(expression=expression), self.assertRaises(PatternError):
                expand_sequence(expression, late)
        with self.assertRaises(PatternError):
            expand_sequence('+0d +1w', datetime(2026, 11, 7, 9))
        with self.assertRaises(PatternError):
            expand_sequence('+0d +1w', date(2026, 11, 7))
        with self.assertRaisesRegex(PatternError, 'supported years'):
            expand_sequence('+0d', datetime(9999, 12, 31, 23, 59, tzinfo=ZoneInfo('America/New_York')))


    def test_date_wrapper_and_human_summary(self):
        self.assertEqual(expand_pattern('+0d 2*+1w', ANCHOR.date()),
                         [date(2026, 11, 7), date(2026, 11, 14), date(2026, 11, 21)])
        self.assertIn('3 events', describe_pattern('+0d 2*+1w', ANCHOR.date()))


class SequenceSerializationTests(unittest.TestCase):
    def test_absolute_serialization_preserves_first_anchor_backward_steps_and_clocks(self):
        values = [ANCHOR, datetime(2026, 11, 13, 9, tzinfo=ZONE), datetime(2026, 11, 20, 14, 15, tzinfo=ZONE),
                  datetime(2025, 2, 3, 11, tzinfo=ZONE)]
        text = serialize_sequence(values)
        self.assertIn('14:15-20.11.2026', text)
        self.assertEqual(expand_sequence(text, values[0]), values)


    def test_timed_reconstruction_and_duplicate_inputs_are_lossless(self):
        values = expand_sequence('+0d +1w 14:00 +1d 18.', ANCHOR)
        self.assertEqual(expand_sequence(reconstruct_sequence(values), values[0]), values)
        self.assertEqual(expand_sequence(serialize_sequence([ANCHOR, ANCHOR, *values[1:]]), ANCHOR), values)

    def test_relative_only_move_retains_exact_literal_when_equivalent(self):
        expression = ' +0d 2*(+1w  +2d) '
        values = expand_sequence(expression, ANCHOR)
        delta = timedelta(days=1, hours=2)
        moved = [value + delta for value in values]
        text, anchor = shift_schedule_expression(expression, ANCHOR.date().isoformat(), delta, moved)
        self.assertEqual(text, expression)
        self.assertEqual(anchor, moved[0].date().isoformat())
        self.assertEqual(expand_sequence(text, moved[0]), moved)

    def test_move_with_absolute_clocks_rewrites_to_actual_instants(self):
        expression = '+0d 13. 14:00 +1w'
        values = expand_sequence(expression, ANCHOR)
        moved = [value + timedelta(hours=1) for value in values]
        text, anchor = shift_schedule_expression(expression, ANCHOR.date().isoformat(), timedelta(hours=1), moved)
        self.assertNotEqual(text, expression)
        self.assertEqual(expand_sequence(text, moved[0]), moved)
        self.assertEqual(anchor, ANCHOR.date().isoformat())

    def test_calendar_relative_move_falls_back_if_clipping_would_change_dates(self):
        start = datetime(2027, 1, 31, 9, tzinfo=ZONE)
        values = expand_sequence('+0d 2*+1m!', start)
        moved = [value - timedelta(days=1) for value in values]
        text, _ = shift_schedule_expression('+0d 2*+1m!', start.date().isoformat(), timedelta(days=-1), moved)
        self.assertEqual(expand_sequence(text, moved[0]), moved)
        self.assertNotEqual(text, '+0d 2*+1m!')

    def test_unrepresentable_clock_differences_raise_instead_of_rounding(self):
        with self.assertRaisesRegex(PatternError, 'sub-minute'):
            serialize_sequence([ANCHOR, ANCHOR.replace(hour=10, second=1)])
        first = datetime(2026, 10, 24, 9, tzinfo=ZONE)
        later_fold = datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)
        with self.assertRaisesRegex(PatternError, 'ambiguous'):
            serialize_sequence([first, later_fold])

    def test_single_value_authorship_normalizes_to_an_empty_addition_sequence(self):
        moved = ANCHOR + timedelta(days=1)
        text, anchor = shift_schedule_expression('7.11.2026', ANCHOR.date().isoformat(), timedelta(days=1), [moved])
        self.assertEqual(text, '+0d')
        self.assertEqual(expand_sequence(text, moved), [moved])
        self.assertEqual(anchor, moved.date().isoformat())



if __name__ == '__main__':
    unittest.main()
