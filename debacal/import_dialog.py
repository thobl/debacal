"""Review calendar files and choose their destination before importing."""
from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from dataclasses import replace
from html import escape

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QSizePolicy,
    QAbstractItemView, QDialog, QTextEdit,
)

from .dialogs import FormDialog
from .import_sources import ImportSource
from .import_preparation import prepare_sources, PREVIEW_CALENDAR_ID
from .import_preview import ImportPreview
from .import_selection import ImportSelection, display_date
from .preferences import Preferences
from .timeutil import local_zone


def _prepare_preview(sources, preferences):
    prepared = prepare_sources(sources)
    month = prepared.initial_month(preferences)
    page = prepared.month_page(month, preferences)
    first = min((event.start.date() if event.all_day else event.start.astimezone(local_zone()).date()
                 for event in prepared.events), default=None)
    return prepared, page, first


class ImportDialog(FormDialog):
    """Preview immutable import snapshots without changing calendar storage."""

    def __init__(self, calendars, paths=(), calendar_id=None, parent=None, *, sources=None,
                 preferences=None, executor=None):
        super().__init__(parent)
        self.setWindowTitle('Import calendar files')
        self.resize(980, 680)
        self.setMinimumSize(760, 560)
        self.preferences = preferences or Preferences()
        self.calendars = {calendar.id: calendar for calendar in calendars}
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix='debacal-import-preview')
        self._owns_executor = executor is None
        self._active = True
        self._load_future = self._page_future = self._selection_future = None
        self._requested_month = None
        self._pages = OrderedDict()
        self._page = None
        self._warnings = ()
        self.preview_ready = False
        self.prepared_import = None
        self.selection = ImportSelection()
        self._import_choice = None
        self.selected_prepared = None
        self.selected_selection = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(30)
        self._preview_timer.timeout.connect(self._poll_preview)
        self.finished.connect(self._finish_preview)
        self.selected_paths: tuple[Path, ...] = ()
        self.selected_sources: tuple[ImportSource, ...] = ()
        self.selected_calendar_id = ''
        self._paths: tuple[Path, ...] = ()
        self._sources: tuple[ImportSource, ...] = ()

        outer = QVBoxLayout(self)
        heading = QHBoxLayout()
        label = QLabel('&Source')
        heading.addWidget(label)
        self.browse_button = QPushButton('&Browse…')
        self.browse_button.clicked.connect(self.browse)
        self.files = QListWidget()
        self.files.setAccessibleName('Calendar files to import')
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.files.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.files.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.files.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        label.setBuddy(self.files)
        heading.addWidget(self.files, 1)
        heading.addWidget(self.browse_button)
        outer.addLayout(heading)

        form = QFormLayout()
        self.calendar = QComboBox()
        self.calendar.setAccessibleName('Import into calendar')
        for calendar in calendars:
            if calendar.read_only or calendar.kind == 'ics':
                continue
            swatch = QPixmap(12, 12)
            color = QColor(calendar.color)
            swatch.fill(color if color.isValid() else QColor('#416c51'))
            self.calendar.addItem(QIcon(swatch), calendar.name, calendar.id)
        index = self.calendar.findData(calendar_id)
        if index >= 0:
            self.calendar.setCurrentIndex(index)
        form.addRow('Import &into', self.calendar)
        outer.addLayout(form)
        self.calendar.currentIndexChanged.connect(self._destination_changed)

        self.preview = ImportPreview(self.preferences)
        self.preview.month_requested.connect(self._request_month)
        self.preview.occurrence_toggled.connect(self._toggle_occurrence)
        self.preview.date_toggled.connect(self._toggle_date)
        self.preview.selection_all_requested.connect(self._select_all)
        self.preview.selection_month_requested.connect(self._select_month)
        outer.addWidget(self.preview, 1)
        status = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setProperty('muted', True)
        status.addWidget(self.summary, 1)
        self.issues_button = QPushButton('Review issues')
        self.issues_button.clicked.connect(self._show_issues)
        policy = self.issues_button.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.issues_button.setSizePolicy(policy)
        self.issues_button.hide()
        status.addWidget(self.issues_button)
        outer.addLayout(status)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setStyleSheet('color: #b54735;')
        outer.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_button = self.buttons.addButton('&Import', QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setObjectName('primary')
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.configure_keyboard([self.browse_button, self.files, self.calendar],
                                [self.import_button, self.cancel_button], self.save)
        self.setTabOrder(self.calendar, self.preview.canvas)
        self.setTabOrder(self.preview.canvas, self.import_button)
        if sources is None:
            self.set_paths(paths)
        else:
            self.set_sources(sources)
        (self.calendar if self._sources else self.browse_button).setFocus(Qt.FocusReason.OtherFocusReason)

    def set_paths(self, paths):
        if isinstance(paths, (str, Path)):
            paths = (paths,)
        # Do not resolve symlinks, stat files, or read their contents on the UI
        # thread. Paths from remote mounts may need slow I/O even to inspect.
        self.set_sources(ImportSource.from_path(path) for path in paths)

    def set_sources(self, sources):
        self._sources = tuple(dict.fromkeys(sources))
        if any(not isinstance(source, ImportSource) for source in self._sources):
            raise ValueError('Choose calendar import sources.')
        self._paths = tuple(source.path for source in self._sources if source.path is not None)
        self.files.clear()
        for source in self._sources:
            item = QListWidgetItem(source.label)
            item.setToolTip(str(source.path) if source.path is not None else source.label)
            self.files.addItem(item)
        self._fit_sources()
        self.error.clear()
        self._start_preview()
        if not self.calendar.count():
            self.error.setText('Add a writable calendar before importing these files.')

    def _fit_sources(self):
        self.files.ensurePolished()
        row = max(self.files.fontMetrics().height() + 8, self.files.sizeHintForRow(0))
        self.files.setFixedHeight(min(2, max(1, len(self._sources))) * row + 6)

    def showEvent(self, event):
        self._fit_sources()
        super().showEvent(event)

    def _start_preview(self):
        for future in (self._load_future, self._page_future, self._selection_future):
            if future is not None:
                future.cancel()
        self._load_future = self._page_future = self._selection_future = None
        self.preview_ready = False
        self.prepared_import = None
        self._import_choice = None
        self.selection.select_all()
        self._page = None
        self._requested_month = None
        self._pages.clear()
        self._warnings = ()
        self.issues_button.hide()
        self.import_button.setEnabled(False)
        self.preview.set_first_date(None)
        if not self._sources:
            self.summary.setText('Choose calendar files or provide calendar contents.')
            self.preview.set_events(self.preview.current_month, (), self._preview_calendars())
            self.preview.set_message('Choose a calendar to preview.')
            return
        self.summary.setText('Loading preview…')
        self.preview.set_loading(True)
        self._load_future = self._executor.submit(_prepare_preview, self._sources, self.preferences)
        self._preview_timer.start()

    def _preview_calendars(self):
        calendar = self.calendars.get(self.calendar.currentData())
        return (replace(calendar, id=PREVIEW_CALENDAR_ID, read_only=True),) if calendar else ()

    def _destination_changed(self, *_):
        if self._page is not None:
            self.preview.set_events(self._page.month, self._page.events, self._preview_calendars())
            self._render_selection()
        self._update_import_button()

    def _update_import_button(self):
        self.import_button.setEnabled(bool(self.preview_ready and self.prepared_import
                                          and self._selection_future is None and self._import_choice
                                          and self._import_choice.event_count and self.calendar.currentData()))

    def _toggle_occurrence(self, event):
        self.selection.toggle(event)
        self._prepare_selection()

    def _toggle_date(self, day):
        if self._page is not None:
            self.selection.toggle_date(day, self._page.events)
            self._prepare_selection()

    def _select_all(self, selected):
        self.selection.select_all() if selected else self.selection.select_none()
        self._prepare_selection()

    def _select_month(self, month):
        if self._page is None or self._page.month != month.replace(day=1):
            return
        self.selection.select_none()
        self.selection.set_selected((event for event in self._page.events
                                     if display_date(event).replace(day=1) == self._page.month), True)
        self._prepare_selection()

    def _render_selection(self):
        if self._page is not None:
            self.preview.set_selection({event.id for event in self._page.events if self.selection.is_selected(event)})

    def _prepare_selection(self):
        if self._selection_future is not None:
            self._selection_future.cancel()
            self._selection_future = None
        self._import_choice = None
        self.preview_ready = False
        self._render_selection()
        if self.prepared_import is not None:
            snapshot = self.selection.snapshot(masters=self.prepared_import.events)
            if snapshot.all_selected and not snapshot.choices:
                self._import_choice = self.prepared_import
                self.preview_ready = self._page is not None
            else:
                self._selection_future = self._executor.submit(self.prepared_import.with_selection, snapshot)
                self._preview_timer.start()
        self._update_summary()
        self._update_import_button()

    def _update_summary(self):
        if self._page is None:
            return
        if self._import_choice is None:
            self.summary.setText('Updating selected dates…')
            return
        included = sum(self.selection.is_selected(event) for event in self._page.events)
        count = self._import_choice.event_count
        if not self.prepared_import.event_count:
            self.summary.setText('No importable events found.')
            return
        if not count:
            self.summary.setText('No dates included.')
            return
        self.summary.setText(f'{count} event' + ('' if count == 1 else 's') +
                             f' ready · {included} of {len(self._page.events)} in view included')

    def _request_month(self, month):
        self._requested_month = month.replace(day=1)
        if self.prepared_import is None:
            return
        if self._page_future is not None:
            self._page_future.cancel()
            self._page_future = None
        page = self._pages.get(self._requested_month)
        if page is not None:
            self._show_page(page)
            return
        self.preview.set_loading(True)
        self._page_future = self._executor.submit(self.prepared_import.month_page,
                                                  self._requested_month, self.preferences)
        self._preview_timer.start()

    def _poll_preview(self):
        if not self._active:
            return
        if self._load_future is not None and self._load_future.done():
            future, self._load_future = self._load_future, None
            try:
                self.prepared_import, page, first = future.result()
                self._prepare_selection()
                self.preview.set_first_date(first)
                if self._requested_month is not None and self._requested_month != page.month:
                    self._request_month(self._requested_month)
                else:
                    self._show_page(page)
            except Exception as exc:
                self._preview_failed(exc)
        if self._page_future is not None and self._page_future.done():
            future, self._page_future = self._page_future, None
            try:
                self._show_page(future.result())
            except Exception as exc:
                self._preview_failed(exc)
        if self._selection_future is not None and self._selection_future.done():
            future, self._selection_future = self._selection_future, None
            try:
                self._import_choice = future.result()
                self.preview_ready = self._page is not None
                self._update_summary()
                self._update_import_button()
            except Exception as exc:
                self._preview_failed(exc)
        if self._load_future is None and self._page_future is None and self._selection_future is None:
            self._preview_timer.stop()

    def _show_page(self, page):
        self._page = page
        self._requested_month = page.month
        self._pages[page.month] = page
        self._pages.move_to_end(page.month)
        while len(self._pages) > 3:
            self._pages.popitem(last=False)
        self.preview.set_events(page.month, page.events, self._preview_calendars())
        self._render_selection()
        self.preview.set_loading(False)
        self.preview_ready = self._selection_future is None
        self._update_summary()
        self._warnings = tuple(dict.fromkeys((*self.prepared_import.warnings, *page.warnings)))
        self.issues_button.setText(f'Review issues ({len(self._warnings)})')
        self.issues_button.setVisible(bool(self._warnings))
        self.issues_button.setToolTip('<qt>' + '<br>'.join(escape(value) for value in self._warnings) + '</qt>')
        self._update_import_button()

    def _preview_failed(self, error):
        self.preview_ready = True
        self.preview.set_loading(False)
        self.preview.set_message('Preview unavailable.')
        self.error.setText(str(error))
        self._update_import_button()

    def _show_issues(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Import issues')
        dialog.resize(650, 360)
        layout = QVBoxLayout(dialog)
        details = QTextEdit()
        details.setReadOnly(True)
        details.setPlainText('\n\n'.join(self._warnings))
        layout.addWidget(details)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        dialog.deleteLater()

    def _finish_preview(self, *_):
        self._active = False
        self._preview_timer.stop()
        for future in (self._load_future, self._page_future, self._selection_future):
            if future is not None:
                future.cancel()
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def browse(self):
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, 'Choose calendar files', str(self._paths[0].parent) if self._paths else '',
            'Calendar files (*.ics);;All files (*)')
        if paths:
            self.set_paths(paths)
            self.calendar.setFocus(Qt.FocusReason.OtherFocusReason)

    def save(self):
        if not self._sources:
            self.show_error('Choose at least one calendar file.', self.browse_button)
            return
        if not self.calendar.currentData():
            self.show_error('Add a writable calendar before importing these files.', self.calendar)
            return
        if not self.preview_ready:
            self.show_error('Wait for the calendar preview to finish loading.')
            return
        if self.prepared_import is None or not self.prepared_import.event_count:
            self.show_error('No importable events found. Review the issues or choose another calendar file.')
            return
        if self._import_choice is None or not self._import_choice.event_count:
            self.show_error('Select at least one event or date to import.')
            return
        self.selected_paths = self._paths
        self.selected_sources = self._sources
        self.selected_calendar_id = self.calendar.currentData()
        self.selected_prepared = self._import_choice
        self.selected_selection = self.selection.snapshot(masters=self.prepared_import.events)
        self.accept()
