"""The offline corpus audit detects loss without publishing source contents."""
import importlib.util
import json
from pathlib import Path
from subprocess import CompletedProcess
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_calendar_interop.py'
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason='Maintainer tooling is not shipped in source archives')


@pytest.fixture
def audit():
    spec = importlib.util.spec_from_file_location('debacal_interop_audit', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def calendar(extra=(), *, uid='synthetic-secret-uid', recurrence=True):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Synthetic interoperability test//EN',
             'METHOD:REQUEST', 'BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:20260101T000000Z',
             'SUMMARY:Synthetic private title', 'DESCRIPTION:Synthetic private notes',
             'DTSTART;TZID=Europe/Berlin:20261023T110000', 'DURATION:PT1H']
    if recurrence:
        lines += ['RRULE:FREQ=DAILY;COUNT=5', 'EXDATE;TZID=Europe/Berlin:20261024T110000']
    lines += ['ORGANIZER:mailto:synthetic-private-organizer@example.invalid',
              'ATTENDEE:mailto:synthetic-private-attendee@example.invalid', 'X-SYNTHETIC:Private metadata',
              *extra, 'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT15M',
              'DESCRIPTION:Synthetic private reminder', 'END:VALARM', 'END:VEVENT', 'END:VCALENDAR', '']
    return '\r\n'.join(lines).encode()


def source(tmp_path, data=None):
    path = tmp_path / 'sample.ics'
    path.write_bytes(calendar() if data is None else data)
    return path


def test_supported_recurring_event_survives_all_audit_stages_without_disclosure(audit, tmp_path, capsys):
    path = source(tmp_path)
    result = audit.audit_file(path, path.name)
    assert result['outcome'] == 'pass' and result['stage'] == 'complete'
    assert result['events'] == 1
    encoded = json.dumps(result)
    for secret in ('synthetic-secret-uid', 'Synthetic private', 'private-organizer', 'Private metadata'):
        assert secret not in encoded
    assert str(tmp_path) not in encoded
    assert not capsys.readouterr().out and not capsys.readouterr().err


@pytest.mark.parametrize('stage', ['single_export', 'caldav_export'])
def test_audit_detects_opaque_metadata_loss_at_exact_export_stage(audit, tmp_path, monkeypatch, stage):
    from debacal import ics
    target = 'export_ics' if stage == 'single_export' else 'normalize_caldav_ics'
    original = getattr(ics, target)
    def lose_attendees(*args, **kwargs):
        return original(*args, **kwargs).replace(
            b'ATTENDEE:mailto:synthetic-private-attendee@example.invalid\r\n', b'')
    monkeypatch.setattr(ics, target, lose_attendees)
    result = audit.audit_file(source(tmp_path), 'sample.ics')
    assert (result['outcome'], result['stage'], result['check']) == ('data_loss', stage, 'retained_metadata')


def test_audit_detects_modeled_time_changes_even_when_metadata_survives(audit, tmp_path, monkeypatch):
    from debacal import ics
    original = ics.export_ics
    def shift_start(*args, **kwargs):
        return original(*args, **kwargs).replace(b'20261023T110000', b'20261023T120000')
    monkeypatch.setattr(ics, 'export_ics', shift_start)
    result = audit.audit_file(source(tmp_path), 'sample.ics')
    assert (result['outcome'], result['stage'], result['check']) == ('data_loss', 'single_export', 'event_semantics')


def test_safe_upload_singleton_repair_does_not_hide_repeatable_metadata_loss(audit, tmp_path):
    raw = calendar(extra=['ORGANIZER:mailto:synthetic-private-organizer@example.invalid',
                          'ATTENDEE:mailto:synthetic-private-attendee@example.invalid'])
    assert audit.audit_file(source(tmp_path, raw), 'sample.ics')['outcome'] == 'pass'
    fingerprint = audit.metadata_fingerprint(raw, storage=True)
    assert sum(count for (scope, line), count in fingerprint.items() if line.startswith('ORGANIZER:')) == 1
    assert sum(count for (scope, line), count in fingerprint.items() if line.startswith('ATTENDEE:')) == 2


