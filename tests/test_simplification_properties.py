"""Independent behavioral properties for calendar-selection simplification."""
from datetime import datetime, timedelta, timezone
import random
from time import perf_counter
from zoneinfo import ZoneInfo

import pytest

from lebacal import patterns
from lebacal.patterns import simplify_sequence


UTC = timezone.utc


@pytest.fixture
def simplify():
    return simplify_sequence


def instants(values):
    # UTC datetime equality distinguishes folds and retains microseconds even
    # at year 9999, where float Unix timestamps no longer have that precision.
    return [value.astimezone(UTC) for value in values]


def assert_roundtrip(simplify, values):
    expression = simplify(iter(values))
    unique = list(dict.fromkeys(instants(values)))
    expanded = patterns.expand_sequence(expression, values[0], limit=max(500, len(unique)))
    assert instants(expanded) == unique, expression
    assert expanded[0].timestamp() == values[0].timestamp()
    return expression


@pytest.mark.parametrize("anchor,known_short", [
    (datetime(2026, 1, 31, 9, tzinfo=UTC), "3*1."),
    (datetime(2026, 10, 31, 9, tzinfo=UTC), "4*1."),
    (datetime(2026, 1, 1, 9, tzinfo=UTC), "3*(1. 15.)"),
    (datetime(2026, 1, 1, 9, tzinfo=UTC), "3*(+1d +2d)"),
    (datetime(2026, 1, 1, 9, tzinfo=UTC), "2*(11:00 17:00)"),
    (datetime(2026, 1, 31, 9, tzinfo=UTC), "4*28."),
    (datetime(2026, 1, 31, 9, tzinfo=UTC), "28. 31."),
    (datetime(2024, 2, 29, 9, tzinfo=UTC), "2*+1y"),
    (datetime(2026, 11, 1, 9, tzinfo=UTC), "15:00 2."),
    (datetime(2026, 11, 1, 9, tzinfo=UTC), "15:00 +1d"),
    (datetime(2026, 11, 1, 9, tzinfo=UTC), "15:00 9:00"),
    (datetime(2026, 11, 10, 9, tzinfo=UTC), "10.11.2025"),
    (datetime(2026, 11, 10, 9, tzinfo=UTC), "3*+1"),
])
def test_compact_known_forms_remain_exact_and_not_longer(simplify, anchor, known_short):
    values = patterns.expand_sequence(known_short, anchor)
    expression = assert_roundtrip(simplify, values)
    # This is a semantic upper bound from an independently valid expression,
    # not an assertion about the optimizer's chosen tokens or implementation.
    assert len(expression) <= len(known_short), (known_short, expression)


def test_randomized_representable_sets_roundtrip_and_beat_valid_upper_bound(simplify):
    randomizer = random.Random(20261105)
    zones = [UTC, ZoneInfo("Europe/Berlin"), ZoneInfo("America/New_York"), ZoneInfo("Australia/Sydney")]
    for case in range(100):
        zone = randomizer.choice(zones)
        anchor = datetime(randomizer.randint(2024, 2028), randomizer.randint(1, 12), randomizer.randint(1, 28),
                          randomizer.randint(8, 17), randomizer.choice([0, 15, 30, 45]), tzinfo=zone)
        steps = []
        for _ in range(randomizer.randint(1, 12)):
            choice = randomizer.randrange(6)
            day, month = randomizer.randint(1, 28), randomizer.randint(1, 12)
            if choice == 0:
                steps.append(f"+{randomizer.randint(1, 3)}{randomizer.choice(['d', 'w', 'm', 'y'])}")
            elif choice == 1:
                steps.append(f"{randomizer.randint(8, 17)}:{randomizer.choice([0, 15, 30, 45]):02d}")
            elif choice == 2:
                steps.append(f"{day}.")
            elif choice == 3:
                steps.append(f"{day}.{month}.")
            elif choice == 4:
                steps.append(f"{day}.{month}.{randomizer.randint(2024, 2028)}")
            else:
                steps.append(f"{randomizer.randint(8, 17)}:30-{day}.{month}.{randomizer.randint(2024, 2028)}")
        known = " ".join(steps)
        values = patterns.expand_sequence(known, anchor)
        # Some timestamps come from external UTC ICS values; representation
        # changes must not change the selected instants or the fixed anchor.
        mixed = [values[0], *(value.astimezone(UTC) if index % 2 else value for index, value in enumerate(values[1:]))]
        expression = assert_roundtrip(simplify, mixed)
        assert len(expression) <= len(known), (case, known, expression)


def test_partial_equality_advances_but_explicit_year_can_return_to_an_earlier_date(simplify):
    anchor = datetime(2026, 1, 1, 9, tzinfo=UTC)
    values = patterns.expand_sequence("1. 1.1.2026 2.", anchor)
    expression = assert_roundtrip(simplify, values)
    assert instants(patterns.expand_sequence(expression, anchor)) == instants(values)


