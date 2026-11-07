"""Render the refined flow and verify one-click and keyboard interactions."""
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from prototype import EventStudy, PresetPreferences
from debacal.button_focus import install_button_focus
from debacal.patterns import expand_sequence


HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'


def settle(app):
    for _ in range(7):
        app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def point_at(strip, minute):
    return QPoint(round(strip.gutter + 20), round(strip.y_for(minute)))


def drag(widget, first, last):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseMove(widget, last)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=last)


def click_day(window, day):
    cell = window.month.cell_rect(day)
    point = QPoint(round(cell.left() + 10), round(cell.top() + 7))
    QTest.mouseClick(window.month.grid, Qt.MouseButton.LeftButton, pos=point)


def assert_sequence(window):
    values = expand_sequence(window.sequence_field.text(), window.entries[0].start)
    assert [value.timestamp() for value in values] == [event.start.timestamp() for event in window.entries]
    assert '+30min' not in window.sequence_field.text() and '+1h' not in window.sequence_field.text()


def verify_single(app, window):
    window.load_scene('single')
    settle(app)
    assert not window.series_panel.isVisible() and not window.count_label.isVisible()
    assert window.month.chip_rects(window.selected_day) == []
    start, duration = window.selected.start, window.duration
    click_day(window, start.date() + timedelta(days=2))
    settle(app)
    assert window.selected_day == start.date() + timedelta(days=2)
    assert window.duration == duration and window.selected.end - window.selected.start == duration
    QTest.mouseClick(window.strip, Qt.MouseButton.LeftButton, pos=point_at(window.strip, 11 * 60))
    settle(app)
    assert window.selected.start.hour == 11 and window.duration == duration
    drag(window.strip, point_at(window.strip, 13 * 60), point_at(window.strip, 14 * 60 + 30))
    settle(app)
    assert window.selected.start.hour == 13 and window.duration == timedelta(minutes=90)
    rect, _item, _handle = window.strip.band_rects()[0]
    origin = rect.center().toPoint()
    shift = round(window.strip.y_for(14 * 60) - window.strip.y_for(13 * 60))
    drag(window.strip, origin, origin + QPoint(0, shift))
    settle(app)
    assert window.selected.start.hour == 14 and window.duration == timedelta(minutes=90)
    _rect, _item, handle = window.strip.band_rects()[0]
    drag(window.strip, handle.center().toPoint(), point_at(window.strip, 16 * 60 + 30))
    settle(app)
    assert window.selected.end.hour == 16 and window.selected.end.minute == 30
    window.clock_field.setFocus()
    window.clock_field.setText('10:15')
    QTest.keyClick(window.clock_field, Qt.Key.Key_Return)
    settle(app)
    assert window.selected.start.hour == 10 and window.selected.start.minute == 15
    assert window.isVisible() and len(window.entries) == 1
    window.date_field.setText('13.11.2026')
    window.apply_date()
    assert window.selected_day.day == 13 and window.month.current_month.month == 11


