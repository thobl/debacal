"""Reproducible offscreen interaction benchmark using disposable calendar data.

Run with .venv/bin/python scripts/benchmark_ui.py --events 10000 --series 200.
Use --source-root to compare a different checkout with the same workload.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from zoneinfo import ZoneInfo

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=10000)
    parser.add_argument('--series', type=int, default=200)
    parser.add_argument('--iterations', type=int, default=3)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path)
    args = parser.parse_args()
    if args.events < 0 or args.series < 0 or args.iterations < 1:
        parser.error('Event counts must be nonnegative and iterations must be positive.')
    sys.path.insert(0, str(args.source_root))

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from debacal.models import Event, Calendar
    from debacal.store import Store
    from debacal.ui import MainWindow

    start = datetime(2026, 11, 4, 9, tzinfo=ZoneInfo('Europe/Berlin'))
    app = QApplication([])
    app.setStyle('Fusion')
    store = Store(':memory:')
    for index in range(3):
        store.save_calendar(Calendar(f'cal-{index}', f'Synthetic {index}', '#587c67', kind='caldav',
                                     url=f'https://benchmark.invalid/calendar-{index}/'))
    events = []
    for index in range(args.events):
        when = start + timedelta(days=index % 365 - 180, minutes=(index % 10) * 40)
        events.append(Event(f'ordinary-{index}', f'cal-{index % 3}', f'Meeting {index}',
                            when, when + timedelta(minutes=30)))
    for index in range(args.series):
        when = start.replace(year=2018) + timedelta(minutes=index % 12 * 30)
        events.append(Event(f'repeating-{index}', f'cal-{index % 3}', f'Weekly meeting {index}',
                            when, when + timedelta(minutes=45), recurrence_rule='FREQ=WEEKLY'))
    candidate_start = start.replace(hour=20)
    candidate = Event('choices', 'cal-0', 'Choose a slot', candidate_start, candidate_start + timedelta(hours=1),
                       recurrence_dates=tuple((candidate_start + timedelta(weeks=offset)).isoformat() for offset in range(1, 500)),
                       candidate=True)
    events.append(candidate)
    store.apply_remote(events)
    # Include pending edits, without making the entire import an unsynced outbox.
    store.apply('Synthetic pending edits', [replace(event, description='Pending local edit')
                                            for event in events[:min(200, len(events))]])

    def settle():
        for _ in range(3):
            app.processEvents()

    timings = {}
    def measure(name, action):
        begin = perf_counter()
        action()
        settle()
        timings.setdefault(name, []).append(round((perf_counter() - begin) * 1000, 3))

    def finish_search():
        begin = perf_counter()
        while window.search_timer.isActive():
            settle()
            QTest.qWait(1)
            if perf_counter() - begin > 10:
                raise RuntimeError('Search did not finish within 10 seconds.')

    profile = None
    if args.profile:
        import cProfile
        profile = cProfile.Profile()
        profile.enable()
    begin = perf_counter()
    window = MainWindow(store, enable_sync=False)
    window.go_date(start.date())
    window.show()
    settle()
    timings['open_workspace'] = [round((perf_counter() - begin) * 1000, 3)]
    for _ in range(args.iterations):
        measure('month_view', lambda: window.set_view('month'))
        measure('week_view', lambda: window.set_view('week'))
        measure('hide_calendar', lambda: window.toggle_calendar('cal-1', False))
        measure('show_calendar', lambda: window.toggle_calendar('cal-1', True))
        window.focus_search()
        measure('type_search', lambda: QTest.keyClicks(window.search, 'meeting 123'))
        # A debounced search still has to finish its work, not merely defer it.
        if hasattr(window, 'flush_search'):
            measure('finish_search', finish_search)
        measure('clear_search', window.exit_search)

    chosen = next(event for event in window.visible_events if event.candidate)
    window.select_event(chosen.id)
    settle()
    hit = next(rect for rect, event_id in window.canvas.confirm_hits if event_id == chosen.id)
    begin = perf_counter()
    QTest.mouseClick(window.canvas, Qt.MouseButton.LeftButton, pos=hit.center().toPoint())
    while store.get_event('choices').candidate:
        settle()
        QTest.qWait(1)
        if perf_counter() - begin > 30:
            raise RuntimeError('Slot confirmation did not finish within 30 seconds.')
    settle()
    timings['confirm_slot'] = [round((perf_counter() - begin) * 1000, 3)]
    if profile:
        profile.disable()
        profile.dump_stats(str(args.profile))
    report = {'ordinary_events': args.events, 'weekly_series': args.series,
              'candidate_dates': 500, 'milliseconds': timings}
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + '\n')
    window.close()
    app.processEvents()
    store.close()


if __name__ == '__main__':
    main()
