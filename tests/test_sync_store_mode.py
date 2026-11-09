"""Background synchronization maintains durable data without a second UI index."""

from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent
from unittest.mock import patch

from debacal.models import Event
from debacal.store import Store
from debacal.sync import SyncEngine


class Credentials:
    def clear_session(self):
        pass


def test_sync_worker_requests_a_store_without_an_in_memory_query_index(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    with Store(path):
        pass
    opened, instances = ThreadEvent(), []
    def open_store(database, **options):
        assert options == {'query_index': False}
        result = Store(database, **options)
        instances.append(result)
        opened.set()
        return result
    with patch('debacal.sync.Store', side_effect=open_store):
        engine = SyncEngine(path, transport_factory=lambda *_args: None,
                            vdir_backend=object(), credential_store=Credentials())
        try:
            assert opened.wait(5), engine._worker_error
            assert instances[0]._query_index is None
        finally:
            engine.close()
            engine._thread.join(timeout=5)
        assert not engine._thread.is_alive()


def test_opening_worker_store_does_not_hydrate_existing_event_models(tmp_path):
    path = tmp_path / 'calendar.sqlite3'
    first = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    with Store(path) as store:
        store.apply_remote([Event(str(index), 'local', 'Synthetic', first, first + timedelta(hours=1),
                                  recurrence_rule='FREQ=WEEKLY;COUNT=8') for index in range(100)])
    with patch.object(Event, 'from_dict', side_effect=AssertionError('Worker startup does not read event payloads')):
        with Store(path, query_index=False) as worker:
            assert worker._query_index is None
            assert len(worker.event_ids()) == 100
