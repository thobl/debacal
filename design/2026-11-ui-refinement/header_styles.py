"""Review-only header: grouped controls and explicit shared alignment rows."""
from types import MethodType

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QBoxLayout, QLabel, QPushButton


def _reflow(header):
    """Wrap all command groups together; keep their order and internal geometry."""
    needed = header.title.minimumSizeHint().width() + header.controls.sizeHint().width() + 24
    inline = header.width() >= needed
    if inline == header.inline:
        return
    header.inline = inline
    header.row.setDirection(QBoxLayout.Direction.LeftToRight if inline else QBoxLayout.Direction.TopToBottom)
    header.row.setSpacing(24 if inline else 12)
    header._range.setFixedHeight(36 if inline else 20)
    header._navigation.setContentsMargins(0, 32 if inline else 0, 0, 0)
    header.updateGeometry()


def apply(window):
    header = window.workspace_header
    controls = window.toolbar.layout()
    sections = {}
    labels = []
    while controls.count():
        item = controls.takeAt(0)
        widget = item.widget()
        if widget is not None:
            sections[widget.accessibleName()] = widget
    controls.setSpacing(16)
    for name in ('View', 'Selection', 'Events'):
        section = sections[name]
        controls.addWidget(section)
        column = section.layout()
        column.setSpacing(4)
        heading = column.itemAt(0).layout()
        row = column.itemAt(1).layout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0 if name == 'Selection' else 4)
        for index in range(heading.count()):
            widget = heading.itemAt(index).widget()
            if isinstance(widget, QLabel):
                widget.setFixedHeight(28)
                widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
                widget.setStyleSheet('color: #596570; font-size: 12px; font-weight: 600;')
                labels.append(widget)
        for button in section.findChildren(QPushButton):
            button.setFixedSize(24, 24) if button.property('compactIcon') else button.setFixedSize(32, 32)
        # Ordinary action rows and segmented controls share a 36px lower row.
        for index in range(row.count()):
            widget = row.itemAt(index).widget()
            if widget is not None and widget.objectName() == 'segmentedControl':
                widget.layout().setContentsMargins(2, 2, 2, 2)
                widget.setFixedHeight(36)
        row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        section.setFixedHeight(68)
    for button in (window.previous_button, window.today_button, window.next_button):
        button.setFixedSize(32, 32)
    window.heading.setStyleSheet('color: #27313a; font-size: 22px; font-weight: 600;')
    window.heading.setFixedHeight(28)
    window.heading.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
    window.heading.ensurePolished()
    # Align text baselines, rather than the unequal descender boxes of 22px
    # period text and 12px group labels.
    for label in labels:
        label.ensurePolished()
        label.setContentsMargins(0, 0, 0, max(0, window.heading.fontMetrics().descent()
                                             - label.fontMetrics().descent()))
    window.range_label.setStyleSheet('color: #596570; font-size: 12px;')
    window.range_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    window.period.layout().setSpacing(4)
    window._navigation_width_signature = None
    window.update_navigation_width()
    header._range = window.range_label
    header._navigation = header.title.layout().itemAt(0).layout()
    header.title.layout().setSpacing(12)
    header.reflow = MethodType(_reflow, header)
    header.inline = None
    header.reflow()
    outer = header.parentWidget().layout()
    outer.setContentsMargins(20, 16, 20, 8)
    outer.setSpacing(16)
