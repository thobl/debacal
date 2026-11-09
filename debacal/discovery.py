"""CalDAV account discovery, with no account credentials persisted or logged."""
from __future__ import annotations

from dataclasses import dataclass
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPBasicAuthHandler, HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm,
    HTTPRedirectHandler, Request, build_opener,
)
from xml.etree import ElementTree as ET


DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
APPLE = "http://apple.com/ns/ical/"
CALENDAR_PROPERTIES = [(DAV, "resourcetype"), (DAV, "displayname"), (APPLE, "calendar-color"),
                       (CALDAV, "supported-calendar-component-set"), (DAV, "current-user-privilege-set")]


@dataclass(frozen=True)
class DiscoveredCalendar:
    name: str
    url: str
    color: str = "#4f7865"
    read_only: bool = False


class DiscoveryError(ValueError):
    pass


def _validated_url(value):
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise DiscoveryError("Enter an HTTP(S) server URL and use the separate credential fields.")
    return value


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class DiscoveryHTTP:
    """Read-only DAV requests; preserves PROPFIND across same-origin redirects."""
    def __init__(self, server_url, username, password):
        self.origin = _origin(server_url)
        manager = HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, server_url, username, password)
        parsed = urlsplit(server_url)
        manager.add_password(None, f"{parsed.scheme}://{parsed.netloc}/", username, password)
        self.opener = build_opener(_NoRedirect(), HTTPBasicAuthHandler(manager), HTTPDigestAuthHandler(manager))

    def propfind(self, url, properties, depth="0"):
        node = ET.Element(f"{{{DAV}}}propfind")
        prop = ET.SubElement(node, f"{{{DAV}}}prop")
        for namespace, name in properties:
            ET.SubElement(prop, f"{{{namespace}}}{name}")
        body = ET.tostring(node, encoding="utf-8", xml_declaration=True)
        for _ in range(6):
            if _origin(url) != self.origin:
                raise DiscoveryError("The server redirected discovery to a different origin. Enter that server URL directly.")
            request = Request(url, data=body, method="PROPFIND", headers={
                "Content-Type": "application/xml; charset=utf-8", "Depth": depth, "User-Agent": "Debacal/0.1",
            })
            try:
                response = self.opener.open(request, timeout=15)
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                    url = urljoin(url, exc.headers["Location"])
                    continue
                if exc.code in {401, 403}:
                    raise DiscoveryError("The server rejected these credentials or denied calendar access.") from None
                raise DiscoveryError(f"Calendar discovery returned HTTP {exc.code}.") from None
            except (URLError, OSError, socket.timeout):
                raise DiscoveryError("Could not reach the calendar server. Check the address and network connection.") from None
            with response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise DiscoveryError("The discovery response is too large.")
            try:
                return url, ET.fromstring(raw)
            except ET.ParseError:
                raise DiscoveryError("The server did not return a CalDAV discovery response.") from None
        raise DiscoveryError("Too many redirects while discovering calendars.")


def _successful_props(tree):
    for response in tree.findall(f"{{{DAV}}}response"):
        href = response.findtext(f"{{{DAV}}}href", "")
        properties = {}
        for propstat in response.findall(f"{{{DAV}}}propstat"):
            status = propstat.findtext(f"{{{DAV}}}status", "")
            if " 200 " in status:
                prop = propstat.find(f"{{{DAV}}}prop")
                if prop is not None:
                    properties.update({child.tag: child for child in prop})
        yield href, properties


def _calendar_metadata(href, props, base, server_url):
    resource_type = props.get(f"{{{DAV}}}resourcetype")
    if resource_type is None or resource_type.find(f"{{{CALDAV}}}calendar") is None:
        return None
    supported = props.get(f"{{{CALDAV}}}supported-calendar-component-set")
    if supported is not None and not any(c.attrib.get("name") == "VEVENT" for c in supported):
        return None
    url = urljoin(base, href)
    if _origin(url) != _origin(server_url):
        raise DiscoveryError("A discovered calendar points to another origin. Connect to that server directly.")
    name_node = props.get(f"{{{DAV}}}displayname")
    color_node = props.get(f"{{{APPLE}}}calendar-color")
    privileges = props.get(f"{{{DAV}}}current-user-privilege-set")
    read_only = privileges is not None and privileges.find(f".//{{{DAV}}}write") is None and privileges.find(f".//{{{DAV}}}all") is None
    color = (color_node.text if color_node is not None else "") or "#4f7865"
    return DiscoveredCalendar((name_node.text if name_node is not None else "") or url.rstrip("/").rsplit("/", 1)[-1],
                              url, color[:7], read_only)


