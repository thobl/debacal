"""Cut ranges synchronize as standard exceptions and compact on a trusted pull."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from debacal.ics import export_ics, parse_ics, patch_ics
from debacal.ics_fallback import folded, parse as parse_fallback
from debacal.ics_resources import _blocks
from debacal.ics_range_policy import GENERATED, POLICY, SIGNATURE, expected_ranges
from debacal import ics_range_policy as range_codec
from debacal.models import Event
from debacal.recurrence import expand_event, recurrence_key
from debacal.schedule_authoring import expression_event
from debacal.vdirsyncer_backend import semantic


UTC = timezone.utc


def source(*, all_day=False, zone=UTC, count=4, **changes):
    start = datetime(2027, 1, 28, 0 if all_day else 9, tzinfo=zone)
    return Event('cut', 'local', 'A monthly event', start,
                 start + timedelta(days=2 if all_day else 1),
                 description='Shared notes', location='Room 7', all_day=all_day,
                 recurrence_rule='FREQ=MONTHLY' + (f';COUNT={count}' if count else ''),
                 range_policy='cut', **changes)


def visible(event):
    return expand_event(event, event.start - timedelta(days=5), event.start + timedelta(days=130))


def ranges(event):
    return [(item.start.date(), item.end.date(), item.title) if item.all_day else
            (item.start.timestamp(), item.end.timestamp(), item.title) for item in visible(event)]


def edit_blocks(raw, action):
    header, blocks = _blocks(raw)
    changed = []
    for block in blocks:
        value = action(block)
        if value is not None:
            changed.extend(value)
    return folded(['BEGIN:VCALENDAR', *header, *changed, 'END:VCALENDAR'])


@pytest.mark.parametrize('parse', [parse_ics, parse_fallback])
@pytest.mark.parametrize('all_day', [False, True])
@pytest.mark.parametrize('zone', [UTC, ZoneInfo('Europe/Berlin')])
def test_own_cut_roundtrip_keeps_policy_symbolic_and_standard_clients_see_same_ranges(parse, all_day, zone):
    original = source(all_day=all_day, zone=zone)
    raw = export_ics([original])
    assert raw.count((GENERATED + ':').encode()) == 1
    restored, = parse(raw, 'local')
    assert restored.range_policy == 'cut'
    assert not restored.recurrence_overrides
    assert GENERATED not in restored.raw_ics
    assert ranges(restored) == ranges(original)
    assert semantic(restored) == semantic(original)
    # A client ignoring all extension properties still gets standard DTENDs.
    standard = edit_blocks(raw, lambda block: [line for line in block
        if not line.startswith(tuple(name + ':' for name in (GENERATED, POLICY, SIGNATURE)))])
    other, = parse(standard, 'local')
    assert other.range_policy == '' and len(other.recurrence_overrides) == 1
    assert ranges(other) == ranges(original)


@pytest.mark.parametrize('parse', [parse_ics, parse_fallback])
@pytest.mark.parametrize('all_day', [False, True])
def test_authored_cut_exports_missing_starts_as_skipped_months_for_all_clients(parse, all_day):
    start = datetime(2027, 1, 30, 0 if all_day else 9, tzinfo=UTC)
    duration = timedelta(days=2) if all_day else timedelta(days=1, hours=1)
    original = expression_event(Event('cut-day30', 'local', 'Month end', start, start + duration,
                                      all_day=all_day),
                                'every(1m, overflow=cut, span=2, count=5)')
    raw = export_ics([original])
    assert raw.count((GENERATED + ':').encode()) == 2  # April and June only.
    restored, = parse(raw, 'local')
    standard = edit_blocks(raw, lambda block: [line for line in block
        if not line.startswith(tuple(name + ':' for name in (GENERATED, POLICY, SIGNATURE)))])
    other, = parse(standard, 'local')
    window = datetime(2027, 1, 1, tzinfo=UTC), datetime(2027, 8, 1, tzinfo=UTC)
    expected = expand_event(original, *window)
    assert [(item.start.month, item.start.day) for item in expected] == [(1, 30), (3, 30), (4, 30), (5, 30), (6, 30)]
    def normalized(items):
        return [(item.start.date(), item.end.date()) if all_day else (item.start, item.end) for item in items]
    for decoded in (restored, other):
        actual = expand_event(decoded, *window)
        assert normalized(actual) == normalized(expected)
    assert restored.range_policy == 'cut' and not restored.recurrence_overrides
    assert other.range_policy == '' and len(other.recurrence_overrides) == 2


@pytest.mark.parametrize('parse', [parse_ics, parse_fallback])
@pytest.mark.parametrize('property, replacement', [('SUMMARY:A monthly event', 'SUMMARY:Foreign title'),
                                                  ('DTEND:20270129T090000Z', 'DTEND:20270130T090000Z'),
                                                  ('RRULE:FREQ=MONTHLY;COUNT=4', 'RRULE:FREQ=MONTHLY;COUNT=5')])
def test_foreign_base_changes_preserve_every_standard_exception_and_clear_policy(parse, property, replacement):
    raw = export_ics([source()]).replace(property.encode(), replacement.encode(), 1)
    restored, = parse(raw, 'local')
    assert restored.range_policy == ''
    assert len(restored.recurrence_overrides) == 1
    february = next(item for item in visible(restored) if item.start.month == 2)
    assert february.end == datetime(2027, 3, 1, tzinfo=UTC)


@pytest.mark.parametrize('change', ['title', 'range', 'unknown', 'alarm'])
def test_foreign_generated_child_edits_become_authoritative_manual_exceptions(change):
    original = source()
    def modify(block):
        if not any(line.startswith(GENERATED + ':') for line in block):
            return block
        if change == 'title':
            return [line.replace('SUMMARY:A monthly event', 'SUMMARY:Child title') for line in block]
        if change == 'range':
            return [line.replace('DTEND:20270301T000000Z', 'DTEND:20270301T120000Z') for line in block]
        if change == 'unknown':
            return [*block[:-1], 'X-REMOTE-CHOICE:Keep this', block[-1]]
        return [*block[:-1], 'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT15M', 'DESCRIPTION:Remember', 'END:VALARM', block[-1]]
    raw = edit_blocks(export_ics([original]), modify)
    restored, = parse_ics(raw, 'local')
    assert restored.range_policy == 'cut'
    assert len(restored.recurrence_overrides) == 1
    again = export_ics([restored])
    assert GENERATED.encode() not in again
    if change == 'title':
        assert b'SUMMARY:Child title' in again
    elif change == 'range':
        assert b'DTEND:20270301T120000Z' in again
    elif change == 'unknown':
        assert b'X-REMOTE-CHOICE:Keep this' in again
    else:
        assert b'BEGIN:VALARM' in again


def test_removing_a_generated_exception_remotely_cannot_silently_restore_cut():
    original = source(count=28)
    raw = export_ics([original])
    removed = False
    def drop_first(block):
        nonlocal removed
        if not removed and any(line.startswith(GENERATED + ':') for line in block):
            removed = True
            return None
        return block
    modified = edit_blocks(raw, drop_first)
    restored, = parse_ics(modified, 'local')
    assert restored.range_policy == ''
    assert len(restored.recurrence_overrides) == 1  # The other February stays canonical.
    first = next(item for item in visible(restored) if item.start.month == 2)
    assert first.end.hour == 9


def test_explicit_overrides_exclusions_and_unknown_master_fields_survive_patches():
    original = source(count=16)
    february = datetime(2027, 2, 28, 9, tzinfo=UTC)
    key = recurrence_key(february)
    custom = replace(original, start=february, end=february + timedelta(hours=2),
                     title='Manual choice', recurrence_rule='', range_policy='')
    original = replace(original, recurrence_overrides={key: custom.to_dict()},
                       excluded_dates=(recurrence_key(datetime(2028, 2, 28, 9, tzinfo=UTC)),))
    raw = export_ics([original]).replace(b'SUMMARY:A monthly event', b'X-REMOTE-MASTER:Preserve\r\nSUMMARY:A monthly event', 1)
    restored, = parse_ics(raw, 'local')
    assert restored.range_policy == 'cut'
    assert set(restored.recurrence_overrides) == {key}
    assert restored.recurrence_overrides[key]['title'] == 'Manual choice'
    changed = replace(restored, description='New shared notes')
    patched = patch_ics(raw, changed)
    assert b'X-REMOTE-MASTER:Preserve' in patched
    assert b'SUMMARY:Manual choice' in patched
    assert GENERATED.encode() not in patched
    reread, = parse_ics(patched, 'local')
    assert semantic(reread) == semantic(changed)


def test_disabling_policy_removes_derived_ranges_but_preserves_manual_ranges():
    original = source(count=16)
    raw = export_ics([original])
    restored, = parse_ics(raw, 'local')
    plain = replace(restored, range_policy='')
    exported = patch_ics(raw, plain)
    assert GENERATED.encode() not in exported and POLICY.encode() not in exported
    reread, = parse_ics(exported, 'local')
    assert not reread.recurrence_overrides
    assert ranges(reread) == ranges(plain)


def test_unbounded_cut_materializes_future_years_but_import_keeps_a_small_master():
    original = source(count=None)
    raw = export_ics([original])
    assert raw.count((GENERATED + ':').encode()) > 6000
    assert b'RECURRENCE-ID:99990228T090000Z' in raw
    restored, = parse_ics(raw, 'local')
    assert restored.range_policy == 'cut' and not restored.recurrence_overrides
    assert len(restored.raw_ics) < 2000
    assert semantic(restored) == semantic(original)
    # The cached schedule projection is reused on a content-identical export.
    assert expected_ranges(restored) is expected_ranges(original)


def test_more_than_ten_thousand_derived_ranges_do_not_hit_the_regular_component_budget():
    start = datetime(2027, 1, 29, tzinfo=UTC)
    original = Event('many-cuts', 'local', 'Three occupied days', start,
                     start + timedelta(days=3), all_day=True,
                     recurrence_rule='FREQ=MONTHLY', range_policy='cut')
    raw = export_ics([original])
    assert raw.count((GENERATED + ':').encode()) > 30000
    restored, = parse_ics(raw, 'local')
    assert restored.range_policy == 'cut'
    assert not restored.recurrence_overrides
    assert len(restored.raw_ics) < 2000
    assert semantic(restored) == semantic(original)


def test_cut_export_budget_fails_before_returning_an_unimportable_resource(monkeypatch):
    monkeypatch.setattr(range_codec, 'MAX_GENERATED_RANGES', 1)
    original = replace(source(count=28), id='budget-cut', ical_uid='budget-cut')
    with pytest.raises(ValueError, match='Shorten the recurrence or choose Skip or Shift'):
        export_ics([original])
    # Bounding the same recurrence produces a complete standard resource again.
    bounded = replace(original, recurrence_rule='FREQ=MONTHLY;COUNT=4')
    raw = export_ics([bounded])
    assert raw.count((GENERATED + ':').encode()) == 1
    restored, = parse_ics(raw, 'local')
    assert restored.range_policy == 'cut' and not restored.recurrence_overrides
