"""Desktop startup, profile selection, and delivery to an existing instance."""
import argparse
from datetime import timedelta
from pathlib import Path
import sys
import sqlite3
import tempfile

from PySide6.QtCore import QEvent, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .calendar_widgets import at
from .models import Calendar, Event
from .store import Store
from .timeutil import now
from .ui import MainWindow
from .profiles import resolve_profile, validate_profile_name
from .single_instance import InstanceCoordinator, InstanceError
from .import_sources import ImportSource, MAX_IMPORT_BYTES
from .fonts import configure_application_font


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


def _file_argument(value):
    if value == '-':
        return '-'
    if value.startswith('file:'):
        url = QUrl(value)
        if not url.isLocalFile():
            raise argparse.ArgumentTypeError('Choose a local calendar file.')
        value = url.toLocalFile()
    if not value or '\0' in value:
        raise argparse.ArgumentTypeError('Choose a valid calendar file path.')
    return Path(value).expanduser().absolute()


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description='A local-first Linux desktop calendar',
                                     epilog='Redirected or piped standard input is opened for calendar import automatically.')
    parser.add_argument('--database', type=Path, help='Use a specific SQLite database')
    profiles = parser.add_mutually_exclusive_group()
    profiles.add_argument('--profile', type=validate_profile_name, default='default', metavar='NAME',
                          help='Use a separate persistent profile')
    profiles.add_argument('--test', dest='profile', action='store_const', const='testing',
                          help='Use the separate persistent testing profile')
    parser.add_argument('--new-instance', action='store_true',
                        help='Allow another window for the same profile/database')
    parser.add_argument('--demo', action='store_true', help='Use disposable sample data')
    parser.add_argument('--demo-conflict', action='store_true', help='Use disposable sample data with a sync conflict to review')
    parser.add_argument('--screenshot', type=Path, help='Save a workspace screenshot and exit')
    parser.add_argument('files', nargs='*', type=_file_argument, metavar='FILE.ics',
                        help='Open calendar files for import; use - to read standard input')
    args = parser.parse_args(argv)
    args.demo = args.demo or args.demo_conflict
    if args.files.count('-') > 1:
        parser.error('Specify standard input only once.')
    args.stdin = '-' in args.files
    args.files = [value for value in args.files if value != '-']
    return args


