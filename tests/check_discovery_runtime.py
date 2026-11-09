"""Offline discovery smoke check using the package interpreter's dependencies.

Run from the source root with python -c/runpy so the checked source, rather than
an older installed Debacal, is imported. No pytest, profile or server is needed.
"""
from contextlib import ExitStack
from unittest.mock import patch
from xml.etree import ElementTree as ET

from debacal.discovery import DiscoveryHTTP, discover_calendars


def check():
    def propfind(_self, url, _properties, depth='0'):
        if depth == '1':
            properties = ('<d:resourcetype><c:calendar/></d:resourcetype>'
                          '<d:displayname>Offline calendar</d:displayname>'
                          '<a:calendar-color>#aabbccff</a:calendar-color>')
            href = '/calendars/offline/'
        else:
            properties = '<c:calendar-home-set><d:href>/calendars/</d:href></c:calendar-home-set>'
            href = '/'
        return url, ET.fromstring(
            '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
            'xmlns:a="http://apple.com/ns/ical/"><d:response><d:href>' + href +
            '</d:href><d:propstat><d:prop>' + properties + '</d:prop>'
            '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>')

    with ExitStack() as stack:
        stack.enter_context(patch.object(DiscoveryHTTP, 'propfind', propfind))
        try:
            import caldav
            client_type = caldav.DAVClient
        except ImportError:
            # Exercise the real missing package/lazy dependency failure.
            pass
        else:
            # A healthy library also takes the offline path, without contacting
            # a server. Its actual constructor and the native parser still run.
            stack.enter_context(patch.object(client_type, 'principal', side_effect=ImportError('Offline discovery check')))
        found = discover_calendars('https://calendar.test', '', '')
    assert len(found) == 1
    assert found[0].name == 'Offline calendar'
    assert found[0].url == 'https://calendar.test/calendars/offline/'
    assert found[0].color == '#aabbcc' and not found[0].read_only


if __name__ == '__main__':
    check()
    print('Offline CalDAV discovery check passed.')
