"""Aligned period navigation and native, single-choice command groups."""
from datetime import date
import math

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QBoxLayout, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .theme import scheme_for


class SegmentedControl(QWidget):
    """Paint one continuous surface behind normal, keyboard-operable buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('segmentedControl')
        self.preferences = None
        self._buttons = []
        self._button_width = self._button_height = 32
        self._keyboard_focus = None
        self.buttons_layout = QHBoxLayout(self)
        self.buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.buttons_layout.setSpacing(0)

    def set_buttons(self, buttons):
        self._buttons = list(buttons)
        for button in self._buttons:
            button.toggled.connect(self.update)
            button.installEventFilter(self)

    def eventFilter(self, button, event):
        if event.type() == QEvent.Type.FocusIn:
            self._keyboard_focus = button if event.reason() in (
                Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason) else None
            self.update()
        elif event.type() == QEvent.Type.FocusOut:
            if self._keyboard_focus is button:
                self._keyboard_focus = None
            self.update()
        elif event.type() in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.EnabledChange):
            self.update()
        return super().eventFilter(button, event)

    def set_preferences(self, preferences, width, height):
        self.preferences = preferences
        self._button_width, self._button_height = width, height
        self.setFixedSize(width * len(self._buttons), height)
        self.update()

    def paintEvent(self, event):
        scheme = scheme_for(self.preferences)
        scale = getattr(self.preferences, 'font_scale', 100) / 100
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if getattr(self.preferences, 'control_style', 'underlined') == 'filled':
            shape = QPainterPath()
            shape.addRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5 * scale, 5 * scale)
            painter.fillPath(shape, QColor(scheme.panel))
            painter.save()
            painter.setClipPath(shape)
            for button in self._buttons:
                if button.isChecked():
                    painter.fillRect(button.geometry(), QColor(scheme.selection))
                elif button.isEnabled() and button.underMouse():
                    painter.fillRect(button.geometry(), QColor(scheme.hover))
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.control_edge), scale))
            painter.drawPath(shape)
        else:
            for button in self._buttons:
                if button.isEnabled() and button.underMouse():
                    painter.fillRect(button.geometry(), QColor(scheme.hover))
            painter.setPen(QPen(QColor(scheme.control_edge), scale))
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
            for button in self._buttons:
                if button.isChecked():
                    painter.fillRect(QRectF(button.x(), self.height() - 2 * scale, button.width(), 2 * scale),
                                     QColor(scheme.accent))
        # Leave source selection clean on mouse clicks, but preserve an explicit
        # focus cue when the user reaches the group through keyboard navigation.
        if self._keyboard_focus is not None and self._keyboard_focus.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(scheme.focus), scale, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(QRectF(self._keyboard_focus.geometry()).adjusted(3 * scale, 3 * scale,
                                                                                   -3 * scale, -3 * scale),
                                    2 * scale, 2 * scale)


class HeaderSeparator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.preferences = None
        self.setObjectName('workspaceDivider')
        self.setFixedHeight(1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(scheme_for(self.preferences).selection_edge))


class WorkspaceHeader(QWidget):
    """Keep period labels and command groups on shared text/control rows.

    Navigation has stable columns. The range spans spare space in Year view so
    its long date label cannot push Next away from the short year heading.
    """

    def __init__(self, window, title, controls):
        super().__init__()
        self.window = window
        self.title, self.controls = title, controls
        self.inline = None
        self._signature = None
        self.title_grid = QGridLayout(title)
        self.title_grid.setContentsMargins(0, 0, 0, 0)
        self.title_grid.setHorizontalSpacing(0)
        self.title_grid.setColumnStretch(3, 1)
        self.title_grid.addWidget(window.previous_button, 0, 0, Qt.AlignmentFlag.AlignCenter)
        self.title_grid.addWidget(window.today_button, 1, 0, Qt.AlignmentFlag.AlignCenter)
        self.title_grid.addWidget(window.heading, 0, 1)
        self.title_grid.addWidget(window.next_button, 0, 2, Qt.AlignmentFlag.AlignCenter)
        self.title_grid.addWidget(window.range_label, 1, 1, 1, 3)
        body = QWidget()
        self.row = QBoxLayout(QBoxLayout.Direction.TopToBottom, body)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSizeConstraint(QBoxLayout.SizeConstraint.SetNoConstraint)
        self.row.addWidget(title, 1, Qt.AlignmentFlag.AlignTop)
        self.row.addWidget(controls, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(body)
        self.separator = HeaderSeparator(self)
        outer.addWidget(self.separator)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self):
        # Allow the window to shrink enough that the toolbar can wrap.
        return QSize(0, self.layout().minimumSize().height())

    def update_preferences(self):
        window, preferences = self.window, self.window.preferences
        window.heading.ensurePolished()
        window.range_label.ensurePolished()
        signature = (preferences, window.view, window.anchor.year,
                     window.heading.font().key(), window.range_label.font().key())
        if signature == self._signature:
            return
        self._signature = signature
        scale = preferences.font_scale / 100
        heading_height, button_height = math.ceil(28 * scale), math.ceil(36 * scale)
        width, nav_size = round(32 * scale), round(28 * scale)
        gap = math.ceil(4 * scale)
        self.heading_height, self.button_height = heading_height, button_height
        for button in (window.previous_button, window.today_button, window.next_button):
            button.setFixedSize(nav_size, nav_size)
            button.setIconSize(QSize(round(18 * scale), round(18 * scale)))
        window.heading.setFixedHeight(heading_height)
        window.heading.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        window.range_label.setFixedHeight(button_height)
        window.range_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.title_grid.setVerticalSpacing(gap)
        gutter = window.canvas.gutter
        self.title_grid.setColumnMinimumWidth(0, gutter)
        heading_metrics, range_metrics = window.heading.fontMetrics(), window.range_label.fontMetrics()
        if window.view == 'year':
            heading_width = max(heading_metrics.horizontalAdvance(str(year))
                                for year in (window.anchor.year, 1111, 8888, 9999)) + round(6 * scale)
        else:
            heading_width = max(heading_metrics.horizontalAdvance(preferences.format_month(date(window.anchor.year, month, 1)))
                                for month in range(1, 13)) + round(6 * scale)
        date_width = max(range_metrics.horizontalAdvance(preferences.format_date(date(2026, month, day)))
                         for month in range(1, 13) for day in (1, 28))
        range_width = date_width * 2 + range_metrics.horizontalAdvance(' — ')
        if window.view != 'year':
            heading_width = max(heading_width, range_width + round(6 * scale))
        self.title_grid.setColumnMinimumWidth(1, heading_width)
        window.heading.setFixedWidth(heading_width)
        # Keep the lower label readable without contributing to the title's
        # column width. The final stretch column absorbs its additional width.
        window.range_label.setMinimumWidth(max(range_width, range_metrics.horizontalAdvance(window.range_label.text())))
        self.title.setFixedHeight(heading_height + gap + button_height)
        self.row.setSpacing(round(24 * scale) if self.inline else round(12 * scale))
        self.layout().setSpacing(round(12 * scale))
        self.separator.preferences = preferences
        self.separator.setFixedHeight(max(1, round(scale)))
        self.separator.update()
        window.toolbar.layout().setSpacing(round(16 * scale))
        for section in window.toolbar_sections:
            section.setFixedHeight(heading_height + gap + button_height)
            section.layout().setSpacing(gap)
            section.layout().itemAt(1).layout().setContentsMargins(0, 0, 0, 0)
            for button in section.findChildren(QPushButton):
                compact = bool(button.property('compactIcon'))
                button.setFixedSize(round(20 * scale) if compact else width,
                                    round(20 * scale) if compact else button_height)
                button.setIconSize(QSize(round((14 if compact else 20) * scale),
                                         round((14 if compact else 20) * scale)))
            for label in section.findChildren(QLabel):
                label.ensurePolished()
                label.setFixedHeight(heading_height)
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
                descent_gap = max(0, window.heading.fontMetrics().descent() - label.fontMetrics().descent())
                label.setStyleSheet(f'padding-bottom: {descent_gap}px;')
        for group in (window.view_control, window.selection_control):
            group.set_preferences(preferences, width, button_height)
        self.updateGeometry()

    def reflow(self):
        self.update_preferences()
        # Tooltips update native button sizes first; reapply shared row sizes
        # even when the preference/layout metrics have not changed.
        preferences = self.window.preferences
        scale = preferences.font_scale / 100
        for button in (self.window.previous_button, self.window.today_button, self.window.next_button):
            button.setFixedSize(round(28 * scale), round(28 * scale))
        for section in self.window.toolbar_sections:
            for button in section.findChildren(QPushButton):
                compact = bool(button.property('compactIcon'))
                button.setFixedSize(round(20 * scale) if compact else round(32 * scale),
                                    round(20 * scale) if compact else self.button_height)
        needed = self.title.minimumSizeHint().width() + self.controls.sizeHint().width() + round(24 * scale)
        inline = self.width() >= needed
        if inline != self.inline:
            self.inline = inline
            self.row.setDirection(QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom)
            self.row.setSpacing(round((24 if inline else 12) * scale))
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()