def read_standard_input(stream=None, *, force=False):
    """Read redirected ICS bytes without consuming an interactive terminal."""
    stream = sys.stdin if stream is None else stream
    if stream is None:
        return None
    if not force and stream.isatty():
        return None
    try:
        source = getattr(stream, 'buffer', stream)
        data = source.read(MAX_IMPORT_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise ValueError('Could not read standard input: ' + str(exc)) from None
    if isinstance(data, str):
        data = data.encode('utf-8')
    if not data:
        return None
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError('Choose standard input smaller than 20 MB.')
    return bytes(data)


class FileOpeningApplication(QApplication):
    """Keep desktop file-open events until the workspace is ready to receive them."""
    file_open_requested = Signal(str)

    def __init__(self, argv):
        self._pending_files = []
        self._files_ready = False
        super().__init__(argv)

    def event(self, event):
        if event.type() == QEvent.Type.FileOpen:
            path = event.file() or event.url().toLocalFile()
            if path:
                path = str(Path(path).expanduser().absolute())
                if self._files_ready:
                    self.file_open_requested.emit(path)
                else:
                    self._pending_files.append(path)
            return True
        return super().event(event)

    def enable_file_requests(self):
        self._files_ready = True
        pending, self._pending_files = self._pending_files, []
        for path in pending:
            self.file_open_requested.emit(path)


def configure_application(app):
    app.setStyle('Fusion')
    configure_application_font(app)
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
    app.setApplicationName('debacal')
    app.setDesktopFileName('debacal')
    app.setOrganizationName('debacal')
    app.setApplicationDisplayName('Debacal')


def main(argv=None, *, delivery_allowed=True, reminder_factory=None):
    args = parse_arguments(argv)
    try:
        stdin_data = read_standard_input(force=args.stdin)
    except ValueError as exc:
        print('Could not import standard input: ' + str(exc), file=sys.stderr)
        return 1
    if args.screenshot and (args.files or stdin_data):
        print('Screenshot mode cannot import calendar files or standard input.', file=sys.stderr)
        return 1
    app = FileOpeningApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    configure_application(app)
    profile = resolve_profile(args.profile, args.database)
    temporary = tempfile.TemporaryDirectory(prefix='debacal-demo-') if args.demo else None
    path = Path(temporary.name) / 'demo.sqlite3' if temporary else profile.database
    workspaces, coordinators = [], []
    window = None
    exit_status = 1 if args.screenshot else 0

    def workspace_closed(workspace):
        if workspace is window:
            app.exit(exit_status)

    def claim_workspace(database, files=(), *, stdin_data=None):
        if args.new_instance or args.screenshot:
            return None, True
        coordinator = InstanceCoordinator(database, parent=app)
        primary = coordinator.start(files, stdin_data=stdin_data) if stdin_data is not None else coordinator.start(files)
        if primary:
            coordinators.append(coordinator)
        else:
            coordinator.close()
        return coordinator, primary

    def create_workspace(database, coordinator, *, demo=False):
        store = Store(database)
        try:
            if demo:
                seed_demo(store)
            workspace = MainWindow(store, enable_sync=not demo, demo_mode=demo,
                                   profile_label=profile.label if profile.name != 'default' else '',
                                   credential_namespace=profile.credential_namespace,
                                   delivery_allowed=delivery_allowed and not demo and not args.screenshot,
                                   reminder_factory=reminder_factory, imports_allowed=not args.screenshot)
            if coordinator is not None:
                workspace.bind_instance(coordinator)
            workspace.workspaceClosed.connect(lambda: workspace_closed(workspace))
        except Exception:
            store.close()
            raise
        workspaces.append((store, workspace))
        return workspace

    def open_persistent_workspace():
        """Called once the demo is safe to retire. A failed switch keeps it open."""
        nonlocal window
        persistent_coordinator = None
        try:
            persistent_coordinator, primary = claim_workspace(profile.database)
            if primary:
                replacement = create_workspace(profile.database, persistent_coordinator)
                window = replacement
                window.restore_workspace()
                if persistent_coordinator:
                    QTimer.singleShot(0, persistent_coordinator.enable_requests)
            return True
        except (InstanceError, OSError, ValueError, sqlite3.Error) as exc:
            if persistent_coordinator in coordinators:
                persistent_coordinator.close()
                coordinators.remove(persistent_coordinator)
            window.report_error('Could not open the workspace: ' + str(exc), title='Could not open workspace')
            return False

    try:
        coordinator, primary = claim_workspace(path, args.files, stdin_data=stdin_data)
        if not primary:
            return 0
        window = create_workspace(path, coordinator, demo=args.demo)
        if args.demo_conflict:
            from .conflict_demo import DemoConflictEngine
            window.engine = DemoConflictEngine(window.store)
            window.anchor = window.engine.conflicts[0].local.start.date()
            window.refresh()
            window.poll()
        if args.demo and not args.screenshot:
            demo = window
            demo.real_workspace_requested.connect(lambda: demo.retire_for_replacement(open_persistent_workspace))
        def native_open(path):
            if not window.receive_file_open(path):
                print('Debacal could not accept the open-file request.', file=sys.stderr)
        app.file_open_requested.connect(native_open)
        if coordinator:
            QTimer.singleShot(0, coordinator.enable_requests)
        elif args.files or stdin_data:
            sources = [ImportSource.from_path(path) for path in args.files]
            if stdin_data:
                sources.append(ImportSource.from_data(stdin_data))
            if not window.import_sources(sources):
                raise InstanceError('The workspace could not accept these calendar inputs.')
            del sources
        stdin_data = None
        window.restore_workspace()
        QTimer.singleShot(0, app.enable_file_requests)
        if args.screenshot:
            def capture():
                nonlocal exit_status
                try:
                    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    saved = window.grab().save(str(args.screenshot))
                except OSError:
                    saved = False
                if not saved:
                    print('Could not save screenshot.', file=sys.stderr)
                exit_status = 0 if saved else 1
                window.request_quit(force=True)
            QTimer.singleShot(350, capture)
        # An external Qt quit can also end exec(). Keep the event loop available
        # if accepted work needs a writer callback or a human review.
        while not window._closed:
            result = app.exec()
            if not window._closed:
                if not args.screenshot:
                    exit_status = result
                window.request_quit(force=True)
        return exit_status
    except (InstanceError, OSError, ValueError, sqlite3.Error) as exc:
        print('Could not start Debacal: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        for workspace_store, workspace_window in reversed(workspaces):
            if not workspace_window._closed:
                workspace_window.request_quit(force=True)
            workspace_store.close()
        for coordinator in coordinators:
            coordinator.close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
