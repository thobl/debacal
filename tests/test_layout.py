from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from debacal.layout import layout_events


@dataclass
class Interval:
    id: str
    start: datetime
    end: datetime


def event(key, start, end):
    midnight = datetime(2026, 11, 4, tzinfo=timezone.utc)
    return Interval(key, midnight + timedelta(hours=start), midnight + timedelta(hours=end))


class LayoutTests(unittest.TestCase):
    def test_touching_events_get_full_width(self):
        self.assertEqual([(p.column, p.columns) for p in layout_events([event("a", 9, 10), event("b", 10, 11)])],
                         [(0, 1), (0, 1)])

    def test_transitive_overlap_shares_column_count_and_reuses_columns(self):
        placements = layout_events([event("a", 9, 11), event("b", 10, 12), event("c", 11, 13)])
        self.assertEqual([(p.event_id, p.column, p.columns) for p in placements],
                         [("a", 0, 2), ("b", 1, 2), ("c", 0, 2)])

    def test_independent_components_get_different_widths(self):
        placements = layout_events([event("a", 9, 11), event("b", 9, 10), event("c", 12, 13)])
        self.assertEqual([p.columns for p in placements], [2, 2, 1])

    def test_three_simultaneous_events(self):
        placements = layout_events([event("a", 9, 11), event("b", 9, 12), event("c", 9, 10)])
        self.assertEqual({p.column for p in placements}, {0, 1, 2})
        self.assertTrue(all(p.columns == 3 for p in placements))

    def test_output_is_input_order_independent(self):
        events = [event("a", 9, 11), event("b", 9, 12), event("c", 9, 10)]
        self.assertEqual(layout_events(events), layout_events(reversed(events)))

    def test_invalid_intervals_and_duplicate_ids(self):
        for events in [[event("a", 10, 9)], [event("a", 9, 10), event("a", 11, 12)]]:
            with self.assertRaises(ValueError):
                layout_events(events)

    def test_real_time_order_across_repeated_dst_hour(self):
        zone = ZoneInfo("Europe/Berlin")
        crossing = Interval("crossing", datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0),
                            datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1))
        overlapping = Interval("overlapping", datetime(2026, 10, 25, 2, 0, tzinfo=zone, fold=1),
                               datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1))
        placements = layout_events([overlapping, crossing])
        self.assertEqual([p.event_id for p in placements], ["crossing", "overlapping"])
        self.assertTrue(all(p.columns == 2 for p in placements))

    def test_visual_ranges_partition_disjoint_instants_that_share_wall_coordinates(self):
        zone = ZoneInfo('Europe/Berlin')
        first = Interval('first', datetime(2026, 10, 25, 2, 15, tzinfo=zone),
                         datetime(2026, 10, 25, 2, 45, tzinfo=zone))
        second = Interval('second', first.start.replace(fold=1), first.end.replace(fold=1))
        self.assertTrue(all(value.columns == 1 for value in layout_events([first, second])))
        visual = layout_events([second, first], visual_ranges={'first': (20, 50), 'second': (20, 50)})
        self.assertEqual([(value.event_id, value.column, value.columns) for value in visual],
                         [('first', 0, 2), ('second', 1, 2)])
        with self.assertRaisesRegex(ValueError, 'positive height'):
            layout_events([first], visual_ranges={'first': (20, 20)})


if __name__ == "__main__":
    unittest.main()
