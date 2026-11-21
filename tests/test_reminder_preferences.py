"""Local policy commits and saved import reminders have independent lifetimes."""
from dataclasses import replace
from datetime import timedelta, timezone

import pytest

from alarm_support import BASE, alarm_event
from debacal.alarms import Alarm
from debacal.ics import export_ics, parse_ics_report
from debacal.ics_resources import ParseReport, RejectedResource, ResourceIssue
from debacal.import_preparation import count_reminder_sources, remap_calendar, transform_import_reminders
from debacal.import_selection import ImportSelection
from debacal.models import Calendar, Event
from debacal.recurrence import occurrence
from debacal.reminder_preferences import (
    CalendarReminderPolicy, ReminderPreferences, load_reminder_policy,
    load_reminder_preferences, read_reminder_policy, save_reminder_preferences,
)
from debacal.store import Store
from debacal.ui_state import ui_state_path


TIMED = Alarm('timed-template', 'DISPLAY', '-PT5M', uid='timed-template', description='Soon')
ALL_DAY = Alarm('all-day-template', 'DISPLAY', '-P1D', description='Tomorrow')
ABSOLUTE = Alarm('absolute', 'DISPLAY', '20261122T100000Z')


def canonical_state(store):
    return store.list_events(), store.history(), store.pending_changes(), store.events_revision(), store.list_calendars()


def test_defaults_policies_and_atomic_eligibility_boundary(tmp_path):
    path = tmp_path / 'synthetic #profile.sqlite3'
    assert read_reminder_policy(path).generation == 0
    assert not path.exists() and not tmp_path.joinpath(ui_state_path(str(path))).exists()
    existing = alarm_event(alarms=(TIMED,))
    with Store(path) as store:
        store.save_calendar(Calendar('c', 'Synthetic source', '#123456', kind='caldav'))
        store.apply('Synthetic event', [existing])
        before_canonical_state = canonical_state(store)
        assert all(before_canonical_state[:3])
        defaults = load_reminder_preferences(store)
        assert defaults.enabled is False and defaults.import_mode == 'keep'
        assert (defaults.delivery, defaults.sound, defaults.missed, defaults.close_behavior) == (
            'both', 'default', 'upcoming', 'quit')
        assert defaults.new_timed == defaults.new_all_day == defaults.import_alarms == ()
        assert defaults.calendars == {}

        policy = replace(defaults, enabled=True, calendars={'c': CalendarReminderPolicy('mute')})
        offset_now = BASE.astimezone(timezone(timedelta(hours=2)))
        with store.ui_transaction():
            save_reminder_preferences(store, policy, now=offset_now)
            inside = load_reminder_policy(store)
            assert inside.preferences == policy
            assert inside.eligible_since == BASE and inside.eligible_since.tzinfo is timezone.utc
            assert read_reminder_policy(path).preferences == defaults
        assert read_reminder_policy(path) == inside
        assert inside.generation == 1
        assert inside.calendar_eligible_since == {}

        # Removing a local mute resumes the default calendar rule, even with
        # a settings reader that never observed the brief mute transition.
        resumed = replace(policy, calendars={})
        resumed_at = BASE + timedelta(minutes=2)
        save_reminder_preferences(store, resumed, now=resumed_at)
        boundary = read_reminder_policy(path)
        assert boundary.preferences == resumed and boundary.generation == 2
        assert boundary.calendar_eligible_since == {'c': resumed_at}
        assert boundary.eligible_since == BASE
        save_reminder_preferences(store, resumed, now=resumed_at + timedelta(minutes=1))
        assert read_reminder_policy(path) == boundary

        with pytest.raises(RuntimeError, match='Abandon settings'):
            with store.ui_transaction():
                save_reminder_preferences(store, replace(resumed, enabled=False), now=resumed_at)
                assert load_reminder_policy(store).generation == 3
                raise RuntimeError('Abandon settings')
        assert read_reminder_policy(path) == boundary
        later = BASE + timedelta(days=1)
        save_reminder_preferences(store, replace(resumed, enabled=False), now=later)
        save_reminder_preferences(store, resumed, now=later + timedelta(minutes=1))
        after = read_reminder_policy(path)
        assert after.eligible_since == later + timedelta(minutes=1)
        assert after.calendar_eligible_since == boundary.calendar_eligible_since
        assert after.generation == 4
        after_canonical_state = canonical_state(store)
        assert before_canonical_state == after_canonical_state
        assert store.get_setting('display_preferences') is None
    with Store(path) as reopened:
        assert load_reminder_policy(reopened) == after