def _fallback_discover(server_url, username, password, http_factory=DiscoveryHTTP):
    http = http_factory(server_url, username, password)
    parsed = urlsplit(server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    # A user-provided DAV path comes first; a bare server uses RFC 6764.
    candidates = ([server_url] if parsed.path.strip("/") else []) + [
        origin + "/.well-known/caldav", origin + "/remote.php/dav/", server_url,
    ]
    last_error = None
    for candidate in dict.fromkeys(candidates):
        try:
            base, tree = http.propfind(candidate, [(DAV, "current-user-principal"), (CALDAV, "calendar-home-set"), (DAV, "resourcetype")])
            records = list(_successful_props(tree))
            homes = []
            for href, props in records:
                if f"{{{CALDAV}}}calendar-home-set" in props:
                    homes.extend(urljoin(base, h.text or "") for h in props[f"{{{CALDAV}}}calendar-home-set"].findall(f"{{{DAV}}}href"))
                principal = props.get(f"{{{DAV}}}current-user-principal")
                principal_href = principal.findtext(f"{{{DAV}}}href") if principal is not None else None
                if principal_href:
                    principal_url, principal_tree = http.propfind(urljoin(base, principal_href), [(CALDAV, "calendar-home-set")])
                    for _, principal_props in _successful_props(principal_tree):
                        home = principal_props.get(f"{{{CALDAV}}}calendar-home-set")
                        if home is not None:
                            homes.extend(urljoin(principal_url, h.text or "") for h in home.findall(f"{{{DAV}}}href"))
                resource_type = props.get(f"{{{DAV}}}resourcetype")
                if resource_type is not None and resource_type.find(f"{{{CALDAV}}}calendar") is not None:
                    homes.append(urljoin(base, href))
            if not homes:
                raise DiscoveryError("No calendar home was advertised at this address.")
            result = {}
            for home in dict.fromkeys(homes):
                final_url, listing = http.propfind(home, CALENDAR_PROPERTIES, depth="1")
                for href, props in _successful_props(listing):
                    calendar = _calendar_metadata(href, props, final_url, server_url)
                    if calendar:
                        result[calendar.url] = calendar
            return sorted(result.values(), key=lambda c: (c.name.casefold(), c.url))
        except DiscoveryError as exc:
            last_error = exc
    raise last_error or DiscoveryError("No CalDAV calendars were found.")


def discover_calendars(server_url: str, username: str, password: str) -> list[DiscoveredCalendar]:
    """Discover calendars with python-caldav, or the read-only DAV fallback.

    The function performs network I/O and belongs on a worker thread.
    """
    server_url = _validated_url(server_url)
    try:
        import caldav
        # python-caldav loads DAVClient lazily. Missing optional/transitive
        # modules can therefore surface here rather than at the import above.
        client = caldav.DAVClient(url=server_url, username=username, password=password, timeout=15)
    except ImportError:
        return _fallback_discover(server_url, username, password)
    try:
        principal = client.principal()
        calendars = principal.calendars()
        http = DiscoveryHTTP(server_url, username, password)
        result = []
        for calendar in calendars:
            url = str(calendar.url)
            if _origin(url) != _origin(server_url):
                raise DiscoveryError("A discovered calendar points to another origin. Connect to that server directly.")
            final_url, tree = http.propfind(url, CALENDAR_PROPERTIES)
            for href, props in _successful_props(tree):
                found = _calendar_metadata(href, props, final_url, server_url)
                if found:
                    result.append(found)
        return sorted(result, key=lambda c: (c.name.casefold(), c.url))
    except DiscoveryError:
        raise
    except Exception:
        # A bare Nextcloud URL or broken well-known redirect may need the
        # explicit principal/home-set path fallback. Keep errors credential-free.
        return _fallback_discover(server_url, username, password)
    finally:
        client.close()
