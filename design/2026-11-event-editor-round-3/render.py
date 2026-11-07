"""Native review screenshots and interaction checks for the third editor study."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import picker as picker_module
import preview as preview_module
from prototype import EventStudy
from debacal.button_focus import install_button_focus
from debacal.patterns import expand_sequence


HERE = Path(__file__).resolve().parent
IMAGES = HERE / 'images'


def settle(app):
    for _ in range(7):
        app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def pointer(strip, minute):
    return QPoint(round(strip.gutter + 24), round(strip.y_for(minute)))


def drag(widget, first, last):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=first)
    QTest.mouseMove(widget, last)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=last)


def hover(widget, point):
    # Offscreen modal windows do not reliably receive passive native cursor
    # moves. Dispatch the same Qt event directly, including its global point.
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(widget.mapToGlobal(point)),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def times(window):
    return {event.start.timestamp(): event.end.timestamp() for event in window.entries}


def assert_expression(window):
    assert [value.timestamp() for value in expand_sequence(window.expression(), window.primary.start)] == [
        event.start.timestamp() for event in window.entries]


def verify_primary_and_navigation(app, window):
    window.load_scene('occurrence')
    settle(app)
    original = window.primary
    window.select_occurrence('occ-2')
    assert window.start_field.edit.text() == window.format_datetime(original.start)
    assert window.end_field.edit.text() == window.format_datetime(original.end)
    window.start_field.edit.setFocus()
    window.start_field.edit.setText('05.11.2026 10:15')
    QTest.keyClick(window.start_field.edit, Qt.Key.Key_Return)
    settle(app)
    assert window.isVisible() and window.primary.start.day == 5 and window.primary.start.hour == 10
    assert window.primary.start.minute == 15 and window.duration == timedelta(minutes=45)
    assert window.primary.end.hour == 11 and window.primary.end.minute == 0
    assert len(window.entries) == 5 and window.entries[1].start.day == 12
    assert window.entries[2].start.day == 13 and window.entries[2].start.hour == 10
    assert_expression(window)
    window.duration_field.setText('1h 30min')
    window.apply_duration()
    assert window.duration == timedelta(minutes=90) and window.primary.end.minute == 45
    window.duration_buttons[0].click()
    assert window.duration == timedelta(minutes=30) and window.primary.end.minute == 45
    original = window.primary
    window.preview.month_next_button.click()
    assert window.primary == original
    window.preview.first_button.click()
    settle(app)
    assert window.selected_id == 'primary' and window.preview.current_month.month == window.primary.start.month
    window.preview.next_occurrence_button.click()
    assert window.selected_id == 'occ-1' and window.primary == original
    assert window.start_field.edit.text() == window.format_datetime(original.start)
    window.preview.previous_occurrence_button.click()
    assert window.selected_id == 'primary'
    # First means the primary template, even when an added date is earlier.
    earlier = replace(window.primary, id='earlier', start=window.primary.start - timedelta(days=3),
                      end=window.primary.end - timedelta(days=3))
    window.set_occurrences([*window.entries, earlier], 'earlier')
    window.preview.first_button.click()
    settle(app)
    assert window.selected_id == 'primary' and window.preview.previous_occurrence_button.isEnabled()
    assert window.preview.position_label.text().startswith('2/')


def verify_shared_picker(app, window):
    window.load_scene('editor')
    original = window.primary
    popup = window.open_primary_picker('start')
    settle(app)
    assert not popup.strip.start_handle().isEmpty() and not popup.strip.end_handle().isEmpty()
    popup.choose_date(original.start.date() + timedelta(days=2))
    assert popup.draft_start.date() == original.start.date() + timedelta(days=2)
    assert popup.draft_end - popup.draft_start == timedelta(minutes=45)
    assert window.primary == original
    popup.cancel_button.click()
    assert window.primary == original and popup.result_start == original.start
    popup = window.open_primary_picker('end')
    settle(app)
    # Both handles work on a same-day picker even when End opened it.
    drag(popup.strip, popup.strip.start_handle().center().toPoint(), pointer(popup.strip, 8 * 60 + 30))
    settle(app)
    assert popup.draft_start.hour == 8 and popup.draft_start.minute == 30
    assert popup.draft_end == original.end
    drag(popup.strip, popup.strip.end_handle().center().toPoint(), pointer(popup.strip, 10 * 60))
    settle(app)
    assert popup.draft_end.hour == 10 and popup.draft_start.minute == 30
    band = popup.strip.band_rect()
    delta = round(popup.strip.y_for(10 * 60) - popup.strip.y_for(9 * 60))
    drag(popup.strip, band.center().toPoint(), band.center().toPoint() + QPoint(0, delta))
    settle(app)
    assert popup.draft_start.hour == 9 and popup.draft_start.minute == 30 and popup.draft_end.hour == 11
    drag(popup.strip, pointer(popup.strip, 13 * 60), pointer(popup.strip, 14 * 60 + 30))
    settle(app)
    assert popup.draft_start.hour == 13 and popup.draft_end.hour == 14 and popup.draft_end.minute == 30
    hover(popup.strip, pointer(popup.strip, 11 * 60 + 30))
    settle(app)
    with patch.object(picker_module, 'text', wraps=picker_module.text) as draw:
        popup.strip.grab()
    assert any(call.args[2] == '11:30' for call in draw.call_args_list)
    popup.apply_button.click()
    assert window.primary.start.hour == 13 and window.primary.end.hour == 14
    assert window.duration == timedelta(minutes=90)


def verify_multi_day(app, window):
    window.load_scene('start')
    original = window.primary
    with patch.object(preview_module, 'text', wraps=preview_module.text) as draw:
        window.preview.grid.grab()
    captions = [call.args[2] for call in draw.call_args_list]
    assert '21:00 →' in captions and '→ 09:00' in captions
    popup = window.open_primary_picker('start')
    settle(app)
    assert popup.strip.continues_after and not popup.strip.continues_before
    assert not popup.strip.start_handle().isEmpty() and popup.strip.end_handle().isEmpty()
    # A continued body does not move the endpoint beyond this day's view.
    band = popup.strip.band_rect()
    drag(popup.strip, band.center().toPoint(), band.center().toPoint() + QPoint(0, -20))
    assert popup.draft_start == original.start and popup.draft_end == original.end
    drag(popup.strip, popup.strip.start_handle().center().toPoint(), pointer(popup.strip, 22 * 60))
    settle(app)
    assert popup.draft_start.hour == 22 and popup.draft_end == original.end
    popup.apply_button.click()
    assert window.primary.start.hour == 22 and window.primary.end == original.end
    popup = window.open_primary_picker('end')
    settle(app)
    assert popup.strip.continues_before and not popup.strip.continues_after
    assert popup.strip.start_handle().isEmpty() and not popup.strip.end_handle().isEmpty()
    previous_start = window.primary.start
    drag(popup.strip, popup.strip.end_handle().center().toPoint(), pointer(popup.strip, 10 * 60 + 30))
    settle(app)
    assert popup.draft_start == previous_start and popup.draft_end.hour == 10 and popup.draft_end.minute == 30
    popup.apply_button.click()
    assert window.primary.start == previous_start and window.primary.end.day == 5
    window.duration_field.setText('2d 3h')
    window.apply_duration()
    assert window.primary.end == window.primary.start + timedelta(days=2, hours=3)


def verify_add_remove_and_crowding(app, window):
    window.load_scene('occurrence')
    window.select_occurrence('occ-2')
    original, before = window.primary, times(window)
    selected_day = window.entries[2].start.date()
    popup = window.add_occurrence()
    settle(app)
    assert popup.draft_start.date() == selected_day
    assert popup.draft_start.time() == original.start.time()
    assert popup.draft_end - popup.draft_start == window.duration
    popup.choose_date(selected_day + timedelta(days=1))
    popup.set_start(popup.draft_start.replace(hour=11, minute=30))
    popup.set_end(popup.draft_start + timedelta(minutes=90))
    assert window.primary == original and times(window) == before
    popup.apply_button.click()
    settle(app)
    assert window.primary == original and len(window.entries) == len(before) + 1
    added = next(event for event in window.entries if event.start.timestamp() not in before)
    assert added.start.hour == 11 and added.start.minute == 30
    assert added.end - added.start == timedelta(minutes=90)
    assert all(times(window)[start] == end for start, end in before.items())
    assert window.start_field.edit.text() == window.format_datetime(original.start)
    assert_expression(window)
    popup = window.add_occurrence()
    popup.set_start(popup.draft_start + timedelta(days=4))
    popup.cancel_button.click()
    assert len(window.entries) == len(before) + 1
    # Date add/remove remains a single click, and protects the primary.
    window.preview.show_month(original.start.date())
    day = original.start.date() + timedelta(days=5)
    cell = window.preview.grid.cell_rect(day)
    point = QPoint(round(cell.left() + 8), round(cell.top() + 5))
    QTest.mouseClick(window.preview.grid, Qt.MouseButton.LeftButton, pos=point)
    settle(app)
    assert any(event.start.date() == day for event in window.entries)
    QTest.mouseClick(window.preview.grid, Qt.MouseButton.LeftButton, pos=point)
    settle(app)
    assert not any(event.start.date() == day for event in window.entries)
    assert window.primary == original
    window.load_scene('editor')
    settle(app)
    width = window.preview.grid.width()
    extras = [replace(window.primary, id=f'dense-{index}', start=window.primary.start + timedelta(minutes=30 * index),
                      end=window.primary.end + timedelta(minutes=30 * index)) for index in range(1, 13)]
    window.set_occurrences([window.primary, *extras])
    settle(app)
    assert len(window.preview.grid.chip_rects(window.primary.start.date())) == 13
    assert window.preview.grid.width() == width
    assert window.preview.scroll.verticalScrollBar().maximum() > 0
    window.select_occurrence('primary')
    target = window.entries[-1]
    window.preview.show_event(target.id)
    settle(app)
    remove = next(remove for _rect, event, remove in window.preview.grid.chip_rects(target.start.date()) if event.id == target.id)
    assert target.id != window.selected_id
    QTest.mouseClick(window.preview.grid, Qt.MouseButton.LeftButton, pos=remove.center().toPoint())
    settle(app)
    assert len(window.entries) == 12 and window.selected_id == 'primary'


def save(widget, name, metrics):
    assert widget.grab().save(str(IMAGES / (name + '.png')))
    metrics[name] = {'width': widget.width(), 'height': widget.height()}


def render():
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    install_button_focus(app)
    IMAGES.mkdir(exist_ok=True)
    errors, metrics = [], {}
    with patch('sys.excepthook', side_effect=lambda _type, error, _trace: errors.append(str(error))):
        for style in ('underlined', 'filled'):
            window = EventStudy(buttons=style)
            try:
                window.show()
                window.activateWindow()
                settle(app)
                verify_primary_and_navigation(app, window)
                verify_shared_picker(app, window)
                verify_multi_day(app, window)
                verify_add_remove_and_crowding(app, window)
                assert not errors, errors
            finally:
                if window.picker:
                    window.picker.close()
                window.close()
                window.deleteLater()
                settle(app)
        for theme in ('light', 'dark'):
            window = EventStudy(theme=theme)
            try:
                window.show()
                settle(app)
                save(window, 'editor-' + theme, metrics)
            finally:
                window.close()
                window.deleteLater()
                settle(app)
        for scene in ('same-day', 'start', 'end', 'occurrence', 'changed-primary'):
            window = EventStudy(scene=scene)
            try:
                window.show()
                settle(app)
                if scene == 'changed-primary':
                    save(window, 'changed-primary-light', metrics)
                    continue
                if scene == 'occurrence':
                    window.select_occurrence('occ-2')
                    popup = window.add_occurrence()
                    popup.choose_date(popup.draft_start.date() + timedelta(days=1))
                    popup.set_start(popup.draft_start.replace(hour=11, minute=30))
                else:
                    popup = window.open_primary_picker('end' if scene == 'end' else 'start')
                settle(app)
                if scene == 'same-day':
                    hover(popup.strip, pointer(popup.strip, 11 * 60 + 30))
                    settle(app)
                save(popup, 'occurrence-light' if scene == 'occurrence' else 'picker-' + scene + '-light', metrics)
            finally:
                if window.picker:
                    window.picker.close()
                window.close()
                window.deleteLater()
                settle(app)
        assert not errors, errors
    (HERE / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    print('Rendered seven native states; primary edits, navigation, shared range picker, overnight endpoints, additions, cancel/apply, and crowded one-click removal passed in both button styles.')


if __name__ == '__main__':
    render()
