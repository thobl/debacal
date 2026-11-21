"""Alarm ownership across recurrence edits and standard detached components."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from alarm_support import BASE, alarm_event
from debacal.alarm_ics import read_alarms, write_alarms
from debacal.alarms import Alarm, alarm_trigger
from debacal.ics import export_ics, parse_ics
from debacal.ics_fallback import folded, unfolded
from debacal.ics_resources import _blocks, parse_report
from debacal.models import Event
from debacal.recurrence import occurrence, recurrence_key
from debacal.selection_export import export_selection
from debacal.series_operations import partition_series
from debacal.store import Store
from test_alarm_ics import exact_date_family
from test_range_policy_transport import source as monthly_range

REL = Alarm('relative', 'DISPLAY', '-PT15M', uid='relative', description='Start')
ABS = Alarm('absolute', 'DISPLAY', '20261122T100000Z', uid='absolute', description='Once')
OWN = Alarm('child', 'AUDIO', '20261230T110000Z', uid='child')
NEW = Alarm('new', 'DISPLAY', '-PT1H', uid='new', description='Earlier')
NEW_ABS = replace(ABS, key='other', uid='other', trigger='20261201T120000Z')
UTC = timezone.utc


def triggers(alarms):
    return [a.trigger for a in alarms]


def family(**changes):
    master = alarm_event(calendar_id='local', alarms=(REL, ABS), recurrence_rule='FREQ=DAILY;COUNT=4', **changes)
    child = replace(occurrence(master, BASE + timedelta(days=2)), title='Exception', alarms=(OWN,))
    return replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
@pytest.mark.parametrize('operation', ['title', 'alarms', 'remove', 'move', 'resize', 'delete', 'copy'])
def test_alarm_scope_operation_matrix(scope, operation):
    master = family()
    with Store(':memory:') as store:
        store.apply_remote([master])
        first, pivot, child, last = store.query_occurrences(BASE, BASE + timedelta(days=5))
        if operation == 'copy':
            exported, = parse_ics(export_selection(store, [pivot], scope), 'copy')
            expected = [REL.trigger] if scope == 'single' else [REL.trigger, ABS.trigger]
            assert triggers(exported.alarms) == expected
            assert store.get_event(master.id) == master
            assert not store.history()
            return
        if operation == 'delete':
            op = store.delete_selection([pivot], scope)
        elif operation in ('move', 'resize'):
            shift = timedelta(hours=2)
            op = store.move_selection([pivot], scope, pivot, pivot.start + (shift if operation == 'move' else timedelta()),
                                      pivot.end + shift)
        else:
            edit = (NEW, NEW_ABS) if operation == 'alarms' else () if operation == 'remove' else None
            form = master if scope == 'group' else partition_series(master, pivot)[1] if scope == 'future' else pivot
            op = store.edit_selection([pivot], scope, pivot, replace(form, start=pivot.start, end=pivot.end,
                                      title='Changed'), alarm_edit=edit)
        resources = store.list_events()
        if operation == 'delete':
            assert len(resources) == (0 if scope == 'group' else 1)
            if resources:
                assert resources[0].alarms == (REL, ABS)
        else:
            assert len(resources) == (2 if scope == 'future' else 1)
            past = store.get_event(master.id)
            target = next((r for r in resources if r.id != master.id), past)
            if scope in ('single', 'future'):
                assert past.alarms == (REL, ABS)
            expected_master = ((NEW, NEW_ABS) if operation == 'alarms' else () if operation == 'remove' else
                               (REL,) if scope == 'future' else (REL, ABS))
            if scope != 'single':
                assert target.alarms == expected_master
            stored_children = [Event.from_dict(p) for p in target.recurrence_overrides.values()]
            other = next(e for e in stored_children if e.recurrence_id == child.recurrence_id
                         or e.start.date() == child.start.date())
            if operation in ('alarms', 'remove') and scope != 'single':
                assert triggers(other.alarms) == ([NEW.trigger] if operation == 'alarms' else [])
                if other.alarms:
                    assert other.alarms[0].uid != NEW.uid
            else:
                assert other.alarms == (OWN,)
            if scope == 'single':
                saved = Event.from_dict(past.recurrence_overrides[pivot.recurrence_id])
                assert triggers(saved.alarms) == ([NEW.trigger, NEW_ABS.trigger] if operation == 'alarms' else
                                                  [] if operation == 'remove' else [REL.trigger])
                if operation in ('title', 'move', 'resize'):
                    assert saved.alarms[0].uid != REL.uid
        after = [r.to_dict() for r in resources]
        assert store.undo(op) == op and store.get_event(master.id) == master
        assert store.redo(op) == op and [r.to_dict() for r in store.list_events()] == after


def test_all_and_following_direct_edits_and_sole_survivor():
    from debacal.alarm_series import split_alarms
    for scope in ('all', 'future'):
        master = family()
        with Store(':memory:') as store:
            store.apply_remote([master])
            first, second, *_ = store.query_occurrences(BASE, BASE + timedelta(days=5))
            assert triggers(first.alarms) == [REL.trigger]
            assert partition_series(master, first)[1].alarms == (REL, ABS)
            assert split_alarms(master, master, None) == (master, None)
            if scope == 'all':
                store.edit_series(first, master, alarm_edit=(NEW_ABS,))
            else:
                store.edit_future(first, master, alarm_edit=(NEW_ABS,))
            only, = store.list_events()
            assert only.ical_uid == master.ical_uid and only.alarms == (NEW_ABS,)
            assert all(Event.from_dict(p).alarms == () for p in only.recurrence_overrides.values())


@pytest.mark.parametrize('detached', [False, True])
def test_confirm_alternative_retains_sole_component_absolute(detached):
    master = family(candidate=True)
    with Store(':memory:') as store:
        store.apply_remote([master])
        selected = store.query_occurrences(BASE, BASE + timedelta(days=5))[2 if detached else 1]
        op = store.confirm_candidate(selected)
        result = store.get_event(master.id)
        assert sorted(triggers(result.alarms)) == sorted([OWN.trigger if detached else REL.trigger, ABS.trigger])
        assert store.undo(op) == op and store.get_event(master.id) == master
        assert store.redo(op) == op and store.get_event(master.id) == result


@pytest.mark.parametrize('full', [False, True])
def test_phone_removal_of_generated_cut_alarm_is_authoritative(full):
    master = replace(monthly_range('cut'), alarms=exact_date_family().alarms)
    header, blocks = _blocks(export_ics([master]))
    child = next(b for b in blocks if any(line.startswith('RECURRENCE-ID') for line in b))
    relative = read_alarms('\n'.join(child), component_key='child')
    assert triggers(relative) == ['-P1D', '-PT5M']
    assert not {a.uid for a in relative} & {a.uid for a in master.alarms}
    assert f'RELATED-TO;RELTYPE=SNOOZE:{relative[0].uid}' in unfolded(relative[1].raw_ics)

    def decode(new):
        data = folded(['BEGIN:VCALENDAR', *header, *(line for b in blocks for line in (new if b is child else b)), 'END:VCALENDAR'])
        result = parse_report(data, 'local', use_full=full)
        assert not result.issues
        return result.events[0]

    intact = decode(child)
    assert intact.range_policy == 'cut' and not intact.recurrence_overrides
    for alarms in ((), (NEW,)):
        changed = decode(unfolded(write_alarms('\n'.join(child), alarms)))
        manual, = changed.recurrence_overrides.values()
        assert triggers(Event.from_dict(manual).alarms) == triggers(alarms)
        reparsed, = parse_ics(export_ics([changed]), 'local')
        manual, = reparsed.recurrence_overrides.values()
        assert triggers(Event.from_dict(manual).alarms) == triggers(alarms)


def test_period_is_generated_and_foreign_detached_alarms_are_authoritative():
    raw = export_ics([alarm_event(alarms=(REL, ABS))]).decode().replace('END:VEVENT',
        'RDATE;VALUE=PERIOD:20261123T090000Z/PT3H\r\nEND:VEVENT').replace('END:VCALENDAR',
        'BEGIN:VEVENT\r\nUID:e\r\nRECURRENCE-ID:20261124T090000Z\r\nDTSTART:20261124T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR')
    master, = parse_ics(raw, 'local')
    period, detached = [Event.from_dict(p) for p in master.recurrence_overrides.values()]
    assert period.end - period.start == timedelta(hours=3)
    assert triggers(period.alarms) == [REL.trigger] and period.alarms[0].uid != REL.uid
    assert detached.alarms == ()


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('schedule', ['rule', 'date'])
def test_named_recurring_point_end_reminder_preserves_nominal_days(full, schedule):
    from test_ics_duration_semantics import strip_metadata
    from debacal.recurrence import expand_event
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 23, 9, tzinfo=zone)
    fields = {'recurrence_rule': 'FREQ=DAILY;COUNT=2'} if schedule == 'rule' else {'recurrence_dates': ('2026-11-02T12:00:00+00:00',)}
    master = alarm_event(start=start, end=start, timezone_id=zone.key, alarms=(replace(REL, related='END', trigger='-P2W'),), **fields)
    raw = export_ics([master])
    for data, related in ((strip_metadata(raw.decode()), 'START'), (raw, 'END')):
        imported, = parse_report(data, 'local', use_full=full).events
        assert (imported.alarms[0].related, imported.alarms[0].trigger) == (related, '-P2W')
        actual = expand_event(imported, start-timedelta(days=1), start+timedelta(days=12))
        assert len(actual) == 2
        for item in actual:
            assert alarm_trigger(item.alarms[0], item.start, item.end) == (item.start.astimezone(zone) - timedelta(weeks=2)).astimezone(UTC)


@pytest.mark.parametrize('full', [False, True])
def test_literal_utc_override_remains_utc_for_nominal_start_alarm(full):
    raw = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:literal
DTSTART;TZID=Europe/Berlin:20261024T090000
DURATION:PT1H
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
BEGIN:VEVENT
UID:literal
RECURRENCE-ID:20261025T080000Z
DTSTART:20261025T090000Z
DURATION:PT1H
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Before
TRIGGER:-P1D
END:VALARM
END:VEVENT
END:VCALENDAR"""
    parsed, = parse_report(raw, 'local', use_full=full).events
    key, = parsed.recurrence_overrides
    with Store(':memory:') as store:
        store.apply_remote([parsed])
        for master in (parsed, store.get_event(parsed.id), parse_ics(export_ics([parsed]), 'local')[0]):
            child = occurrence(master, BASE, key)
            for item in (child, store.get_occurrence(child.id)):
                assert alarm_trigger(item.alarms[0], item.start, item.end) == datetime(2026, 10, 24, 9, tzinfo=UTC)


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_true_clipboard_copy_gets_new_alarm_identities_and_relationships(scope):
    from debacal.selection import capture_selection
    master = replace(family(), alarms=exact_date_family().alarms)
    with Store(':memory:') as store:
        store.apply_remote([master])
        ref = occurrence(master, BASE + timedelta(days=1))
        snapshot = capture_selection(store, [ref], scope)
        first, = snapshot.instantiate('group', ref.start, 'local')
        second, = snapshot.instantiate('group', ref.start, 'local')
        seen = {a.uid for a in master.alarms} | {OWN.uid}
        for copy in (first, second):
            alarms = copy.alarms
            assert triggers(alarms) == triggers(master.alarms[:2] if scope == 'single' else master.alarms)
            assert f'RELATED-TO;RELTYPE=SNOOZE:{alarms[0].uid}' in unfolded(alarms[1].raw_ics)
            owned = [*alarms, *(a for p in copy.recurrence_overrides.values() for a in Event.from_dict(p).alarms)]
            ids = {a.uid for a in owned}
            assert len(ids) == len(owned) and not seen.intersection(ids)
            seen.update(ids)
        assert store.get_event(master.id) == master and not store.history()


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_partial_exports_have_stable_distinct_alarm_uids(scope):
    master = family()
    with Store(':memory:') as store:
        store.apply_remote([master])
        ref = occurrence(master, BASE + timedelta(days=1))
        copies = [parse_ics(export_selection(store, [ref], scope), 'export')[0] for _ in range(2)]
        ids = [tuple(a.uid for a in copy.alarms) for copy in copies]
        assert ids[0] == ids[1]
        if scope == 'group':
            assert ids[0] == (REL.uid, ABS.uid)
        else:
            assert not set(ids[0]).intersection((REL.uid, ABS.uid))


