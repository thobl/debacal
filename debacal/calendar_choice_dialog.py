"""The same calendar choice in a compact, keyboard-operable export dialog."""
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout

from .appearance import stylesheet, widget_palette
from .button_focus import install_button_focus
from .calendar_choice import CalendarChoice
from .dialog_base import FormDialog
from .preferences import Preferences


class CalendarChoiceDialog(FormDialog):
    def __init__(self, calendars, parent=None, *, preferences=None, selected_id=None,
                 writable_only=False, title='Choose calendar', accept_text='Continue'):
        super().__init__(parent)
        install_button_focus(QApplication.instance())
        self.preferences = preferences or Preferences()
        self.setWindowTitle(title)
        self.setLocale(self.preferences.qt_locale())
        self.setPalette(widget_palette(self.preferences))
        self.setStyleSheet(stylesheet(self.preferences.font_scale, self.preferences))
        self.resize(round(360 * self.preferences.font_scale / 100), 160)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 16)
        outer.setSpacing(14)
        self.calendar = CalendarChoice(calendars, self.preferences, selected_id=selected_id,
                                       writable_only=writable_only)
        form = QFormLayout()
        label = QLabel('&Calendar')
        label.setBuddy(self.calendar)
        form.addRow(label, self.calendar)
        outer.addLayout(form)
        self.error = QLabel()
        self.error.setProperty('error', True)
        self.error.setWordWrap(True)
        self.error.hide()
        outer.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.accept_button.setText(accept_text)
        self.accept_button.setEnabled(bool(self.calendar.count()))
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        self.configure_keyboard([self.calendar], [self.accept_button,
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel)], self.save)
        self.calendar.setFocus()

    @property
    def selected_calendar(self):
        return self.calendar.selected_calendar

    def save(self):
        if self.selected_calendar is None:
            self.show_error('Choose a calendar.', self.calendar)
            self.error.show()
            return
        self.accept()
