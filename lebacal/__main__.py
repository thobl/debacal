"""Launch with `python -m lebacal`; --demo never touches your calendar data."""
import argparse
from datetime import timedelta
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .calendar_widgets import at
from .models import Calendar, Event
from .store import Store
from .timeutil import now
from .ui import MainWindow


def seed_demo(store):
    calendars = [Calendar('work', 'Work & projects', '#587c67'),
                 Calendar('local', 'Personal', '#b6815c'),
                 Calendar('community', 'Community', '#7f79a5')]
    for cal in calendars:
        store.save_calendar(cal)
    week = now().date() - timedelta(days=now().weekday())
    data = [
        (0, 9, 90, 'Plan the week', 'work', 'A little clarity before the week begins'),
        (0, 11, 120, 'Deep work', 'work', 'Research & writing'),
        (0, 17, 60, 'A walk by the river', 'personal', ''),
        (1, 9, 180, 'Make something useful', 'work', 'Studio'),
        (1, 10, 60, 'Project check-in', 'community', 'Library · meeting room'),
        (1, 14, 90, 'Workshop · option A', 'community', 'Candidate time slot'),
        (2, 9, 120, 'Quiet morning', 'work', 'No meetings, just focus'),
        (2, 12, 60, 'Lunch with Alex', 'personal', 'The little café'),
        (2, 15, 90, 'Workshop · option B', 'community', 'Candidate time slot'),
        (3, 9, 90, 'Design & explore', 'work', 'Studio'),
        (3, 11, 60, 'Reading group', 'community', 'Chapter 04'),
        (3, 15, 120, 'Build, test, refine', 'work', ''),
        (4, 9, 120, 'Deep work', 'work', 'Finish one good thing'),
        (4, 13, 60, 'Weekly reflection', 'work', ''),
        (4, 18, 90, 'Dinner with friends', 'personal', ''),
        (5, 10, 120, 'Farmers’ market', 'personal', ''),
        (6, 10, 90, 'Coffee & a good book', 'personal', ''),
    ]
    events = []
    for i, (day, hour, duration, title, cal, location) in enumerate(data):
        if cal == 'personal':
            cal = 'local'
        start = at(week + timedelta(days=day), hour)
        candidate = 'option ' in title
        events.append(Event(str(i), cal, title, start, start + timedelta(minutes=duration),
                            location=location, group_id='workshop' if candidate else None, candidate=candidate))
    store.apply('Create demonstration calendar', events)


def main(argv=None):
    parser = argparse.ArgumentParser(description='A local-first Linux desktop calendar')
    parser.add_argument('--database', type=Path, help='Use a specific SQLite database')
    parser.add_argument('--demo', action='store_true', help='Use disposable sample data')
    parser.add_argument('--demo-conflict', action='store_true', help='Use disposable sample data with a sync conflict to review')
    parser.add_argument('--screenshot', type=Path, help='Save a workspace screenshot and exit')
    args = parser.parse_args(argv)
    args.demo = args.demo or args.demo_conflict
    app = QApplication(sys.argv[:1])
    app.setStyle('Fusion')
    palette = QPalette()
    for role, color in [
        (QPalette.ColorRole.Window, '#fafbf8'),
        (QPalette.ColorRole.WindowText, '#29382f'),
        (QPalette.ColorRole.Base, '#fafbf8'),
        (QPalette.ColorRole.AlternateBase, '#edf0e8'),
        (QPalette.ColorRole.Text, '#29382f'),
        (QPalette.ColorRole.Button, '#edf0e8'),
        (QPalette.ColorRole.ButtonText, '#29382f'),
        (QPalette.ColorRole.Highlight, '#496e50'),
        (QPalette.ColorRole.HighlightedText, '#ffffff'),
        (QPalette.ColorRole.ToolTipBase, '#fafbf8'),
        (QPalette.ColorRole.ToolTipText, '#29382f'),
    ]:
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setApplicationName('lebacal')
    app.setOrganizationName('lebacal')
    app.setApplicationDisplayName('Lebacal')
    temporary = tempfile.TemporaryDirectory(prefix='lebacal-demo-') if args.demo else None
    persistent_path = args.database or Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)) / 'calendar.sqlite3'
    path = Path(temporary.name) / 'demo.sqlite3' if temporary else persistent_path
    store = Store(path)
    if args.demo:
        seed_demo(store)
    window = MainWindow(store, enable_sync=not args.demo, demo_mode=args.demo)
    if args.demo_conflict:
        from .conflict_demo import DemoConflictEngine
        window.engine = DemoConflictEngine(store)
        window.anchor = window.engine.conflicts[0].local.start.date()
        window.refresh()
        window.poll()
    workspaces = [(store, window)]
    def open_persistent_workspace():
        real_store = Store(persistent_path)
        real_window = MainWindow(real_store)
        workspaces.append((real_store, real_window))
        real_window.show()
        window.close()
    window.real_workspace_requested.connect(open_persistent_workspace)
    window.show()
    if args.demo:
        window.show_notice('Offline conflict demo · click Review conflicts to compare the versions. No server is connected.' if args.demo_conflict
                           else 'Demo workspace · temporary and offline. Choose Open my calendars to connect and sync a real account.')
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            saved = window.grab().save(str(args.screenshot))
            if not saved:
                print('Could not save screenshot.', file=sys.stderr)
            window.close()
            app.exit(0 if saved else 1)
        QTimer.singleShot(350, capture)
    code = app.exec()
    for workspace_store, workspace_window in workspaces:
        workspace_window.close()
        workspace_store.close()
    if temporary:
        temporary.cleanup()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