@pytest.mark.parametrize('count', ['', ';COUNT=3'])
def test_independent_repeated_hour_rule_end_is_guarded_per_resource(count):
    start = datetime(2026, 10, 25, 0, 10, tzinfo=UTC).astimezone(ZoneInfo('America/New_York'))
    end = datetime(2026, 10, 25, 2, 45, tzinfo=ZoneInfo('Europe/Berlin'), fold=1)
    master = alarm_event(start=start, end=end, alarms=(replace(REL, related='END', trigger='-P1D'),),
                         recurrence_rule='FREQ=DAILY'+count)
    with pytest.raises(ValueError, match='event .e.*elapsed-hour reminder'):
        export_ics([master])
    assert master.end.fold == 1 and master.alarms[0].trigger == '-P1D'


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_move_preserves_independently_zoned_end_and_detached_start(scope):
    zone = ZoneInfo('Europe/Berlin')
    end_zone = ZoneInfo('America/Phoenix')
    start = datetime(2026, 10, 22, 9, tzinfo=zone)
    master = alarm_event(calendar_id='local', start=start, end=(start+timedelta(hours=2)).astimezone(end_zone),
                         recurrence_rule='FREQ=DAILY;COUNT=4', alarms=(replace(REL, related='END', trigger='-P1D'),))
    child = occurrence(master, start + timedelta(days=2))
    child = replace(child, timezone_id='', start=child.start.astimezone(UTC))
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    with Store(':memory:') as store:
        store.apply_remote([master])
        ref = child if scope == 'single' else occurrence(master, start + timedelta(days=1))
        store.move_selection([ref], scope, ref, ref.start+timedelta(hours=1), ref.end+timedelta(hours=1))
        for result in store.list_events():
            assert result.end_timezone_id == end_zone.key
            for payload in result.recurrence_overrides.values():
                moved = Event.from_dict(payload)
                assert moved.end_timezone_id == end_zone.key
                assert moved.timezone_id == '' and moved.start.utcoffset() == timedelta()
                assert alarm_trigger(moved.alarms[0], moved.start, moved.end) == (moved.end-timedelta(days=1)).astimezone(UTC)


