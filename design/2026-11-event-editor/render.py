"""Render native studies and exercise their temporary interaction model."""
import json
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from prototype import EditorStudy
from debacal.button_focus import install_button_focus
from debacal.patterns import expand_sequence


HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'


def settle(app):
    for _ in range(8):
        app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def save(window, name):
    assert window.grab().save(str(IMAGES / (name + '.png')))


def verify_interactions(app, window):
    original = {item.id: item.start for item in window.entries}
    literal = window.sequence_field.text()
    if window.option == 'b':
        window.change_view(0)
        settle(app)
    day = window.entries[2].start.date()
    rect, item = window.month.grid.event_rects(day)[0]
    QTest.mouseClick(window.month.grid, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())
    settle(app)
    assert window.selected_id == item.id
    assert window.sequence_field.text() == literal
    assert {item.id: item.start for item in window.entries} == original
    surface = (window.day_planner or window.week_planner)
    surface = surface.canvas if surface else window.ribbons
    assert surface.hasFocus()
    QTest.keyClick(surface, Qt.Key.Key_Down)
    settle(app)
    assert window.selected.start == original[item.id] + timedelta(minutes=15)
    assert all(value.start == original[value.id] for value in window.entries if value.id != item.id)
    assert expand_sequence(window.sequence_field.text(), window.entries[0].start) == [value.start for value in window.entries]
    QTest.keyClick(surface, Qt.Key.Key_Delete)
    settle(app)
    assert item.id not in {value.id for value in window.entries}
    assert len(window.entries) == len(original) - 1
    # Exercise the real drag path as well as the keyboard alternative.
    moving = window.entries[1]
    before = {value.id: value.start for value in window.entries}
    window.select(moving.id)
    settle(app)
    if window.ribbons:
        rect = next(rect for rect, value in window.ribbons.hits if value.id == moving.id)
        delta = QPoint(round((surface.width() - surface.gutter - 12) / 12), 0)
    else:
        rect = next(rect for rect, value in surface.rects() if value.id == moving.id)
        delta = QPoint(0, round(surface.hour_height))
    point = QPoint(round(rect.left() + 8), round(rect.top() + 8))
    QTest.mousePress(surface, Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(surface, point + delta)
    QTest.mouseRelease(surface, Qt.MouseButton.LeftButton, pos=point + delta)
    settle(app)
    assert window.selected.start == before[moving.id] + timedelta(hours=1)
    assert all(value.start == before[value.id] for value in window.entries if value.id != moving.id)
    assert expand_sequence(window.sequence_field.text(), window.entries[0].start) == [value.start for value in window.entries]
    window.selected_clock.setFocus()
    window.selected_clock.setText('12:30')
    QTest.keyClick(window.selected_clock, Qt.Key.Key_Return)
    settle(app)
    assert window.selected.start.hour == 12 and window.selected.start.minute == 30
    assert window.isVisible() and window.date_target is None
    duration = window.duration
    first = window.entries[0]
    window.pick_date('first')
    window.click_date(first.start.date() + timedelta(days=2))
    assert window.entries[0].start == first.start + timedelta(days=2)
    assert window.entries[0].end - window.entries[0].start == duration
    assert window.duration == duration
    window.reset()
    extra_day = window.entries[0].start.date() + timedelta(days=8)
    window.click_date(extra_day)
    assert any(value.start.date() == extra_day for value in window.entries)
    window.click_date(extra_day)
    assert not any(value.start.date() == extra_day for value in window.entries)
    window.reset()
    window.change_timing_mode(1)
    assert window.end_controls.isVisible() and not window.duration_field.isVisible()
    window.end_clock.setText('10:30')
    window.change_end()
    assert window.duration == timedelta(minutes=90)
    finish = window.entries[0].end
    window.start_clock.setText('09:30')
    window.change_first_clock()
    assert window.entries[0].end == finish
    assert window.duration == timedelta(minutes=60)
    window.change_timing_mode(0)
    window.reset()
    settle(app)


def render():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    install_button_focus(app)
    IMAGES.mkdir(exist_ok=True)
    metrics = {}
    for theme in ('light', 'dark'):
        for option in 'abc':
            window = EditorStudy(option, theme)
            try:
                window.show()
                window.activateWindow()
                settle(app)
                if theme == 'light':
                    verify_interactions(app, window)
                # Keep sample selection and focus comparable between appearances.
                window.title_field.setFocus(Qt.FocusReason.MouseFocusReason)
                settle(app)
                save(window, option + '-' + theme)
                metrics[option + '-' + theme] = {
                    'width': window.width(), 'height': window.height(),
                    'sequence': window.sequence_field.text(),
                    'occurrences': len(window.entries),
                    'duration_minutes': window.duration.total_seconds() / 60,
                }
                if theme == 'light':
                    selected = window.entries[1]
                    window.move(selected.id, selected.start + timedelta(minutes=30), selected.end + timedelta(minutes=30))
                    window.select(selected.id)
                    settle(app)
                    save(window, option + '-selected' if option != 'b' else 'b-week')
                    if option == 'a':
                        window.reset()
                        window.change_timing_mode(1)
                        window.end_clock.setText('10:30')
                        window.change_end()
                        settle(app)
                        save(window, 'a-end')
            finally:
                window.close()
                window.deleteLater()
                settle(app)
    (HERE / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    print('Rendered 10 native previews; pointer, keyboard, sequence and duration checks passed for A/B/C.')


if __name__ == '__main__':
    render()
