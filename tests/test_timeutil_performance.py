"""Timezone lookup stays cheap while reflecting process and OS zone changes."""
from zoneinfo import ZoneInfo

from debacal import timeutil


def test_repeated_clock_lookups_do_not_repeat_filesystem_resolution(monkeypatch):
    monkeypatch.setattr(timeutil, '_zone_cache', None)
    monkeypatch.setattr(timeutil, 'monotonic', lambda: 100)
    resolutions = []
    zone = ZoneInfo('Europe/Berlin')
    monkeypatch.setattr(timeutil, '_resolve_local_zone', lambda setting: resolutions.append(setting) or zone)
    for _ in range(1000):
        assert timeutil.local_zone() is zone
    assert len(resolutions) == 1


def test_environment_timezone_change_is_immediate(monkeypatch):
    monkeypatch.setattr(timeutil, '_zone_cache', None)
    monkeypatch.setattr(timeutil, 'monotonic', lambda: 100)
    monkeypatch.setenv('TZ', 'Europe/Berlin')
    assert timeutil.local_zone().key == 'Europe/Berlin'
    monkeypatch.setenv('TZ', 'Europe/London')
    assert timeutil.local_zone().key == 'Europe/London'


def test_system_zone_is_resolved_again_after_cache_window(monkeypatch):
    monkeypatch.setattr(timeutil, '_zone_cache', None)
    ticks = iter([100, 100.5, 101.1])
    monkeypatch.setattr(timeutil, 'monotonic', lambda: next(ticks))
    zones = iter([ZoneInfo('Europe/Berlin'), ZoneInfo('Europe/London')])
    monkeypatch.setattr(timeutil, '_resolve_local_zone', lambda setting: next(zones))
    assert timeutil.local_zone().key == 'Europe/Berlin'
    assert timeutil.local_zone().key == 'Europe/Berlin'
    assert timeutil.local_zone().key == 'Europe/London'
