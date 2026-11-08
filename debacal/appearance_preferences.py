"""Visual appearance choices and a live, local sample of the calendar workspace."""
from dataclasses import replace
from datetime import date

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal, QRegularExpression
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QColorDialog, QFormLayout, QGridLayout, QHBoxLayout,
    QComboBox, QFrame, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

from .event_style import event_colors
from .calendar_appearance import weekend_fill, weekend_text
from .theme import SCHEMES, corner_radius, scheme_for
from .fonts import ui_font
from .year_presence import paint_day_presence


def _label(painter, rect, value, color, size=11, bold=False, align=Qt.AlignmentFlag.AlignLeft):
    font = QFont(painter.font())
    font.setPixelSize(max(8, round(size)))
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(QColor(color))
    value = painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, max(0, int(rect.width())))
    painter.drawText(rect, align | Qt.AlignmentFlag.AlignVCenter, value)


def _card(painter, rect, fill, edge, radius=3):
    painter.setPen(QPen(QColor(edge), 1))
    painter.setBrush(QColor(fill))
    painter.drawRoundedRect(rect, radius, radius)


class _ScrollSafeSpinBox(QSpinBox):
    """Scrolling the settings page must not change a preference."""
    def wheelEvent(self, event):
        event.ignore()


class _ScrollSafeComboBox(QComboBox):
    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


