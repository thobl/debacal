"""System timezone helpers, retaining DST rules when the OS exposes its zone."""
import os
from datetime import datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_zone_cache = None


def _resolve_local_zone(setting):
    candidates = [setting.lstrip(':')]
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


def local_zone():
    """Resolve the OS zone without filesystem work in each paint/layout call.

    Environment overrides take effect immediately. A system timezone change is
    picked up within a second, including the offset-only fallback at DST changes.
    """
    global _zone_cache
    setting, current = os.environ.get('TZ', ''), monotonic()
    cached = _zone_cache
    if cached is not None and cached[0] == setting and current < cached[1]:
        return cached[2]
    zone = _resolve_local_zone(setting)
    _zone_cache = setting, current + 1, zone
    return zone


def now():
    return datetime.now(local_zone())
