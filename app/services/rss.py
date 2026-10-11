import http.client
import ipaddress
import socket
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from app.services.discovery import clean_text

MAX_FEED_BYTES = 2_000_000
MAX_FEED_RESPONSE_BYTES = MAX_FEED_BYTES + 1
MAX_ENTRIES = 500
FETCH_TIMEOUT_SECONDS = 15


class FeedError(ValueError):
    pass


def _public_addresses(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return {ipaddress.ip_address(host)}
    except ValueError:
        try:
            return {
                ipaddress.ip_address(answer[4][0])
                for answer in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise FeedError("Feed hostname did not resolve to a public IP address") from exc


def validate_public_feed_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise FeedError("Feed URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise FeedError("RSS/Atom sources must use an absolute HTTPS URL on port 443")

    host = parsed.hostname.rstrip(".").casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise FeedError("Local hostnames cannot be used as feed sources")
    addresses = _public_addresses(host)
    if not addresses or any(not address.is_global for address in addresses):
        raise FeedError("Feed sources must resolve only to public IP addresses")
    return value.strip()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, port=443, timeout=FETCH_TIMEOUT_SECONDS)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _fetch_body(url: str) -> bytes:
    try:
        parsed = urlsplit(validate_public_feed_url(url))
    except ValueError as exc:
        if isinstance(exc, FeedError):
            raise
        raise FeedError("Feed URL is malformed") from exc
    host = parsed.hostname.rstrip(".").casefold()
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    addresses = sorted(_public_addresses(host), key=lambda ip: (ip.version, int(ip)))
    last_error = None
    for address in addresses:
        connection = _PinnedHTTPSConnection(host, str(address))
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": "JARVIS-Company-AI/1.0",
                    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise FeedError("Feed redirects are disabled; configure the final HTTPS feed URL")
            if response.status >= 400:
                raise FeedError(f"Feed server returned HTTP {response.status}")
            body = response.read(MAX_FEED_RESPONSE_BYTES)
            if len(body) > MAX_FEED_BYTES:
                raise FeedError("Feed exceeds the 2 MB response limit")
            return body
        except OSError as exc:
            last_error = exc
        finally:
            connection.close()
    raise FeedError(f"Could not fetch feed: {last_error or 'no public IP addresses resolved'}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(element: ET.Element, *names: str) -> str:
    expected = {name.casefold() for name in names}
    for child in element:
        if _local_name(child.tag) in expected:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _entry_link(entry: ET.Element) -> str:
    for child in entry:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if href and child.attrib.get("rel", "alternate") in {"alternate", ""}:
            return href
        text = (child.text or "").strip()
        if text:
            return text
    return ""


def fetch_rss_feed(source: dict) -> list[dict]:
    content = _fetch_body(source["url"])
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise FeedError("Response is not a valid RSS or Atom XML document") from exc

    entries = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    results = []
    for entry in entries[:MAX_ENTRIES]:
        title = clean_text(_child_text(entry, "title"), 300)
        link = _entry_link(entry)
        if not title or not link:
            continue
        description = _child_text(entry, "description", "summary", "content", "encoded")
        source_id = _child_text(entry, "guid", "id") or link
        results.append({
            "title": title,
            "company_name": source["name"],
            "location": "",
            "description": clean_text(description),
            "url": link,
            "slug": source_id,
        })
    return results
