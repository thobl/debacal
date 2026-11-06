"""A: retain the layout and familiar warmth, improve the visual hierarchy."""
from mockup_base import install_paint_palette
from debacal.appearance import stylesheet


def apply(window):
    install_paint_palette(background='#fafbf8', muted='#59675d', ink='#26362e',
                          line='#dce2d8', accent='#416c51', text_scale=1.04)
    window.setStyleSheet(stylesheet() + '''
        QWidget { font-size: 14px; }
        QLabel#brand { font-size: 21px; letter-spacing: -.5px; }
        QLabel#heading { font-size: 25px; }
        QLabel#section { color: #53604f; font-size: 13px; }
        QLabel[muted="true"] { color: #59675d; font-size: 13px; }
        QPushButton#syncStatus { color: #53604f; font-size: 12px; }
        QCalendarWidget QWidget { font-size: 12px; }
        QTreeWidget { border-color: #d4dcce; }
        QTreeWidget::item { padding: 9px 8px; }
        QPushButton:focus, QLineEdit:focus { border-color: #416c51; }
    ''')
    window.sidebar.layout().setSpacing(10)
    window.search.setMinimumHeight(34)
    window.refresh()


def apply_dialog(dialog):
    dialog.layout().setSpacing(10)