def test_generated_late_fold_child_has_stable_own_relative_alarm_uids():
    from debacal.recurrence import canonicalize_authored_primary
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    master = alarm_event(start=start, end=start.replace(hour=3), alarms=exact_date_family().alarms,
                         recurrence_rule='FREQ=DAILY;COUNT=4')
    canonical = canonicalize_authored_primary(master)
    restored, = parse_ics(export_ics([canonical]), 'local')
    children = [Event.from_dict(next(iter(m.recurrence_overrides.values()))) for m in (canonical, restored)]
    for child in children:
        assert child.start.timestamp() == start.timestamp()
        assert triggers(child.alarms) == triggers(master.alarms[:2])
        assert child.alarms[0].uid != master.alarms[0].uid
    assert [a.uid for a in children[0].alarms] == [a.uid for a in children[1].alarms]


def test_legacy_cut_no_alarm_child_is_verified_only_with_unchanged_fields():
    from debacal.ics_range_policy import signature, GENERATED
    header, blocks = _blocks(export_ics([replace(monthly_range('cut'), alarms=(REL, ABS))]))
    master, child = [b for b in blocks if b[0] == 'BEGIN:VEVENT']
    legacy = unfolded(write_alarms('\n'.join(child), ()))
    legacy = [GENERATED + ':' + signature(monthly_range('cut')) if line.startswith(GENERATED + ':') else line for line in legacy]
    for extra in ([], ['X-PHONE:changed']):
        parts = [*legacy[:-1], *extra, legacy[-1]]
        data = folded(['BEGIN:VCALENDAR', *header, *master, *parts, 'END:VCALENDAR'])
        decoded, = parse_ics(data, 'local')
        assert decoded.range_policy == 'cut'
        assert bool(decoded.recurrence_overrides) == bool(extra)


