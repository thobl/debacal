"""Focused layout/style alternatives; never imported by the application."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QWidget, QBoxLayout, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel, QFrame, QPushButton

from debacal.theme import scheme_for


LAYOUTS = {
    'compact': dict(name='Compact reference',
        detail='Smaller borderless navigation stays together to the left of the month. The title remains offset from the first day boundary.'),
    'split': dict(name='Arrows beside the month',
        detail='Month and range align with the first day boundary. Previous/next flank the month; Today sits in the axis gutter beside the range. Navigation positions are reserved for the longest month.'),
    'range': dict(name='Navigation with the range',
        detail='The month starts at the first day boundary. Previous/Today/next share the lower line with the date range; the range itself is consequently indented.'),
}
STYLES = {
    'filled': dict(name='Filled choices',
        detail='A quiet filled choice within one outer group outline, no inner selected border, and ghost action buttons. The group surface separates controls from the calendar.'),
    'underlined': dict(name='Underlined choices + separator',
        detail='Square, flat underlines for chosen options. A separate rule and gap distinguish the complete command area from the calendar, using 13px more height.'),
}


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
                                    20 if button.property('compactIcon') else command_size)
            for control in (window.view_control, window.selection_control):
                control.setFixedHeight(self.button_height)
                control.layout().setContentsMargins(1 if style == 'filled' else 2, 1,
                                                   1 if style == 'filled' else 2, 1)
            for label in section.findChildren(QLabel):
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
            edge, panel = QColor(scheme.selection_edge), QColor(scheme.panel)
            selected_fill = QColor(*(round(a * .55 + b * .45) for a, b in
                zip((edge.red(), edge.green(), edge.blue()), (panel.red(), panel.green(), panel.blue())))).name()
            css = f'''
            QWidget#segmentedControl {{ background: {scheme.panel}; border: 1px solid {scheme.control_edge}; border-radius: 6px; }}
            QWidget#segmentedControl QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; }}
            QWidget#segmentedControl QPushButton:checked {{ background: {selected_fill}; border-color: transparent; }}
            QWidget#segmentedControl QPushButton:focus {{ border-color: {scheme.focus}; }}
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
        if self.layout_name == 'compact':
            nav = QWidget()
            row = QHBoxLayout(nav)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            for button in (window.previous_button, window.today_button, window.next_button):
                row.addWidget(button)
            self.left.addWidget(nav, 1, 0, Qt.AlignmentFlag.AlignVCenter)
            self.left.setColumnMinimumWidth(0, 96)
            self.left.addWidget(title, 0, 1)
            self.left.addWidget(subtitle, 1, 1)
        elif self.layout_name == 'split':
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
            for button in (window.previous_button, window.today_button, window.next_button):
                row.addWidget(button)
            row.addSpacing(6)
            row.addWidget(subtitle)
            self.left.addWidget(band, 1, 1)
        self.left.setColumnMinimumWidth(1, self._period.width())

    def minimumSizeHint(self):
        return QSize(0, self.layout().minimumSize().height()) if self.layout() else QSize(0, 0)

    def reflow(self):
        for button in (self.window.previous_button, self.window.today_button, self.window.next_button):
            button.setFixedSize(28, 28)
        if self.tight:
            for button in self.controls.findChildren(QPushButton):
                size = 20 if button.property('compactIcon') else 28
                button.setFixedSize(size, size)
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
