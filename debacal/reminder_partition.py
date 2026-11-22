"""Bind sidecar mutations and source reads to the acquired owner and canonical file set."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .reminder_sources import ReminderSources


class Partition:
    def __init__(self, database, db):
        self.database, self.db = database, db
        self.owner = self.reader = self.epoch = None

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, json.dumps(value)))

    def attach(self, owner, now):
        if owner.epoch is None or owner.database != str(Path(self.database).expanduser().resolve()):
            raise RuntimeError('A delivery owner for this calendar database is required.')
        self.owner, self.epoch = owner, owner.epoch
        self.refresh(now)
        with self.db:
            self.set('owner', self.epoch)
            self.db.execute("UPDATE ledger SET status='uncertain' WHERE status='pending'")

    def check_owner(self):
        if self.database == ':memory:' and self.owner is None:
            return
        if (self.owner is None or self.epoch != self.owner.epoch or self.epoch != self.get('owner')
                or str(Path(self.database).expanduser().resolve()) != self.owner.database):
            raise RuntimeError('Reminder delivery ownership changed.')

    def refresh(self, now):
        if self.database == ':memory:':
            return
        if self.owner is None or self.owner.epoch != self.epoch:
            raise RuntimeError('Reminder delivery ownership changed.')
        saved = self.get('evidence')
        try:
            if self.reader is None:
                self.reader = ReminderSources(self.database, evidence=saved)
            token = self.reader.incarnation()
            with self.reader.for_incarnation(token):
                revision = self.reader.revision()
            evidence = self.reader.identity_evidence()
            reset = (saved is None or tuple(saved[0]) != evidence[0] or saved[2] != token
                     or revision < self.get('revision', 0) or self.get('incoherent'))
            with self.db:
                if reset:
                    for table in ('frontier', 'source', 'ledger', 'ledger_zone', 'delivery_context', 'suppressed', 'metadata'):
                        self.db.execute('DELETE FROM ' + table)
                    self.set('boundary', now)
                    self.set('partition', uuid4().hex)
                    self.set('bootstrap', [revision, None])
                    self.set('owner', self.epoch)
                if reset or saved != json.loads(json.dumps(evidence)):
                    self.set('evidence', evidence)
                if reset or revision != self.get('revision'):
                    self.set('revision', revision)
        except (OSError, sqlite3.DatabaseError) as error:
            self.latch(error)
            raise

    def latch(self, error):
        if 'previous WAL' in str(error):
            with self.db:
                if self.reader:
                    self.set('evidence', self.reader.identity_evidence())
                self.set('incoherent', True)

    @contextmanager
    def guarded(self):
        self.check_owner()
        if self.reader is None:
            if self.database != ':memory:':
                raise RuntimeError('A reminder source partition is required.')
            yield None
            return
        evidence = self.get('evidence')
        if self.get('incoherent') or self.reader.identity_evidence()[0] != tuple(evidence[0]):
            raise sqlite3.OperationalError('Reminder source identity is unavailable.')
        try:
            with self.reader.for_incarnation(evidence[2]):
                yield self.reader
        except (OSError, sqlite3.DatabaseError) as error:
            if not self.db.in_transaction:
                self.latch(error)
            raise

    def close(self):
        if self.reader:
            self.reader.close()
