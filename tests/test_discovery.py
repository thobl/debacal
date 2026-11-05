from xml.etree import ElementTree as ET

import pytest

from lebacal.discovery import DiscoveryError, _fallback_discover, discover_calendars


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
    import sys
    from types import SimpleNamespace
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
    monkeypatch.setattr("lebacal.discovery.DiscoveryHTTP", HTTP)
    found = discover_calendars("https://cloud.test", "unused", "unused")
    assert found[0].name == "Shared"
    assert found[0].color == "#aa3344"
    assert found[0].read_only
    assert closed
    calendars[0].url = "https://other.test/calendar/"
    with pytest.raises(DiscoveryError, match="another origin"):
        discover_calendars("https://cloud.test", "unused", "unused")
