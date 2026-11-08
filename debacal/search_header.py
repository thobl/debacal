"""One stable agenda header that reveals event search with immediate focus."""
from PySide6.QtCore import QEvent, QTimer, Qt, QSize, Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStackedWidget, QWidget

from .icons import icon
from .theme import scheme_for
from .shortcuts import shortcut_label


class SearchHeader(QStackedWidget):
    search_requested = Signal()

    def __init__(self, field, filter_button, heading, preferences, shortcut='', parent=None):
        super().__init__(parent)
        self.setObjectName('searchHeader')
        self.field = field
        self.filter_button = filter_button
        self.heading = heading
        self.resting = QWidget()
        resting = QHBoxLayout(self.resting)
        resting.setContentsMargins(0, 0, 0, 0)
        resting.setSpacing(4)
        resting.addWidget(heading, 1)
        self.button = QPushButton()
        self.button.setProperty('iconOnly', True)
        self.button.setAccessibleName('Search events')
        self.button.clicked.connect(self.search_requested)
        resting.addWidget(self.button)
        self.active = QWidget()
        active = QHBoxLayout(self.active)
        active.setContentsMargins(0, 0, 0, 0)
        active.setSpacing(4)
        active.addWidget(field, 1)
        active.addWidget(filter_button)
        self.addWidget(self.resting)
        self.addWidget(self.active)
        self._signature = None
        self._blur = QTimer(self)
        self._blur.setSingleShot(True)
        self._blur.timeout.connect(self.sync_state)
        field.installEventFilter(self)
        filter_button.installEventFilter(self)
        field.textChanged.connect(self.sync_state)
        self.update_preferences(preferences, shortcut)
        self.sync_state()

    def update_preferences(self, preferences, shortcut=''):
        shortcut = shortcut_label(shortcut)
        signature = preferences.font_scale, scheme_for(preferences), shortcut
        if signature == self._signature:
            return
        self._signature = signature
        scheme = scheme_for(preferences)
        scale = preferences.font_scale / 100
        self.button.setIcon(icon('search', scheme.muted))
        self.button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
        self.button.setFixedSize(round(32 * scale), round(32 * scale))
        self.button.setToolTip('Search events' + (f' ({shortcut})' if shortcut else ''))
        self.setFixedHeight(max(round(36 * scale), max(30, round(32 * scale)), self.field.sizeHint().height(),
                                self.filter_button.sizeHint().height()))

    def reveal(self):
        # Show the field synchronously before MainWindow gives it focus. A
        # queued focus request can lose the user's first typed character.
        self._blur.stop()
        self.setCurrentWidget(self.active)

    def sync_state(self, *_):
        visible = bool(self.field.text().strip() or self.field.hasFocus() or self.filter_button.hasFocus())
        self.setCurrentWidget(self.active if visible else self.resting)

    def eventFilter(self, obj, event):
        if obj is self.field or obj is self.filter_button:
            if event.type() == QEvent.Type.FocusIn:
                self.reveal()
            elif event.type() == QEvent.Type.FocusOut:
                # Let Qt finish transferring focus before collapsing an empty
                # query. A populated query stays visible with result focus.
                self._blur.start(0)
        return super().eventFilter(obj, event)
