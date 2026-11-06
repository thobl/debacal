"""System timezone helpers, retaining DST rules when the OS exposes its zone."""
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_zone():
    candidates = [os.environ.get('TZ', '').lstrip(':')]
    try:
        candidates.append(str(Path('/etc/localtime').resolve()).split('/zoneinfo/')[-1])
    except OSError:
        pass
    for name in candidates:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo


def now():
    return datetime.now(local_zone())
