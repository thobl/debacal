"""Audit local ICS files without downloading or exposing their event contents.

Examples (run from the checkout root):
    .venv/bin/python scripts/check_calendar_interop.py calendars/ --report-only
    .venv/bin/python scripts/check_calendar_interop.py --installed-icalendar --report-only
    .venv/bin/python scripts/check_calendar_interop.py calendars/ --expectations expected.json

Expectations have the shape {"cases": {"calendars/sample.ics": {
    "outcome": "rejected", "classification": "invalid"}}}.
Classifications describe why an input is excluded from the supported-event
denominator: invalid, unsupported, non_event, or fixture. Partial acceptance is
reported explicitly. Report-only permits unclassified inputs; actual data loss,
unexpected exceptions and timeouts still fail. Reports never contain event
titles, UIDs, calendar text or exception messages. Network access is never used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import warnings


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT = 60.0
INPUT_OUTCOMES = {'rejected', 'partial', 'non_event', 'incomplete', 'oversized', 'blocked_upload'}
CLASSIFICATIONS = {'invalid', 'unsupported', 'non_event', 'fixture'}
# The model controls these fields, so its semantic comparison covers them.
# Everything else, including unknown extensions and complete VALARMs, must
# survive editing independently of the model's represented fields.
MODELED_PROPERTIES = {
    'UID', 'RECURRENCE-ID', 'DTSTART', 'DTEND', 'DURATION', 'DTSTAMP',
    'SUMMARY', 'DESCRIPTION', 'LOCATION', 'RRULE', 'RDATE', 'EXDATE',
}
# RFC 5545 optional singleton metadata may be safely deduplicated at storage
# boundaries. Distinct or repeatable metadata must never disappear.
OPTIONAL_SINGLETONS = {'CLASS', 'CREATED', 'COLOR', 'GEO', 'LAST-MODIFIED', 'ORGANIZER',
                       'PRIORITY', 'SEQUENCE', 'STATUS', 'TRANSP', 'URL'}


class AuditTimeout(TimeoutError):
    pass


@contextmanager
def time_budget(seconds):
    """Bound each case, including parser/recurrence work, on supported Linux."""
    if not hasattr(signal, 'setitimer'):
        raise RuntimeError('This audit requires POSIX interval timers.')
    previous_handler = signal.getsignal(signal.SIGALRM)
    def expired(_number, _frame):
        raise AuditTimeout()
    signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


@contextmanager
def private_output():
    """Third-party warnings and logs may quote source calendar properties."""
    disabled = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink), warnings.catch_warnings():
            warnings.simplefilter('ignore')
            yield
    finally:
        logging.disable(disabled)


def metadata_fingerprint(data, *, storage=False):
    """Retain non-modeled event properties without putting them in reports."""
    from debacal.ics_fallback import content_line, unfolded, folded
    from debacal.ics_resources import _blocks
    from debacal.ics_stream import calendar_documents
    lines = unfolded(data)
    if sum(line.upper() == 'BEGIN:VCALENDAR' for line in lines) > 1:
        retained = Counter()
        for document in calendar_documents(lines):
            retained.update(metadata_fingerprint(folded(document), storage=storage))
        return retained
    retained = Counter()
    for block in _blocks(data)[1]:
        if block[0] != 'BEGIN:VEVENT':
            continue
        stack, singletons = [], set()
        for line in block:
            if not line:
                continue
            name, _parameters, value = content_line(line)
            if name == 'BEGIN':
                stack.append(value.upper())
            elif name == 'END':
                stack.pop()
            elif len(stack) > 1 or name not in MODELED_PROPERTIES and not name.startswith('X-DEBACAL-'):
                if storage and len(stack) == 1 and name in OPTIONAL_SINGLETONS:
                    if line in singletons:
                        continue
                    singletons.add(line)
                retained[(tuple(stack), line)] += 1
    return retained


def metadata_for_events(events):
    retained = Counter()
    for event in events:
        retained.update(metadata_fingerprint(event.raw_ics))
    return retained


def same_events(expected, actual):
    from debacal.vdirsyncer_backend import semantic
    if len(expected) != len(actual):
        return False
    expected_by_uid = {event.ical_uid: event for event in expected}
    actual_by_uid = {event.ical_uid: event for event in actual}
    return (len(expected_by_uid) == len(expected) and expected_by_uid.keys() == actual_by_uid.keys()
            and all(semantic(event) == semantic(actual_by_uid[uid])
                    for uid, event in expected_by_uid.items()))


def audit_file(path, source, *, max_bytes=DEFAULT_MAX_BYTES, timeout=DEFAULT_TIMEOUT, sabre_autoload=None):
    """Return a bounded, content-free result with the precise failing stage."""
    from debacal.ics import export_ics, normalize_caldav_ics
    from debacal.ics_fallback import CalendarValueError
    from debacal.ics_resources import parse_report
    from debacal.models import Calendar
    from debacal.store import Store

    result = {'source': source, 'stage': 'read'}
    try:
        with time_budget(timeout), private_output():
            with path.open('rb') as stream:
                raw = stream.read(max_bytes + 1)
            result['bytes_read'] = len(raw)
            if len(raw) > max_bytes:
                return dict(result, outcome='oversized')
            result['sha256'] = hashlib.sha256(raw).hexdigest()
            upper = raw.upper()
            if b'BEGIN:VCALENDAR' not in upper or b'END:VCALENDAR' not in upper:
                return dict(result, outcome='incomplete')
            result['stage'] = 'import'
            try:
                report = parse_report(raw, 'interop-audit', repair_missing_uids=True)
            except ValueError:
                return dict(result, outcome='rejected', error_type='ValueError')
            result.update(events=len(report.events), rejected_resources=len(report.rejected),
                          import_warnings=len(report.issues))
            if not report.events:
                return dict(result, outcome='rejected' if report.rejected or report.issues else 'non_event')

            result['stage'] = 'persistence'
            with Store(':memory:') as store:
                store.save_calendar(Calendar('interop-audit', 'Synthetic audit', '#345678'))
                store.import_report(report, calendar_id='interop-audit')
                persisted = store.list_events(calendar_ids=['interop-audit'])
                if not same_events(report.events, persisted):
                    return dict(result, outcome='data_loss', check='event_semantics')
                before_metadata = metadata_for_events(report.events)
                if before_metadata != metadata_for_events(persisted):
                    return dict(result, outcome='data_loss', check='retained_metadata')

                result['stage'] = 'edit'
                edited = [replace(event, title='Edited interoperability test event') for event in persisted]
                store.apply('Edit audit titles', edited)
                persisted = store.list_events(calendar_ids=['interop-audit'])
                if not same_events(edited, persisted):
                    return dict(result, outcome='data_loss', check='event_semantics')

                caldav_payloads = {}
                for stage, normalizer in [('single_export', lambda raw: raw),
                                           ('caldav_export', normalize_caldav_ics)]:
                    result['stage'] = stage
                    for event in persisted:
                        try:
                            data = normalizer(export_ics([event]))
                        except CalendarValueError:
                            if stage != 'caldav_export':
                                raise
                            return dict(result, outcome='blocked_upload', error_type='CalendarValueError')
                        restored = parse_report(data, 'interop-audit')
                        if restored.rejected or not same_events([event], restored.events):
                            return dict(result, outcome='data_loss', check='event_semantics')
                        if metadata_fingerprint(event.raw_ics, storage=stage == 'caldav_export') != metadata_fingerprint(data):
                            return dict(result, outcome='data_loss', check='retained_metadata')
                        if stage == 'caldav_export' and sabre_autoload is not None:
                            caldav_payloads['item-' + str(len(caldav_payloads))] = data.decode('utf-8')

                if sabre_autoload is not None:
                    result['stage'] = 'caldav_validation'
                    validation = subprocess.run(
                        ['php', str(ROOT / 'scripts/validate_caldav.php'), str(sabre_autoload)],
                        input=json.dumps(caldav_payloads), capture_output=True, text=True,
                        timeout=timeout, check=False)
                    if validation.returncode:
                        return dict(result, outcome='error', error_type='ServerValidatorError')
                    findings = json.loads(validation.stdout)
                    if not isinstance(findings, dict) or set(findings) != set(caldav_payloads):
                        return dict(result, outcome='error', error_type='ServerValidatorError')
                    errors, notices, failures = 0, 0, 0
                    for item in findings.values():
                        if (not isinstance(item, dict) or type(item.get('errors')) is not int
                                or type(item.get('warnings')) is not int or type(item.get('exception')) is not bool
                                or item['errors'] < 0 or item['warnings'] < 0):
                            return dict(result, outcome='error', error_type='ServerValidatorError')
                        errors += item['errors']
                        notices += item['warnings']
                        failures += item['exception']
                    result.update(server_errors=errors, server_warnings=notices, server_exceptions=failures)
                    if errors or failures:
                        return dict(result, outcome='server_rejected')

                result['stage'] = 'combined_export'
                data = export_ics(persisted)
                restored = parse_report(data, 'interop-audit')
                if restored.rejected or not same_events(persisted, restored.events):
                    return dict(result, outcome='data_loss', check='event_semantics')
                if before_metadata != metadata_fingerprint(data):
                    return dict(result, outcome='data_loss', check='retained_metadata')
            return dict(result, outcome='partial' if report.rejected else 'pass', stage='complete')
    except AuditTimeout:
        return dict(result, outcome='timeout')
    except subprocess.TimeoutExpired:
        return dict(result, outcome='timeout')
    except Exception as error:
        # Calendar exceptions often include raw values. Class names alone
        # describe the kind of failure without leaking titles, UIDs or text.
        return dict(result, outcome='error', error_type=type(error).__name__)


def sources(paths, *, installed_icalendar=False):
    """Use relative labels and never follow corpus symlinks outside a root."""
    roots = [(Path(path), None) for path in paths]
    if installed_icalendar:
        import icalendar
        roots.append((Path(icalendar.__file__).parent / 'tests', 'icalendar-' + icalendar.__version__))
    gathered = {}
    for path, label in roots:
        if path.is_symlink() or not path.exists():
            raise ValueError('An input is missing or is a symbolic link.')
        if path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.suffix.lower() != '.ics' or not child.is_file() or child.is_symlink():
                    continue
                relative = child.relative_to(path)
                if any(parent.is_symlink() for parent in child.parents if parent != path and parent.is_relative_to(path)):
                    continue
                key = (Path(label or path.name) / relative).as_posix()
                if key in gathered:
                    raise ValueError('Two inputs have the same relative source label.')
                gathered[key] = child
        elif path.is_file():
            if path.name in gathered:
                raise ValueError('Two inputs have the same relative source label.')
            gathered[path.name] = path
        else:
            raise ValueError('An input is not a regular file or directory.')
    return gathered


def classify(result, expectations, *, report_only=False):
    result = dict(result)
    expected = expectations.get(result['source'])
    if result['outcome'] in {'error', 'timeout', 'data_loss', 'server_rejected'}:
        return dict(result, classification='failure', unexpected=True)
    if expected is not None:
        matches = result['outcome'] == expected['outcome']
        return dict(result, classification=expected.get('classification', 'supported'), unexpected=not matches,
                    expected_outcome=expected['outcome'])
    if result['outcome'] == 'pass':
        return dict(result, classification='supported', unexpected=False)
    if result['outcome'] == 'non_event':
        return dict(result, classification='non_event', unexpected=False)
    return dict(result, classification='unclassified', unexpected=not report_only)


def read_expectations(path):
    if path is None:
        return {}
    # A hand-maintained classification file must not become an unbounded input.
    with path.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Expectations exceed the 1 MiB limit.')
    value = json.loads(raw)
    cases = value['cases']
    if not isinstance(cases, dict):
        raise ValueError('Expectations need a cases mapping.')
    for label, expected in cases.items():
        if (not isinstance(label, str) or not isinstance(expected, dict)
                or expected.get('outcome') not in INPUT_OUTCOMES | {'pass'}
                or expected.get('classification', 'supported') not in CLASSIFICATIONS | {'supported'}):
            raise ValueError('An expected classification is invalid.')
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='*', type=Path)
    parser.add_argument('--installed-icalendar', action='store_true', help='Include fixtures shipped with installed icalendar')
    parser.add_argument('--report-only', action='store_true', help='Permit unclassified inputs; data loss, crashes and timeouts still fail')
    parser.add_argument('--expectations', type=Path, help='JSON case outcomes and invalid/unsupported/fixture classifications')
    parser.add_argument('--sabre-autoload', type=Path, help='Optionally validate uploads using Sabre VObject via a Composer vendor/autoload.php')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help='Maximum seconds per file (default: 60)')
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES, help='Maximum source file size (default: 20 MiB)')
    parser.add_argument('--output', type=Path, help='Write the JSON report here instead of standard output')
    args = parser.parse_args(argv)
    if not args.paths and not args.installed_icalendar:
        parser.error('Provide a corpus path or --installed-icalendar.')
    if not 0 < args.timeout <= 3600 or not 0 < args.max_bytes <= 1024 * 1024 * 1024:
        parser.error('Use a positive timeout of at most 3600 seconds and size of at most 1 GiB.')
    try:
        expected = read_expectations(args.expectations)
        files = sources(args.paths, installed_icalendar=args.installed_icalendar)
        if set(expected) - set(files):
            raise ValueError('Expected corpus inputs are missing.')
        if args.sabre_autoload is not None and not args.sabre_autoload.is_file():
            raise ValueError('Missing validator autoloader.')
    except (OSError, ValueError, KeyError, TypeError):
        parser.error('Could not read corpus inputs or expectations; check their paths and JSON structure.')
    if not files:
        parser.error('No ICS files were found.')
    results = [classify(audit_file(path, label, max_bytes=args.max_bytes, timeout=args.timeout,
                                  sabre_autoload=args.sabre_autoload), expected,
                        report_only=args.report_only) for label, path in files.items()]
    from importlib.metadata import version
    import debacal
    code_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(Path(debacal.__file__).parent.glob('*.py'))}
    value = {'schema_version': 1,
             'runtime': {'python': platform.python_version(), 'debacal': debacal.__version__,
                         'icalendar': version('icalendar'), 'python_dateutil': version('python-dateutil')},
             'source_hashes': code_hashes,
             'limits': {'max_bytes': args.max_bytes, 'timeout_seconds': args.timeout},
             'summary': {'files': len(results), 'events': sum(item.get('events', 0) for item in results),
                         'outcomes': dict(Counter(item['outcome'] for item in results)),
                         'classifications': dict(Counter(item['classification'] for item in results)),
                         'unexpected': sum(item['unexpected'] for item in results)},
             'results': results}
    encoded = json.dumps(value, indent=2) + '\n'
    if args.output:
        try:
            args.output.write_text(encoded, encoding='utf-8')
        except OSError:
            parser.error('Could not write the audit report.')
    else:
        print(encoded, end='')
    return 1 if value['summary']['unexpected'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