def test_default_templates_and_canceled_preference_drafts_are_independent():
    with Store(':memory:') as store:
        defaults = load_reminder_preferences(store)
        calendars = {'c': CalendarReminderPolicy('replace', [ALL_DAY])}
        draft = replace(defaults, new_timed=[TIMED], new_all_day=[ALL_DAY],
                        import_mode='replace', import_alarms=[TIMED], calendars=calendars)
        calendars.clear()
        assert draft.calendars['c'].alarms == (ALL_DAY,)
        with pytest.raises(TypeError):
            draft.calendars['other'] = CalendarReminderPolicy('mute')
        assert ReminderPreferences().calendars is not ReminderPreferences().calendars
        assert load_reminder_preferences(store) == defaults

        save_reminder_preferences(store, draft, now=BASE)
        saved = load_reminder_preferences(store)
        assert saved == draft and saved.new_timed == saved.import_alarms == (TIMED,)
        assert saved.new_all_day == (ALL_DAY,)
        timed = alarm_event()
        all_day = alarm_event(all_day=True, start=BASE.replace(hour=0), end=BASE.replace(hour=0) + timedelta(days=1))
        timed_alarms = saved.alarms_for_new_event(timed)
        assert [a.trigger for a in timed_alarms] == ['-PT5M']
        assert [a.trigger for a in saved.alarms_for_new_event(all_day)] == ['-P1D']
        assert timed_alarms == saved.alarms_for_new_event(replace(timed, title='Renamed draft'))
        assert timed_alarms[0].uid != saved.alarms_for_new_event(replace(timed, ical_uid='different'))[0].uid
        assert timed.alarms == all_day.alarms == ()


@pytest.mark.parametrize('alarm', [ABSOLUTE, replace(TIMED, trigger='PX'), replace(TIMED, trigger=None)])
def test_reusable_reminders_require_valid_relative_triggers(alarm):
    with pytest.raises(ValueError, match='relative'):
        CalendarReminderPolicy('replace', (alarm,))
    for name in ('new_timed', 'new_all_day', 'import_alarms'):
        with pytest.raises(ValueError, match='relative'):
            ReminderPreferences(**{name: (alarm,)})
    with pytest.raises(ValueError, match='relative'):
        transform_import_reminders(ParseReport((alarm_event(),)), 'replace', (alarm,))


def mixed_report():
    unsupported = Alarm('email', 'EMAIL', '-PT15M', raw_ics=(
        'BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER:-PT15M\r\nDESCRIPTION:Legacy\r\nEND:VALARM\r\n'))
    master = alarm_event(alarms=(unsupported,), recurrence_rule='FREQ=DAILY')
    blank = replace(occurrence(master, BASE + timedelta(days=1)), alarms=())
    absolute = replace(occurrence(master, BASE + timedelta(days=2)), alarms=(ABSOLUTE,))
    deselected = replace(occurrence(master, BASE + timedelta(days=3)), alarms=(TIMED,))
    master = replace(master, recurrence_overrides={
        child.recurrence_id: child.to_dict() for child in (blank, absolute, deselected)
    })
    report = parse_ics_report(export_ics((master,)), 'preview')
    issue = ResourceIssue('synthetic', 'Synthetic issue')
    rejected = RejectedResource('bad', 'synthetic data', 'synthetic', issue.reason)
    return replace(report, issues=(issue,), rejected=(rejected,)), (blank, absolute, deselected)


