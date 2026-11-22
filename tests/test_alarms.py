from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from alarm_support import BASE, alarm_event
from debacal.alarms import Alarm, alarm_trigger
from debacal.models import Event
from debacal.prepared_storage import StateReader, StateWriter


@pytest.mark.parametrize('trigger, related, repeat_index, expected', [
    ('-PT5M', 'START', 0, '08:55'), ('+PT5M', 'START', 0, '09:05'),
    ('-PT5M', 'END', 0, '09:55'), ('PT5M', 'END', 0, '10:05'),
    ('20261121T060000Z', 'START', 0, '06:00'), ('20261121T060000Z', 'START', 2, '06:14'),
    ('-PT5M', 'START', 2, '09:09'),
])
def test_trigger_uses_occurrence_bounds_and_repeat(trigger, related, repeat_index, expected):
    alarm = Alarm('k', 'DISPLAY', trigger, related, repeat=2, repeat_interval='PT7M')
    value = alarm_trigger(alarm, BASE, BASE + timedelta(hours=1), repeat_index=repeat_index)
    assert value == datetime.fromisoformat('2026-11-21T' + expected + '+00:00')
    assert value.tzinfo is timezone.utc


@pytest.mark.parametrize('start, fold, trigger, expected', [
    ('2026-03-29T09:00', 0, '-P1D', '2026-03-28T08:00'),
    ('2026-03-29T09:00', 0, '-PT24H', '2026-03-28T07:00'),
    ('2026-03-28T02:30', 0, 'P1D', '2026-03-29T01:30'),
    ('2026-10-25T02:30', 1, '-PT15M', '2026-10-25T01:15'),
    ('2026-10-25T02:30', 0, '-PT15M', '2026-10-25T00:15'),
])
def test_zoned_trigger_keeps_nominal_days_and_distinguishes_folds(start, fold, trigger, expected):
    bound = datetime.fromisoformat(start).replace(tzinfo=ZoneInfo('Europe/Berlin'), fold=fold)
    assert alarm_trigger(Alarm('k', 'AUDIO', trigger), bound, bound) == datetime.fromisoformat(expected + '+00:00')


@pytest.mark.parametrize('all_day,floating,hour,expected', [(True, False, 0, 22), (False, True, 9, 7)])
def test_all_day_and_floating_evaluate_in_occurrence_zone(all_day, floating, hour, expected):
    start = datetime(2026, 11, 21, hour, tzinfo=ZoneInfo('Europe/Berlin'))
    event = alarm_event(start=start, end=start + timedelta(days=1), all_day=all_day, floating_time=floating)
    instant = alarm_trigger(Alarm('k', 'DISPLAY', '-PT1H'), event.start, event.end)
    assert instant == datetime(2026, 11, 20 if all_day else 21, expected, tzinfo=timezone.utc)


def test_repeat_cannot_extend_past_finite_count():
    with pytest.raises(ValueError):
        alarm_trigger(Alarm('k', 'DISPLAY', 'PT0S', repeat=1, repeat_interval='PT5M'), BASE, BASE, repeat_index=2)


def test_prepared_legacy_explicit_empty_and_delta_inheritance():
    writer, reader = StateWriter(), StateReader()
    event = alarm_event()
    state = json.loads(json.dumps(writer.event(event)))
    assert state['f']['alarms'] == []
    assert reader.event(state).alarms == ()
    del state['f']['alarms']
    assert reader.event(state).alarms is None

    alarm = Alarm('uid', 'DISPLAY', '-PT1M', acknowledged=BASE)
    parent = replace(event, alarms=(alarm,))
    assert reader.event(json.loads(json.dumps(writer.event(parent)))).alarms == (alarm,)
    delta = writer.event(replace(parent, title='Moved'), base=parent)
    assert 'alarms' not in delta['f']
    assert reader.event(delta, base=parent).alarms == (alarm,)
    assert reader.event(writer.event(event, base=parent), base=parent).alarms == ()
    old = event.to_dict()
    del old['alarms']
    old['raw_ics'] = 'legacy retained resource'
    assert Event.from_dict(old).alarms is None
