"""Asking the server about a file before splitting it: a request for its first byte.

A 206 answer proves the server serves byte ranges and tells the file's size, name and version
(ETag and Last-Modified), as in Plexo. A 200 means it ignores ranges, so there is nothing to split.
"""

import os
import re
import time
from email.message import Message
from urllib.parse import unquote, urljoin, urlsplit

from .engine import REDIRECTS, StatusError, parse_content_range
from .routing import Client

PASSED_HEADERS = ("User-Agent", "Referer", "Authorization", "Accept-Language")


def cookie_header(cookies, url):
    """The Cookie header a browser would send to `url`, from FDM's cookie list."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    secure = parts.scheme == "https"
    path = parts.path or "/"
    now = time.time()
    pairs = []
    for c in cookies or []:
        domain = str(c.get("domain", "")).lower().lstrip(".")
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        if c.get("isSecure") and not secure:
            continue
        expires = c.get("expires") or 0
        if expires and expires < now:
            continue
        cookie_path = c.get("path") or "/"
        if not path.startswith(cookie_path):
            continue
        if c.get("name") is not None:
            pairs.append("%s=%s" % (c["name"], c.get("value", "")))
    return "; ".join(pairs)


def build_headers(meta, url):
    headers = {"Accept": "*/*"}
    for key, value in (meta.get("headers") or {}).items():
        if key in PASSED_HEADERS and value:
            headers[key] = value
    headers.setdefault("User-Agent", "Mozilla/5.0 (PlexoFDM bridge)")
    cookie = cookie_header(meta.get("cookies"), url)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def filename_from(disposition, url):
    if disposition:
        msg = Message()
        msg["content-disposition"] = disposition
        name = msg.get_filename()
        if name:
            return sanitize(name)
    path = urlsplit(url).path
    base = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return sanitize(base) if base else "download"


def sanitize(name):
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:240] or "download"


def resolve(net, url, headers_for, timeout=20):
    """The URL redirects end at, for `url`."""
    return probe_on(net, url, headers_for, timeout)["resolved"]


def probe_on(net, url, headers_for, timeout=20):
    client = Client(net, timeout=timeout)
    current = url
    try:
        for _ in range(10):
            parts = urlsplit(current)
            if parts.scheme not in ("http", "https"):
                raise StatusError(0, "Only http and https links can be split")
            headers = headers_for(current)
            headers["Range"] = "bytes=0-0"
            headers["Accept-Encoding"] = "identity"
            resp = client.request(parts, headers)
            if resp.status in REDIRECTS and resp.getheader("Location"):
                resp.read()
                current = urljoin(current, resp.getheader("Location"))
                continue
            info = {
                "resolved": current,
                "status": resp.status,
                "etag": resp.getheader("ETag"),
                "last_modified": resp.getheader("Last-Modified"),
                "content_type": (resp.getheader("Content-Type") or "application/octet-stream").split(";")[0].strip(),
                "filename": filename_from(resp.getheader("Content-Disposition"), current),
                "network": net.id,
            }
            if resp.status == 206:
                span = parse_content_range(resp.getheader("Content-Range"))
                info["size"] = span[2] if span and span[2] is not None else 0
                info["splittable"] = bool(span and span[2])
                resp.read()
            elif resp.status == 200:
                length = resp.getheader("Content-Length")
                info["size"] = int(length) if length and length.isdigit() else 0
                info["splittable"] = False
            else:
                raise StatusError(resp.status)
            return info
        raise StatusError(310, "Too many redirects")
    finally:
        client.close()


def probe(nets, url, headers_for):
    """Probes through the first network that gets an answer, fastest first."""
    errors = []
    for net in nets:
        try:
            return probe_on(net, url, headers_for)
        except Exception as error:
            errors.append("%s: %s" % (net.name, error))
    raise ConnectionError("; ".join(errors) or "No network is online")
