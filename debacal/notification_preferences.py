"""Draft controls for local reminder delivery and reusable relative reminders."""
from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QLabel,
                               QStackedWidget, QVBoxLayout, QWidget)

from .buttons import action_button
from .dialog_base import hint
from .reminder_editor import ReminderEditor
from .reminder_preferences import ReminderPreferences


def choice(options):
    box = QComboBox()
    for label, value in options:
        box.addItem(label, value)
    return box


class NotificationPreferencesPage(QWidget):
    testRequested = Signal()

    def __init__(self, value: ReminderPreferences, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        def section(title):
            if layout.count():
                divider = QFrame()
                divider.setObjectName('sourceDivider')
                divider.setFixedHeight(1)
                layout.addWidget(divider)
            heading = QLabel(title)
            heading.setObjectName('sidebarSectionHeading')
            layout.addWidget(heading)
            form = QFormLayout()
            layout.addLayout(form)
            return form

        form = section('Delivery')
        self.enabled = QCheckBox('Enable event notifications')
        self.delivery = choice([('Desktop', 'desktop'), ('In the app', 'in_app'), ('Both', 'both')])
        self.sound = choice([('Follow desktop settings', 'default'), ('Silent', 'silent')])
        self.desktop_status = hint('')
        self.test_button = action_button('Test notification', self.testRequested.emit)
        self.test_result = hint('')
        self.test_result.hide()
        form.addRow(self.enabled)
        form.addRow('Deliver to', self.delivery)
        form.addRow('Desktop sound', self.sound)
        form.addRow('', self.desktop_status)
        form.addRow('', self.test_button)
        form.addRow('', self.test_result)

        form = section('Default reminders')
        self.template_kind = choice([('New timed events', 'new_timed'), ('New all-day events', 'new_all_day'),
                                     ('Manual imports', 'import_alarms')])
        form.addRow('Defaults for', self.template_kind)
        self.import_mode = choice([('Keep saved reminders', 'keep'), ('Remove reminders', 'remove'),
                                   ('Add when missing', 'add_missing'), ('Replace reminders', 'replace')])
        form.addRow('When importing', self.import_mode)
        self.templates = QStackedWidget()
        self.new_timed = ReminderEditor(templates_only=True, parent=self)
        self.new_all_day = ReminderEditor(templates_only=True, parent=self)
        self.new_all_day.all_day = True
        self.import_alarms = ReminderEditor(templates_only=True, parent=self)
        for editor in (self.new_timed, self.new_all_day, self.import_alarms):
            self.templates.addWidget(editor)
        self.templates.setFixedHeight(self.fontMetrics().height() * 7)
        layout.addWidget(self.templates)

        def template_changed():
            importing = self.template_kind.currentData() == 'import_alarms'
            form_defaults.setRowVisible(self.import_mode, importing)
            self.templates.setCurrentIndex(self.template_kind.currentIndex())
            self.templates.setVisible(not importing or self.import_mode.currentData() in ('add_missing', 'replace'))
        form_defaults = form
        self.template_kind.currentIndexChanged.connect(template_changed)
        self.import_mode.currentIndexChanged.connect(template_changed)
        template_changed()

        form = section('Missed reminders')
        self.missed = choice([('Upcoming or ongoing events', 'upcoming'), ('All recent reminders', 'all'),
                              ('Skip missed reminders', 'none')])
        self.missed.setToolTip('Applies to reminders from the last 24 hours.')
        form.addRow('Last 24 hours', self.missed)

        form = section('Background behavior')
        self.close_behavior = choice([('Quit Debacal', 'quit'), ('Keep running in the tray', 'tray')])
        self.tray_status = hint('')
        form.addRow('Close window', self.close_behavior)
        form.addRow('', self.tray_status)
        self.set_availability(None, None)
        self.set_value(value)

    def set_availability(self, desktop, tray):
        for label, name, available in ((self.desktop_status, 'Desktop notifications', desktop),
                                       (self.tray_status, 'System tray', tray)):
            status = 'not checked' if available is None else ('available' if available else 'unavailable')
            label.setText(f'{name} · {status}')

    def set_value(self, value):
        self._value = value
        self.enabled.setChecked(value.enabled)
        for name in ('delivery', 'sound', 'import_mode', 'missed', 'close_behavior'):
            box = getattr(self, name)
            box.setCurrentIndex(box.findData(getattr(value, name)))
        for name in ('new_timed', 'new_all_day', 'import_alarms'):
            getattr(self, name).set_alarms(getattr(value, name))

    def value(self) -> ReminderPreferences:
        return replace(self._value, enabled=self.enabled.isChecked(), delivery=self.delivery.currentData(),
                       sound=self.sound.currentData(), missed=self.missed.currentData(),
                       close_behavior=self.close_behavior.currentData(), import_mode=self.import_mode.currentData(),
                       new_timed=self.new_timed.alarms(), new_all_day=self.new_all_day.alarms(),
                       import_alarms=self.import_alarms.alarms())