def test_partial_import_remaps_owned_alarms_and_links_without_touching_source():
    from debacal.ics_resources import ParseReport
    from debacal.import_selection import ImportSelection
    master = replace(family(), alarms=exact_date_family().alarms)
    original = master.to_dict()
    report = ParseReport((master,))
    selection = ImportSelection(False)
    selection.set_selected([occurrence(master, BASE), occurrence(master, BASE+timedelta(days=2))], True)
    copied, = selection.snapshot().filter_report(report).events
    assert master.to_dict() == original
    assert triggers(copied.alarms) == triggers(master.alarms)
    assert not {a.uid for a in master.alarms}.intersection(a.uid for a in copied.alarms)
    assert f'RELATED-TO;RELTYPE=SNOOZE:{copied.alarms[0].uid}' in unfolded(copied.alarms[1].raw_ics)
    detached, = copied.recurrence_overrides.values()
    own, = Event.from_dict(detached).alarms
    assert own.trigger == OWN.trigger and own.uid != OWN.uid


def test_legacy_prepared_occurrence_delta_inherits_only_its_owned_reminders():
    from debacal.prepared_storage import StateReader, StateWriter
    writer, reader = StateWriter(), StateReader()
    master = family()
    for alarms in (None, (), (REL, ABS)):
        base = replace(master, alarms=alarms)
        old_projection = replace(occurrence(base, BASE), alarms=alarms)
        delta = writer.event(old_projection, base=base)
        assert 'alarms' not in delta['f']
        assert reader.event(delta, base=base).alarms == ((REL,) if alarms else alarms)
    key, = master.recurrence_overrides
    for alarms in ((), (REL, ABS), (OWN,)):
        manual = replace(Event.from_dict(master.recurrence_overrides[key]), alarms=alarms)
        base = replace(master, recurrence_overrides={key: manual.to_dict()})
        assert reader.event(writer.event(manual, base=base), base=base).alarms == alarms


