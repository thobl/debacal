import sys
from pathlib import Path
import runpy
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree as ET

import pytest

from debacal.discovery import DiscoveredCalendar, DiscoveryError, _fallback_discover, discover_calendars


def response(href, props):
    return f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'


def multistatus(*items):
    return ET.fromstring('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/">' + ''.join(items) + '</d:multistatus>')


def test_nextcloud_style_principal_home_discovery_filters_task_calendars():
    calls = []
    class HTTP:
        def __init__(self, *args):
            pass
        def propfind(self, url, props, depth="0"):
            calls.append((url, depth))
            if url.endswith("/.well-known/caldav"):
                return "https://cloud.test/remote.php/dav/", multistatus(response("/remote.php/dav/", '<d:current-user-principal><d:href>/remote.php/dav/principals/users/alice/</d:href></d:current-user-principal>'))
            if "/principals/" in url:
                return url, multistatus(response(url, '<c:calendar-home-set><d:href>/remote.php/dav/calendars/alice/</d:href></c:calendar-home-set>'))
            return url, multistatus(
                response("/remote.php/dav/calendars/alice/work/", '<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Work &amp; teaching</d:displayname><a:calendar-color>#abcdefFF</a:calendar-color><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set><d:current-user-privilege-set><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>'),
                response("/remote.php/dav/calendars/alice/tasks/", '<d:resourcetype><c:calendar/></d:resourcetype><c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>'),
            )
    found = _fallback_discover("https://cloud.test", "alice", "unused", http_factory=HTTP)
    assert len(found) == 1
    assert found[0].name == "Work & teaching"
    assert found[0].url.endswith("/work/")
    assert found[0].color == "#abcdef"
    assert not found[0].read_only
    assert calls[-1][1] == "1"


def test_invalid_embedded_credentials_are_rejected():
    with pytest.raises(DiscoveryError, match="separate credential"):
        discover_calendars("https://alice:secret@cloud.test", "", "")


def test_failed_discovery_does_not_echo_credentials():
    class HTTP:
        def __init__(self, *args):
            pass
        def propfind(self, *args, **kwargs):
            raise DiscoveryError("Could not reach the calendar server.")
    with pytest.raises(DiscoveryError) as exc:
        _fallback_discover("https://cloud.test", "alice", "sensitive", http_factory=HTTP)
    assert "alice" not in str(exc.value)
    assert "sensitive" not in str(exc.value)


def test_installed_caldav_discovery_preserves_metadata_and_rejects_foreign_origins(monkeypatch):
    calendars = [SimpleNamespace(url="https://cloud.test/calendars/shared/")]
    closed = []
    class Client:
        def __init__(self, **kwargs):
            pass
        def principal(self):
            return SimpleNamespace(calendars=lambda: calendars)
        def close(self):
            closed.append(True)
    class HTTP:
        def __init__(self, *args):
            pass
        def propfind(self, url, props):
            return url, multistatus(response(url, '<d:resourcetype><c:calendar/></d:resourcetype><d:displayname>Shared</d:displayname><a:calendar-color>#aa3344ff</a:calendar-color><d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>'))
    monkeypatch.setitem(sys.modules, "caldav", SimpleNamespace(DAVClient=Client))
    monkeypatch.setattr("debacal.discovery.DiscoveryHTTP", HTTP)
    found = discover_calendars("https://cloud.test", "unused", "unused")
    assert found[0].name == "Shared"
    assert found[0].color == "#aa3344"
    assert found[0].read_only
    assert closed
    calendars[0].url = "https://other.test/calendar/"
    with pytest.raises(DiscoveryError, match="another origin"):
        discover_calendars("https://cloud.test", "unused", "unused")


@pytest.mark.parametrize('failure', ['import', 'lazy_attribute', 'constructor'])
def test_missing_caldav_dependency_uses_read_only_discovery(monkeypatch, failure):
    expected = [DiscoveredCalendar('Shared', 'https://cloud.test/calendars/shared/')]
    fallback = Mock(return_value=expected)
    monkeypatch.setattr('debacal.discovery._fallback_discover', fallback)

    def missing_dependency(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'calendaring_config'", name='calendaring_config')

    if failure == 'import':
        library = None
    elif failure == 'lazy_attribute':
        class LazyLibrary(ModuleType):
            def __getattr__(self, name):
                if name == 'DAVClient':
                    return missing_dependency()
                raise AttributeError(name)
        library = LazyLibrary('caldav')
    else:
        library = SimpleNamespace(DAVClient=missing_dependency)
    monkeypatch.setitem(sys.modules, 'caldav', library)

    assert discover_calendars('cloud.test', 'synthetic-user', 'unused') is expected
    fallback.assert_called_once_with('https://cloud.test', 'synthetic-user', 'unused')


@pytest.mark.parametrize('fallback_fails', [False, True])
def test_caldav_client_closes_when_lazy_discovery_dependency_fails(monkeypatch, fallback_fails):
    client = Mock()
    client.principal.side_effect = ModuleNotFoundError("No module named 'calendaring_config'")
    monkeypatch.setitem(sys.modules, 'caldav', SimpleNamespace(DAVClient=Mock(return_value=client)))
    expected = [DiscoveredCalendar('Shared', 'https://cloud.test/calendars/shared/')]
    fallback = Mock(side_effect=DiscoveryError('Unavailable') if fallback_fails else None, return_value=expected)
    monkeypatch.setattr('debacal.discovery._fallback_discover', fallback)

    if fallback_fails:
        with pytest.raises(DiscoveryError, match='Unavailable'):
            discover_calendars('https://cloud.test', '', '')
    else:
        assert discover_calendars('https://cloud.test', '', '') is expected
    client.close.assert_called_once_with()


def test_non_import_constructor_failure_is_not_hidden_by_discovery_fallback(monkeypatch):
    constructor = Mock(side_effect=ValueError('Invalid client configuration'))
    monkeypatch.setitem(sys.modules, 'caldav', SimpleNamespace(DAVClient=constructor))
    fallback = Mock()
    monkeypatch.setattr('debacal.discovery._fallback_discover', fallback)

    with pytest.raises(ValueError, match='Invalid client configuration'):
        discover_calendars('https://cloud.test', '', '')
    fallback.assert_not_called()


@pytest.mark.parametrize('missing_library', [False, True])
def test_offline_package_discovery_check(monkeypatch, missing_library):
    if missing_library:
        monkeypatch.setitem(sys.modules, 'caldav', None)
    runpy.run_path(str(Path(__file__).with_name('check_discovery_runtime.py')), run_name='__main__')
