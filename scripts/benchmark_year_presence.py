"""Local-only first-visit annual presence queries, independently of painting.

Use the exact synthetic fixture from benchmark_extended. Each source checkout
builds its own database, then fresh workers query distinct, randomly ordered
years without warm-up. All scored queries finish before full Event queries
validate the daily/calendar counters, counts and per-source issues. Older
versions time their equivalent full-query plus difference-counter summary.

Example: python scripts/benchmark_year_presence.py --output /tmp/year.json
Use --source-root with the same seed/configuration for historical comparisons.
This benchmark is deliberately absent from release CI and needs no display.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import platform
import random
import select
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter

if __package__:
    from .benchmark_extended import Config, build_fixture, isolated_environment, source_metadata, summary, verify_source
else:
    from benchmark_extended import Config, build_fixture, isolated_environment, source_metadata, summary, verify_source


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def year_plan(seed=81423, years=None):
    """One deterministic non-monotonic plan, identical across source roots."""
    chosen = list(years) if years is not None else random.Random(seed).sample(range(2019, 2036), 7)
    if not chosen or len(set(chosen)) != len(chosen) or any(not 1 <= year < 9999 for year in chosen):
        raise ValueError('Choose distinct years between 1 and 9998.')
    if years is None and chosen in (sorted(chosen), sorted(chosen, reverse=True)):
        chosen[0], chosen[1] = chosen[1], chosen[0]
    return [{'year': year, 'start': datetime(year, 1, 1, tzinfo=UTC).isoformat(),
             'end': datetime(year + 1, 1, 1, tzinfo=UTC).isoformat()} for year in chosen]


def day_counts(events, start, end, *, zone=UTC):
    """Independent difference counters for half-open ranges and point events.

    DATE values use their civil dates. Timed ranges use the viewer's dates,
    including the last touched instant rather than the exclusive end date.
    No application recurrence/projection implementation is imported here.
    """
    first_ordinal = start.date().toordinal()
    days = (end.date() - start.date()).days + (end.time() != time.min)
    differences = {}
    for event in events:
        if event.all_day:
            first, last = event.start.date().toordinal(), event.end.date().toordinal()
            if first == last:
                last += 1
        else:
            first = event.start.astimezone(zone).date().toordinal()
            if event.start.timestamp() == event.end.timestamp():
                last = first + 1
            else:
                previous = (event.end.astimezone(UTC) - timedelta(microseconds=1)).astimezone(zone)
                last = previous.date().toordinal() + 1
        first, last = max(0, first - first_ordinal), min(days, last - first_ordinal)
        if first < last:
            counter = differences.setdefault(event.calendar_id, [0] * (days + 1))
            counter[first] += 1
            counter[last] -= 1
    result = [{} for _ in range(days)]
    for calendar, values in differences.items():
        count = 0
        for day in range(days):
            count += values[day]
            if count:
                result[day][calendar] = (count, '')
    return result


def issue_values(issues):
    """Compare the same per-source budget/error contract on either API."""
    return sorted((master.id, type(error).__name__, str(error)) for master, error in issues)


def query_presence(store, start, end, calendars):
    """Select the actual implementation without importing new baseline code."""
    issues = []
    query = getattr(store, 'query_year_presence', None)
    if callable(query):
        values = query(start, end, calendars, issues=issues)
        return values.summaries, len(values), issues, 'compact-ranges'
    values = store.query_occurrences(start, end, calendars, issues=issues)
    return day_counts(values, start, end), len(values), issues, 'full-events-and-counters'


def worker(source_root, database, config, plan, mode):
    sys.path.insert(0, str(source_root))
    imported = perf_counter()
    from debacal.store import Store
    verify_source(source_root)
    import_ms = (perf_counter() - imported) * 1000
    if mode == 'fixture':
        result = build_fixture(database, config)
        verify_source(source_root)
        print(json.dumps({'fixture': result}), flush=True)
        return
    started = perf_counter()
    store = Store(database)
    startup_ms = (perf_counter() - started) * 1000
    print(json.dumps({'ready': True, 'store_open_ms': startup_ms, 'application_import_ms': import_ms}), flush=True)
    records, measured = [], []
    calendars = [f'cal-{index}' for index in range(config.calendars)]
    try:
        for request in plan:
            start, end = datetime.fromisoformat(request['start']), datetime.fromisoformat(request['end'])
            started = perf_counter()
            counters, count, issues, implementation = query_presence(store, start, end, calendars)
            elapsed = (perf_counter() - started) * 1000
            records.append({'year': request['year'], 'elapsed_ms': elapsed, 'occurrences': count,
                            'implementation': implementation, 'issues': issue_values(issues)})
            measured.append((counters, count, issue_values(issues)))
        # Do not let oracle expansions warm any later scored year. Validation
        # intentionally happens only after the whole first-visit query plan.
        for request, actual in zip(plan, measured):
            start, end = datetime.fromisoformat(request['start']), datetime.fromisoformat(request['end'])
            issues = []
            events = store.query_occurrences(start, end, calendars, issues=issues)
            expected = day_counts(events, start, end), len(events), issue_values(issues)
            if actual != expected:
                raise AssertionError(f"Annual presence disagrees with full Event query for {request['year']}.")
        verify_source(source_root)
        print(json.dumps({'records': records, 'validated': True}), flush=True)
    finally:
        store.close()


def run_worker(source_root, database, config, plan, mode, directory):
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', mode,
               '--source-root', str(source_root), '--database', str(database),
               '--configuration', json.dumps(asdict(config)), '--plan', json.dumps(plan)]
    started = perf_counter()
    with tempfile.TemporaryFile(mode='w+t') as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True,
                                   stdin=subprocess.DEVNULL, env=isolated_environment(directory))
        try:
            ready, _, _ = select.select([process.stdout], [], [], config.timeout_seconds)
            if not ready:
                raise TimeoutError(f'{mode} worker did not report readiness.')
            first_line = process.stdout.readline()
            ready_ms = (perf_counter() - started) * 1000
            output, _ = process.communicate(timeout=config.timeout_seconds)
            errors.seek(0)
            error_text = errors.read()
            if process.returncode:
                raise RuntimeError(f'{mode} worker failed ({process.returncode}):\n{error_text}')
            initial = json.loads(first_line)
            if mode == 'fixture':
                return initial
            if not initial.get('ready'):
                raise RuntimeError('The query worker did not publish startup readiness.')
            result = json.loads(output)
            result.update(initial, process_to_query_ready_ms=ready_ms)
            return result
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--source-root', type=Path, default=ROOT)
    result.add_argument('--revision', default='')
    result.add_argument('--output', type=Path)
    result.add_argument('--samples', type=int, default=3)
    result.add_argument('--years', nargs='+', type=int)
    for name in ('events', 'series', 'finite', 'active_series', 'heavy_overrides',
                 'candidate_dates', 'calendars', 'payload_bytes', 'seed', 'timeout_seconds'):
        result.add_argument('--' + name.replace('_', '-'), type=int, default=getattr(Config(), name))
    result.add_argument('--worker', choices=('fixture', 'query'), help=argparse.SUPPRESS)
    result.add_argument('--database', type=Path, help=argparse.SUPPRESS)
    result.add_argument('--configuration', help=argparse.SUPPRESS)
    result.add_argument('--plan', help=argparse.SUPPRESS)
    return result


def configuration(arguments):
    names = ('events', 'series', 'finite', 'active_series', 'heavy_overrides',
             'candidate_dates', 'calendars', 'payload_bytes', 'seed', 'timeout_seconds')
    values = {name: getattr(arguments, name) for name in names}
    if arguments.samples < 1:
        raise ValueError('Samples must be positive.')
    if any(values[name] < 0 for name in ('events', 'series', 'finite', 'active_series', 'heavy_overrides', 'payload_bytes')):
        raise ValueError('Fixture sizes cannot be negative.')
    if values['calendars'] < 1 or values['candidate_dates'] < 1 or values['timeout_seconds'] < 1:
        raise ValueError('Calendar/count/timeout values must be positive.')
    return Config(**values)


def main(argv=None):
    arguments = parser().parse_args(argv)
    source_root = arguments.source_root.resolve()
    if arguments.worker:
        worker(source_root, arguments.database, Config(**json.loads(arguments.configuration)),
               json.loads(arguments.plan), arguments.worker)
        return
    try:
        config = configuration(arguments)
        plan = year_plan(config.seed, arguments.years)
        metadata = source_metadata(source_root, arguments.revision)
    except ValueError as error:
        parser().error(str(error))
    with tempfile.TemporaryDirectory(prefix='debacal-year-presence-') as temporary:
        directory = Path(temporary)
        database = directory / 'fixture.sqlite3'
        fixture = run_worker(source_root, database, config, plan, 'fixture', directory / 'build')
        samples = []
        for index in range(arguments.samples):
            sample_directory = directory / f'sample-{index}'
            sample_directory.mkdir()
            sample_database = sample_directory / 'calendar.sqlite3'
            shutil.copy2(database, sample_database)
            result = run_worker(source_root, sample_database, config, plan, 'query', sample_directory)
            result['sample'] = index
            samples.append(result)
        timings = [item['elapsed_ms'] for sample in samples for item in sample['records']]
        output = {'benchmark': 'isolated-year-presence', 'schema': 1, 'source': metadata,
                  'environment': {'python': sys.version, 'platform': platform.platform(), 'timezone': 'UTC'},
                  'configuration': asdict(config), 'fixture': fixture['fixture'], 'plan': plan,
                  'queries': summary(timings),
                  'by_year': {str(request['year']): summary([
                      item['elapsed_ms'] for sample in samples for item in sample['records']
                      if item['year'] == request['year']]) for request in plan},
                  'startup': {
                      'process_to_query_ready': summary([item['process_to_query_ready_ms'] for item in samples]),
                      'application_import': summary([item['application_import_ms'] for item in samples]),
                      'store_open': summary([item['store_open_ms'] for item in samples])},
                  'samples': samples}
        encoded = json.dumps(output, indent=2) + '\n'
        if arguments.output:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(encoded)
        else:
            sys.stdout.write(encoded)


if __name__ == '__main__':
    main()
