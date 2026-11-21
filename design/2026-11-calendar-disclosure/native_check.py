"""Check disclosure dragging with real X11 input and save native previews."""
import ctypes
from ctypes.util import find_library
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
assert display
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
height = listing.more_button.height()


def move(point):
    xtst.XTestFakeMotionEvent(display, -1, point.x(), point.y(), 0)
    x11.XFlush(display)


def button(down):
    xtst.XTestFakeButtonEvent(display, 1, int(down), 0)
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
    picture = window.grab()
    assert picture.save(str(HERE / f'implemented-{state}.png'))
    section = window.calendar_section
    rect = QRect(section.mapTo(window, QPoint()), section.size())
    assert picture.copy(rect).save(str(HERE / f'implemented-{state}-detail.png'))


def divider(dragging):
    assert listing.more_button.height() == height
    assert listing.more_button.text() == ('More calendars' if dragging else '')


def begin(identity):
    captured['origin'] = bounds(window.calendar_rows[identity].swatch).center()
    move(captured['origin'])
    button(True)


def more():
    divider(True)
    move(bounds(listing.more_button).center())


def prepare_all_more():
    divider(False)
    assert not listing.more_expanded
    capture('collapsed')
    store.set_calendar_layout(listing.ordered_ids(), [])
    window.refresh_calendar_list(store.list_calendars())
    listing.set_expanded(True)


def close_more():
    assert not window.calendar_rows['readonly'].choice.isEnabled()
    assert not store.get_calendar('local').in_main_list
    assert not store.get_calendar('work').in_main_list
    listing.set_expanded(False)


def eye():
    assert all(not value.in_main_list for value in store.list_calendars())
    assert listing.ordered_ids()[0] == 'readonly'
    captured['visible'] = store.get_calendar('work').visible
    QTest.mouseClick(window.calendar_rows['work'].eye, Qt.MouseButton.LeftButton)


def finish():
    assert store.get_calendar('work').visible != captured['visible']
    assert all(not value.in_main_list for value in store.list_calendars())
    assert listing.more_expanded
    divider(False)
    capture('expanded')


stages = []
def stage(label, action=lambda: None, ready=lambda: True):
    stages.append((label, action, ready))


def drag(identity, target, group, *, hover=False):
    stage('press ' + identity, lambda: begin(identity), settled(window.calendar_rows[identity].swatch))
    stage('cross drag threshold', lambda: move(captured['origin'] + QPoint(25, 0)),
          lambda: bool(app.mouseButtons() & Qt.MouseButton.LeftButton))
    stage('wait for native drag', ready=lambda: listing._dragging and target.isVisible())
    stage('move to current target', more if not group else lambda: move(bounds(target).center()), settled(target))
    stage('target accepts drag', ready=lambda: listing._drop_group is group and bounds(target).contains(QCursor.pos()))
    if hover:
        stage('hover opens More calendars', ready=lambda: listing.more_expanded)
        stage('retarget expanded divider', more, settled(target))
        stage('expanded divider accepts drag', ready=lambda: listing._drop_group is False and bounds(target).contains(QCursor.pos()))
    stage('capture drag state', lambda: capture('drag'))
    stage('release over target', lambda: button(False))
    stage('membership saved', ready=lambda: not listing._dragging and store.get_calendar(identity).in_main_list is group)
    stage('clear drag caption', lambda: divider(False))

stage('main window active', ready=lambda: app.activeWindow() is window)
drag('local', listing.more_button, False)
stage('prepare empty Main group', prepare_all_more, settled(listing.more_button))
drag('readonly', listing.main_drop_target, True)
stage('close More calendars', close_more)
drag('readonly', listing.more_button, False, hover=True)
stage('toggle eye independently', eye, settled(window.calendar_rows['work'].eye))
stage('verify final state', finish, lambda: store.get_calendar('work').visible != captured['visible'])

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
                print('PASS: three native drag/drop gestures, empty groups, hover expansion, independent eye, stable divider.', flush=True)
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