def _segments(painter, rect, preferences):
    colors = scheme_for(preferences)
    third = rect.width() / 3
    if preferences.control_style == 'filled':
        painter.save()
        painter.setClipRect(rect)
        painter.fillRect(rect, QColor(colors.panel))
        painter.fillRect(QRectF(rect.left(), rect.top(), third, rect.height()), QColor(colors.selection))
        painter.restore()
        _card(painter, rect, QColor('transparent'), colors.control_edge, corner_radius(preferences))
    else:
        painter.setPen(QPen(QColor(colors.control_edge), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.fillRect(QRectF(rect.left(), rect.bottom() - 1, third, 2), QColor(colors.accent))
    for index in range(3):
        center = rect.left() + (index + .5) * third
        painter.setPen(QPen(QColor(colors.accent), 1.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(center - 4, rect.center().y() - 4, 8, 8), 1, 1)


def _sample(painter, rect, kind, value, preferences):
    colors = scheme_for(preferences)
    painter.save()
    painter.setClipRect(rect)
    if kind == 'palette':
        candidate = replace(preferences, color_scheme=value)
        colors = scheme_for(candidate)
        for index, color in enumerate((colors.sidebar, colors.accent, colors.weekend, colors.ink)):
            painter.fillRect(QRectF(rect.left() + index * rect.width() / 4, rect.top(), rect.width() / 4, rect.height()), QColor(color))
    elif kind == 'mode':
        candidate = replace(preferences, appearance_mode=value)
        colors = scheme_for(candidate)
        _card(painter, rect.adjusted(1, 1, -1, -1), colors.background, colors.control_edge,
              corner_radius(preferences, 3))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colors.ink))
        painter.drawEllipse(rect.center(), 7, 7)
        if value == 'dark':
            painter.setBrush(QColor(colors.background))
            painter.drawEllipse(rect.center() + QPointF(4, -3), 6, 6)
    elif kind == 'controls':
        _segments(painter, rect.adjusted(3, 1, -3, -1), replace(preferences, control_style=value))
    elif kind == 'agenda':
        x = rect.left() + (24 if value == 'timeline' else 3)
        if value != 'cards':
            _label(painter, QRectF(rect.left(), rect.top(), rect.width(), 12), 'Today', colors.muted, 8)
        start = rect.top() + (13 if value != 'cards' else 3)
        height = 12 if value == 'timeline' else 16
        for index in range(2):
            y = start + index * (height + 3)
            if value == 'timeline':
                _label(painter, QRectF(rect.left(), y, 17, height), '9:00' if not index else '11:00', colors.muted, 7)
                painter.setPen(QPen(QColor(colors.control_edge), 1))
                painter.drawLine(rect.left() + 20, y, rect.left() + 20, y + height + (3 if not index else 0))
            fill = event_colors('#587c67' if not index else '#b6815c', colors)
            box = QRectF(x, y, max(2, rect.right() - x - 2), height)
            _card(painter, box, fill.fill, fill.outline, 2)
            painter.setPen(QPen(QColor(fill.ink), 1))
            painter.drawLine(x + 5, y + 5, min(rect.right() - 6, x + 32), y + 5)
    elif kind == 'weekend':
        column = rect.width() / 3
        candidate = replace(preferences, weekend_style=value)
        for index, title in enumerate(('Fri', 'Sat', 'Sun')):
            cell = QRectF(rect.left() + index * column, rect.top(), column, rect.height())
            shade = weekend_fill(index + 4, candidate, colors)
            if shade:
                painter.fillRect(cell, QColor(shade))
            ink = weekend_text(index + 4, candidate, colors)
            _label(painter, cell.adjusted(1, 0, -1, 0), title, ink, 11, True,
                   Qt.AlignmentFlag.AlignCenter)
    elif kind == 'corners':
        candidate = replace(preferences, corner_style=value)
        _card(painter, rect.adjusted(5, 1, -5, -1), colors.selection, colors.accent,
              corner_radius(candidate, 5))
    elif kind == 'year_view':
        sources = ('#587c67', '#b6815c', '#627fa0')
        width, height = min(42, rect.width() / 3), min(35, rect.height())
        left, top = rect.center().x() - width * 1.5, rect.center().y() - height / 2
        for index in range(3):
            cell = QRectF(left + index * width, top, width, height)
            painter.fillRect(cell, QColor(colors.panel))
            number = paint_day_presence(painter, cell, sources[:index + 1], preferences, colors,
                                       style=value, scale=1)
            _label(painter, number, str(8 + index), colors.ink, 9, align=Qt.AlignmentFlag.AlignCenter)
    painter.restore()


class _ChoiceButton(QPushButton):
    def __init__(self, owner, title, value, kind, description):
        super().__init__(title, owner)
        self.owner, self.value, self.kind = owner, value, kind
        self.setCheckable(True)
        self.setAutoDefault(False)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)
        self.setToolTip(description)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(96, (55 if self.kind in ('agenda', 'year_view') else 34) + max(18, self.fontMetrics().height()) + 8)

    def minimumSizeHint(self):
        return QSize(0, self.sizeHint().height())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            delta = -1 if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1
            index = (self.owner.buttons.index(self) + delta) % len(self.owner.buttons)
            self.owner.setCurrentIndex(index)
            self.owner.buttons[index].setFocus(Qt.FocusReason.TabFocusReason)
            event.accept()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        preferences = self.owner.preferences
        colors = scheme_for(preferences)
        painter = QPainter(self)
        painter.setFont(ui_font(preferences))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        _card(painter, rect, colors.selection if self.isChecked() else colors.hover if self.underMouse() else colors.panel,
              colors.accent if self.isChecked() else colors.control_edge, corner_radius(preferences, 4))
        if self.hasFocus() and self.property('keyboardFocus'):
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(colors.focus), 1, Qt.PenStyle.DotLine))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 3, 3)
        sample_height = 43 if self.kind in ('agenda', 'year_view') else 22
        sample = QRectF(8, 7, self.width() - 16, sample_height)
        _sample(painter, sample, self.kind, self.value, preferences)
        _label(painter, QRectF(7, sample_height + 11, self.width() - 28, self.height() - sample_height - 15), self.text(), colors.ink,
               self.font().pixelSize() if self.font().pixelSize() > 0 else 12, self.isChecked(),
               Qt.AlignmentFlag.AlignCenter)
        if self.isChecked():
            painter.setPen(QPen(QColor(colors.accent), 1.8))
            x, y = self.width() - 14, self.height() - 15
            painter.drawLine(x - 3, y, x, y + 3)
            painter.drawLine(x, y + 3, x + 5, y - 4)


