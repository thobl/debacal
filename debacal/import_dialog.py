"""Review calendar files and choose their destination before importing."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from .dialogs import FormDialog
from .import_sources import ImportSource


class ImportDialog(FormDialog):
    """Collect an import request; file reads and calendar writes happen later."""

    def __init__(self, calendars, paths=(), calendar_id=None, parent=None, *, sources=None):
        super().__init__(parent)
        self.setWindowTitle('Import calendar files')
        self.resize(560, 360)
        self.selected_paths: tuple[Path, ...] = ()
        self.selected_sources: tuple[ImportSource, ...] = ()
        self.selected_calendar_id = ''
        self._paths: tuple[Path, ...] = ()
        self._sources: tuple[ImportSource, ...] = ()

        outer = QVBoxLayout(self)
        heading = QHBoxLayout()
        label = QLabel('&Files')
        heading.addWidget(label)
        heading.addStretch()
        self.browse_button = QPushButton('&Browse…')
        self.browse_button.clicked.connect(self.browse)
        heading.addWidget(self.browse_button)
        outer.addLayout(heading)
        self.files = QListWidget()
        self.files.setAccessibleName('Calendar files to import')
        label.setBuddy(self.files)
        outer.addWidget(self.files, 1)

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
        self.error.clear()
        self.import_button.setEnabled(bool(self._sources) and self.calendar.count() > 0)
        if not self.calendar.count():
            self.error.setText('Add a writable calendar before importing these files.')

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
        self.selected_paths = self._paths
        self.selected_sources = self._sources
        self.selected_calendar_id = self.calendar.currentData()
        self.accept()