def test_harmless_blank_lines_in_legacy_event_templates_do_not_break_the_audit(audit, tmp_path):
    raw = calendar().replace(b'BEGIN:VEVENT\r\n', b'BEGIN:VEVENT\r\n\r\n')
    assert audit.audit_file(source(tmp_path, raw), 'sample.ics')['outcome'] == 'pass'


def test_conflicting_upload_metadata_is_explicitly_classifiable(audit, tmp_path):
    result = audit.audit_file(source(tmp_path, calendar(extra=['ORGANIZER:mailto:other@example.invalid'])), 'sample.ics')
    assert result['outcome'] == 'blocked_upload' and result['stage'] == 'caldav_export'
    assert audit.classify(result, {})['unexpected']
    expected = {'sample.ics': {'outcome': 'blocked_upload', 'classification': 'invalid'}}
    assert not audit.classify(result, expected)['unexpected']


def test_invalid_and_partial_inputs_require_classification_without_becoming_passes(audit, tmp_path):
    invalid = calendar().replace(b'DTSTART;TZID=Europe/Berlin:20261023T110000', b'DTSTART:not-a-date')
    rejected = audit.audit_file(source(tmp_path, invalid), 'sample.ics')
    assert rejected['outcome'] == 'rejected' and rejected['events'] == 0
    assert audit.classify(rejected, {})['unexpected']
    expected = {'sample.ics': {'outcome': 'rejected', 'classification': 'invalid'}}
    classified = audit.classify(rejected, expected)
    assert classified['classification'] == 'invalid' and not classified['unexpected']
    assert not audit.classify(rejected, {}, report_only=True)['unexpected']
    healthy_block = calendar(uid='healthy-peer', recurrence=False).split(b'BEGIN:VEVENT', 1)[1].split(b'END:VEVENT', 1)[0]
    partial = invalid.replace(b'END:VCALENDAR', b'BEGIN:VEVENT' + healthy_block + b'END:VEVENT\r\nEND:VCALENDAR')
    result = audit.audit_file(source(tmp_path, partial), 'sample.ics')
    assert result['outcome'] == 'partial' and result['events'] == result['rejected_resources'] == 1


def test_non_event_and_component_fragments_have_separate_outcomes(audit, tmp_path):
    raw = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:task\r\nEND:VTODO\r\nEND:VCALENDAR\r\n'
    result = audit.audit_file(source(tmp_path, raw), 'sample.ics')
    assert result['outcome'] == 'non_event' and not audit.classify(result, {})['unexpected']
    result = audit.audit_file(source(tmp_path, b'BEGIN:VEVENT\r\nUID:fragment\r\nEND:VEVENT\r\n'), 'sample.ics')
    assert result['outcome'] == 'incomplete' and result['stage'] == 'read'


def test_size_and_time_limits_are_enforced_with_exact_stage(audit, tmp_path, monkeypatch):
    path = source(tmp_path)
    result = audit.audit_file(path, 'sample.ics', max_bytes=32)
    assert result['outcome'] == 'oversized' and result['bytes_read'] == 33 and 'sha256' not in result
    from debacal import ics_resources
    original = ics_resources.parse_report
    def slow(*args, **kwargs):
        time.sleep(0.1)
        return original(*args, **kwargs)
    monkeypatch.setattr(ics_resources, 'parse_report', slow)
    result = audit.audit_file(path, 'sample.ics', timeout=0.02)
    assert (result['outcome'], result['stage']) == ('timeout', 'import')
    assert audit.classify(result, {}, report_only=True)['unexpected']


def test_arbitrary_parser_crashes_and_logs_do_not_expose_source_data(audit, tmp_path, monkeypatch, capsys):
    from debacal import ics_resources
    def crash(*args, **kwargs):
        print('synthetic-private-log')
        raise AttributeError('synthetic-private-exception')
    monkeypatch.setattr(ics_resources, 'parse_report', crash)
    result = audit.audit_file(source(tmp_path), 'sample.ics')
    assert (result['outcome'], result['stage'], result['error_type']) == ('error', 'import', 'AttributeError')
    assert 'private' not in json.dumps(result)
    assert not capsys.readouterr().out and not capsys.readouterr().err


