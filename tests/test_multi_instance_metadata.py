"""Custom authoring survives two independent SQLite/vdirsyncer workspaces.

The shared CLI stand-in operates real private mirrors, configuration and
outboxes. Only its remote transport is simulated; no account or network is used.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from debacal.dialogs import EventDialog
from debacal.ics import parse_ics
from debacal.ics_fallback import folded, unfolded
from debacal.models import Calendar
from debacal.patterns import expand_sequence
from debacal.recurrence import base_schedule_starts, expand_series, recurrence_key
from debacal.store import Store
from debacal.schedule_authoring import displayed_starts
from debacal.vdirsyncer_backend import VdirsyncerBackend, semantic
from test_vdirsyncer import FakeCLI


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2027, 3, 20, 9, tzinfo=ZONE)
REGULAR = '  4*(+2)  '
IRREGULAR = '+5d 11:00-2.4.2027 +3d'
METADATA = {'X-DEBACAL-PATTERN', 'X-DEBACAL-PATTERN-VERSION', 'X-DEBACAL-PATTERN-ANCHOR'}


def instants(values):
    return tuple(sorted(value.astimezone(timezone.utc) for value in values))


def schedule(master):
    return tuple((event.start.astimezone(timezone.utc), event.end.astimezone(timezone.utc))
                 for event in expand_series(master, START - timedelta(days=365), START + timedelta(days=800)))


@dataclass
class Instance:
    store: Store
    calendar: Calendar
    backend: VdirsyncerBackend

    def sync(self):
        result = self.backend.sync(self.store, self.calendar, 'synthetic-session-secret')
        assert not result.conflicts
        assert not result.issues
        assert not self.store.pending_changes(self.calendar.id)
        return result

    def masters(self):
        return self.store.list_events(calendar_ids=[self.calendar.id])

    def resource(self, uid):
        return next(master for master in self.masters() if master.ical_uid == uid)


@pytest.fixture
def workspaces(tmp_path):
    app = QApplication.instance() or QApplication([])
    cli = FakeCLI()
    instances, dialogs = [], []
    with patch('debacal.event_dialog.local_zone', return_value=ZONE):
        for name in ('installation-a', 'installation-b'):
            store = Store(tmp_path / (name + '.sqlite'))
            calendar = Calendar(name + '-calendar', 'Shared test calendar', '#123456', kind='caldav',
                                url='https://calendar.test/shared/', username='synthetic-user')
            store.save_calendar(calendar)
            instances.append(Instance(store, calendar, VdirsyncerBackend(store.path, runner=cli)))

        def editor(instance, master=None, *, scope='group', start=START):
            values = expand_series(master, START - timedelta(days=365), START + timedelta(days=800)) if master else []
            selected = values[len(values) // 2] if values else None
            dialog = EventDialog(instance.store.list_calendars(), selected.start if selected else start,
                                 selected.end if selected else start + timedelta(minutes=90),
                                 event=selected, series=master, edit_scope=scope)
            dialogs.append(dialog)
            if master is None:
                dialog.calendar.setCurrentIndex(dialog.calendar.findData(instance.calendar.id))
                dialog.title.setText('Synthetic custom schedule')
            return dialog

        yield (*instances, cli, editor)
        for dialog in reversed(dialogs):
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        for instance in instances:
            instance.store.close()


def save(dialog):
    dialog.save()
    assert dialog.result() == QDialog.DialogCode.Accepted, dialog.error.text()
    assert len(dialog.result_events) == 1
    return dialog.result_events[0]


def create(instance, editor, literal, *, candidate=False, start=START):
    dialog = editor(instance, start=start)
    dialog.pattern.setText(literal)
    dialog.schedule_mode.setCurrentData('alternatives' if candidate else 'repeat')
    master = save(dialog)
    instance.store.apply('Create custom schedule', [master])
    return master


def assert_definition(actual, expected):
    assert actual.schedule_expression == expected.schedule_expression
    assert actual.schedule_syntax == expected.schedule_syntax == 2
    assert actual.expression_anchor == expected.expression_anchor
    assert actual.timezone_id == expected.timezone_id == ZONE.key
    assert actual.ical_uid == expected.ical_uid
    assert schedule(actual) == schedule(expected)


@pytest.mark.parametrize('literal', [REGULAR, IRREGULAR], ids=['standard-RRULE', 'standard-RDATE'])
@pytest.mark.parametrize('display_zone', [ZONE, ZoneInfo('UTC')], ids=['same-zone', 'other-zone'])
def test_custom_configuration_roundtrips_bidirectionally_across_installations(workspaces, literal, display_zone):
    a, b, cli, editor = workspaces
    original = create(a, editor, literal)
    assert bool(original.recurrence_rule) == (literal == REGULAR)
    assert a.sync().sent == 1
    # Another client may add metadata that Debacal must preserve while editing.
    cli.remote[original.ical_uid] = cli.remote[original.ical_uid].replace(
        'END:VEVENT', 'X-OTHER-CLIENT-FLAG:retained\nEND:VEVENT')
    assert b.sync().received == 1
    imported = b.resource(original.ical_uid)
    assert imported.calendar_id == b.calendar.id != original.calendar_id
    assert imported.id != original.id
    assert_definition(imported, original)
    with patch('debacal.event_dialog.local_zone', return_value=display_zone):
        reopened = editor(b, imported)
        assert reopened.pattern.text() == literal
        assert instants(reopened.sequence_starts()) == instants(base_schedule_starts(original))
        # A different machine's clock display must not rewrite the authoring.
        reopened.title.setText('Synthetic title-only edit in installation B')
        b.store.edit_series(reopened.edit_reference, save(reopened), replace_schedule=reopened.schedule_replacement)
        renamed = b.resource(original.ical_uid)
        assert_definition(renamed, original)
        assert b.sync().sent == 1

        reopened = editor(b, renamed)
        assert reopened.pattern.text() == literal
        edited_literal = '3*+3' if literal == REGULAR else '10:00 +2d 15:30-5.4.2027'
        reopened.pattern.setText(edited_literal)
        b.store.edit_series(reopened.edit_reference, save(reopened), replace_schedule=reopened.schedule_replacement)
        changed = b.resource(original.ical_uid)
        assert changed.schedule_expression == edited_literal
        assert instants(value.start for value in expand_series(changed, START, START + timedelta(days=800))) == instants(
            expand_sequence(edited_literal, original.start))
        assert b.sync().sent == 1
    remote = parse_ics(cli.remote[original.ical_uid], 'independent-reader')[0]
    assert_definition(remote, changed)
    assert 'X-OTHER-CLIENT-FLAG:retained' in cli.remote[original.ical_uid]
    assert a.sync().received == 1
    returned = a.resource(original.ical_uid)
    assert returned.id == original.id
    assert_definition(returned, changed)
    assert editor(a, returned).pattern.text() == edited_literal


@pytest.mark.parametrize('display_zone', [ZONE, ZoneInfo('UTC')], ids=['same-zone', 'other-zone'])
def test_text_only_configuration_change_is_published_without_changing_standard_dates(workspaces, display_zone):
    a, b, cli, editor = workspaces
    original = create(a, editor, '4*+2')
    a.sync()
    b.sync()
    imported = b.resource(original.ical_uid)
    with patch('debacal.event_dialog.local_zone', return_value=display_zone):
        dialog = editor(b, imported)
        literal = '  4*(+2)  '
        dialog.pattern.setText(literal)
        b.store.edit_series(dialog.edit_reference, save(dialog), replace_schedule=dialog.schedule_replacement)
    formatted = b.resource(original.ical_uid)
    assert schedule(formatted) == schedule(original)
    assert semantic(formatted) != semantic(imported)
    assert b.store.pending_changes(b.calendar.id)
    assert b.sync().sent == 1
    assert parse_ics(cli.remote[original.ical_uid], 'reader')[0].schedule_expression == literal
    a.sync()
    returned = a.resource(original.ical_uid)
    assert returned.schedule_expression == literal
    assert schedule(returned) == schedule(original)
    assert editor(a, returned).pattern.text() == literal


def test_other_machine_previous_display_date_preserves_original_fixed_anchor(workspaces):
    a, b, _, editor = workspaces
    initial = START.replace(hour=0, minute=30)
    literal = '1:00 +2d 2:30'
    original = create(a, editor, literal, start=initial)
    a.sync()
    b.sync()
    imported = b.resource(original.ical_uid)
    with patch('debacal.event_dialog.local_zone', return_value=ZoneInfo('UTC')):
        dialog = editor(b, imported)
        assert dialog.start.date().toPython() == initial.date() - timedelta(days=1)
        assert dialog.pattern.text() == literal
        assert instants(dialog.sequence_starts()) == instants(base_schedule_starts(original))
        dialog.title.setText('Synthetic midnight title change')
        b.store.edit_series(dialog.edit_reference, save(dialog), replace_schedule=dialog.schedule_replacement)
    changed = b.resource(original.ical_uid)
    assert_definition(changed, original)
    assert changed.expression_anchor == initial.date().isoformat()
    b.sync()
    a.sync()
    returned = a.resource(original.ical_uid)
    assert_definition(returned, original)
    assert editor(a, returned).pattern.text() == literal


def test_candidate_overrides_exclusions_and_following_split_sync_as_complete_resources(workspaces):
    a, b, cli, editor = workspaces
    original = create(a, editor, '4*+2', candidate=True)
    values = expand_series(original, START, START + timedelta(days=200))
    moved = replace(values[1], title='Synthetic moved exception',
                    start=values[1].start + timedelta(hours=2), end=values[1].end + timedelta(hours=2))
    a.store.edit_occurrence(values[1], moved)
    a.store.delete_occurrence(values[3])
    original = a.resource(original.ical_uid)
    a.sync()
    b.sync()
    imported = b.resource(original.ical_uid)
    assert_definition(imported, original)
    assert imported.candidate
    assert imported.excluded_dates == original.excluded_dates
    assert set(imported.recurrence_overrides) == set(original.recurrence_overrides)
    opened = editor(b, imported)
    assert instants(expand_sequence(opened.pattern.text(), imported.start)) == instants(displayed_starts(imported))

    selected = next(value for value in expand_series(imported, START, START + timedelta(days=200))
                    if value.start == values[2].start)
    dialog = EventDialog(b.store.list_calendars(), selected.start, selected.end,
                         event=selected, series=imported, edit_scope='future')
    try:
        dialog.title.setText('Synthetic following alternatives')
        b.store.edit_future(selected, save(dialog), replace_schedule=dialog.schedule_replacement,
                            expected_master=imported)
    finally:
        dialog.close()
        dialog.deleteLater()
    partitioned = b.masters()
    assert len(partitioned) == 2
    assert all(master.candidate for master in partitioned)
    assert sum(len(master.recurrence_overrides) for master in partitioned) == 1
    assert sum(len(master.excluded_dates) for master in partitioned) == 1
    assert b.sync().sent == 2
    assert len(cli.remote) == 2
    a.sync()
    for expected in partitioned:
        received = a.resource(expected.ical_uid)
        assert_definition(received, expected)
        assert received.candidate
        reopened = editor(a, received)
        assert reopened.pattern.text() == expected.schedule_expression
        assert instants(expand_sequence(reopened.pattern.text(), received.start)) == instants(displayed_starts(received))


@pytest.mark.parametrize('stripped', [METADATA, {'X-DEBACAL-PATTERN'}, {'X-DEBACAL-PATTERN-VERSION'},
                                     {'X-DEBACAL-PATTERN-ANCHOR'}], ids=['all', 'literal', 'version', 'anchor'])
def test_server_stripped_annotations_keep_standard_schedule_editable(workspaces, stripped):
    a, b, cli, editor = workspaces
    original = create(a, editor, IRREGULAR)
    a.sync()
    b.sync()
    lines = [line for line in unfolded(cli.remote[original.ical_uid])
             if line.partition(':')[0].split(';')[0].upper() not in stripped]
    cli.remote[original.ical_uid] = folded(lines).decode()
    # The server is authoritative about removed properties. Standard dates
    # remain editable without silently resurrecting an old private annotation.
    a.sync()
    b.sync()
    for instance in (a, b):
        received = instance.resource(original.ical_uid)
        assert received.schedule_expression == ('' if 'X-DEBACAL-PATTERN' in stripped else original.schedule_expression)
        assert received.schedule_syntax == (1 if 'X-DEBACAL-PATTERN-VERSION' in stripped else 2)
        assert received.expression_anchor == ('' if 'X-DEBACAL-PATTERN-ANCHOR' in stripped else original.expression_anchor)
        assert schedule(received) == schedule(original)
        assert received.timezone_id == original.timezone_id
        reopened = editor(instance, received)
        assert instants(reopened.sequence_starts()) == instants(base_schedule_starts(original))
        assert not reopened.error.text()
