"""Deliberately removing authoring metadata remains a synchronizable edit."""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lebacal.ics import parse_ics
from lebacal.models import Calendar, Event
from lebacal.recurrence import expand_series, recurrence_key
from lebacal.store import Store
from lebacal.vdirsyncer_backend import VdirsyncerBackend
from test_vdirsyncer import FakeCLI


ZONE = ZoneInfo("Europe/Berlin")
START = datetime(2027, 3, 20, 9, tzinfo=ZONE)


def timetable(event):
    return [(value.start.timestamp(), value.end.timestamp())
            for value in expand_series(event, START - timedelta(days=30), START + timedelta(days=100))]


@pytest.mark.parametrize("syntax", [1, 2], ids=["legacy", "sequential"])
@pytest.mark.parametrize("kind", ["RRULE", "RDATE"])
def test_explicit_authoring_removal_reaches_other_instance_without_resurrection(tmp_path, syntax, kind):
    cli = FakeCLI()
    with Store(tmp_path / "a.sqlite") as a, Store(tmp_path / "b.sqlite") as b:
        cal_a = Calendar("machine-a", "Shared calendar", "#123456", kind="caldav",
                         url="https://calendar.test/shared/", username="test")
        cal_b = replace(cal_a, id="machine-b")
        a.save_calendar(cal_a)
        b.save_calendar(cal_b)
        bridge_a = VdirsyncerBackend(a.path, runner=cli)
        bridge_b = VdirsyncerBackend(b.path, runner=cli)
        expression = (("2*+1" if kind == "RRULE" else "+3d +5d") if syntax == 2 else
                      ("1, 1" if kind == "RRULE" else "20.3.2027, 23.3.2027, 28.3.2027"))
        source = Event("authored", cal_a.id, "Synthetic schedule", START, START + timedelta(hours=1),
                       recurrence_rule="FREQ=WEEKLY;COUNT=3" if kind == "RRULE" else "",
                       recurrence_dates=() if kind == "RRULE" else tuple(recurrence_key(START + timedelta(days=days)) for days in (3, 8)),
                       schedule_expression=expression,
                       expression_anchor=START.date().isoformat(), schedule_syntax=syntax)
        a.apply("Create", [source])
        assert bridge_a.sync(a, cal_a, "synthetic-session").sent == 1
        assert bridge_b.sync(b, cal_b, "synthetic-session").received == 1
        imported = b.list_events(calendar_ids=[cal_b.id])[0]
        assert imported.id != source.id and imported.schedule_expression == source.schedule_expression
        original_dates = timetable(imported)

        # Keep the interoperable dates while deliberately deleting all three
        # private properties. This is a meaningful edit even with no time change.
        cleared = replace(imported, schedule_expression="", expression_anchor="", schedule_syntax=1)
        b.apply("Remove custom authoring annotation", [cleared])
        assert len(b.pending_changes(cal_b.id)) == 1
        result = bridge_b.sync(b, cal_b, "synthetic-session")
        assert result.sent == 1 and not result.conflicts and not result.issues
        remote = cli.remote[source.ical_uid]
        assert "X-LEBACAL-PATTERN" not in remote
        assert timetable(parse_ics(remote, "reader")[0]) == original_dates

        result = bridge_a.sync(a, cal_a, "synthetic-session")
        assert not result.conflicts and not result.issues
        returned = a.get_event(source.id)
        assert not returned.schedule_expression and not returned.expression_anchor
        assert timetable(returned) == original_dates
        for store, calendar, bridge in ((a, cal_a, bridge_a), (b, cal_b, bridge_b)):
            again = bridge.sync(store, calendar, "synthetic-session")
            assert again.sent == 0 and not again.conflicts and not again.issues
            assert not store.pending_changes(calendar.id)
            current = store.list_events(calendar_ids=[calendar.id])[0]
            assert not current.schedule_expression and not current.expression_anchor
            assert timetable(current) == original_dates
