"""Persistent freedesktop notification connection, confined to its asyncio loop."""
import asyncio
from contextlib import ExitStack, suppress
from html import escape

from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.asyncio import DBusRouter, open_dbus_connection

NAME = 'org.freedesktop.Notifications'
PATH = '/org/freedesktop/Notifications'
BUS = 'org.freedesktop.DBus'
ACTIONS = ['default', 'Open', 'dismiss', 'Dismiss', 'snooze', 'Snooze']


class NativeBus:
    def __init__(self, address, post, timeout):
        self.address, self.post, self.timeout = address, post, timeout
        self.owner = None
        self.generation = 0
        self.caps = None
        self.ids = {}
        self.tasks = set()
        self.router = None

    def spawn(self, work):
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def call(self, msg, signature, sender=None):
        reply = await asyncio.wait_for(self.router.send_and_get_reply(msg), self.timeout)
        if (reply.header.message_type != MessageType.method_return
                or reply.header.fields.get(HeaderFields.signature, '') != signature
                or sender and reply.header.fields.get(HeaderFields.sender) != sender):
            raise ValueError('The desktop notification service returned an unexpected reply.')
        return reply.body

    def method(self, name, signature=None, body=()):
        target = DBusAddress(PATH, bus_name=self.owner, interface=NAME)
        return new_method_call(target, name, signature, body)

    async def discover(self, owner, generation):
        try:
            caps, = await self.call(self.method('GetCapabilities'), 'as', owner)
        except Exception:
            caps = None
        if self.generation == generation:
            self.caps = None if caps is None else set(caps)
            self.post('availability', self.caps is not None)

    def owner_changed(self, owner):
        self.generation += 1
        for key, token in self.ids.values():
            self.post('retired', (key, token))
        self.ids.clear()
        self.owner, self.caps = owner, None
        self.post('owner', self.generation)
        if owner:
            self.spawn(self.discover(owner, self.generation))
        else:
            self.post('availability', False)

    async def probe(self, *, activate):
        generation = self.generation
        try:
            if activate:
                # StartServiceByName is idempotent for a running server.
                await self.call(message_bus.StartServiceByName(NAME), 'u', BUS)
            owner, = await self.call(message_bus.GetNameOwner(NAME), 's', BUS)
        except Exception:
            # Non-activatable services can still be present under a live name.
            try:
                owner, = await self.call(message_bus.GetNameOwner(NAME), 's', BUS)
            except Exception:
                owner = ''
        if generation == self.generation and (owner != self.owner or self.caps is None):
            self.owner_changed(owner)

    def signal(self, msg):
        fields = msg.header.fields
        sender = fields.get(HeaderFields.sender)
        member = fields.get(HeaderFields.member)
        sig = fields.get(HeaderFields.signature)
        if sender == BUS and member == 'NameOwnerChanged' and sig == 'sss' and msg.body[0] == NAME:
            self.owner_changed(msg.body[2])
        elif sender == self.owner and fields.get(HeaderFields.interface) == NAME:
            if member == 'NotificationClosed' and sig == 'uu':
                if entry := self.ids.pop(msg.body[0], None):
                    self.post('retired', entry)
            if member == 'ActionInvoked' and sig == 'us':
                action = {'default': 'open', 'dismiss': 'dismiss', 'snooze': 'snooze'}.get(msg.body[1])
                if action and (entry := self.ids.pop(msg.body[0], None)):
                    self.post('action', (*entry, self.generation, action))

    async def connected(self):
        conn = await asyncio.wait_for(open_dbus_connection(self.address), self.timeout)
        async with conn, DBusRouter(conn) as router:
            self.router = router
            owner_rule = MatchRule(type='signal', sender=BUS, interface=BUS, path='/org/freedesktop/DBus',
                                   member='NameOwnerChanged')
            owner_rule.add_arg_condition(0, NAME)
            queue = asyncio.Queue()
            with ExitStack() as filters:
                for rule in [owner_rule, *(MatchRule(type='signal', path=PATH, interface=NAME, member=member)
                                           for member in ('ActionInvoked', 'NotificationClosed'))]:
                    filters.enter_context(router.filter(rule, queue=queue))
                    daemon_rule = rule.serialise()
                    if rule is not owner_rule:
                        daemon_rule += ",sender='" + NAME + "'"
                    await self.call(message_bus.AddMatch(daemon_rule), '', BUS)
                self.spawn(self.probe(activate=True))
                while not conn.reader.at_eof():
                    try:
                        self.signal(await asyncio.wait_for(queue.get(), 1))
                    except TimeoutError:
                        continue
                raise EOFError

    async def run(self):
        while True:
            try:
                await self.connected()
            except Exception:
                self.owner_changed('')
            finally:
                self.router = None
                tasks = tuple(self.tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(2)

    async def notify(self, due, token, calendar, profile, silent, generation, current):
        key = due.key
        try:
            if not current():
                return
            if generation != self.generation or self.caps is None:
                raise ValueError
            when = (due.start or key.trigger_utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')
            context = ' · '.join(filter(None, (when, calendar, profile)))
            title = due.title
            body = ''
            if 'body' in self.caps:
                body = escape(context) if 'body-markup' in self.caps else context
            else:
                title += ' — ' + context
            actions = ACTIONS if 'actions' in self.caps else []
            hints = {'suppress-sound': ('b', True)} if silent else {}
            msg = self.method('Notify', 'susssasa{sv}i', ('Debacal', 0, 'debacal', title, body, actions, hints, -1))
            remote_id, = await self.call(msg, 'u', self.owner)
            if generation != self.generation or not 0 < remote_id <= 0xffffffff:
                raise ValueError
            if not current():
                await self.call(self.method('CloseNotification', 'u', (remote_id,)), '', self.owner)
                return
            if previous := self.ids.get(remote_id):
                self.post('retired', previous)
            self.ids[remote_id] = (key, token)
            self.post('delivered', (key, token, generation, remote_id))
        except (Exception, asyncio.CancelledError):
            self.post('failed', (key, token, 'Desktop notification delivery could not be confirmed.'))

    async def withdraw(self, key, token):
        for remote_id, entry in tuple(self.ids.items()):
            if entry == (key, token):
                del self.ids[remote_id]
                with suppress(Exception):
                    await self.call(self.method('CloseNotification', 'u', (remote_id,)), '', self.owner)
