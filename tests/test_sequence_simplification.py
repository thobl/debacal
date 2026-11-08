"""Compact clicked-date text must remain equivalent to the actual instants."""
from datetime import datetime, timedelta, timezone
import random
import unittest
from zoneinfo import ZoneInfo

from debacal.patterns import (
    PatternError, expand_sequence, reconstruct_sequence, serialize_sequence, simplify_sequence,
)


ZONE = ZoneInfo('Europe/Berlin')
ANCHOR = datetime(2026, 11, 7, 9, tzinfo=ZONE)


class SimplificationTests(unittest.TestCase):
    def assert_equivalent(self, values, expression):
        expected, seen = [], set()
        for value in values:
            instant = value.astimezone(timezone.utc)
            if instant not in seen:
                expected.append(instant)
                seen.add(instant)
        actual = [value.astimezone(timezone.utc) for value in expand_sequence(expression, values[0])]
        self.assertEqual(actual, expected)

    def test_regular_fortnightly_dates_use_a_repeat_after_the_fixed_event(self):
        values = [ANCHOR + timedelta(weeks=2 * index) for index in range(5)]
        expression = simplify_sequence(values)
        self.assertEqual(expression, '+0d 4*+2w')
        self.assert_equivalent(values, expression)

    def test_hour_and_minute_spacing_uses_explicit_clocks(self):
        for delta, expected in ((timedelta(hours=2), '+0d 11:00 13:00 15:00 17:00'),
                                (timedelta(minutes=15), '+0d 9:15 9:30 9:45 10:00')):
            with self.subTest(expected=expected):
                values = [ANCHOR + index * delta for index in range(5)]
                expression = simplify_sequence(values)
                self.assertEqual(expression, expected)
                self.assert_equivalent(values, expression)

    def test_reconstructed_clocks_are_certified_across_midnight_and_dst(self):
        cases = [(ANCHOR.replace(hour=23, minute=30), '+0d 0:15 1:00 1:45 2:30'),
                 (datetime(2027, 3, 28, 1, 30, tzinfo=ZONE), '+0d 3:30 4:00'),
                 (datetime(2026, 10, 25, 1, 30, tzinfo=ZONE, fold=1), '+0d 2:30 3:30')]
        for anchor, original in cases:
            with self.subTest(original=original):
                values = expand_sequence(original, anchor)
                expression = simplify_sequence(values)
                self.assertNotRegex(expression, r'\+[0-9]+(?:h|min)')
                self.assert_equivalent(values, expression)

    def test_changing_one_same_day_clock_keeps_later_clocks_fixed(self):
        values = [ANCHOR, ANCHOR.replace(hour=10), ANCHOR.replace(hour=11), ANCHOR.replace(hour=14)]
        expression = reconstruct_sequence(values)
        self.assertEqual(expression, '+0d 10:00 11:00 14:00')
        changed = expand_sequence(expression.replace('10:00', '10:30'), ANCHOR)
        self.assertEqual([(value.hour, value.minute) for value in changed],
                         [(9, 0), (10, 30), (11, 0), (14, 0)])

    def test_unrepresentable_seconds_are_rejected_without_reintroducing_offsets(self):
        precise = ANCHOR.replace(second=17, microsecond=123)
        values = [precise + timedelta(hours=2 * index) for index in range(5)]
        with self.assertRaisesRegex(PatternError, 'sub-minute'):
            reconstruct_sequence(values)
        # Existing authored text remains readable with its full precision.
        self.assertTrue(all((value.second, value.microsecond) == (17, 123) for value in values))

    def test_first_of_month_prefers_a_repeated_short_date(self):
        values = [datetime(2027, 1, 31, 9, tzinfo=ZONE),
                  *(datetime(2027, month, 1, 9, tzinfo=ZONE) for month in range(2, 6))]
        expression = simplify_sequence(values)
        self.assertEqual(expression, '+0d 4*1.')
        self.assert_equivalent(values, expression)

    def test_month_and_year_steps_are_checked_for_calendar_clipping(self):
        for initial, original in [(datetime(2027, 1, 31, 9, tzinfo=ZONE), '+0d 4*+1m'),
                                  (datetime(2028, 2, 29, 9, tzinfo=ZONE), '+0d 4*+1y')]:
            with self.subTest(original=original):
                values = expand_sequence(original, initial)
                expression = simplify_sequence(values)
                self.assertLessEqual(len(expression), len(original))
                self.assert_equivalent(values, expression)

    def test_year_is_omitted_only_when_the_partial_date_has_the_same_result(self):
        forward = [ANCHOR, datetime(2027, 2, 3, 9, tzinfo=ZONE)]
        expression = simplify_sequence(forward)
        self.assertEqual(expression, '+0d 3.2.')
        self.assertNotIn('2027', expression)
        self.assert_equivalent(forward, expression)
        backward = [ANCHOR, datetime(2025, 2, 3, 9, tzinfo=ZONE)]
        expression = simplify_sequence(backward)
        self.assertIn('2025', expression)
        self.assert_equivalent(backward, expression)

    def test_date_only_atoms_use_original_clock_after_a_changed_clock(self):
        values = [ANCHOR, ANCHOR.replace(hour=11),
                  (ANCHOR + timedelta(days=1)).replace(hour=11), ANCHOR + timedelta(days=2)]
        expression = simplify_sequence(values)
        self.assertLessEqual(len(expression), len(serialize_sequence(values)))
        self.assert_equivalent(values, expression)
        self.assertEqual([value.hour for value in expand_sequence(expression, ANCHOR)], [9, 11, 11, 9])

    def test_longer_common_atoms_enable_a_repeated_block(self):
        original = '+0d 3*(+1d +2d)'
        values = expand_sequence(original, ANCHOR)
        expression = simplify_sequence(values)
        self.assertLessEqual(len(expression), len(original))
        self.assertIn('*(', expression)
        self.assert_equivalent(values, expression)

    def test_repeated_blocks_can_contain_compressed_atom_runs(self):
        original = '+0d 3*(4*+1d +2w)'
        values = expand_sequence(original, ANCHOR)
        expression = simplify_sequence(values)
        self.assertLessEqual(len(expression), len(original))
        self.assertIn('*(', expression)
        self.assert_equivalent(values, expression)

    def test_long_primitive_relative_block_is_discovered(self):
        rng = random.Random(37)
        body = ' '.join(f'+{rng.randint(1, 9)}d' for _ in range(37))
        original = '+0d 2*(' + body + ')'
        values = expand_sequence(original, ANCHOR)
        expression = simplify_sequence(values)
        self.assertLessEqual(len(expression), len(original))
        self.assert_equivalent(values, expression)

    def test_dense_five_hundred_date_and_clock_sequences_remain_compact(self):
        cases = [([ANCHOR + timedelta(days=index) for index in range(500)], '+0d 499*+1d'),
                 ([ANCHOR + timedelta(weeks=2 * index) for index in range(500)], '+0d 499*+2w'),
                 ([ANCHOR + timedelta(days=index // 3, hours=index % 3) for index in range(500)],
                  '+0d 10:00 166*(11:00 9:00 10:00)')]
        for values, known in cases:
            with self.subTest(known=known):
                expression = simplify_sequence(values)
                self.assertLessEqual(len(expression), len(known))
                self.assert_equivalent(values, expression)

    def test_large_sets_use_concise_dates_and_shared_runs_without_quadratic_search(self):
        values = [ANCHOR + timedelta(days=index) for index in range(900)]
        self.assertEqual(simplify_sequence(values), '+0d 899*+1d')
        irregular = [ANCHOR]
        for index in range(550):
            irregular.append(irregular[-1] + timedelta(days=(index % 7) + 1))
        expression = simplify_sequence(irregular)
        self.assertNotRegex(expression, r'\d\.\d{1,2}\.\d{4}')
        actual = expand_sequence(expression, ANCHOR, limit=1000)
        self.assertEqual(actual, irregular)

    def test_different_times_same_day_and_earlier_dates_preserve_instruction_order(self):
        values = [ANCHOR, ANCHOR.replace(hour=15), datetime(2025, 12, 31, 14, 30, tzinfo=ZONE),
                  datetime(2026, 1, 2, 9, tzinfo=ZONE)]
        expression = simplify_sequence(values)
        self.assert_equivalent(values, expression)
        self.assertEqual(expand_sequence(expression, values[0])[0].timestamp(), ANCHOR.timestamp())

    def test_dst_and_original_seconds_are_preserved_exactly(self):
        initial = datetime(2027, 3, 20, 9, 0, 17, 123, tzinfo=ZONE)
        values = expand_sequence('+0d +1w 14:00 +1d 2.4.', initial)
        expression = simplify_sequence(values)
        self.assert_equivalent(values, expression)
        self.assertEqual(expand_sequence(expression, initial)[-1].second, 17)

    def test_unrepresentable_precision_or_fold_is_never_silently_changed(self):
        for values in [[ANCHOR, ANCHOR.replace(hour=10, second=1)],
                       [datetime(2026, 10, 24, 9, tzinfo=ZONE),
                        datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=1)]]:
            with self.subTest(values=values), self.assertRaises(PatternError):
                simplify_sequence(values)
        far = datetime(9999, 1, 1, 9, microsecond=1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(PatternError, 'sub-minute'):
            simplify_sequence([far, far.replace(microsecond=2)])

    def test_parser_and_simplifier_keep_distinct_instants_near_float_precision_limit(self):
        far = datetime(9999, 1, 1, 9, microsecond=1, tzinfo=timezone.utc)
        values = expand_sequence('+0d 9:00-1.1.9999', far)
        self.assertEqual(len(values), 2)
        self.assertEqual([value.microsecond for value in values], [1, 0])
        self.assert_equivalent(values, simplify_sequence(values))

    def test_single_precise_anchor_and_duplicate_inputs(self):
        precise = ANCHOR.replace(second=12, microsecond=345)
        self.assertEqual(simplify_sequence([precise]), '+0d')
        self.assertEqual(simplify_sequence([precise, precise]), '+0d')
        values = [ANCHOR, ANCHOR + timedelta(weeks=1), ANCHOR + timedelta(weeks=1), ANCHOR + timedelta(weeks=2)]
        self.assert_equivalent(values, simplify_sequence(values))

    def test_reconstruction_delegates_but_full_serializer_is_unchanged(self):
        values = [ANCHOR + timedelta(weeks=2 * index) for index in range(5)]
        self.assertEqual(reconstruct_sequence(values), simplify_sequence(values))
        self.assertIn('2026', serialize_sequence(values))
        self.assertNotIn('2026.', serialize_sequence(values))
        self.assert_equivalent(values, serialize_sequence(values))

    def test_irregular_results_are_deterministic_and_verified(self):
        rng = random.Random(492)
        values = [ANCHOR]
        for _ in range(40):
            values.append(values[-1] + timedelta(days=rng.randint(1, 17)))
        first = simplify_sequence(values)
        self.assertEqual(simplify_sequence(values), first)
        self.assertLessEqual(len(first), len(serialize_sequence(values)))
        self.assert_equivalent(values, first)


if __name__ == '__main__':
    unittest.main()
