"""Small contracts for the opt-in local responsiveness benchmark."""
from scripts.benchmark_sync_responsiveness import fixed_ics, statistics_for


def test_fixed_transport_stamp_preserves_other_calendar_content():
    data = (b'BEGIN:VEVENT\r\nDTSTAMP:20261117T105544Z\r\n'
            b'SUMMARY:Synthetic 20261117T105544Z\r\nEND:VEVENT\r\n')
    assert fixed_ics(data) == data.replace(b'DTSTAMP:20261117T105544Z', b'DTSTAMP:20261104T090000Z')
    assert fixed_ics(fixed_ics(data)) == fixed_ics(data)


def test_timings_are_grouped_by_actual_operation_and_preserve_worst_sample():
    base = {'workload': 'sync_changed', 'view': 'year', 'category': 'Calendar', 'action': 'cancel'}
    records = [{**base, 'open_ms': value, 'sample': index, 'sync_active_before': True}
               for index, value in enumerate((10, 20, 300))]
    records.append({**base, 'action': 'save', 'open_ms': 7})
    values = statistics_for(records)
    assert values['sync_changed/year/Calendar/cancel'] == {
        'open_ms': {'samples': 3, 'mean_ms': 110, 'max_ms': 300}}
    assert values['sync_changed/year/Calendar/save']['open_ms']['samples'] == 1
    assert statistics_for([]) == {}
