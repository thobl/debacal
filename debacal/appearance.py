"""Workspace styling, kept separate from calendar behavior."""
import re


_BASE_STYLE = """
QWidget { font-family: "Sans Serif"; font-size: 13px; color: #29382f; }
QMainWindow, QDialog { background: #fafbf8; }
QWidget#sidebar, QWidget#sidebarShell, QWidget#sidebarFooter { background: #edf0e8; }
QWidget#sidebarShell { border-right: 1px solid #dce2d5; }
QLabel[muted="true"] { color: #788373; font-size: 12px; }
QLabel#brand { font-size: 25px; font-weight: 700; letter-spacing: -1px; }
QLabel#heading { font-size: 24px; font-weight: 600; }
QLabel#section { color: #65735f; font-size: 12px; font-weight: 600; }
QPushButton { background: transparent; border: 1px solid #dce2d6; border-radius: 6px; padding: 7px 12px; }
QPushButton:hover { background: #e5ebde; }
QPushButton:pressed, QPushButton:checked { background: #e0e9d8; border-color: #b7c8ac; color: #35513d; }
QPushButton#primary { background: #446d50; color: #fff; border: 1px solid #446d50; font-weight: 600; }
QPushButton#primary:hover { background: #355b40; }
QPushButton:disabled { color: #a5aea0; }
QLineEdit, QTextEdit, QDateTimeEdit, QComboBox, QKeySequenceEdit { background: #fff; border: 1px solid #dce2d6; border-radius: 5px; padding: 6px; selection-background-color: #c9dcc1; }
QLineEdit:focus, QTextEdit:focus { border-color: #719268; }
QListWidget, QTreeWidget { background: #fafbf8; border: 1px solid #e1e6da; border-radius: 5px; outline: none; }
QListWidget::item, QTreeWidget::item { padding: 8px; }
QListWidget::item:selected, QTreeWidget::item:selected { color: #2c4835; background: #e2ebda; }
QScrollArea { border: none; background: #fafbf8; }
QScrollBar:vertical { background: #f0f3ec; width: 9px; }
QScrollBar::handle:vertical { background: #cbd4c4; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCalendarWidget QWidget { background: transparent; font-size: 11px; }
QCalendarWidget QToolButton { color: #3b563e; border: none; padding: 2px; }
QCalendarWidget QAbstractItemView { background: #edf0e8; alternate-background-color: #edf0e8; color: #29382f; selection-background-color: #496e50; selection-color: white; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #aabb9e; border-radius: 3px; background: #fafbf8; }
QCheckBox::indicator:checked { background: #65865b; border-color: #65865b; }
QLabel#notice { background: #edf0dc; padding: 9px; color: #626a39; border-radius: 5px; }
QPushButton#calendarChoice { text-align: left; border: 1px solid transparent; padding: 5px 7px; }
QPushButton#calendarChoice:checked { border-color: #b7c8ac; background: #e0e9d8; font-weight: 600; }

QPushButton[iconOnly="true"] { padding: 5px; }
QPushButton[compactIcon="true"] { padding: 1px; border-color: transparent; border-radius: 4px; }
QWidget#segmentedControl { background: #edf0e8; border: 1px solid #d4ddcd; border-radius: 8px; }
QWidget#segmentedControl QPushButton { background: transparent; border: 1px solid transparent; border-radius: 0; }
QWidget#segmentedControl QPushButton[segment="first"] { border-top-left-radius: 5px; border-bottom-left-radius: 5px; }
QWidget#segmentedControl QPushButton[segment="last"] { border-top-right-radius: 5px; border-bottom-right-radius: 5px; }
QWidget#segmentedControl QPushButton:hover { background: #e0e7d9; }
QWidget#segmentedControl QPushButton:checked { background: #fff; border-color: #b4c6aa; color: #35513d; }
QWidget#segmentedControl QPushButton:pressed { background: #dce7d3; }
QPushButton#syncStatus { text-align: left; border: none; padding: 5px 0; font-size: 11px; color: #65735f; }
QMenu { background: #fafbf8; border: 1px solid #dce2d6; padding: 4px; }
QMenu::item { padding: 7px 18px; }
QMenu::item:selected { background: #e0e9d8; }
QTabWidget::pane { border: 1px solid #dce2d6; }
QTabBar::tab { padding: 9px 12px; }
QTabBar::tab:selected { background: #e0e9d8; }
QToolTip { background: #fafbf8; color: #29382f; border: 1px solid #b7c8ac; padding: 5px; }
"""


def stylesheet(font_scale=100):
    return re.sub(r"font-size: (\d+)px",
                  lambda match: f"font-size: {round(int(match[1]) * font_scale / 100)}px",
                  _BASE_STYLE)
