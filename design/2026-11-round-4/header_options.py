"""Focused layout/style alternatives; never imported by the application."""
from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget, QBoxLayout, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel, QFrame, QPushButton

from debacal.theme import scheme_for


LAYOUTS = {
    'split': dict(name='Arrows beside the month',
        detail='Month and range align with the first day boundary. Previous/next flank the month; Today sits in the axis gutter beside the range. Navigation positions are reserved for the longest month.'),
    'after': dict(name='Range, then navigation',
        detail='Swaps the lower-row order of H5/H6: the month and date range start on the first day boundary; Previous/Today/Next follow the range. Both lines align with the command-group rows.'),
}
STYLES = {
    'filled': dict(name='Filled choices',
        detail='A continuous rounded group with an edge-to-edge selected fill, no inner selected border, zero internal margins and identical vertical/horizontal insets. Individual actions remain borderless.'),
    'underlined': dict(name='Underlined choices + separator',
        detail='Square, flat underlines for chosen options. A separate rule and gap distinguish the complete command area from the calendar, using 13px more height.'),
}


class FlushSegments(QWidget):
    """One clipped group surface, with transparent native icon buttons above it."""
    def __init__(self, window, previous):
        super().__init__(previous.parentWidget())
        self.scheme = scheme_for(window.preferences)
        self.setObjectName('reviewFlushSegments')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.buttons = []
        while previous.layout().count():
            item = previous.layout().takeAt(0)
            if item.widget():
                button = item.widget()
                layout.addWidget(button)
                button.toggled.connect(self.update)
                self.buttons.append(button)
        parent_layout = previous.parentWidget().layout()
        # Segments live in a nested row, not directly in the section's VBox.
        def replace(container):
            for index in range(container.count()):
                item = container.itemAt(index)
                if item.widget() is previous:
                    container.replaceWidget(previous, self)
                    return True
                if item.layout() and replace(item.layout()):
                    return True
            return False
        assert replace(parent_layout)
        previous.hide()
        previous.setParent(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5, 5)
        painter.fillPath(shape, QColor(self.scheme.panel))
        painter.save()
        painter.setClipPath(shape)
        for button in self.buttons:
            if button.isChecked():
                painter.fillRect(button.geometry(), QColor(self.scheme.selection))
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(self.scheme.control_edge), 1))
        painter.drawPath(shape)