def test_long_repeated_body_can_use_shared_atoms_that_are_not_locally_shortest(simplify):
    randomizer = random.Random(370037)
    body = " ".join(f"+{randomizer.randint(1, 9)}d" for _ in range(37))
    known = "2*(" + body + ")"
    anchor = datetime(2026, 1, 1, 9, tzinfo=UTC)
    expression = assert_roundtrip(simplify, patterns.expand_sequence(known, anchor))
    assert len(expression) <= len(known), (known, expression)


def test_five_hundred_regular_multiple_clock_events_keep_the_short_block(simplify):
    anchor = datetime(2026, 1, 1, 9, tzinfo=UTC)
    known = "166*(10:00 11:00 9:00) 10:00"
    values = patterns.expand_sequence(known, anchor)
    assert len(values) == 500
    started = perf_counter()
    expression = assert_roundtrip(simplify, values)
    assert len(expression) <= len(known), expression
    assert perf_counter() - started < 2.0


def test_dst_and_year_edges_preserve_instants(simplify):
    berlin = ZoneInfo("Europe/Berlin")
    new_york = ZoneInfo("America/New_York")
    cases = [
        (datetime(2026, 3, 22, 9, tzinfo=berlin), "3*+1"),
        (datetime(2026, 10, 18, 9, tzinfo=berlin), "3*+1"),
        (datetime(2026, 3, 8, 1, 30, tzinfo=new_york), "3:30 +1d"),
        (datetime(2026, 12, 31, 9, tzinfo=UTC), "2*1."),
        (datetime(9999, 12, 31, 9, tzinfo=UTC), "1.1.0001"),
        (datetime(1, 1, 1, 9, tzinfo=UTC), "31.12.9999"),
    ]
    for anchor, known in cases:
        assert_roundtrip(simplify, patterns.expand_sequence(known, anchor))


def test_unrepresentable_fold_and_changed_subminute_clock_fail_without_rounding(simplify):
    berlin = ZoneInfo("Europe/Berlin")
    first_fold = datetime(2026, 10, 25, 2, 30, tzinfo=berlin, fold=0)
    second_fold = first_fold.replace(fold=1)
    with pytest.raises(patterns.PatternError):
        simplify([first_fold, second_fold])
    anchor = datetime(2026, 11, 1, 9, 0, 30, 123456, tzinfo=UTC)
    assert_roundtrip(simplify, [anchor, anchor + timedelta(days=1)])
    with pytest.raises(patterns.PatternError):
        simplify([anchor, datetime(2026, 11, 2, 11, 0, 45, tzinfo=UTC)])
    # Float Unix timestamps lose microseconds far from 1970. Those distinct
    # selections must be rejected rather than silently deduplicated.
    far_future = datetime(9999, 1, 1, 9, 0, 0, 1, tzinfo=UTC)
    with pytest.raises(patterns.PatternError):
        simplify([far_future, far_future.replace(microsecond=2)])
    exact_past = patterns.expand_sequence("9:00-1.1.9999", far_future)
    assert len(exact_past) == 2
    assert_roundtrip(simplify, exact_past)


def test_nonexistent_dst_wall_time_is_rejected(simplify):
    berlin = ZoneInfo("Europe/Berlin")
    with pytest.raises(patterns.PatternError):
        simplify([datetime(2026, 3, 28, 2, 30, tzinfo=berlin), datetime(2026, 3, 29, 2, 30, tzinfo=berlin)])


def test_five_hundred_weekly_events_simplify_within_interactive_budget(simplify):
    zone = ZoneInfo("Europe/Berlin")
    anchor = datetime(2026, 1, 4, 9, tzinfo=zone)
    values = [anchor + timedelta(weeks=index) for index in range(500)]
    started = perf_counter()
    expression = assert_roundtrip(simplify, values)
    elapsed = perf_counter() - started
    assert len(expression) <= len("499*+1")
    assert elapsed < 2.0, f"500 regular selections took {elapsed:.3f}s"


def test_five_hundred_irregular_mixed_clock_selections_stay_responsive(simplify):
    randomizer = random.Random(500071)
    zone = ZoneInfo("Europe/Berlin")
    anchor = datetime(2026, 1, 1, 9, tzinfo=zone)
    extras = set()
    while len(extras) < 499:
        extras.add((randomizer.randint(1, 1800), randomizer.randint(8, 18), randomizer.randrange(0, 60, 5)))
    values = [anchor, *(anchor.replace(hour=hour, minute=minute) + timedelta(days=days)
                        for days, hour, minute in sorted(extras))]
    started = perf_counter()
    assert_roundtrip(simplify, values)
    elapsed = perf_counter() - started
    assert elapsed < 2.0, f"500 irregular selections took {elapsed:.3f}s"