def test_import_policies_apply_only_to_selected_components_and_preserve_existing_alarm_lists():
    report, (blank, absolute, deselected) = mixed_report()
    master = report.events[0]
    selection = ImportSelection()
    selection.toggle(occurrence(master, deselected.start, deselected.recurrence_id))
    selected = selection.snapshot().filter_report(report)
    selected_master = selected.events[0]
    existing_alarms = selected_master.alarms
    assert existing_alarms[0].action == 'EMAIL'
    assert count_reminder_sources(report) == (3, 4)
    assert count_reminder_sources(selected) == (2, 3)
    assert transform_import_reminders(selected, 'keep', ()) is selected

    transformed_remove = transform_import_reminders(selected, 'remove', ())
    assert transformed_remove.events[0].alarms == ()
    assert all(p['alarms'] == [] for p in transformed_remove.events[0].recurrence_overrides.values())
    assert b'BEGIN:VALARM' not in export_ics(transformed_remove.events)
    assert count_reminder_sources(transformed_remove) == (0, 3)

    transformed_add_missing = transform_import_reminders(selected, 'add_missing', (TIMED,))
    assert transformed_add_missing.events[0].alarms == existing_alarms
    children = transformed_add_missing.events[0].recurrence_overrides
    assert set(children) == {blank.recurrence_id, absolute.recurrence_id}
    assert Event.from_dict(children[absolute.recurrence_id]).alarms == Event.from_dict(
        selected_master.recurrence_overrides[absolute.recurrence_id]).alarms
    assert Event.from_dict(children[blank.recurrence_id]).alarms[0].trigger == '-PT5M'
    assert count_reminder_sources(transformed_add_missing) == (3, 3)
    assert count_reminder_sources(selected) == (2, 3)
    assert transformed_add_missing.issues == selected.issues and transformed_add_missing.rejected == selected.rejected
    only_blank = ImportSelection(False)
    only_blank.toggle(occurrence(master, blank.start, blank.recurrence_id))
    blank_report = only_blank.snapshot().filter_report(report)
    assert count_reminder_sources(blank_report) == (0, 1)
    assert count_reminder_sources(transform_import_reminders(blank_report, 'add_missing', (TIMED,))) == (1, 1)


def test_import_template_uids_are_stable_for_retries_and_unique_per_destination_component():
    report, _ = mixed_report()
    destination = remap_calendar(report, 'destination')
    replaced = transform_import_reminders(destination, 'replace', (TIMED,))
    assert transform_import_reminders(destination, 'replace', (TIMED,)) == replaced
    assert transform_import_reminders(replaced, 'replace', (TIMED,)) == replaced

    def component_uids(report):
        master = report.events[0]
        components = [master, *(Event.from_dict(p) for p in master.recurrence_overrides.values())]
        assert all([a.trigger for a in e.alarms] == ['-PT5M'] for e in components)
        return {e.alarms[0].uid for e in components}

    uids = component_uids(replaced)
    assert len(uids) == 4 and TIMED.uid not in uids and None not in uids
    alternate = transform_import_reminders(remap_calendar(report, 'other'), 'replace', (TIMED,))
    assert uids.isdisjoint(component_uids(alternate))
    master = replaced.events[0]
    assert (master.ical_uid, master.id) == (destination.events[0].ical_uid, destination.events[0].id)
    assert count_reminder_sources(replaced) == (4, 4)


def test_legacy_alarm_presence_is_hydrated_for_count_and_add_missing():
    report, (blank, absolute, _) = mixed_report()
    master = report.events[0]
    legacy = replace(master, alarms=None, recurrence_overrides={
        key: {name: value for name, value in payload.items() if name != 'alarms'}
        for key, payload in master.recurrence_overrides.items()
    })
    selected = replace(report, events=(legacy,))
    assert transform_import_reminders(selected, 'keep', ()) is selected
    assert count_reminder_sources(selected) == (3, 4)
    added = transform_import_reminders(selected, 'add_missing', (TIMED,)).events[0]
    assert added.alarms == master.alarms
    assert Event.from_dict(added.recurrence_overrides[blank.recurrence_id]).alarms[0].trigger == '-PT5M'
    assert Event.from_dict(added.recurrence_overrides[absolute.recurrence_id]).alarms == Event.from_dict(
        master.recurrence_overrides[absolute.recurrence_id]).alarms
    assert selected.events[0].alarms is None