class HeaderOption(QWidget):
    def __init__(self, window, layout_name, style, tight=False):
        old = window.workspace_header
        parent = old.parentWidget()
        super().__init__(parent)
        self.window, self.layout_name, self.tight = window, layout_name, tight
        self.heading_height, self.button_height = (24, 32) if tight else (28, 36)
        command_size = 28 if tight else 32
        self.inline = None
        self.setObjectName('reviewHeader')
        self.setStyleSheet('QWidget#reviewHeader { background: #ffffff; }')
        self.title = QWidget()
        self.left = QGridLayout(self.title)
        self.left.setContentsMargins(0, 0, 0, 0)
        self.left.setHorizontalSpacing(0)
        self.left.setVerticalSpacing(2 if tight else 4)
        self.left.setColumnStretch(3, 1)
        self.controls = window.toolbar
        self.controls.setParent(self)
        self._period = window.period
        for label in (window.heading, window.range_label):
            window.period.layout().removeWidget(label)
            label.setParent(self.title)
        self._period.setParent(self)
        self._period.hide()
        if tight:
            window.heading.setStyleSheet('font-size: 20px; font-weight: 600;')
            window._navigation_width_signature = None
            window.update_navigation_width()
        window.heading.setFixedHeight(self.heading_height)
        window.heading.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        window.range_label.setFixedHeight(self.button_height)
        window.range_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for button in (window.previous_button, window.today_button, window.next_button):
            button.setParent(self.title)
            button.setFixedSize(28, 28)
            button.setIconSize(QSize(18, 18))
        self._build_left()

        # Keep the same group order in every alternative; this comparison is
        # about navigation geometry and selected-state treatment, not ordering.
        tools = self.controls.layout()
        tools.setSpacing(16)
        for index in range(tools.count()):
            section = tools.itemAt(index).widget()
            if section is None:
                continue
            section.setFixedHeight(58 if tight else 68)
            section.layout().setSpacing(2 if tight else 4)
            section.layout().itemAt(1).layout().setContentsMargins(0, 0, 0, 0)
            for button in section.findChildren(QPushButton):
                button.setFixedSize(20 if button.property('compactIcon') else command_size,
                                    20 if button.property('compactIcon') else self.button_height)
            for control in (window.view_control, window.selection_control):
                control.setFixedHeight(self.button_height)
                control.layout().setContentsMargins(0, 0, 0, 0)
            for label in section.findChildren(QLabel):
                label.ensurePolished()
                label.setFixedHeight(self.heading_height)
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
                gap = max(0, window.heading.fontMetrics().descent() - label.fontMetrics().descent())
                label.setContentsMargins(0, 0, 0, gap)
        body = QWidget()
        self.row = QBoxLayout(QBoxLayout.Direction.LeftToRight, body)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(24)
        self.row.setSizeConstraint(QBoxLayout.SizeConstraint.SetNoConstraint)
        self.row.addWidget(self.title, 1, Qt.AlignmentFlag.AlignTop)
        self.row.addWidget(self.controls, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        outer.addWidget(body)
        scheme = scheme_for(window.preferences)
        if style == 'filled':
            for name in ('view_control', 'selection_control'):
                replacement = FlushSegments(window, getattr(window, name))
                replacement.setFixedHeight(self.button_height)
                setattr(window, name, replacement)
            css = f'''
            QWidget#reviewFlushSegments {{ background: transparent; border: none; }}
            QWidget#reviewFlushSegments QPushButton {{ background: transparent; border: none; border-radius: 0; padding: 5px; }}
            QWidget#reviewFlushSegments QPushButton:checked {{ background: transparent; border: none; }}
            QWidget#reviewFlushSegments QPushButton:focus {{ border: 1px dotted {scheme.focus}; }}
            '''
        else:
            css = f'''
            QWidget#segmentedControl {{ background: transparent; border: none; border-bottom: 1px solid {scheme.control_edge}; border-radius: 0; }}
            QWidget#segmentedControl QPushButton {{ background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0; }}
            QWidget#segmentedControl QPushButton:checked {{ background: transparent; border-bottom-color: {scheme.accent}; }}
            QWidget#segmentedControl QPushButton:focus {{ background: {scheme.hover}; }}
            '''
            rule = QFrame()
            rule.setFixedHeight(1)
            rule.setStyleSheet(f'background: {scheme.selection_edge}; border: none;')
            outer.addWidget(rule)
        window.setStyleSheet(window.styleSheet() + css)
        slot = parent.layout().indexOf(old)
        parent.layout().removeWidget(old)
        old.setParent(self)
        old.hide()
        parent.layout().insertWidget(slot, self)
        window.workspace_header = self
        self.reflow()

    def _build_left(self):
        window = self.window
        title, subtitle = window.heading, window.range_label
        if self.layout_name == 'split':
            self.left.setColumnMinimumWidth(0, window.canvas.gutter)
            self.left.addWidget(window.previous_button, 0, 0, Qt.AlignmentFlag.AlignCenter)
            self.left.addWidget(window.today_button, 1, 0, Qt.AlignmentFlag.AlignCenter)
            self.left.addWidget(title, 0, 1)
            self.left.addWidget(subtitle, 1, 1)
            self.left.addWidget(window.next_button, 0, 2, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        else:
            self.left.setColumnMinimumWidth(0, window.canvas.gutter)
            self.left.addWidget(title, 0, 1)
            subtitle.setMinimumWidth(subtitle.fontMetrics().horizontalAdvance(subtitle.text()) + 4)
            band = QWidget()
            row = QHBoxLayout(band)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            row.addWidget(subtitle)
            row.addSpacing(8)
            for button in (window.previous_button, window.today_button, window.next_button):
                row.addWidget(button)
            self.left.addWidget(band, 1, 1)
        self.left.setColumnMinimumWidth(1, self._period.width())

    def minimumSizeHint(self):
        return QSize(0, self.layout().minimumSize().height()) if self.layout() else QSize(0, 0)

    def reflow(self):
        for button in (self.window.previous_button, self.window.today_button, self.window.next_button):
            button.setFixedSize(28, 28)
        for button in self.controls.findChildren(QPushButton):
            size = 20 if button.property('compactIcon') else (28 if self.tight else 32)
            button.setFixedSize(size, size if button.property('compactIcon') else self.button_height)
        for control in (self.window.view_control, self.window.selection_control):
            if isinstance(control, FlushSegments):
                control.setFixedWidth(sum(button.width() for button in control.buttons))
        self.window.heading.ensurePolished()
        for label in self.controls.findChildren(QLabel):
            label.ensurePolished()
            gap = max(0, self.window.heading.fontMetrics().descent() - label.fontMetrics().descent())
            style = f'padding-bottom: {gap}px;'
            if label.styleSheet() != style:
                label.setStyleSheet(style)
        self.left.setColumnMinimumWidth(1, self._period.width())
        needed = self.title.minimumSizeHint().width() + self.controls.sizeHint().width() + 24
        inline = self.width() >= needed
        if inline != self.inline:
            self.inline = inline
            self.row.setDirection(QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom)
            self.row.setSpacing(24 if inline else 12)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


def apply(window, layout_name, style, tight=False):
    if layout_name not in LAYOUTS or style not in STYLES:
        raise ValueError('Unknown header alternative')
    return HeaderOption(window, layout_name, style, tight=tight)
