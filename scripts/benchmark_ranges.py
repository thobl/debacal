"""Measure first-visit range queries against disposable, file-backed calendars.

Every timed request has a different interval. Each worker opens the database in
a new process, reports preparation separately, and times both source selection
and the complete Store query. There is no warm-up and no average/median score.
The fixture contains only generated data; no user profile or sync folder is read.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import random
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)
TIMEZONE = ('BEGIN:VTIMEZONE\r\nTZID:Benchmark/UTC\r\nBEGIN:STANDARD\r\n'
            'DTSTART:19700101T000000\r\nTZOFFSETFROM:+0000\r\nTZOFFSETTO:+0000\r\n'
            'TZNAME:UTC\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n')


@dataclass(frozen=True)
class Fixture:
    ordinary: int = 6000
    series: int = 1200
    finite: int = 120
    calendars: int = 6
    payload_bytes: int = 2048
    irrelevant: int = 0


def generated_events(config: Fixture):
    """A mix of historical/future rules, active rules and finite irregular sets."""
    from debacal.models import Event
    def make(identifier, index, first, duration, **changes):
        title = f'Synthetic {identifier}'
        raw = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Debacal benchmark//EN\r\n'
               + (TIMEZONE if index % 8 == 0 else '') + 'BEGIN:VEVENT\r\nUID:'
               + identifier + '\r\nSUMMARY:' + title + '\r\nDTSTART:'
               + first.strftime('%Y%m%dT%H%M%SZ') + '\r\nDTEND:'
               + (first + duration).strftime('%Y%m%dT%H%M%SZ')
               + '\r\nDESCRIPTION:' + 'synthetic ' * (config.payload_bytes // 10)
               + '\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        zone = {'timezone_id': 'Benchmark/UTC', 'timezone_definitions': {'Benchmark/UTC': TIMEZONE}} if index % 8 == 0 else {}
        return Event(identifier, f'cal-{index % config.calendars}', title, first,
                     first + duration, raw_ics=raw, **zone, **changes)

    for index in range(config.ordinary):
        first = ANCHOR + timedelta(days=(index * 37) % 5844 - 2922,
                                   hours=8 + index % 9, minutes=index % 12 * 5)
        all_day = index % 17 == 0
        if all_day:
            first = first.replace(hour=0, minute=0)
        yield make(f'ordinary-{index}', index, first,
                   timedelta(days=2) if all_day else timedelta(minutes=45), all_day=all_day)
    for index in range(config.series):
        category = index % 10
        if category < 7:
            first = datetime(1995 + index % 20, 1, 1, 9, tzinfo=UTC) + timedelta(days=index % 28)
            rule = 'FREQ=WEEKLY;COUNT=8'
        elif category < 9:
            first = datetime(2045 + index % 10, 1, 1, 9, tzinfo=UTC) + timedelta(days=index % 28)
            rule = 'FREQ=MONTHLY;COUNT=6'
        else:
            first = datetime(2010, 1, 1, 9 + index % 7, tzinfo=UTC) + timedelta(days=index % 28)
            rule = 'FREQ=MONTHLY' if index % 3 == 0 else 'FREQ=WEEKLY'
        changes = {}
        if category == 9 and index % 2:
            # Exceptions include an old detached item, an in-range item, and
            # another occurrence moved outside the nominal series envelope.
            nominal = first + timedelta(weeks=2)
            moved = ANCHOR + timedelta(days=index % 365, hours=10)
            changes = {'excluded_dates': ((first + timedelta(weeks=1)).isoformat(),),
                       'recurrence_overrides': {nominal.isoformat(): {
                           'id': f'override-{index}', 'calendar_id': f'cal-{index % config.calendars}',
                           'title': 'Synthetic detached event', 'start': moved.isoformat(),
                           'end': (moved + timedelta(minutes=45)).isoformat()}}}
        yield make(f'rule-{index}', index, first, timedelta(minutes=45), recurrence_rule=rule, **changes)
    for index in range(config.finite):
        first = ANCHOR + timedelta(days=index % 365 - 100, hours=11)
        dates = tuple((first + timedelta(days=offset * offset + offset * 3)).isoformat()
                      for offset in range(1, 33))
        yield make(f'finite-{index}', index, first, timedelta(hours=1), recurrence_dates=dates)
    for index in range(config.irrelevant):
        # Scaling this count leaves the query answers/relevant sources fixed.
        year = 1995 + index % 20 if index % 2 else 2045 + index % 10
        first = datetime(year, 1, 1, 9, tzinfo=UTC) + timedelta(days=index % 28)
        yield make(f'irrelevant-{index}', index, first, timedelta(hours=1), recurrence_rule='FREQ=DAILY;COUNT=4')


def requests(count: int, calendars: int, seed: int = 81423):
    """Unique, non-monotonic windows; neither query order nor repeats can help."""
    rng = random.Random(seed)
    days = rng.sample(range(-730, 1461), count)
    result = []
    for index, day in enumerate(days):
        first = ANCHOR + timedelta(days=day)
        width = (1, 7, 42)[index % 3]
        chosen = [f'cal-{n}' for n in range(calendars) if (n + index) % 3 != 0]
        if not chosen:
            chosen = ['cal-0']
        result.append({'kind': ('day', 'week', 'month')[index % 3],
                       'start': first.isoformat(), 'end': (first + timedelta(days=width)).isoformat(),
                       'calendars': chosen})
    return result


def build_database(path: Path, config: Fixture):
    from debacal.models import Calendar
    from debacal.store import Store
    with Store(path) as store:
        for index in range(config.calendars):
            store.save_calendar(Calendar(f'cal-{index}', f'Synthetic {index}', '#587c67'))
        events = list(generated_events(config))
        begin = perf_counter()
        store.apply_remote(events)
        return (perf_counter() - begin) * 1000


def expected_rows(events, request):
    """Independent dateutil/reference overlap oracle, outside timed regions."""
    from dateutil.rrule import rrulestr
    first, end = map(datetime.fromisoformat, (request['start'], request['end']))
    selected = set(request['calendars'])
    result = []
    for event in events:
        if event.calendar_id not in selected:
            continue
        duration = event.end - event.start
        starts = {event.start, *(datetime.fromisoformat(value) for value in event.recurrence_dates)}
        if event.recurrence_rule:
            starts.update(rrulestr(event.recurrence_rule, dtstart=event.start).between(first - duration, end, inc=True))
        excluded = {datetime.fromisoformat(value) for value in event.excluded_dates}
        overrides = {datetime.fromisoformat(key): value for key, value in event.recurrence_overrides.items()}
        intervals = [(value, value + duration) for value in starts if value not in excluded and value not in overrides]
        intervals.extend((datetime.fromisoformat(value['start']), datetime.fromisoformat(value['end']))
                         for key, value in overrides.items() if key not in excluded)
        for start, stop in intervals:
            overlaps = (start.date() < end.date() and stop.date() > first.date()) if event.all_day else (
                start < end and (stop > first or stop == start and stop == first))
            if overlaps:
                result.append((event.id, start.astimezone(UTC).isoformat(), stop.astimezone(UTC).isoformat()))
    return sorted(result)


def result_rows(events):
    return sorted((event.series_id or event.id, event.start.astimezone(UTC).isoformat(),
                   event.end.astimezone(UTC).isoformat()) for event in events)


def worker(source_root: Path, database: Path, request_file: Path, phase: str):
    sys.path.insert(0, str(source_root))
    from debacal.models import Event
    from debacal.store import Store
    import debacal.recurrence as recurrence
    try:
        from debacal.prepared_recurrence import PreparedSeries
    except ModuleNotFoundError as error:
        if error.name != 'debacal.prepared_recurrence':
            raise
        PreparedSeries = None
    if not Path(recurrence.__file__).resolve().is_relative_to(source_root):
        raise RuntimeError('The requested source checkout was not imported.')
    config = json.loads(request_file.read_text())
    begin = perf_counter()
    store = Store(database)
    preparation_ms = (perf_counter() - begin) * 1000
    records = []
    try:
        for request in config:
            first, end = map(datetime.fromisoformat, (request['start'], request['end']))
            decoded = expanded = prepared_expanded = decoded_ics_bytes = 0
            decode, expand = Event.from_dict, recurrence.expand_series
            prepared_query = PreparedSeries.query_rule if PreparedSeries is not None else None

            def observe_decode(_cls, value):
                nonlocal decoded, decoded_ics_bytes
                decoded += 1
                decoded_ics_bytes += len(value.get('raw_ics', ''))
                return decode(value)

            def observe_expand(*args, **kwargs):
                nonlocal expanded
                expanded += 1
                return expand(*args, **kwargs)

            def observe_prepared(instance, *args, **kwargs):
                nonlocal prepared_expanded
                prepared_expanded += 1
                return prepared_query(instance, *args, **kwargs)

            with ExitStack() as stack:
                stack.enter_context(patch.object(Event, 'from_dict', classmethod(observe_decode)))
                stack.enter_context(patch.object(recurrence, 'expand_series', observe_expand))
                if PreparedSeries is not None:
                    stack.enter_context(patch.object(PreparedSeries, 'query_rule', observe_prepared))
                begin = perf_counter()
                if phase == 'sources':
                    ordinary, masters = store._occurrence_sources(first, end, request['calendars'])
                    value = {'ordinary_sources': len(ordinary), 'recurring_sources': len(masters)}
                else:
                    found = store.query_occurrences(first, end, request['calendars'])
                elapsed = (perf_counter() - begin) * 1000
            if phase == 'query':
                value = {'occurrences': len(found), 'rows': result_rows(found)}
            records.append({**request, **value, 'elapsed_ms': round(elapsed, 3),
                            'decoded_models': decoded, 'decoded_ics_bytes': decoded_ics_bytes,
                            'expanded_sources': expanded + prepared_expanded,
                            'legacy_expansions': expanded, 'prepared_expansions': prepared_expanded})
    finally:
        store.close()
    return {'phase': phase, 'preparation_ms': round(preparation_ms, 3), 'records': records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=6000)
    parser.add_argument('--series', type=int, default=1200)
    parser.add_argument('--finite', type=int, default=120)
    parser.add_argument('--irrelevant-series', type=int, default=0,
                        help='Additional ended/future rules; keeps relevant sources and answers unchanged.')
    parser.add_argument('--queries', type=int, default=24)
    parser.add_argument('--calendars', type=int, default=6)
    parser.add_argument('--payload-bytes', type=int, default=2048)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-query-ms', '--max-ms', dest='max_query_ms', type=float,
                        help='Fail if any isolated complete range query exceeds this limit.')
    parser.add_argument('--max-source-ms', type=float, help='Fail if any source-selection query exceeds this limit.')
    parser.add_argument('--max-prepare-ms', type=float, help='Fail if either Store/index preparation exceeds this limit.')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--worker', choices=('sources', 'query'), help=argparse.SUPPRESS)
    parser.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--requests', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    if args.worker:
        print(json.dumps(worker(source, args.database, args.requests, args.worker)))
        return 0
    if min(args.events, args.series, args.finite, args.payload_bytes, args.irrelevant_series) < 0 or not 1 <= args.queries <= 2191 or args.calendars < 1:
        parser.error('Counts must be nonnegative; choose 1–2191 queries and at least one calendar.')
    if args.timeout <= 0 or any(value is not None and value <= 0
                                for value in (args.max_query_ms, args.max_source_ms, args.max_prepare_ms)):
        parser.error('Timeout and latency limit must be positive.')
    sys.path.insert(0, str(source))
    config = Fixture(args.events, args.series, args.finite, args.calendars, args.payload_bytes, args.irrelevant_series)
    workload = requests(args.queries, args.calendars)
    # Build and reference generation happen outside both measured workers.
    with TemporaryDirectory(prefix='debacal-range-benchmark-') as directory:
        database, request_file = Path(directory) / 'calendar.sqlite3', Path(directory) / 'requests.json'
        fixture_write_ms = build_database(database, config)
        request_file.write_text(json.dumps(workload))
        reference_events = list(generated_events(config))
        expected = [expected_rows(reference_events, request) for request in workload]
        phases = []
        for phase in ('sources', 'query'):
            command = [sys.executable, str(Path(__file__).resolve()), '--source-root', str(source),
                       '--worker', phase, '--database', str(database), '--requests', str(request_file)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
            if result.returncode:
                raise RuntimeError(f'{phase} worker failed: {result.stderr.strip()}')
            phases.append(json.loads(result.stdout))
        for index, record in enumerate(phases[1]['records']):
            actual = [tuple(row) for row in record.pop('rows')]
            if actual != expected[index]:
                missing = set(expected[index]) - set(actual)
                extra = set(actual) - set(expected[index])
                raise AssertionError(f'Query {index} differs from reference: missing={len(missing)}, extra={len(extra)}')
        report = {'environment': {'python': platform.python_version(), 'storage': 'file-backed SQLite',
                                  'fresh_process_per_phase': True, 'warmup_queries': 0,
                                  'physical_disk_cache_flushed': False, 'instrumented': True},
                  'fixture': config.__dict__, 'fixture_write_ms': round(fixture_write_ms, 3),
                  'request_count': len(workload),
                  'summary': {phase['phase']: {'max_ms': max(record['elapsed_ms'] for record in phase['records']),
                                             'preparation_ms': phase['preparation_ms'],
                                             'max_decoded_models': max(record['decoded_models'] for record in phase['records']),
                                             'max_expanded_sources': max(record['expanded_sources'] for record in phase['records'])}
                              for phase in phases},
                  'phases': phases}
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n')
        print(json.dumps({'output': str(args.output), 'summary': report['summary']}, indent=2))
    else:
        print(rendered)
    return int(args.max_query_ms is not None and report['summary']['query']['max_ms'] > args.max_query_ms
               or args.max_source_ms is not None and report['summary']['sources']['max_ms'] > args.max_source_ms
               or args.max_prepare_ms is not None and any(phase['preparation_ms'] > args.max_prepare_ms for phase in phases))


if __name__ == '__main__':
    raise SystemExit(main())
