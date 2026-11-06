"""Private vdirsyncer password/conflict command helper; no interactive prompts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def conflict(directory, left, right):
    from debacal.ics import patch_ics
    from debacal.vdirsyncer_backend import _digest, atomic_write, parsed_one, semantic
    local_data, remote_data = Path(left).read_text(), Path(right).read_text()
    local, remote = parsed_one(local_data), parsed_one(remote_data)
    if local.ical_uid != remote.ical_uid:
        return 1
    marker_path = Path(directory) / (_digest(local.ical_uid) + ".json")
    marker = json.loads(marker_path.read_text()) if marker_path.exists() else {}
    unchanged = marker.get("local_hash") == semantic(local) and marker.get("remote_hash") == semantic(remote)
    choice = marker.get("choice") if unchanged else None
    if choice in {"local", "remote"}:
        chosen = patch_ics(remote_data, local).decode() if choice == "local" else remote_data
        Path(left).write_text(chosen)
        Path(right).write_text(chosen)
        return 0
    atomic_write(marker_path, json.dumps({
        "uid": local.ical_uid, "event_id": marker.get("event_id", ""),
        "local_ics": local_data, "remote_ics": remote_data,
        "local_hash": semantic(local), "remote_hash": semantic(remote),
        "reason": "Both this app and the server changed the event. Review the current versions.",
    }))
    return 1


def main(argv=None):
    args = argv if argv is not None else sys.argv[1:]
    if args == ["password"]:
        password = os.environ.get("DEBACAL_VDIR_PASSWORD", "")
        if not password:
            return 1
        sys.stdout.write(password)
        return 0
    if len(args) == 4 and args[0] == "conflict":
        return conflict(*args[1:])
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Never print conflict contents or authentication environment variables.
        raise SystemExit(1)