def test_single_explicit_edit_materializes_inherited_uid_and_preserves_new_absolute():
    master = replace(family(), alarms=exact_date_family().alarms)
    with Store(':memory:') as store:
        store.apply_remote([master])
        ref = occurrence(master, BASE)
        edit = (*ref.alarms, NEW_ABS)
        store.edit_occurrence(ref, ref, alarm_edit=edit)
        result = store.get_occurrence(ref.id)
        assert triggers(result.alarms) == triggers(edit)
        assert result.alarms[0].uid != master.alarms[0].uid
        assert f'RELATED-TO;RELTYPE=SNOOZE:{result.alarms[0].uid}' in unfolded(result.alarms[1].raw_ics)
        assert store.get_event(master.id).alarms == master.alarms


def test_legacy_cut_export_materializes_reminders_from_retained_master():
    master = replace(monthly_range('cut'), alarms=(REL, ABS))
    parsed, = parse_ics(export_ics([master]), 'local')
    old = replace(parsed, alarms=None)
    blocks = _blocks(export_ics([old]))[1]
    child = next(b for b in blocks if any(line.startswith('RECURRENCE-ID') for line in b))
    alarm, = read_alarms('\n'.join(child), component_key='child')
    assert alarm.trigger == REL.trigger and alarm.uid != REL.uid


@pytest.mark.parametrize('kind', ['coincident', 'document'])
def test_authored_detached_ranges_have_stable_relative_alarm_groups(kind):
    import json
    from debacal.schedule_authoring import expression_event
    from debacal.sequence_document import document_event
    master = replace(alarm_event(), alarms=exact_date_family().alarms)
    if kind == 'coincident':
        project = lambda: expression_event(master, '2*+0d')
    else:
        text = 'schedule' + json.dumps({'rule': 'FREQ=DAILY;COUNT=2', 'move': {
            recurrence_key(BASE): {'start': (BASE+timedelta(hours=2)).isoformat(),
                                   'end': (BASE+timedelta(hours=3)).isoformat()}}})
        project = lambda: document_event(master, text)
    def child_alarms():
        child, = project().recurrence_overrides.values()
        return Event.from_dict(child).alarms
    alarms = child_alarms()
    assert triggers(alarms) == triggers(master.alarms[:2])
    assert not {a.uid for a in alarms}.intersection(a.uid for a in master.alarms)
    assert f'RELATED-TO;RELTYPE=SNOOZE:{alarms[0].uid}' in unfolded(alarms[1].raw_ics)
    assert alarms == child_alarms()


@pytest.mark.parametrize('edit', [None, (), (NEW,), (NEW, NEW_ABS)])
def test_mixed_single_selection_changes_reminders_only_with_explicit_edit(edit):
    master = family()
    with Store(':memory:') as store:
        store.apply_remote([master])
        first, _, detached, _ = store.query_occurrences(BASE, BASE+timedelta(days=5))
        store.edit_selection([first, detached], 'single', first, replace(first, title='Mixed', alarms=()), alarm_edit=edit)
        for ref, previous in ((first, [REL.trigger]), (detached, [OWN.trigger])):
            assert triggers(store.get_occurrence(ref.id).alarms) == (previous if edit is None else triggers(edit))
        assert store.get_event(master.id).alarms == (REL, ABS)


@pytest.mark.parametrize('api', ['edit_series', 'edit_future', 'selection-group', 'selection-future'])
def test_alarm_only_occurrence_submission_preserves_the_selected_schedule(api):
    master = family()
    with Store(':memory:') as store:
        store.apply_remote([master])
        ref = occurrence(master, BASE+timedelta(days=1))
        if api.startswith('selection'):
            store.edit_selection([ref], api.split('-')[1], ref, ref, alarm_edit=(NEW_ABS,))
        else:
            getattr(store, api)(ref, ref, alarm_edit=(NEW_ABS,))
        assert len(store.query_occurrences(BASE, BASE+timedelta(days=5))) == 4
        edited, = [item for item in store.list_events() if item.alarms == (NEW_ABS,)]
        assert len(edited.recurrence_overrides) == 1