def verify_series(app, window):
    window.load_scene('times')
    settle(app)
    assert window.series_panel.isVisible()
    assert len(window.month.chip_rects(window.entries[0].start.date())) == 2
    count, selected = len(window.entries), window.selected_id
    day = window.entries[0].start.date() + timedelta(days=1)
    _chip, target, remove = window.month.chip_rects(day)[1]
    assert target.id != selected and not remove.isEmpty()
    before = {event.id: event.start for event in window.entries}
    QTest.mouseClick(window.month.grid, Qt.MouseButton.LeftButton, pos=remove.center().toPoint())
    settle(app)
    assert len(window.entries) == count - 1 and window.selected_id == selected
    assert target.id not in {event.id for event in window.entries}
    assert all(event.start == before[event.id] for event in window.entries)
    extra_day = window.entries[0].start.date() + timedelta(days=9)
    click_day(window, extra_day)
    settle(app)
    assert len(window.entries) == count and any(event.start.date() == extra_day for event in window.entries)
    click_day(window, extra_day)
    settle(app)
    assert len(window.entries) == count - 1
    assert not any(event.start.date() == extra_day for event in window.entries)
    window.select_entry('first')
    settle(app)
    first, count = window.entries[0], len(window.entries)
    QTest.mouseClick(window.strip, Qt.MouseButton.LeftButton, pos=point_at(window.strip, 12 * 60))
    settle(app)
    assert len(window.entries) == count + 1
    assert window.selected.start.hour == 12 and window.selected.start.date() == first.start.date()
    assert window.entries[0].start == first.start
    assert_sequence(window)
    later = {event.id: event.start for event in window.entries if event.id != window.selected_id}
    window.clock_field.setText('12:30')
    window.apply_clock()
    assert all(event.start == later[event.id] for event in window.entries if event.id in later)
    assert_sequence(window)
    # A regular draft still offers a one-click omitted date.
    window.load_scene('single')
    window.repeat_field.setCurrentIndex(1)
    settle(app)
    omitted = window.entries[0].start.date() + timedelta(days=3)
    click_day(window, omitted)
    settle(app)
    assert window.mode == 'custom' and len(window.entries) == 5
    assert not any(event.start.date() == omitted for event in window.entries)
    assert_sequence(window)
    window.repeat_field.setCurrentIndex(0)
    settle(app)
    assert len(window.entries) == 1 and not window.series_panel.isVisible()
    assert window.month.chip_rects(window.selected_day) == [] and window.height() <= 700


def verify_presets_and_end(app, window):
    window.load_scene('single')
    preferences = PresetPreferences(window)
    preferences.show()
    settle(app)
    duration = window.duration
    preferences.values.setText('15min, 1h, 2h')
    preferences.buttons.button(QDialogButtonBox.StandardButton.Apply).click()
    settle(app)
    assert window.presets == [timedelta(minutes=value) for value in (15, 60, 120)]
    assert window.duration == duration
    window.presets_row.itemAt(2).widget().click()
    assert window.duration == timedelta(hours=2)
    preferences.values.setText('0min')
    assert not preferences.buttons.button(QDialogButtonBox.StandardButton.Apply).isEnabled()
    preferences.close()
    preferences.deleteLater()
    window.presets = [timedelta(minutes=value) for value in (30, 45, 60)]
    window.rebuild_presets()
    window.load_scene('end')
    finish = window.selected.end
    window.clock_field.setText('09:15')
    window.apply_clock()
    assert window.selected.end == finish and window.duration == timedelta(minutes=30)
    window.end_date_button.click()
    assert window.date_target == 'end'
    click_day(window, window.selected_day)
    assert window.date_target == 'start' and window.selected.end == finish
    window.load_scene('single')
    settle(app)


def render():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    install_button_focus(app)
    IMAGES.mkdir(exist_ok=True)
    errors, metrics = [], {}
    with patch('sys.excepthook', side_effect=lambda _kind, error, _trace: errors.append(str(error))):
        window = EventStudy()
        try:
            window.show()
            window.activateWindow()
            settle(app)
            verify_single(app, window)
            verify_series(app, window)
            verify_presets_and_end(app, window)
            assert not errors, errors
        finally:
            window.close()
            window.deleteLater()
            settle(app)
        for theme, scene in (('light', 'single'), ('dark', 'single'), ('light', 'series'), ('dark', 'series'),
                             ('light', 'times'), ('light', 'end'), ('light', 'preferences')):
            window = EventStudy(theme=theme, scene=scene)
            try:
                window.show()
                settle(app)
                target = window
                if scene == 'preferences':
                    window.show_preset_preferences()
                    window.preferences_dialog.values.setText('15min, 30min, 1h, 2h')
                    settle(app)
                    target = window.preferences_dialog
                name = f'{scene}-{theme}'
                assert target.grab().save(str(IMAGES / (name + '.png')))
                metrics[name] = {'width': target.width(), 'height': target.height(),
                                 'occurrences': len(window.entries), 'sequence': window.sequence_field.text()}
            finally:
                if window.preferences_dialog:
                    window.preferences_dialog.close()
                window.close()
                window.deleteLater()
                settle(app)
        assert not errors, errors
    (HERE / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    print('Rendered seven native states. Single-event, direct remove, date toggle, same-day time, duration, keyboard and preset checks passed.')


if __name__ == '__main__':
    render()
