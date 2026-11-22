"""Real wire checks against an explicitly addressed, private notification service."""
from contextlib import contextmanager
from pathlib import Path
from queue import Queue
import shlex
import sys
from tempfile import TemporaryDirectory
import subprocess
from threading import Event, Thread

from PySide6.QtWidgets import QApplication
from jeepney import DBusAddress, HeaderFields, new_method_return, new_signal
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import Proxy, open_dbus_connection

from alarm_support import BASE, wait_for
from debacal.alarms import Alarm
from debacal.reminder_compilation import DeliveryKey, DueReminder
from debacal.reminder_transport import NotificationTransport


NAME = 'org.freedesktop.Notifications'
PATH = '/org/freedesktop/Notifications'


@contextmanager
def private_bus(*, activated_service=None):
    # Use only the private service directory, even on desktops that install an
    # activatable notification daemon in the standard session service paths.
    with TemporaryDirectory() as directory:
        root = Path(directory)
        services = root/'services'
        services.mkdir()
        if activated_service:
            script = root/'service.py'
            script.write_text(activated_service)
            (services/(NAME + '.service')).write_text(
                '[D-BUS Service]\nName=' + NAME + '\nExec=' + shlex.join([sys.executable, str(script)]) + '\n')
        config = root/'bus.conf'
        config.write_text(f'<busconfig><type>session</type><listen>unix:tmpdir={root}</listen>'
                          f'<servicedir>{services}</servicedir><policy context="default"><allow own="*"/>'
                          '<allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>')
        with subprocess.Popen(['dbus-daemon', '--config-file='+str(config), '--nofork', '--print-address=1'],
                              stdout=subprocess.PIPE, text=True) as daemon:
            try:
                address = daemon.stdout.readline().strip()
                assert address.startswith('unix:')
                yield address
            finally:
                daemon.terminate()
                daemon.wait(timeout=3)


@contextmanager
def service(capabilities):
    with private_bus() as address:
        with running_service(address, capabilities) as (connection, calls):
            yield address, connection, calls


@contextmanager
def running_service(address, capabilities):
    connection = open_dbus_connection(address)
    Proxy(message_bus, connection).RequestName(NAME)
    calls, stopped = Queue(), Event()
    def run():
        while not stopped.is_set():
            try:
                msg = connection.receive(timeout=.05)
            except TimeoutError:
                continue
            member = msg.header.fields.get(HeaderFields.member)
            if member == 'GetCapabilities':
                connection.send_message(new_method_return(msg, 'as', (capabilities,)))
            elif member in ('Notify', 'CloseNotification'):
                calls.put(msg)
                reply = new_method_return(msg, 'u', (0xffffffff,)) if member == 'Notify' else new_method_return(msg)
                connection.send_message(reply)
    thread = Thread(target=run)
    thread.start()
    try:
        yield connection, calls
    finally:
        stopped.set()
        thread.join(2)
        connection.close()



def reminder():
    key = DeliveryKey('event', '', 'alarm', '', BASE, 0)
    return DueReminder(key, 'c', 1, 'stamp', 'Planning <talk>', BASE, BASE,
                       Alarm('alarm', 'DISPLAY', 'PT0S', description='private notes'))


def test_private_wire_and_directed_action():
    app = QApplication.instance() or QApplication([])
    with service(['actions','body','body-markup','sound']) as (address, connection, calls):
        transport = NotificationTransport(bus_address=address)
        delivered, actions = [], []
        transport.delivered.connect(lambda *args: delivered.append(args))
        transport.action.connect(lambda *args: actions.append(args))
        try:
            transport.start()
            wait_for(lambda: transport.available is True)
            due = reminder()
            transport.send(due, calendar_name='Work & Home', profile_label='Test', silent=True)
            wait_for(lambda: delivered)
            call = calls.get_nowait()
            assert call.header.fields[HeaderFields.signature] == 'susssasa{sv}i'
            app_name, replaces, _, title, body, buttons, hints, timeout = call.body
            assert (app_name, replaces, title, timeout) == ('Debacal', 0, 'Planning <talk>', -1)
            assert 'Work &amp; Home' in body and 'Test' in body and 'private notes' not in str(call.body)
            assert buttons == ['default','Open','dismiss','Dismiss','snooze','Snooze']
            assert hints == {'suppress-sound': ('b', True)}
            assert delivered == [(due.key, 0xffffffff)]
            target = DBusAddress(PATH, interface=NAME)
            action = new_signal(target, 'ActionInvoked', 'us', (0xffffffff, 'default'))
            action.header.fields[HeaderFields.destination] = call.header.fields[HeaderFields.sender]
            connection.send_message(action)
            wait_for(lambda: actions)
            assert actions == [(due.key, 'open')]
        finally:
            transport.stop()


