"""Component ownership of recurrence reminders at projection and edit time."""
from collections import Counter
from dataclasses import replace
import json
from uuid import NAMESPACE_URL, uuid5

from . import ics_fallback as ics
from .alarm_ics import _alarm_lines, _trigger_identity, direct_parts
from .alarms import AlarmEdit
from .models import Event


def relative_trigger(trigger):
    return (trigger or '').upper().startswith(('P', '+P', '-P'))


def is_relative(alarm):
    return relative_trigger(alarm.trigger)


def relative_alarms(alarms):
    """A regular projection shares relative definitions without parsing wire data."""
    if not alarms:
        return alarms
    return tuple(alarm for alarm in alarms if is_relative(alarm))


def relative_child_alarms(alarms, component_key):
    """Keep relative alarm data and remap intra-child UID references."""
    return copied_alarms(relative_alarms(alarms), component_key)


def copied_alarms(alarms, component_key):
    """Reidentify owned alarms and links within their component."""
    if not alarms:
        return alarms
    uids = {a.uid: 'urn:uuid:' + str(uuid5(NAMESPACE_URL, f'{component_key}/alarm/{a.uid}'))
            for a in alarms if a.uid}
    result, ordinals = [], Counter()
    for alarm in alarms:
        if not alarm.raw_ics:
            # UID insertion must have the same wire spelling before and after
            # an authored alarm acquires its retained template on first read.
            alarm = replace(alarm, raw_ics=ics.folded(_alarm_lines(alarm)).decode())
        if uids:
            lines = ['BEGIN:VALARM']
            for part in direct_parts(_alarm_lines(alarm)):
                if len(part) == 1 and part[0].split(':', 1)[0].split(';', 1)[0].upper() == 'RELATED-TO':
                    try:
                        _, _, value = ics.content_line(part[0])
                    except ValueError:
                        value = ''  # Preserve an unrecognized producer property verbatim.
                    if value and ics.text(value) in uids:
                        part = [part[0].removesuffix(value) + ics.escaped(uids[ics.text(value)])]
                lines.extend(part)
            alarm = replace(alarm, raw_ics=ics.folded([*lines, 'END:VALARM']).decode())
        if alarm.uid:
            uid = uids[alarm.uid]
        else:
            identity = (alarm.action, alarm.related, _trigger_identity(alarm.trigger))
            seed = json.dumps((component_key, identity, ordinals[identity]))
            uid = 'urn:uuid:' + str(uuid5(NAMESPACE_URL, seed))
            ordinals[identity] += 1
        result.append(replace(alarm, key=uid, uid=uid))
    return tuple(result)


def materialize_alarms(master: Event, child: Event) -> Event:
    """Give a new detached component its own relative alarm identifiers."""
    alarms = master.alarms
    if alarms:
        alarms = relative_child_alarms(alarms, master.ical_uid + '/' + child.recurrence_id)
    return replace(child, alarms=alarms)


def edit_child_alarms(master: Event, child: Event, edit: AlarmEdit) -> Event:
    """Materialize a new Single exception. Existing exceptions own their list."""
    old = master.recurrence_overrides.get(child.recurrence_id)
    if old is not None:
        alarms = Event.from_dict(old).alarms if edit is None else edit
        return replace(child, alarms=alarms)
    if edit is None:
        return materialize_alarms(master, child)
    return replace(child, alarms=copied_alarms(edit, master.ical_uid + '/' + child.recurrence_id))


def apply_alarm_edit(master: Event, edit: AlarmEdit) -> Event:
    if edit is None:
        return master
    edited = replace(master, alarms=edit)
    if not master.recurrence_overrides:
        return edited
    overrides = {key: materialize_alarms(edited, Event.from_dict(payload)).to_dict()
                 for key, payload in master.recurrence_overrides.items()}
    return replace(edited, recurrence_overrides=overrides)


def split_alarms(original: Event, before: Event | None, after: Event | None) -> tuple[Event | None, Event | None]:
    """Retain one original-UID owner. Partitions initially share the source UID."""
    if before is None:
        return None, replace(after, alarms=original.alarms) if after is not None else None
    if after is None:
        return replace(before, alarms=original.alarms), None
    relative = relative_alarms(original.alarms)
    if before.ical_uid != original.ical_uid and after.ical_uid == original.ical_uid:
        return replace(before, alarms=relative), replace(after, alarms=original.alarms)
    return replace(before, alarms=original.alarms), replace(after, alarms=relative)
