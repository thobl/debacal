"""Exercise calendar popover dragging with native X11 input and save previews."""
import ctypes
from ctypes.util import find_library
from dataclasses import replace
from pathlib import Path
import sys
from time import monotonic

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from PySide6.QtCore import QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from debacal.models import Calendar
from debacal.store import Store
from debacal.ui import MainWindow

x11 = ctypes.CDLL(find_library('X11'))
xtst = ctypes.CDLL(find_library('Xtst'))
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XFlush.argtypes = [ctypes.c_void_p]
xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
display = x11.XOpenDisplay(None)
assert display, 'An X11 display is required.'
app = QApplication([])
store = Store(':memory:')
store.save_calendar(Calendar('work', 'Work', '#7f79a5'))
store.save_calendar(Calendar('readonly', 'Read only', '#b6815c', read_only=True))
window = MainWindow(store, enable_sync=False)
window.resize(1500, 1050)
window.show()
window.activateWindow()
listing = window.calendar_list
errors, captured = [], {}


def move(point):
    xtst.XTestFakeMotionEvent(display, -1, point.x(), point.y(), 0)
    x11.XFlush(display)


def button(down, number=1):
    xtst.XTestFakeButtonEvent(display, number, int(down), 0)
    x11.XFlush(display)


def bounds(widget):
    return QRect(widget.mapToGlobal(QPoint()), widget.size())


def settled(widget):
    previous, since = None, monotonic()
    def ready():
        nonlocal previous, since
        current = bounds(widget) if widget.isVisible() else None
        if current != previous:
            previous, since = current, monotonic()
        return current is not None and monotonic() - since >= .1
    return ready


def capture(state):
    # A root-window capture includes the separate native popover window.
    screen = window.screen()
    picture = screen.grabWindow(0)
    origin = screen.geometry().topLeft()
    client = bounds(window).translated(-origin)
    assert picture.copy(client).save(str(HERE / f'implemented-{state}.png'))
    detail = bounds(window.calendar_section)
    if listing.more_expanded:
        detail = detail.united(bounds(listing.popup))
    assert picture.copy(detail.translated(-origin)).save(str(HERE / f'implemented-{state}-detail.png'))


def begin(identity):
    row = window.calendar_rows[identity]
    captured['origin'] = bounds(row.swatch).center()
    move(captured['origin'])
    button(True)


def popup_target():
    assert listing.more_expanded
    move(bounds(listing.popup_scroll.viewport()).center())


def main_target():
    target = listing.main_drop_target if listing.main_drop_target.isVisible() else listing
    point = bounds(target).center()
    assert not bounds(listing.popup).contains(point), 'Popover covers the main-list drop target.'
    move(point)


def prepare_empty_main():
    listing.set_expanded(False)
    for value in store.list_calendars():
        store.save_calendar(replace(value, read_only=True))
    store.set_calendar_layout(listing.ordered_ids(), [])
    window.refresh_calendar_list(store.list_calendars())
    assert not listing.display_rows(True)
    listing.set_expanded(True)


def prepare_long_popup():
    listing.set_expanded(False)
    for identity in ('local', 'work'):
        store.save_calendar(replace(store.get_calendar(identity), read_only=False))
    store.set_default_calendar('local')
    for index in range(28):
        store.save_calendar(Calendar(f'extra-{index}', f'Occasional {index + 1:02}', '#7f79a5',
                                     in_main_list=False))
    window.refresh_calendar_list(store.list_calendars())
    captured['main_size'] = window.calendar_scroll.size()
    captured['main_scroll'] = window.calendar_scroll.verticalScrollBar().value()
    assert window.calendar_rows['local'] in listing.display_rows(True)
    assert not store.get_calendar('local').in_main_list
    listing.set_expanded(True)


def scroll_popup():
    assert window.calendar_scroll.size() == captured['main_size']
    assert listing.popup_scroll.verticalScrollBar().maximum() > 0
    capture('popup')
    move(bounds(listing.popup_scroll.viewport()).center())
    for _ in range(8):
        button(True, 5)
        button(False, 5)


