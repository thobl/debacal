"""First-use setup may serialize; normal UI choices must remain fail-fast."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier, Event

import pytest

from debacal.models import Calendar
from debacal.store import Store
from debacal.ui_state import UiState, ui_state_path


def test_schema_initialization_waits_for_another_connection_to_finish(tmp_path, monkeypatch):
    path = str(tmp_path / 'profile.sqlite3')
    attempted = Event()
    connect = sqlite3.connect
    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(lambda statement: attempted.set() if statement == 'BEGIN IMMEDIATE' else None)
        return connection
    blocker = connect(ui_state_path(path), isolation_level=None)
    blocker.execute('PRAGMA journal_mode=WAL')
    blocker.execute('BEGIN IMMEDIATE')
    blocker.execute('CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    monkeypatch.setattr('debacal.ui_state.sqlite3.connect', tracked_connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(UiState, path)
        try:
            assert attempted.wait(3)
            assert not future.done(), 'Schema initialization failed instead of waiting for its concurrent initializer.'
        finally:
            blocker.commit()
            blocker.close()
        state = future.result(timeout=5)
        try:
            assert state.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 0
            state.set_visible('local', False)
            assert state.visibility('local') is False
        finally:
            state.close()


def test_legacy_migration_serializes_and_keeps_explicit_ui_choices(tmp_path):
    path = str(tmp_path / 'profile.sqlite3')
    source = sqlite3.connect(':memory:', check_same_thread=False)
    source.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);'
                         'CREATE TABLE calendars(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
    source.execute('INSERT INTO settings VALUES (?,?)', ('selection_mode', json.dumps('future')))
    source.execute('INSERT INTO calendars VALUES (?,?)', ('local', json.dumps(Calendar('local', 'Local', '#527365').to_dict())))
    state = UiState(path)
    attempted = Event()
    state.connection.set_trace_callback(lambda statement: attempted.set() if statement == 'BEGIN IMMEDIATE' else None)
    blocker = sqlite3.connect(ui_state_path(path), isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    blocker.execute('INSERT INTO settings VALUES (?,?)', ('selection_mode', json.dumps('group')))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(state.import_legacy, source, seed=True)
        try:
            assert attempted.wait(3)
            assert not future.done(), 'Legacy migration failed instead of waiting for its concurrent writer.'
        finally:
            blocker.commit()
            blocker.close()
        future.result(timeout=5)
    try:
        assert state.get('selection_mode') == (True, 'group')
        assert state.get('_legacy_import_complete') == (True, True)
        assert state.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 0
    finally:
        state.close()
        source.close()


def test_runtime_ui_writes_still_fail_fast_after_startup_and_migration(tmp_path):
    path = str(tmp_path / 'profile.sqlite3')
    state = UiState(path)
    state.import_legacy(None, seed=False)
    blocker = sqlite3.connect(ui_state_path(path), isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(state.set_visible, 'local', False)
        try:
            # The other writer stays held until the fail-fast result arrives.
            # This checks ordering rather than a machine-dependent millisecond
            # threshold. The timeout only prevents a regression deadlock.
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                future.result(timeout=2)
        finally:
            blocker.rollback()
            blocker.close()
            state.close()


@pytest.mark.parametrize('iteration', range(15))
def test_simultaneous_store_first_opens_and_ui_migrations_are_safe(tmp_path, iteration):
    path = tmp_path / f'profile-{iteration}.sqlite3'
    barrier = Barrier(4)
    def initialize():
        barrier.wait(timeout=5)
        with Store(path, query_index=False) as store:
            assert store.get_calendar('local') is not None
            assert store._ui_state.get('_legacy_import_complete') == (True, True)
            assert store._ui_state.connection.execute('PRAGMA busy_timeout').fetchone()[0] == 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)