def test_replacing_rule_preserves_existing_override_start_and_end_zones():
    from debacal.recurrence import updated_series
    zone = ZoneInfo('Europe/Berlin')
    endpoint = ZoneInfo('America/Phoenix')
    base = datetime(2026, 10, 23, 9, tzinfo=zone)
    master = alarm_event(start=base, end=base+timedelta(hours=2), recurrence_rule='FREQ=DAILY;COUNT=3')
    child = occurrence(master, base+timedelta(days=2))
    child = replace(child, start=child.start.astimezone(UTC), end=child.end.astimezone(endpoint),
                    alarms=(replace(REL, trigger='-P1D'),), timezone_id='')
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    projected = updated_series(master, master, replace(master, recurrence_rule='FREQ=DAILY;COUNT=4'), replace_schedule=True)
    restored = Event.from_dict(projected.recurrence_overrides[child.recurrence_id])
    assert restored.timezone_id == child.timezone_id and restored.end_timezone_id == child.end_timezone_id
    assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == datetime(2026, 10, 24, 8, tzinfo=UTC)


@pytest.mark.parametrize('scope', ['single', 'group', 'future'])
def test_direct_clipboard_paste_from_legacy_family_uses_scope_ownership(scope):
    from debacal.event_clipboard import EventClipboard
    modern, = parse_ics(export_ics([family()]), 'local')
    old = replace(modern, alarms=None)
    ref = occurrence(old, BASE+timedelta(days=1))
    captured = EventClipboard.capture([old], ref, scope)
    copy, = captured.instantiate('group', ref.start, 'local')
    assert triggers(copy.alarms) == ([REL.trigger] if scope == 'single' else [REL.trigger, ABS.trigger])
    assert old.alarms is None
    owner, tail = partition_series(old, ref)
    assert triggers(owner.alarms) == [REL.trigger, ABS.trigger]
    assert triggers(tail.alarms) == [REL.trigger]


def test_gui_capture_of_legacy_resource_preserves_opaque_envelope(monkeypatch):
    from debacal import alarm_ics
    from debacal.selection import capture_selection
    modern, = parse_ics(export_ics([alarm_event(calendar_id='local', alarms=(REL, ABS))]), 'local')
    old = replace(modern, alarms=None)
    with Store(':memory:') as store:
        store.apply_remote([old])
        store.configure_ui_access(lambda *args: None)
        ref = store.get_occurrence(old.id)
        def unexpected(*args, **kwargs):
            pytest.fail('GUI capture hydrated legacy alarm blocks')
        monkeypatch.setattr(alarm_ics, 'hydrate_alarms', unexpected)
        monkeypatch.setattr(alarm_ics, 'read_alarms', unexpected)
        captured = capture_selection(store, [ref], 'single')
        assert captured.masters[0].alarms is None
        assert captured.masters[0].raw_ics == old.raw_ics


@pytest.mark.parametrize('full', [False, True])
def test_detached_nominal_duration_with_start_alarm_keeps_dst_firing(full):
    from test_ics_duration_semantics import strip_metadata
    zone = ZoneInfo('Europe/Berlin')
    start = datetime(2026, 10, 24, 9, tzinfo=zone)
    master = alarm_event(start=start, end=start+timedelta(days=1), duration_spec='P1D',
                         alarms=(replace(REL, trigger='-P1D'),), recurrence_rule='FREQ=DAILY;COUNT=3')
    from debacal.alarm_series import materialize_alarms
    child = materialize_alarms(master, occurrence(master, start+timedelta(days=1)))
    master = replace(master, recurrence_overrides={child.recurrence_id: child.to_dict()})
    raw = export_ics([master])
    for data in (raw, strip_metadata(raw)):
        parsed, = parse_report(data, 'local', use_full=full).events
        restored = Event.from_dict(parsed.recurrence_overrides[child.recurrence_id])
        assert restored.start.timestamp() == child.start.timestamp()
        assert restored.end.timestamp() == child.end.timestamp()
        assert alarm_trigger(restored.alarms[0], restored.start, restored.end) == datetime(2026, 10, 24, 7, tzinfo=UTC)
