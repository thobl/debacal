"""Identity captured by an explicit Open in the profile's retained inbox."""
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3


@dataclass(frozen=True, slots=True)
class ActivationContext:
    partition: str
    main_file: tuple[int, int]
    incarnation: str

    def current_partition(self, database):
        """Reject a queued action if its sidecar or canonical file was replaced."""
        try:
            path = Path(database).resolve(strict=True)
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != self.main_file:
                return False
            uri = Path(str(path) + '.reminders.sqlite3').as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=0)) as reader:
                row = reader.execute("SELECT value FROM metadata WHERE key='partition'").fetchone()
                return row is not None and json.loads(row[0]) == self.partition
        except (OSError, ValueError, sqlite3.Error):
            return False

    def current_source(self, reader, due):
        row = reader.connection.execute(
            'SELECT i.token, COALESCE(c.version,0), e.calendar_id FROM database_incarnation i '
            'JOIN events e ON e.id=? LEFT JOIN event_query_changes c ON c.event_id=e.id WHERE i.id=1',
            (due.key.source_id,)).fetchone()
        return row is not None and tuple(row) == (self.incarnation, due.source_revision, due.calendar_id)