class VisualChoice(QWidget):
    """Selectable previews with the small value API shared by native choices."""
    currentIndexChanged = Signal(int)

    def __init__(self, options, kind, preferences, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.kind = kind
        self.options = list(options)
        self.buttons = []
        self._index = -1
        self._columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(7)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (title, value, description) in enumerate(self.options):
            button = _ChoiceButton(self, title, value, kind, description)
            self.group.addButton(button)
            button.clicked.connect(lambda _=False, item=index: self.setCurrentIndex(item))
            self.buttons.append(button)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCurrentIndex(0)
        self._reflow()

    def count(self):
        return len(self.options)

    def currentData(self):
        return self.options[self._index][1] if self._index >= 0 else None

    def currentText(self):
        return self.options[self._index][0] if self._index >= 0 else ''

    def findData(self, value):
        return next((index for index, option in enumerate(self.options) if option[1] == value), -1)

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self.options):
            return
        changed = self._index != index
        self._index = index
        for item, button in enumerate(self.buttons):
            button.setChecked(item == index)
            button.update()
        if changed:
            self.currentIndexChanged.emit(index)

    def set_preferences(self, preferences):
        self.preferences = preferences
        for button in self.buttons:
            button.update()
        self._reflow()

    def _reflow(self):
        minimum = max(88, round(96 * self.preferences.font_scale / 100))
        columns = max(1, min(len(self.buttons), max(1, self.width() // minimum)))
        if columns != self._columns:
            self._columns = columns
            for index, button in enumerate(self.buttons):
                self.grid.addWidget(button, index // columns, index % columns)
        height = max(button.sizeHint().height() for button in self.buttons)
        for button in self.buttons:
            button.setFixedHeight(height)
        rows = (len(self.buttons) + columns - 1) // columns
        target = height * rows + self.grid.spacing() * (rows - 1)
        if self.height() != target:
            self.setFixedHeight(target)

    def minimumSizeHint(self):
        return QSize(0, self.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and self.buttons:
            self._reflow()


class ColorField(QWidget):
    changed = Signal()

    def __init__(self, title, value, parent=None):
        super().__init__(parent)
        self._valid_color = value
        self.preferences = None
        self.button = QPushButton()
        self.button.setAutoDefault(False)
        self.button.setFixedWidth(36)
        self.button.setAccessibleName('Choose ' + title.lower() + ' color')
        self.button.setToolTip('Choose ' + title.lower() + ' color')
        self.field = QLineEdit(value)
        self.field.setAccessibleName(title + ' color, hexadecimal RGB')
        self.field.setMaxLength(7)
        self.field.setPlaceholderText('#RRGGBB')
        self.field.setMaximumWidth(120)
        self.field.setValidator(QRegularExpressionValidator(QRegularExpression('#[0-9a-fA-F]{6}'), self.field))
        self.field.textChanged.connect(self._change)
        self.button.clicked.connect(self.choose)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        row.addWidget(self.button)
        row.addWidget(self.field, 1)
        row.addStretch(1)
        self._change()

    def _change(self, *_):
        if self.field.hasAcceptableInput():
            self._valid_color = self.field.text()
        self._update_swatch()
        self.changed.emit()

    def _update_swatch(self):
        edge = scheme_for(self.preferences).control_edge
        self.button.setStyleSheet('background: ' + self._valid_color + '; border: 1px solid ' + edge +
                                 '; border-radius: ' + str(corner_radius(self.preferences)) + 'px;')

    def value(self):
        if not self.field.hasAcceptableInput():
            raise ValueError('Enter a complete six-digit RGB color, such as #475f52.')
        return self.field.text()

    def preview_value(self):
        return self._valid_color

    def setColor(self, value):
        self.field.setText(value)

    def set_preferences(self, preferences):
        self.preferences = preferences
        self._update_swatch()

    def choose(self):
        color = QColorDialog.getColor(QColor(self._valid_color), self, 'Choose color')
        if color.isValid():
            self.field.setText(color.name())


class AppearancePreview(QWidget):
    """A fixed sample, never attached to a user's store or calendar accounts."""
    def __init__(self, preferences, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.setObjectName('appearancePreview')
        self.setAccessibleName('Live appearance preview. Saturday is today in this sample.')
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(174)
        self.setMinimumWidth(0)

    def set_preferences(self, preferences):
        self.preferences = preferences
        self.update()

    def paintEvent(self, event):
        preferences, painter = self.preferences, QPainter(self)
        painter.setFont(ui_font(preferences))
        colors = scheme_for(preferences)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        _card(painter, outer, colors.background, colors.control_edge, corner_radius(preferences, 5))
        sidebar = min(128, self.width() * .28)
        painter.fillRect(QRectF(1, 1, sidebar - 1, self.height() - 2), QColor(colors.sidebar))
        scale = preferences.font_scale / 100
        if preferences.show_sidebar_title:
            _label(painter, QRectF(10, 7, sidebar - 20, 22), preferences.sidebar_title, colors.ink, 13 * scale, True)
        top = 34 if preferences.show_sidebar_title else 8
        _label(painter, QRectF(10, top, sidebar - 20, 18), 'Calendars', colors.ink, 10 * scale, True)
        painter.fillRect(QRectF(10, top + 25, 7, 7), QColor('#587c67'))
        _label(painter, QRectF(23, top + 19, sidebar - 30, 20), 'Personal', colors.ink, 10 * scale)
        _label(painter, QRectF(10, top + 44, sidebar - 20, 18), 'Next events', colors.ink, 10 * scale, True)
        _sample(painter, QRectF(9, top + 65, sidebar - 18, min(50, self.height() - top - 69)), 'agenda', preferences.agenda_style, preferences)
        left, right = sidebar + 10, self.width() - 10
        _label(painter, QRectF(left, 7, max(12, right - left - 90), 24), 'November 2026', colors.ink, 14 * scale, True)
        _segments(painter, QRectF(max(left, right - 80), 10, 80, 21), preferences)
        painter.setPen(QPen(QColor(colors.selection_edge), 1))
        painter.drawLine(left, 37, right, 37)
        axis = 27
        column = max(1, (right - left - axis) / 3)
        if preferences.show_week_numbers:
            _label(painter, QRectF(left, 43, axis - 2, 18), 'W45', colors.muted, 8)
        for index, day in enumerate((date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8))):
            x = left + axis + index * column
            shade = weekend_fill(day, preferences, colors)
            if shade:
                painter.fillRect(QRectF(x, 42, column, self.height() - 51), QColor(shade))
            if index == 1:
                painter.fillRect(QRectF(x + 2, 43, column - 4, 31), QColor(colors.selection))
            ink = weekend_text(day, preferences, colors, colors.accent if index == 1 else colors.ink)
            _label(painter, QRectF(x + 5, 42, column - 9, 17), preferences.weekday_name(day).upper(), ink, 9 * scale, True)
            _label(painter, QRectF(x + 5, 59, column - 9, 16), str(day.day), ink, 12 * scale, True)
            painter.setPen(QPen(QColor(colors.line), 1))
            painter.drawLine(x, 42, x, self.height() - 10)
        for index, hour in enumerate((preferences.visible_start_hour, preferences.visible_end_hour)):
            y = 86 + index * 55
            _label(painter, QRectF(left, y - 7, max(16, axis - 3), 15), str(hour), colors.muted, 8)
            painter.setPen(QPen(QColor(colors.line), 1))
            painter.drawLine(left + axis, y, right, y)
        fill = event_colors('#587c67', colors)
        box = QRectF(left + axis + 4, 94, column - 8, 34)
        _card(painter, box, fill.fill, fill.outline, corner_radius(preferences, 3))
        _label(painter, box.adjusted(4, 0, -4, 0), 'Planning', fill.ink, 10 * scale, True)


class AppearanceEditor(QWidget):
    """Compact, named settings sections with one workspace preview."""
    changed = Signal()

    def __init__(self, preferences, parent=None):
        super().__init__(parent)
        self._context = preferences
        self._updating = True
        frame = QVBoxLayout(self)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(14)
        self.preview = AppearancePreview(preferences)
        frame.addWidget(self.preview)
        self.controls_scroll = QScrollArea()
        self.controls_scroll.setObjectName('appearanceControls')
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls = QWidget()
        controls.setObjectName('appearancePage')
        outer = QVBoxLayout(controls)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(16)
        self.controls_scroll.setWidget(controls)
        frame.addWidget(self.controls_scroll, 1)
        self.section_widgets = {}

        layout = self._section(outer, 'Colors')
        self.appearance_mode = VisualChoice([
            ('Light', 'light', 'Light surfaces and dark text.'),
            ('Dark', 'dark', 'Dark surfaces and light text.')], 'mode', preferences)
        self.appearance_mode.setAccessibleName('Brightness')
        self._choice(layout, 'Brightness', self.appearance_mode)
        self.color_scheme = VisualChoice([(scheme.label, value, 'A coordinated ' + scheme.label.lower() + ' palette.')
                                          for value, scheme in SCHEMES.items()] +
                                         [('Custom', 'custom', 'Choose three colors; readable text and borders are derived automatically.')],
                                        'palette', preferences)
        self.color_scheme.setAccessibleName('Color palette')
        self._choice(layout, 'Palette', self.color_scheme)
        self.custom_colors = QWidget()
        form = QFormLayout(self.custom_colors)
        form.setContentsMargins(0, 0, 0, 0)
        self.custom_accent = ColorField('Accent', preferences.custom_accent)
        self.custom_surface = ColorField('Surface tint', preferences.custom_surface)
        self.custom_weekend = ColorField('Weekend', preferences.custom_weekend)
        form.addRow('Accent', self.custom_accent)
        form.addRow('Surface tint', self.custom_surface)
        form.addRow('Weekend', self.custom_weekend)
        self.custom_colors.setToolTip('Surface tint creates light or dark surfaces. Text and borders adjust for contrast.')
        layout.addWidget(self.custom_colors)
        self.weekend_style = VisualChoice([
            ('Text color', 'text', 'Tint weekend labels.'),
            ('Background', 'background', 'Shade weekend cells instead of tinting labels.'),
            ('None', 'none', 'Use the same styling for every weekday.')], 'weekend', preferences)
        self.weekend_style.setAccessibleName('Weekend highlighting')
        self._choice(layout, 'Weekend highlighting', self.weekend_style)

        layout = self._section(outer, 'Font')
        self.font_family = _ScrollSafeComboBox()
        self.font_family.setAccessibleName('Typeface')
        self.font_family.setToolTip('Applies to interface controls and calendar text. System default follows your desktop font.')
        self.font_family.addItem('System default', 'system')
        for family in QFontDatabase.families():
            self.font_family.addItem(family, family)
            self.font_family.setItemData(self.font_family.count() - 1, QFont(family), Qt.ItemDataRole.FontRole)
        if self.font_family.findData(preferences.font_family) < 0:
            self.font_family.addItem(preferences.font_family, preferences.font_family)
        self.font_family.setMaxVisibleItems(15)
        self.font_scale = _ScrollSafeSpinBox()
        self.font_scale.setRange(80, 140)
        self.font_scale.setAccessibleName('Text size percent')
        self.font_scale.setSingleStep(10)
        self.font_scale.setSuffix(' %')
        form = QFormLayout()
        form.addRow('Typeface', self.font_family)
        form.addRow('Text size', self.font_scale)
        layout.addLayout(form)

        layout = self._section(outer, 'Interface')
        self.control_style = VisualChoice([
            ('Underlined', 'underlined', 'Underline the selected choice.'),
            ('Filled', 'filled', 'Fill the selected choice within a continuous group.')], 'controls', preferences)
        self.control_style.setAccessibleName('Button group style')
        self._choice(layout, 'Button groups', self.control_style)
        self.corner_style = VisualChoice([
            ('Square', 'square', 'Straight corners on controls and event cards.'),
            ('Subtle', 'subtle', 'Slightly rounded controls and cards; the current default.'),
            ('Rounded', 'rounded', 'Softer corners on controls and event cards.')], 'corners', preferences)
        self.corner_style.setAccessibleName('Corner shape')
        self._choice(layout, 'Corners', self.corner_style)
        layout = self._section(outer, 'Year view')
        self.year_view_style = VisualChoice([
            ('Calendar tracks', 'tracks', 'Stack wide calendar-color bars beneath each date.'),
            ('Tinted day cells', 'tints', 'Tint each day with calendar-color segments and solid color edges.')],
            'year_view', preferences)
        self.year_view_style.setAccessibleName('Year view appearance')
        self._choice(layout, 'Calendar indicators', self.year_view_style)
        layout = self._section(outer, 'Sidebar')
        self.agenda_style = VisualChoice([
            ('Event cards', 'cards', 'Show dates and times inside each event card.'),
            ('Day groups', 'grouped', 'Group cards under day headings.'),
            ('Timeline', 'timeline', 'Put clocks beside timed events and use full-width all-day cards.')], 'agenda', preferences)
        self.agenda_style.setAccessibleName('Upcoming events layout')
        self._choice(layout, 'Upcoming events', self.agenda_style)
        self.show_sidebar_title = QCheckBox('Show sidebar title')
        self.show_sidebar_title.setChecked(preferences.show_sidebar_title)
        self.sidebar_title = QLineEdit(preferences.sidebar_title)
        self.sidebar_title.setAccessibleName('Sidebar title')
        self.sidebar_title.setMaxLength(120)
        self.sidebar_title.setPlaceholderText('debacal')
        form = QFormLayout()
        form.addRow(self.show_sidebar_title)
        form.addRow('Title', self.sidebar_title)
        layout.addLayout(form)

        outer.addStretch()
        self._choices = (self.appearance_mode, self.color_scheme, self.control_style, self.corner_style,
                         self.agenda_style, self.weekend_style, self.year_view_style)
        for choice in self._choices:
            choice.currentIndexChanged.connect(self._update)
        self.font_scale.valueChanged.connect(self._update)
        self.font_family.currentIndexChanged.connect(self._update)
        self.show_sidebar_title.toggled.connect(self._update)
        self.sidebar_title.textChanged.connect(self._update)
        for field in (self.custom_accent, self.custom_surface, self.custom_weekend):
            field.changed.connect(self._update)
        self.set_preferences(preferences)

    def _section(self, outer, title):
        if self.section_widgets:
            divider = QFrame()
            divider.setObjectName('sourceDivider')
            divider.setFixedHeight(1)
            outer.addWidget(divider)
        widget = QWidget()
        widget.setObjectName('appearanceSection')
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName('sidebarSectionHeading')
        layout.addWidget(label)
        outer.addWidget(widget)
        self.section_widgets[title] = widget
        return layout

    @staticmethod
    def _choice(layout, caption, field):
        label = QLabel(caption)
        label.setBuddy(field.buttons[0])
        layout.addWidget(label)
        layout.addWidget(field)

    def values(self, *, preview=False):
        custom = self.color_scheme.currentData() == 'custom'
        color = lambda field: field.preview_value() if preview or not custom else field.value()
        return dict(color_scheme=self.color_scheme.currentData(), appearance_mode=self.appearance_mode.currentData(),
                    custom_accent=color(self.custom_accent), custom_surface=color(self.custom_surface),
                    custom_weekend=color(self.custom_weekend), control_style=self.control_style.currentData(),
                    corner_style=self.corner_style.currentData(), agenda_style=self.agenda_style.currentData(),
                    year_view_style=self.year_view_style.currentData(),
                    weekend_style=self.weekend_style.currentData(), font_scale=self.font_scale.value(),
                    font_family=self.font_family.currentData(),
                    show_sidebar_title=self.show_sidebar_title.isChecked(),
                    sidebar_title=(self.sidebar_title.text().strip() or self._context.sidebar_title
                                   if preview or not self.show_sidebar_title.isChecked() else self.sidebar_title.text().strip()))

    def set_context(self, preferences):
        self._context = preferences
        self._update()

    def focus_invalid_field(self):
        target = None
        if self.color_scheme.currentData() == 'custom':
            target = next((field.field for field in (self.custom_accent, self.custom_surface, self.custom_weekend)
                           if not field.field.hasAcceptableInput()), None)
        if target is None and self.show_sidebar_title.isChecked() and not self.sidebar_title.text().strip():
            target = self.sidebar_title
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            self.controls_scroll.ensureWidgetVisible(target)
        return target

    def set_preferences(self, preferences):
        self._updating = True
        for field in self._choices:
            field_name = {id(self.appearance_mode): 'appearance_mode', id(self.color_scheme): 'color_scheme',
                          id(self.control_style): 'control_style', id(self.corner_style): 'corner_style',
                          id(self.agenda_style): 'agenda_style', id(self.weekend_style): 'weekend_style',
                          id(self.year_view_style): 'year_view_style'}[id(field)]
            field.setCurrentIndex(field.findData(getattr(preferences, field_name)))
        for name in ('custom_accent', 'custom_surface', 'custom_weekend'):
            getattr(self, name).setColor(getattr(preferences, name))
        self.font_scale.setValue(preferences.font_scale)
        if self.font_family.findData(preferences.font_family) < 0:
            self.font_family.addItem(preferences.font_family, preferences.font_family)
        self.font_family.setCurrentIndex(self.font_family.findData(preferences.font_family))
        self.show_sidebar_title.setChecked(preferences.show_sidebar_title)
        self.sidebar_title.setText(preferences.sidebar_title)
        self._updating = False
        self._update()

    def _update(self, *_):
        if self._updating:
            return
        self.custom_colors.setVisible(self.color_scheme.currentData() == 'custom')
        self.sidebar_title.setEnabled(self.show_sidebar_title.isChecked())
        try:
            preferences = replace(self._context, **self.values(preview=True))
        except ValueError:
            return
        self.preview.set_preferences(preferences)
        for field in (self.custom_accent, self.custom_surface, self.custom_weekend):
            field.set_preferences(preferences)
        for choice in self._choices:
            choice.set_preferences(preferences)
        self.changed.emit()
