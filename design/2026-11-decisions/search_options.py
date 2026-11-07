"""Native search-placement prototypes; never imported by the production app.

Each option uses the existing field, filter button and results heading, retaining
their application connections. Apply to a fresh synthetic window for comparison.
"""
from types import MethodType

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QStackedWidget, QWidget

from debacal.theme import scheme_for


OPTIONS = {
    'baseline': {
        'name': 'Field above the heading',
        'thesis': 'Keep the familiar always-visible field directly above its results heading.',
        'tradeoff': 'Uses two rows, but the query and result state are both explicit.',
        'title_meaning': 'Next events means upcoming events; Search results appears when a query is active.',
    },
    'inline': {
        'name': 'Heading and field together',
        'thesis': 'Put a short dynamic heading and an always-visible search field on one line.',
        'tradeoff': 'Saves a row; uses shorter captions and displays less of a long query.',
        'title_meaning': 'Agenda means upcoming events; Results means query matches. Full captions remain in accessible names and tooltips.',
    },
    'reveal': {
        'name': 'Search on demand',
        'thesis': 'Show a search icon beside Next events; replace this same header slot with the field when used.',
        'tradeoff': 'The quietest agenda state; search needs one click or Ctrl+F and hides the heading while active.',
        'title_meaning': 'The resting heading says Next events. During search the query field identifies the results below.',
    },
    'global': {
        'name': 'Global search under the overview',
        'thesis': 'Place event search under the mini-calendar, above the source controls.',
        'tradeoff': 'Signals that search covers events across calendars, but separates the field from its results.',
        'title_meaning': 'Next events / Search results stays directly above the event list, independently of the global field.',
    },
}


def _take_search_row(window):
    agenda = window.agenda_section.layout()
    item = agenda.takeAt(0)
    if item is None or item.layout() is None or item.layout().indexOf(window.search) < 0:
        raise RuntimeError('Expected the existing search row at the top of the agenda section')
    return item.layout()


def _slot_height(window):
    scale = window.preferences.font_scale / 100
    return max(round(36 * scale), window.filter_search.height(),
               window.filter_search.sizeHint().height(), window.search.sizeHint().height())


def _header_slot(window):
    slot = QWidget()
    slot.setObjectName('decisionSearchHeader')
    slot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    slot.setFixedHeight(_slot_height(window))
    row = QHBoxLayout(slot)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    return slot, row


def _compact_placeholder(window):
    """Keep Ctrl+F visible in the short inline field after ordinary view changes."""
    original = window.update_hints

    def update_hints(self):
        original()
        shortcut = self.bindings.get('search', '')
        self.search.setPlaceholderText('Search' + ('  ' + shortcut if shortcut else ''))

    window.update_hints = MethodType(update_hints, window)
    window.update_hints()


def _search_icon(color, scale=1):
    size = round(18 * scale)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)
    painter.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawEllipse(QRectF(3, 3, 8, 8))
    painter.drawLine(QPointF(10, 10), QPointF(15, 15))
    painter.end()
    return QIcon(pixmap)


class _SearchReveal(QObject):
    """Switch one fixed header slot while keeping the app's original query input."""

    def __init__(self, window, stack):
        super().__init__(window)
        self.window, self.stack = window, stack
        window.search.installEventFilter(self)
        window.search.textChanged.connect(self.sync)
        original = window.focus_search

        def focus_search(owner):
            self.stack.setCurrentIndex(1)
            original()

        window.focus_search = MethodType(focus_search, window)
        self.sync()

    def sync(self, *_):
        active = bool(self.window.search.text().strip() or self.window.search.hasFocus())
        self.stack.setCurrentIndex(1 if active else 0)

    def eventFilter(self, obj, event):
        if obj is self.window.search and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.sync()
        return False


def _inline(window):
    agenda = window.agenda_section.layout()
    search_row = _take_search_row(window)
    agenda.removeWidget(window.results_heading)
    slot, row = _header_slot(window)
    # Short captions leave a usable field even at the largest text setting.
    original_text = window.results_heading.setText

    def set_text(label, value):
        original_text({'Next events': 'Agenda', 'Search results': 'Results'}.get(value, value))
        label.setAccessibleName(value)
        label.setToolTip(value)

    window.results_heading.setText = MethodType(set_text, window.results_heading)
    window.results_heading.setText(window.results_heading.text())
    # Reserve both captions so beginning a query does not resize the field.
    metrics = window.results_heading.fontMetrics()
    window.results_heading.setFixedWidth(max(metrics.horizontalAdvance(value)
                                            for value in ('Agenda', 'Results')) + 2)
    row.addWidget(window.results_heading)
    window.search.setMinimumWidth(0)
    row.addLayout(search_row, 1)
    agenda.insertWidget(0, slot)
    _compact_placeholder(window)


def _reveal(window):
    agenda = window.agenda_section.layout()
    search_row = _take_search_row(window)
    agenda.removeWidget(window.results_heading)
    stack = QStackedWidget()
    stack.setObjectName('decisionSearchSlot')
    stack.setFixedHeight(_slot_height(window))
    resting, resting_row = _header_slot(window)
    resting_row.addWidget(window.results_heading, 1)
    search_button = QPushButton()
    search_button.setProperty('iconOnly', True)
    search_button.setAccessibleName('Search events')
    shortcut = window.bindings.get('search', '')
    search_button.setToolTip('Search events' + (' (' + shortcut + ')' if shortcut else ''))
    scale = window.preferences.font_scale / 100
    search_button.setIcon(_search_icon(scheme_for(window).muted, scale))
    search_button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
    search_button.setFixedSize(round(32 * scale), round(32 * scale))
    search_button.clicked.connect(lambda _=False: window.focus_search())
    resting_row.addWidget(search_button)
    active, active_row = _header_slot(window)
    active_row.addLayout(search_row, 1)
    stack.addWidget(resting)
    stack.addWidget(active)
    agenda.insertWidget(0, stack)
    window._decision_search_reveal = _SearchReveal(window, stack)


def apply(window, option='baseline'):
    """Apply one search placement to a fresh, account-free fixture window."""
    if option not in OPTIONS:
        raise ValueError('Unknown search placement: ' + option)
    window._decision_search_option = option
    if option == 'inline':
        _inline(window)
    elif option == 'reveal':
        _reveal(window)
    elif option == 'global':
        search_row = _take_search_row(window)
        window.sidebar_overview.layout().addLayout(search_row)
    window.agenda_section.layout().activate()
    window.sidebar.layout().activate()
