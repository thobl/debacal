"""Workspace styling, kept separate from calendar behavior and source colors."""
from dataclasses import asdict
import re
from string import Template
from PySide6.QtGui import QColor, QPalette

from .theme import scheme_for
from .control_glyphs import control_glyphs


_BASE_STYLE = Template("""
QWidget { font-family: "Sans Serif"; font-size: 13px; color: $ink; }
QMainWindow, QDialog { background: $background; }
QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter, QWidget#sidebarOverview { background: $background; }
QWidget#sidebarShell { border-right: 1px solid $line; }
QWidget#sidebarFooter { border-top: 1px solid $line; }
QWidget#sourceSections { background: $sidebar; border-top: 1px solid $line; }
QWidget#calendarSection, QWidget#agendaSection, QWidget#sidebarSectionHeader { background: transparent; }
QWidget#calendarListBody, QWidget#calendarListViewport, QScrollArea#calendarSources { background: $sidebar; }
QFrame#sourceDivider { background: $line; border: none; }
QLabel[muted="true"] { color: $muted; font-size: 12px; }
QLabel[error="true"] { color: $error; }
QLabel#brand { font-size: 20px; font-weight: 600; letter-spacing: -0.5px; }
QLabel#heading { font-size: 22px; font-weight: 600; }
QLabel#section { color: $muted; font-size: 12px; font-weight: 600; }
QLabel#sidebarSectionHeading { color: $ink; font-size: 13px; font-weight: 600; }
QPushButton { background: transparent; border: 1px solid $control_edge; border-radius: 4px; padding: 7px 11px; }
QPushButton:hover { background: $hover; }
QPushButton:pressed, QPushButton:checked { background: $selection; border-color: $selection_edge; color: $accent; }
QPushButton[keyboardFocus="true"]:focus, QToolButton[keyboardFocus="true"]:focus { border: 1px solid $focus; }
QPushButton:disabled { color: $disabled; border-color: $line; }
QPushButton#primary { background: $accent; color: $on_accent; border-color: $accent; font-weight: 600; }
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary[keyboardFocus="true"]:focus { border: 2px solid $ink; padding: 6px 10px; }
QPushButton#primary:disabled { background: $control_surface; color: $disabled; border-color: $control_edge; }
QPushButton[iconOnly="true"] { background: transparent; border-color: transparent; padding: 5px; }
QPushButton[iconOnly="true"]:hover { background: $hover; border-color: transparent; }
QPushButton[iconOnly="true"]:pressed, QPushButton[iconOnly="true"]:checked { background: $selection; border-color: transparent; }
QPushButton[iconOnly="true"][keyboardFocus="true"]:focus { border-color: $focus; }
QPushButton[compactIcon="true"] { padding: 1px; border-color: transparent; }
QPushButton[compactIcon="true"][keyboardFocus="true"]:focus { border-color: $focus; }
QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox, QKeySequenceEdit {
    background: $panel; color: $ink; border: 1px solid $control_edge; border-radius: 4px; padding: 6px;
    selection-background-color: $selection; selection-color: $ink;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QKeySequenceEdit:focus { border-color: $focus; }
QComboBox, QAbstractSpinBox { padding-right: 30px; }
QComboBox::drop-down { subcontrol-origin: border; subcontrol-position: top right; width: 24px;
    background: $control_surface; border: 1px solid $control_edge; border-top-right-radius: 4px; border-bottom-right-radius: 4px; }
QComboBox::down-arrow { image: url("$arrow_down"); width: 12px; height: 10px; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button { subcontrol-origin: border; width: 24px;
    background: $control_surface; border-left: 1px solid $control_edge; }
QAbstractSpinBox::up-button { subcontrol-position: top right; border-top: 1px solid $control_edge;
    border-right: 1px solid $control_edge; border-bottom: 1px solid $control_edge; border-top-right-radius: 4px; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; border-right: 1px solid $control_edge;
    border-bottom: 1px solid $control_edge; border-bottom-right-radius: 4px; }
QAbstractSpinBox::up-arrow { image: url("$arrow_up"); width: 12px; height: 10px; }
QAbstractSpinBox::down-arrow { image: url("$arrow_down"); width: 12px; height: 10px; }
QComboBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:disabled { image: url("$arrow_disabled_down"); }
QAbstractSpinBox::up-arrow:disabled { image: url("$arrow_disabled_up"); }
QComboBox::drop-down:hover, QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: $hover; }
QComboBox::drop-down:pressed, QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed { background: $selection; }
QComboBox QAbstractItemView { background: $panel; color: $ink; selection-background-color: $selection; selection-color: $ink; }
QListWidget, QTreeWidget { background: $panel; border: 1px solid $line; border-radius: 4px; }
QListWidget:focus, QTreeWidget:focus { border-color: $focus; }
QListWidget::item, QTreeWidget::item { padding: 8px; }
QListWidget::item:selected, QTreeWidget::item:selected { color: $ink; background: $selection; }
QTreeWidget#searchResults::item:selected { background: transparent; }
QTreeWidget#searchResults::branch, QTreeWidget#searchResults::branch:selected { background: $sidebar; }
QTreeWidget#searchResults::branch:has-children { image: none; }
QTreeWidget#searchResults { background: transparent; border: none; }
QScrollArea { border: none; background: $background; }
QScrollBar:vertical { background: $control_surface; width: 8px; }
QScrollBar::handle:vertical { background: $control_edge; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCalendarWidget QWidget { background: transparent; font-size: 12px; }
QCalendarWidget QToolButton { color: $accent; border: none; padding: 2px; }
QCalendarWidget QToolButton[keyboardFocus="true"]:focus { border: 1px solid $focus; }
QCalendarWidget QAbstractItemView { background: $background; alternate-background-color: $background; color: $ink; selection-background-color: $selection; selection-color: $ink; }
QCheckBox { spacing: 8px; padding: 4px 0; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid $control_edge; border-radius: 3px; background: $panel; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }
QCheckBox[keyboardFocus="true"]::indicator:focus { border-color: $focus; }
QPushButton#calendarChoice, QPushButton#calendarChoice:checked { text-align: left; background: transparent; color: $ink; border: 1px solid transparent; padding: 5px 7px; }
QPushButton#calendarChoice:checked { font-weight: 600; }
QPushButton#calendarChoice:focus { border-color: transparent; }
QPushButton#calendarVisibility, QPushButton#calendarVisibility:checked { background: transparent; border-color: transparent; }
QPushButton#calendarVisibility:hover { background: $hover; }
QPushButton#calendarVisibility[keyboardFocus="true"]:focus { border-color: $focus; }
QWidget#segmentedControl { background: transparent; border: none; border-radius: 0; }
QWidget#segmentedControl QPushButton,
QWidget#segmentedControl QPushButton:checked,
QWidget#segmentedControl QPushButton:hover,
QWidget#segmentedControl QPushButton:pressed,
QWidget#segmentedControl QPushButton:focus { background: transparent; border: none; border-radius: 0; }
QPushButton#syncStatus { text-align: left; border: 1px solid transparent; padding: 4px 0; font-size: 12px; color: $muted; }
QPushButton#syncStatus[keyboardFocus="true"]:focus { border-color: $focus; }
QMenu { background: $panel; color: $ink; border: 1px solid $control_edge; padding: 4px; }
QMenu::item { padding: 7px 18px; }
QMenu::item:selected { background: $selection; color: $ink; }
QTabWidget::pane { background: $background; border: 1px solid $line; }
QWidget#preferencesPage, QWidget#appearancePage { background: $background; }
QTabBar::tab { background: $control_surface; color: $ink; padding: 8px 12px; border: 1px solid $line; }
QTabBar::tab:selected { background: $selection; color: $accent; }
QTabBar::tab:hover { background: $hover; }
QTableWidget { background: $panel; color: $ink; gridline-color: $line; border: 1px solid $line; }
QHeaderView::section { background: $control_surface; color: $ink; padding: 6px; border: 1px solid $line; }
QToolTip { background: $panel; color: $ink; border: 1px solid $control_edge; padding: 6px; }
""")


