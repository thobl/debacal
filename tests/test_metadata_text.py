"""ICS physical line handling must not truncate proprietary TEXT metadata."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from lebacal.ics import export_ics, parse_ics, patch_ics
from lebacal.ics_fallback import parse as parse_fallback, unfolded
from lebacal.models import Event
from lebacal.patterns import expand_sequence
from lebacal.recurrence import schedule_fields, rules_equivalent


ZONE = ZoneInfo('Europe/Berlin')
START = datetime(2026, 10, 24, 9, tzinfo=ZONE)
SEPARATORS = ['\u2028', '\u2029', '\u0085']


def authored(expression, separator):
    starts = expand_sequence(expression, START)
    return Event('definition', 'instance-a', 'Planning' + separator + 'session', START,
                 START + timedelta(hours=1), **schedule_fields(starts),
                 schedule_expression=expression, expression_anchor=START.date().isoformat(), schedule_syntax=2)


@pytest.mark.parametrize('parse', [parse_ics, parse_fallback])
@pytest.mark.parametrize('separator', SEPARATORS)
@pytest.mark.parametrize('tail', ['+1d', '+2d'])
def test_definition_unicode_whitespace_is_not_a_physical_ics_line(parse, separator, tail):
    expression = '  +1d,' + separator + '  ' + tail + ' '
    original = authored(expression, separator)
    data = export_ics([original])
    returned = parse(data, 'instance-b')[0]
    assert returned.id != original.id
    assert returned.ical_uid == original.ical_uid
    assert returned.schedule_expression == expression
    assert returned.expression_anchor == original.expression_anchor
    assert returned.schedule_syntax == 2
    assert returned.timezone_id == 'Europe/Berlin'
    assert returned.title == original.title
    assert rules_equivalent(returned.recurrence_rule, original.recurrence_rule, original)
    assert returned.recurrence_dates == original.recurrence_dates
    assert [value.astimezone(ZoneInfo('UTC')) for value in expand_sequence(returned.schedule_expression, returned.start)] == [
        value.astimezone(ZoneInfo('UTC')) for value in expand_sequence(expression, START)]


@pytest.mark.parametrize('parse', [parse_ics, parse_fallback])
@pytest.mark.parametrize('separator', SEPARATORS)
def test_long_folded_definition_and_metadata_only_patch_preserve_text(parse, separator):
    expression = ('+1d' + separator + '  ') * 120
    original = authored(expression, separator)
    data = export_ics([original])
    assert all(len(line) <= 75 for line in data.split(b'\r\n'))
    assert b'\r\n ' in data
    returned = parse(data, 'instance-b')[0]
    assert returned.schedule_expression == expression
    edited = replace(returned, schedule_expression=expression.replace('  ', '\t'))
    patched = patch_ics(data, edited)
    final = parse(patched, 'instance-a')[0]
    assert final.schedule_expression == edited.schedule_expression
    assert rules_equivalent(final.recurrence_rule, original.recurrence_rule, original)
    assert final.recurrence_dates == original.recurrence_dates
    assert sum(line.startswith('X-LEBACAL-PATTERN:') for line in unfolded(patched)) == 1


@pytest.mark.parametrize('newline', ['\r\n', '\n', '\r'])
def test_only_physical_newlines_split_and_unfold_content(newline):
    data = newline.join(['BEGIN:VCALENDAR', 'X-TEXT:first\u2028part', ' second\u2029part',
                         '\tthird\u0085part', 'END:VCALENDAR', ''])
    assert unfolded(data) == ['BEGIN:VCALENDAR', 'X-TEXT:first\u2028partsecond\u2029partthird\u0085part', 'END:VCALENDAR']