@pytest.mark.parametrize('errors,exception,outcome', [(0, False, 'pass'), (1, False, 'server_rejected'), (0, True, 'server_rejected')])
def test_optional_server_validation_uses_private_stdin_and_opaque_ids(audit, tmp_path, monkeypatch, errors, exception, outcome):
    requests = []
    def validator(arguments, **kwargs):
        payload = json.loads(kwargs['input'])
        requests.append((arguments, payload))
        assert set(payload) == {'item-0'}
        assert 'synthetic-secret-uid' in payload['item-0']
        assert 'synthetic-secret-uid' not in ' '.join(arguments)
        assert '\r\nMETHOD:REQUEST\r\n' not in payload['item-0']
        response = {'item-0': {'errors': errors, 'warnings': 2, 'exception': exception}}
        return CompletedProcess(arguments, 0, json.dumps(response), 'private-server-log')
    monkeypatch.setattr(audit.subprocess, 'run', validator)
    result = audit.audit_file(source(tmp_path), 'sample.ics', sabre_autoload=tmp_path / 'autoload.php')
    assert result['outcome'] == outcome and result['server_warnings'] == 2
    assert len(requests) == 1 and 'private' not in json.dumps(result)
    if outcome != 'pass':
        assert result['stage'] == 'caldav_validation'


def test_sources_use_relative_labels_and_do_not_follow_symbolic_links(audit, tmp_path):
    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    source(corpus)
    outside = tmp_path / 'outside'
    outside.mkdir()
    source(outside)
    (corpus / 'linked').symlink_to(outside, target_is_directory=True)
    (corpus / 'copy.ics').symlink_to(outside / 'sample.ics')
    assert list(audit.sources([corpus])) == ['corpus/sample.ics']


def test_calendar_stream_with_distinct_explicit_clocks_passes_complete_audit(audit, tmp_path):
    from test_ics_streams import document
    raw = (document('first', offset='+0100') + document('second', offset='+0300')).encode()
    result = audit.audit_file(source(tmp_path, raw), 'stream.ics')
    assert result['outcome'] == 'pass' and result['events'] == 2


def test_strict_expectations_require_all_declared_inputs(audit, tmp_path, capsys):
    path = source(tmp_path)
    expected = tmp_path / 'expected.json'
    expected.write_text(json.dumps({'cases': {'missing.ics': {'outcome': 'rejected', 'classification': 'unsupported'}}}))
    with pytest.raises(SystemExit) as error:
        audit.main([str(path), '--expectations', str(expected)])
    assert error.value.code == 2
    assert 'Could not read corpus inputs' in capsys.readouterr().err


def test_cli_strict_and_report_only_classification_are_reproducible(audit, tmp_path, capsys):
    path = source(tmp_path, b'BEGIN:VEVENT\r\nEND:VEVENT\r\n')
    assert audit.main([str(path)]) == 1
    strict = json.loads(capsys.readouterr().out)
    assert strict['summary']['unexpected'] == 1
    assert audit.main([str(path), '--report-only']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['summary']['classifications'] == {'unclassified': 1}
    expected = tmp_path / 'expected.json'
    expected.write_text(json.dumps({'cases': {'sample.ics': {'outcome': 'incomplete', 'classification': 'fixture'}}}))
    output = tmp_path / 'report.json'
    assert audit.main([str(path), '--expectations', str(expected), '--output', str(output)]) == 0
    assert json.loads(output.read_text())['summary']['classifications'] == {'fixture': 1}


def test_public_source_manifest_contains_only_pinned_provenance():
    value = json.loads((SCRIPT.parents[1] / 'tests/interop_sources.json').read_text())
    records = [record for record in value['sources'] if record['project'] == 'ICAL.js']
    assert len(records) == 8
    for record in records:
        assert len(record['git_blob_sha1']) == 40 and len(record['sha256']) == 64
        assert record['url'].endswith('/git/blobs/' + record['git_blob_sha1'])
        assert record['license'] == 'MPL-2.0' and record['path'].startswith('samples/')