def verify_scroll_and_select():
    assert window.calendar_scroll.size() == captured['main_size']
    assert window.calendar_scroll.verticalScrollBar().value() == captured['main_scroll']
    listing.popup_scroll.ensureWidgetVisible(window.calendar_rows['work'])


def choose_work():
    QTest.mouseClick(window.calendar_rows['work'].choice, Qt.MouseButton.LeftButton)


def finish():
    assert not listing.more_expanded
    assert store.get_default_calendar().id == 'work'
    assert window.calendar_rows['work'] in listing.display_rows(True)
    assert window.calendar_rows['local'] in listing.display_rows(False)
    assert not store.get_calendar('work').in_main_list


stages = []
def stage(label, action=lambda: None, ready=lambda: True):
    stages.append((label, action, ready))


def begin_drag(identity):
    stage('press ' + identity, lambda: begin(identity), settled(window.calendar_rows[identity].swatch))
    stage('cross drag threshold', lambda: move(captured['origin'] + QPoint(25, 0)),
          lambda: bool(app.mouseButtons() & Qt.MouseButton.LeftButton))
    stage('native drag active', ready=lambda: listing._dragging)


def drop(identity, group):
    stage('release over destination', lambda: button(False))
    stage('membership saved', ready=lambda: not listing._dragging
          and store.get_calendar(identity).in_main_list is group)


stage('main window active', ready=lambda: app.activeWindow() is window)
begin_drag('work')
stage('hover overflow target', lambda: move(bounds(listing.more_button).center()), settled(listing.more_button))
stage('overflow hover opens native popover', ready=lambda: listing.more_expanded)
stage('move into empty popover', popup_target, settled(listing.popup_scroll))
stage('popover accepts native drag', ready=lambda: listing._drop_group is False)
stage('capture drag hint', lambda: capture('drag'))
drop('work', False)
stage('open popover', lambda: listing.set_expanded(True))
begin_drag('work')
stage('cross from popover into main list', main_target)
stage('main list accepts native drag', ready=lambda: listing._drop_group is True)
drop('work', True)
stage('prepare empty main list', prepare_empty_main)
begin_drag('readonly')
stage('cross into empty main list', main_target, settled(listing.main_drop_target))
stage('empty main accepts native drag', ready=lambda: listing._drop_group is True)
drop('readonly', True)
stage('prepare long popover with selected optional calendar', prepare_long_popup)
stage('scroll popover independently', scroll_popup, settled(listing.popup_scroll))
stage('popup scroll moved', verify_scroll_and_select,
      lambda: listing.popup_scroll.verticalScrollBar().value() > 0)
stage('select an occasional calendar', choose_work, settled(window.calendar_rows['work'].choice))
stage('selected optional calendar stays in main list', finish,
      lambda: not listing.more_expanded and store.get_default_calendar().id == 'work')
stage('capture restored main list', lambda: capture('normal'), settled(window.calendar_section))

pump = QTimer()
pump.setInterval(15)
current, deadline, running = None, 0, False


def tick():
    global current, deadline, running
    if running:
        return
    running = True
    try:
        if current is None:
            if not stages:
                print('PASS: native popover drag/drop, empty groups, independent scrolling and visible selection.', flush=True)
                pump.stop()
                app.quit()
                return
            current = stages.pop(0)
            deadline = monotonic() + 5
        label, action, ready = current
        if monotonic() >= deadline:
            raise AssertionError('Timed out: ' + label)
        if ready():
            action()
            print('PASS:', label, flush=True)
            current = None
    except BaseException as error:
        errors.append(error)
        print('FAIL:', current[0], repr(error), 'pointer:', QCursor.pos(), flush=True)
        button(False)
        pump.stop()
        QTimer.singleShot(0, app.quit)
    finally:
        running = False


pump.timeout.connect(tick)
pump.start()
app.exec()
window.close()
store.close()
if errors:
    raise errors[0]