def stylesheet(font_scale=100, color_scheme='neutral'):
    """Render interface roles while retaining the existing text-scale API."""
    scheme = scheme_for(color_scheme)
    up, down = control_glyphs(scheme.ink)
    disabled_up, disabled_down = control_glyphs(scheme.disabled)
    style = _BASE_STYLE.substitute(asdict(scheme) | {
        'arrow_up': up, 'arrow_down': down, 'arrow_disabled_up': disabled_up, 'arrow_disabled_down': disabled_down})
    return re.sub(r"font-size: (\d+)px",
                  lambda match: f"font-size: {round(int(match[1]) * font_scale / 100)}px",
                  style)


def widget_palette(preferences):
    """Give native subcontrols and popups the same roles as painted surfaces."""
    scheme = scheme_for(preferences)
    palette = QPalette()
    for role, value in {
        QPalette.ColorRole.Window: scheme.background,
        QPalette.ColorRole.WindowText: scheme.ink,
        QPalette.ColorRole.Base: scheme.panel,
        QPalette.ColorRole.AlternateBase: scheme.sidebar,
        QPalette.ColorRole.Text: scheme.ink,
        QPalette.ColorRole.Button: scheme.control_surface,
        QPalette.ColorRole.ButtonText: scheme.ink,
        QPalette.ColorRole.Highlight: scheme.selection,
        QPalette.ColorRole.HighlightedText: scheme.ink,
        QPalette.ColorRole.ToolTipBase: scheme.panel,
        QPalette.ColorRole.ToolTipText: scheme.ink,
        QPalette.ColorRole.PlaceholderText: scheme.muted,
    }.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(scheme.disabled))
    return palette
