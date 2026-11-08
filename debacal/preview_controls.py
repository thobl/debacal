"""Shared month and occurrence navigation for editor and import previews."""
from PySide6.QtCore import QObject, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QWidget

from .icons import icon
from .buttons import action_button, icon_button
from .compact_controls import ElidedLabel
from .preferences import Preferences
from .theme import scheme_for
from .timeutil import local_zone


def _endpoint_icon(color, last=False):
    pixels = QPixmap(40, 40)
    pixels.setDevicePixelRatio(2)
    pixels.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixels)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    bar, edge, point = (15, 7, 12) if last else (5, 13, 8)
    painter.drawLine(QPointF(bar, 5), QPointF(bar, 15))
    painter.drawLine(QPointF(edge, 5), QPointF(point, 10))
    painter.drawLine(QPointF(point, 10), QPointF(edge, 15))
    painter.end()
    return QIcon(pixels)


class PreviewControls(QObject):
    previous_month_requested = Signal()
    next_month_requested = Signal()
    today_requested = Signal()
    first_requested = Signal()
    previous_requested = Signal()
    next_requested = Signal()
    last_requested = Signal()

    def __init__(self, preferences=None, parent=None):
        super().__init__(parent)
        self.preferences = preferences or Preferences()
        self.month_bar, self.occurrence_bar = QWidget(parent), QWidget(parent)
        months = QHBoxLayout(self.month_bar)
        months.setContentsMargins(0, 0, 0, 0)
        months.setSpacing(4)
        self.previous_button = self._button('previous', 'Previous month', self.previous_month_requested)
        self.today_button = self._button('today', 'Today', self.today_requested)
        self.next_button = self._button('next', 'Next month', self.next_month_requested)
        self.month_label = ElidedLabel()
        self.month_label.setObjectName('sidebarSectionHeading')
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.month_label.setContentsMargins(8, 0, 0, 0)
        self.month_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        months.addWidget(self.previous_button)
        months.addWidget(self.today_button)
        months.addWidget(self.next_button)
        months.addWidget(self.month_label, 1)
        navigation = QHBoxLayout(self.occurrence_bar)
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.setSpacing(3)
        self.first_button = self._button('first', 'First occurrence', self.first_requested)
        self.previous_occurrence_button = self._button('previous', 'Previous occurrence', self.previous_requested)
        self.next_occurrence_button = self._button('next', 'Next occurrence', self.next_requested)
        self.last_button = self._button('last', 'Last occurrence', self.last_requested)
        self.position_label = ElidedLabel()
        self.position_label.setProperty('muted', True)
        self.position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.position_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        navigation.addWidget(self.first_button)
        navigation.addWidget(self.previous_occurrence_button)
        navigation.addWidget(self.position_label, 1)
        navigation.addWidget(self.next_occurrence_button)
        navigation.addWidget(self.last_button)
        self.set_preferences(self.preferences)

    def _button(self, glyph, caption, signal):
        button = (action_button(tooltip=caption) if glyph in ('first', 'last') else
                  icon_button(glyph, caption, preferences=self.preferences))
        button.setProperty('iconOnly', True)
        button.setProperty('previewGlyph', glyph)
        button.setAccessibleName(caption)
        button.setToolTip(caption)
        button.clicked.connect(lambda _checked=False: signal.emit())
        return button

    def set_preferences(self, preferences):
        self.preferences = preferences
        scale, colors = preferences.font_scale / 100, scheme_for(preferences)
        for bar in (self.month_bar, self.occurrence_bar):
            for button in bar.findChildren(QPushButton):
                glyph = button.property('previewGlyph')
                button.setIcon(_endpoint_icon(colors.accent, glyph == 'last') if glyph in ('first', 'last') else icon(glyph, colors.accent))
                button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
                button.setFixedSize(round(30 * scale), round(30 * scale))

    def set_month(self, day):
        self.month_label.setText(self.preferences.format_month(day))
        self.month_label.setToolTip(self.month_label.text())

    def set_position(self, event, index=None, total=None, *, unbounded=False, display_zone=None):
        if event is None:
            self.position_label.clear()
            self.position_label.setToolTip('')
            return
        value = event.start if event.all_day else event.start.astimezone(display_zone or local_zone())
        caption = self.preferences.format_date(value.date())
        if not event.all_day:
            caption += ' · ' + self.preferences.format_time(value)
        if index is not None or total is not None or unbounded:
            denominator = '∞' if unbounded else str(total) if total is not None else '…'
            caption = (str(index) if index is not None else '…') + '/' + denominator + '  ·  ' + caption
        self.position_label.setText(caption)
        self.position_label.setToolTip(event.title + '\n' + caption)