def test_passive_server_and_native_close_retires_id():
    app = QApplication.instance() or QApplication([])
    with service([]) as (address, connection, calls):
        transport = NotificationTransport(bus_address=address)
        delivered, actions = [], []
        transport.delivered.connect(lambda *args: delivered.append(args))
        transport.action.connect(lambda *args: actions.append(args))
        try:
            transport.start()
            wait_for(lambda: transport.available is True)
            due = reminder()
            transport.send(due, calendar_name='Appointments', profile_label='Test', silent=False)
            wait_for(lambda: delivered)
            call = calls.get_nowait()
            assert call.body[4:7] == ('', [], {}) and 'Appointments' in call.body[3]
            transport.forget(due.key)
            close = calls.get(timeout=2)
            assert close.header.fields[HeaderFields.signature] == 'u' and close.body == (0xffffffff,)
            # Closing at the server also releases the associated local key.
            transport.send(due, calendar_name='A', profile_label='', silent=False)
            wait_for(lambda: len(delivered)==2)
            call = calls.get_nowait()
            target = DBusAddress(PATH, interface=NAME)
            for member, signature, value in [('NotificationClosed','uu',1), ('ActionInvoked','us','dismiss')]:
                msg = new_signal(target, member, signature, (0xffffffff, value))
                msg.header.fields[HeaderFields.destination] = call.header.fields[HeaderFields.sender]
                connection.send_message(msg)
            wait_for(lambda: due.key not in transport._tokens)
            assert not actions
        finally:
            transport.stop()


def test_missing_owner_recovery_pending_old_reply_and_id_reuse():
    app = QApplication.instance() or QApplication([])
    with private_bus() as address:
        check_owner_recovery(address)


def check_owner_recovery(address):
    first, second = (open_dbus_connection(address) for _ in range(2))
    transport = NotificationTransport(bus_address=address)
    changes, delivered, failed = [], [], []
    transport.availabilityChanged.connect(changes.append)
    transport.delivered.connect(lambda *args: delivered.append(args))
    transport.failed.connect(lambda *args: failed.append(args))

    def method(conn, name):
        while True:
            msg = conn.receive(timeout=2)
            if msg.header.fields.get(HeaderFields.member) == name:
                return msg

    try:
        transport.start()
        wait_for(lambda: changes == [False])
        Proxy(message_bus, first).RequestName(NAME)
        first.send_message(new_method_return(method(first, 'GetCapabilities'), 'as', (['body'],)))
        wait_for(lambda: changes[-1] is True)
        due = reminder()
        transport.send(due, calendar_name='First', profile_label='', silent=False)
        old_call = method(first, 'Notify')
        Proxy(message_bus, first).ReleaseName(NAME)
        Proxy(message_bus, second).RequestName(NAME)
        second.send_message(new_method_return(method(second, 'GetCapabilities'), 'as', ([],)))
        wait_for(lambda: len(changes) >= 4 and changes[-2:] == [False, True])
        first.send_message(new_method_return(old_call, 'u', (7,)))
        wait_for(lambda: failed)
        assert not delivered
        transport.send(due, calendar_name='Second', profile_label='', silent=False)
        second.send_message(new_method_return(method(second, 'Notify'), 'u', (7,)))
        wait_for(lambda: delivered)
        assert delivered == [(due.key, 7)]
    finally:
        transport.stop()
        first.close()
        second.close()



def test_notify_timeout_is_bounded_and_never_replayed():
    app = QApplication.instance() or QApplication([])
    with service(['body']) as (address, connection, calls):
        transport = NotificationTransport(bus_address=address, timeout=.10)
        failures = []
        transport.failed.connect(lambda *args: failures.append(args))
        try:
            transport.start()
            wait_for(lambda: transport.available is True)
            # A held reply exercises the actual native call timeout.
            original = connection.send_message
            def hold_returns(msg, **kwargs):
                if msg.header.fields.get(HeaderFields.signature) != 'u':
                    original(msg, **kwargs)
            connection.send_message = hold_returns
            transport.send(reminder(), calendar_name='Work', profile_label='', silent=False)
            wait_for(lambda: failures)
            assert calls.qsize() == 1 and failures[0][0] == reminder().key
        finally:
            transport.stop()

def test_starts_activatable_private_service_without_an_existing_owner():
    app = QApplication.instance() or QApplication([])
    script = '''import os
from jeepney import HeaderFields, new_method_return
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import Proxy, open_dbus_connection
conn = open_dbus_connection(os.environ['DBUS_STARTER_ADDRESS'])
Proxy(message_bus, conn).RequestName('org.freedesktop.Notifications')
while True:
    try:
        msg = conn.receive(timeout=3)
    except (TimeoutError, ConnectionError):
        break
    if msg.header.fields.get(HeaderFields.member) == 'GetCapabilities':
        conn.send_message(new_method_return(msg, 'as', ([],)))
'''
    with private_bus(activated_service=script) as address:
        transport = NotificationTransport(bus_address=address)
        try:
            transport.start()
            wait_for(lambda: transport.available is True)
        finally:
            transport.stop()


def test_dismiss_while_native_reply_pending_withdraws_eventual_id():
    app = QApplication.instance() or QApplication([])
    with private_bus() as address:
        conn = open_dbus_connection(address)
        Proxy(message_bus, conn).RequestName(NAME)
        transport = NotificationTransport(bus_address=address)
        delivered = []
        transport.delivered.connect(lambda *args: delivered.append(args))
        def method(name):
            while True:
                msg = conn.receive(timeout=1)
                if msg.header.fields.get(HeaderFields.member) == name:
                    return msg
        try:
            transport.start()
            conn.send_message(new_method_return(method('GetCapabilities'), 'as', ([],)))
            wait_for(lambda: transport.available)
            due = reminder()
            transport.send(due, calendar_name='A', profile_label='', silent=False)
            notify = method('Notify')
            transport.forget(due.key)
            conn.send_message(new_method_return(notify, 'u', (300,)))
            close = method('CloseNotification')
            assert close.body == (300,)
            conn.send_message(new_method_return(close))
            app.processEvents()
            assert not delivered
        finally:
            transport.stop()
            conn.close()
