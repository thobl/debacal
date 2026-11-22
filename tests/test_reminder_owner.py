from debacal.reminder_owner import ReminderOwner


def test_one_owner_across_aliases_and_takeover(tmp_path):
    db = tmp_path / 'profile.sqlite3'
    db.touch()
    alias = tmp_path / 'symlink'
    alias.symlink_to(db)
    first, other, separate = map(ReminderOwner, map(str, (db, alias, tmp_path / 'independent')))
    try:
        assert first.acquire()
        assert not other.acquire()
        assert separate.acquire()
        first.release()
        assert other.acquire()
    finally:
        for owner in (first, other, separate):
            owner.release()


def test_process_exit_releases_delivery_lock(tmp_path):
    import subprocess
    import sys
    db = str(tmp_path/'profile')
    script = """import os, sys
from debacal.reminder_owner import ReminderOwner
owner = ReminderOwner(sys.argv[1])
print(owner.acquire(), flush=True)
sys.stdin.readline()
os._exit(0)
"""
    child = subprocess.Popen([sys.executable, '-c', script, db], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    owner = ReminderOwner(db)
    try:
        assert child.stdout.readline().strip() == 'True'
        assert not owner.acquire()
        child.communicate('\n', timeout=10)
        assert owner.acquire()
    finally:
        owner.release()
        if child.poll() is None:
            child.kill()
            child.wait()
