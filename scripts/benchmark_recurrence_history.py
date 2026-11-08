"""Compare bounded historical queries against any source checkout, without accounts.

Each synthetic case runs in its own process with a timeout. Point --source-root
at another checkout for a controlled comparison using the same interpreter and
dependencies. A timeout is reported explicitly; results are never truncated.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time


CASES = ('hourly', 'dense_daily')


def worker(source_root, case, repeats):
    sys.path.insert(0, str(source_root))
    from debacal.models import Event
    from debacal.recurrence import expand_event
    import debacal.recurrence
    from dateutil.rrule import rrule
    from unittest.mock import patch
    if not Path(debacal.recurrence.__file__).resolve().is_relative_to(source_root):
        raise RuntimeError('The requested source checkout was not imported.')
    first = datetime(2000, 1, 1, 9, tzinfo=timezone.utc)
    begin = datetime(2026, 11, 4, tzinfo=timezone.utc)
    if case == 'dense_daily':
        rule = ('FREQ=DAILY;BYHOUR=' + ','.join(map(str, range(24)))
                + ';BYMINUTE=' + ','.join(map(str, range(60))))
        duration, count = timedelta(minutes=1), 1440
    else:
        rule, duration, count = 'FREQ=HOURLY', timedelta(hours=1), 24
    event = Event('synthetic-history', 'local', 'Synthetic historical query', first,
                  first + duration, recurrence_rule=rule)
    expected = [begin + duration * index for index in range(count)]

    def query():
        actual = expand_event(event, begin, begin + timedelta(days=1))
        if [item.start for item in actual] != expected:
            raise AssertionError('The complete query does not match its independently expected dates.')

    elapsed = []
    for _ in range(repeats):
        started = time.perf_counter()
        query()
        elapsed.append((time.perf_counter() - started) * 1000)
    produced, original = 0, rrule._iter

    def observed(rule):
        nonlocal produced
        for value in original(rule):
            produced += 1
            yield value

    with patch.object(rrule, '_iter', observed):
        query()
    return {'case': case, 'status': 'ok', 'events': count,
            'median_ms': round(statistics.median(elapsed), 3),
            'min_ms': round(min(elapsed), 3), 'max_ms': round(max(elapsed), 3),
            'iterator_candidates': produced}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--worker', choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    if not (source_root / 'debacal/recurrence.py').is_file():
        parser.error('--source-root must contain debacal/recurrence.py')
    if args.timeout <= 0 or not 1 <= args.repeats <= 20:
        parser.error('Choose a positive timeout and between 1 and 20 repeats.')
    if args.worker:
        print(json.dumps(worker(source_root, args.worker, args.repeats)))
        return 0
    results = []
    for case in CASES:
        command = [sys.executable, str(Path(__file__).resolve()), '--source-root', str(source_root),
                   '--worker', case, '--repeats', str(args.repeats)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            results.append({'case': case, 'status': 'timeout', 'timeout_seconds': args.timeout})
            continue
        if result.returncode:
            results.append({'case': case, 'status': 'error', 'details': result.stderr.strip()})
        else:
            results.append(json.loads(result.stdout))
    print(json.dumps({'python': sys.version.split()[0], 'repeats': args.repeats,
                      'cases': results}, indent=2))
    return int(any(item['status'] == 'error' for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
