"""Selection painting borrows prepared data; edits still review originals."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from debacal.models import Event
from debacal.selection import affected_visible, capture_selection
from debacal.store import HistoryConflict, Store


START = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)


@pytest.fixture
def family():
    with Store(':memory:') as store:
        overrides = {}
        for index in range(80):
            nominal = START + timedelta(days=index)
            shifted = nominal + timedelta(hours=2)
            overrides[nominal.isoformat()] = {
                'id': f'exception-{index}', 'calendar_id': 'local', 'title': 'Moved appointment',
                'start': shifted.isoformat(), 'end': (shifted + timedelta(hours=1)).isoformat(),
                'description': 'Synthetic retained notes ' * 40,
            }
        source = Event('family', 'local', 'Appointments', START, START + timedelta(hours=1),
                       recurrence_rule='FREQ=DAILY;COUNT=600', recurrence_overrides=overrides,
                       raw_ics='Synthetic retained transport ' * 100)
        store.apply_remote([source])
        values = store.query_occurrences(START, START + timedelta(days=100))
        yield store, source, values


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_highlighting_never_decodes_a_selected_exception_heavy_resource(family, scope):
    store, _source, values = family
    reference = values[30]
    expected = ({reference.id} if scope == 'single' else {event.id for event in values}
                if scope == 'group' else {event.id for event in values[30:]})
    with patch.object(store, 'get_event', side_effect=AssertionError('Painting must not read canonical payloads')), \
            patch.object(Event, 'from_dict', side_effect=AssertionError('Painting must not decode event JSON')):
        assert affected_visible(store, [reference], scope, values) == expected


def test_related_references_borrow_their_source_once_per_highlight_pass(family):
    store, _source, values = family
    with patch.object(store, 'query_source_events', wraps=store.query_source_events) as reads:
        selected = affected_visible(store, [values[60], values[20], values[40]], 'future', values)
    assert selected == {event.id for event in values[20:]}
    assert reads.call_count == 1


def test_legacy_group_membership_is_still_calendar_scoped_and_ordered():
    with Store(':memory:') as store:
        values = [Event(str(index), 'local', 'Grouped', START + timedelta(days=index),
                        START + timedelta(days=index, hours=1), group_id='linked') for index in range(4)]
        store.apply_remote(values)
        visible = store.query_occurrences(START, START + timedelta(days=5))
        with patch.object(store, 'get_event', side_effect=AssertionError('Do not reread group roots')), \
                patch.object(store, 'list_group_events', wraps=store.list_group_events) as groups:
            selected = affected_visible(store, [values[2], values[1]], 'future', visible)
        assert selected == {value.id for value in values[1:]}
        assert groups.call_count == 1


def test_projected_highlights_cannot_authorize_a_stale_copy(family):
    store, source, values = family
    previous = values[90]
    store.apply_remote([replace(source, title='Changed by another client')])
    assert previous.id in affected_visible(store, [previous], 'group', values)
    with patch.object(store, 'get_event', wraps=store.get_event) as reads:
        with pytest.raises(HistoryConflict, match='changed'):
            capture_selection(store, [previous], 'group')
        assert reads.call_count


def test_empty_selection_does_not_touch_storage(family):
    store, _source, values = family
    with patch.object(store, 'query_source_events', side_effect=AssertionError('No selection needs no sources')):
        assert affected_visible(store, [], 'future', values) == set()
