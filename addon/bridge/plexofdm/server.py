"""The bridge FDM talks to: download links under /d/, a small API, and a status page.

It only listens on 127.0.0.1. Everything but the download links and /api/ping needs the key in
the data folder, sent as X-Plexo-Key: a header a web page can't add to a request to another site
without that site agreeing, so a page open in a browser can't drive the bridge. Requests naming a
Host other than this computer are refused, which stops DNS rebinding.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from . import VERSION, autostart
from .config import data_file, read_json, write_json
from .engine import MIB, KIB, Session, plan_block_size, same_version
from .netif import NetworkMonitor
from .probe import build_headers, probe

log = logging.getLogger("plexofdm.server")

FORCE_MARKER = "plexo"
SESSION_TTL_S = 60 * 86400


class Rejected(Exception):
    """Not worth splitting: FDM should download it the usual way."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class Bridge:
    def __init__(self, config, key, port):
        self.config = config
        self.key = key
        self.port = port
        self.lock = threading.Lock()
        self.sessions = {}
        self.bytes_by_net = defaultdict(int)
        self.rates = {}
        self._sample = (time.monotonic(), {})
        self.started = time.time()
        self.monitor = NetworkMonitor(config)
        self.monitor.on_change(self._networks_changed)
        self._load_sessions()
        threading.Thread(target=self._ticker, name="ticker", daemon=True).start()

    # --- Sessions --------------------------------------------------------------------------

    def _sessions_path(self):
        return data_file("sessions.json")

    def _load_sessions(self):
        stored = read_json(self._sessions_path(), {})
        now = time.time()
        for token, meta in (stored.items() if isinstance(stored, dict) else []):
            try:
                if now - meta.get("last_used", 0) > SESSION_TTL_S:
                    continue
                self.sessions[token] = Session(self, token, meta)
            except (KeyError, TypeError, ValueError):
                continue

    def _save_sessions(self):
        with self.lock:
            stored = {token: s.meta for token, s in self.sessions.items()}
        write_json(self._sessions_path(), stored)

    def token_for(self, url, size, version):
        """Unguessable, and the same for the same file, so adding a link again gives FDM the
        same address. A new version of the file gets a new one: FDM must never resume one
        version's partial file with another's bytes."""
        message = "\n".join([url, str(size), version or ""]).encode()
        return hmac.new(self.key.encode(), message, hashlib.sha256).hexdigest()[:32]

    def local_url(self, token, filename):
        return "http://127.0.0.1:%d/d/%s/%s" % (self.port, token, quote(filename))

    def register(self, body):
        cfg = self.config.snapshot()
        url = str(body.get("url") or "").strip()
        parts = urlsplit(url)
        force = bool(body.get("force")) or parts.fragment.lower() == FORCE_MARKER
        if parts.fragment.lower() == FORCE_MARKER:
            url = urlunsplit(parts._replace(fragment=""))
            parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise Rejected("unsupported", "Only http and https links can be split")
        if parts.hostname in ("127.0.0.1", "localhost", "::1") and (parts.port or 80) == self.port:
            raise Rejected("unsupported", "That is a bridge link already")
        if not force and not cfg["auto"]:
            raise Rejected("disabled", "Automatic mode is off; add #plexo to a link to split it")
        min_bytes = cfg["min_size_mb"] * MIB
        hint = int(body.get("size") or -1)
        if not force and 0 <= hint < min_bytes:
            raise Rejected("small", "Smaller than %d MB" % cfg["min_size_mb"])

        self.monitor.wait_for_checks()
        nets = self.monitor.usable()
        if not nets:
            raise Rejected("offline", "No network is online")
        if not force and len(nets) < cfg["min_networks"]:
            raise Rejected("networks", "Only %d network online; nothing to combine" % len(nets))

        headers = {k: v for k, v in (body.get("headers") or {}).items() if isinstance(v, str)}
        meta = {"url": url, "host": parts.hostname, "headers": headers, "cookies": body.get("cookies") or []}
        info = probe(nets, url, lambda u: build_headers(meta, u))
        if not info["splittable"]:
            raise Rejected("ranges", "The server can't send parts of this file")
        if not force and info["size"] < min_bytes:
            raise Rejected("small", "Smaller than %d MB" % cfg["min_size_mb"])

        token = self.token_for(url, info["size"], info["etag"] or info["last_modified"])
        meta.update(
            resolved=info["resolved"],
            size=info["size"],
            etag=info["etag"],
            last_modified=info["last_modified"],
            content_type=info["content_type"],
            filename=info["filename"],
            last_used=time.time(),
        )
        with self.lock:
            session = self.sessions.get(token)
            if session is not None and session.error is None:
                # Same file again (FDM re-checking it, or the link added twice): keep its blocks,
                # take the fresh cookies.
                with session.cv:
                    session.meta.update(headers=headers, cookies=meta["cookies"], last_used=time.time())
                    session.resolved = info["resolved"]
            else:
                meta["block_size"] = plan_block_size(
                    info["size"], len(nets), cfg["streams_per_network"],
                    cfg["min_block_kb"] * KIB, cfg["max_block_kb"] * KIB)
                session = Session(self, token, meta)
                self.sessions[token] = session
        self._save_sessions()
        log.info("Registered %s (%d bytes) as %s", info["filename"], info["size"], token[:8])
        return {
            "ok": True,
            "url": self.local_url(token, session.meta["filename"]),
            "filename": session.meta["filename"],
            "size": session.size,
            "content_type": session.meta.get("content_type"),
            "networks": [n.name for n in nets],
            "ui": "http://127.0.0.1:%d/" % self.port,
        }

    # --- Networks and rates ----------------------------------------------------------------

    def count(self, net_id, n):
        self.bytes_by_net[net_id] += n  # Called with a session's lock held; ints are atomic enough.

    def _networks_changed(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.networks_changed()

    def _ticker(self):
        while True:
            time.sleep(1)
            now = time.monotonic()
            then, before = self._sample
            current = dict(self.bytes_by_net)
            elapsed = max(0.001, now - then)
            self.rates = {k: (v - before.get(k, 0)) / elapsed for k, v in current.items()}
            self._sample = (now, current)
            with self.lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                if session.requests or session.workers:
                    try:
                        session.tick(now)
                    except Exception:
                        log.exception("Session tick failed")

    def state(self):
        with self.lock:
            sessions = list(self.sessions.values())
        statuses = [s.status() for s in sessions]
        statuses.sort(key=lambda s: (not s["active"], s["filename"] or ""))
        networks = self.monitor.all()
        for net in networks:
            net["rate"] = round(self.rates.get(net["id"], 0))
            net["bytes"] = self.bytes_by_net.get(net["id"], 0)
        cfg = self.config.snapshot()
        cfg.pop("test_networks", None)
        return {
            "version": VERSION,
            "pid": os.getpid(),
            "platform": sys.platform,
            "uptime": round(time.time() - self.started),
            "networks": networks,
            "downloads": [s for s in statuses if s["active"] or s["served"] or s["error"]][:50],
            "config": cfg,
        }

    def update_config(self, changes):
        applied = self.config.update(changes)
        if "autostart" in applied:
            try:
                autostart.set_enabled(applied["autostart"])
            except Exception as error:
                log.warning("Couldn't change autostart: %s", error)
                self.config.update({"autostart": not applied["autostart"]})
                raise
        self.monitor.refresh()
        self._networks_changed()
        return applied

    def forget(self, token):
        with self.lock:
            session = self.sessions.pop(token, None)
        if session:
            with session.cv:
                session.error = "Removed"
                session.cv.notify_all()
            self._save_sessions()


def parse_range(value, size):
    """A Range header's first range as (start, end), None to ignore it, or 'invalid' when it
    can't be satisfied."""
    m = re.match(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*(?:,.*)?$", value or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):
        suffix = int(m.group(2))
        if suffix == 0:
            return "invalid"
        return max(0, size - suffix), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


def make_handler(bridge):
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
    allowed_hosts = {"127.0.0.1:%d" % bridge.port, "localhost:%d" % bridge.port, "[::1]:%d" % bridge.port}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "PlexoFDM/" + VERSION

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status, body=b"", content_type="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self):
            return hmac.compare_digest(self.headers.get("X-Plexo-Key", ""), bridge.key)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4 * MIB:
                raise ValueError("Request too large")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def _route(self):
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(403, {"ok": False, "message": "Unknown host"})
                return None
            return urlsplit(self.path).path

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self._route()
            if path is None:
                return
            if path.startswith("/d/"):
                return self._download(path)
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/api/ping":
                return self._send(200, {"ok": True, "version": VERSION, "pid": os.getpid()})
            if path == "/api/state":
                if not self._authorized():
                    return self._send(401, {"ok": False, "message": "Missing key"})
                return self._send(200, bridge.state())
            if path in ("/", "/index.html"):
                with open(ui_path, "r", encoding="utf-8") as f:
                    page = f.read().replace("__PLEXO_KEY__", bridge.key)
                return self._send(200, page, "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                })
            self._send(404, {"ok": False, "message": "Not found"})

        def do_POST(self):
            path = self._route()
            if path is None:
                return
            if not self._authorized():
                return self._send(401, {"ok": False, "message": "Missing key"})
            try:
                body = self._body()
                if path == "/api/register":
                    try:
                        return self._send(200, bridge.register(body))
                    except Rejected as rejected:
                        return self._send(200, {"ok": False, "reason": rejected.reason, "message": str(rejected)})
                    except Exception as error:
                        # The server or the networks couldn't be reached: FDM tries the link itself.
                        log.warning("Couldn't check %s: %s", body.get("url"), error)
                        return self._send(200, {"ok": False, "reason": "unreachable", "message": str(error)})
                if path == "/api/config":
                    applied = bridge.update_config(body)
                    return self._send(200, {"ok": True, "applied": applied})
                if path == "/api/forget":
                    bridge.forget(str(body.get("token", "")))
                    return self._send(200, {"ok": True})
                if path == "/api/shutdown":
                    self._send(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
            except Exception as error:
                log.exception("API call %s failed", path)
                return self._send(500, {"ok": False, "reason": "error", "message": str(error)})
            self._send(404, {"ok": False, "message": "Not found"})

        def _download(self, path):
            token = unquote(path[3:].split("/", 1)[0])
            with bridge.lock:
                session = bridge.sessions.get(token)
            if session is None:
                return self._send(404, "This download isn't known to the bridge. Add the link to FDM again.",
                                  "text/plain; charset=utf-8")
            if session.error:
                return self._send(409, session.error, "text/plain; charset=utf-8")
            size = session.size
            meta = session.meta
            version = '"%s"' % token
            span = parse_range(self.headers.get("Range"), size)
            if_range = self.headers.get("If-Range")
            if span and if_range and not same_version(if_range, version) and if_range != meta.get("last_modified"):
                span = None
            if span == "invalid":
                return self._send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % size})
            start, end = span if span else (0, size - 1)

            self.send_response(206 if span else 200)
            self.send_header("Content-Type", meta.get("content_type") or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if span:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("ETag", version)
            if meta.get("last_modified"):
                self.send_header("Last-Modified", meta["last_modified"])
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(meta.get("filename") or "download"))
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                session.serve(start, end, self.wfile.write)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True  # FDM closed this connection: paused, or split it.
            except Exception as error:
                # The body is cut short, so FDM sees the part as unfinished and asks again.
                log.warning("Couldn't finish bytes %d-%d of %s: %s", start, end, meta.get("filename"), error)
                self.close_connection = True

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR lets a second program take the same port.
    allow_reuse_address = sys.platform != "win32"
    request_queue_size = 64


def run(config, key, port):
    bridge = Bridge(config, key, port)
    httpd = Server(("127.0.0.1", port), make_handler(bridge))
    write_json(data_file("daemon.json"), {"port": port, "pid": os.getpid(), "version": VERSION, "started": time.time()})
    log.info("Plexo bridge %s listening on http://127.0.0.1:%d/", VERSION, port)
    if config["autostart"]:
        try:
            autostart.set_enabled(True)  # Rewritten in case the add-on moved when it updated.
        except Exception as error:
            log.warning("Couldn't refresh autostart: %s", error)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        info = read_json(data_file("daemon.json"), {})
        if info.get("pid") == os.getpid():
            try:
                os.remove(data_file("daemon.json"))
            except OSError:
                pass
    return bridge
