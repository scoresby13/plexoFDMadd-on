"""A test server for the bridge's tests: serves one file with byte ranges, at a speed set per
client address, so loopback addresses (127.0.0.2, 127.0.0.3, ...) stand in for networks of
different speeds."""

import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Origin:
    def __init__(self, size=24 * 1024 * 1024, speeds=None, ranges=True, etag='"v1"'):
        self.data = os.urandom(size)
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        # client address -> bytes per second (absent: unlimited); "stall" hangs after a while.
        self.speeds = speeds or {}
        self.ranges = ranges
        self.etag = etag
        self.refuse = {}  # client address -> how many more requests to answer 503
        self.served_by = {}
        self._link_free_at = {}  # client address -> when its simulated link is next free
        self.requests = []
        self.lock = threading.Lock()
        origin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                client = self.client_address[0]
                path = self.path.split("?")[0]
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/file.bin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path != "/file.bin":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                with origin.lock:
                    origin.requests.append((client, self.headers.get("Range"), self.headers.get("Cookie")))
                    if origin.refuse.get(client, 0) > 0:
                        origin.refuse[client] -= 1
                        refused = True
                    else:
                        refused = False
                if refused:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = len(origin.data)
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
                if m and origin.ranges:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                    end = min(end, size - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                else:
                    start, end = 0, size - 1
                    self.send_response(200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="test file.bin"')
                if origin.etag:
                    self.send_header("ETag", origin.etag)
                self.end_headers()
                speed = origin.speeds.get(client)
                pos = start
                sent = 0
                try:
                    while pos <= end:
                        chunk = 16 * 1024
                        piece = origin.data[pos:min(pos + chunk, end + 1)]
                        if speed == "stall" and sent > 128 * 1024:
                            time.sleep(60)
                            return
                        self.wfile.write(piece)
                        pos += len(piece)
                        sent += len(piece)
                        with origin.lock:
                            origin.served_by[client] = origin.served_by.get(client, 0) + len(piece)
                        if isinstance(speed, (int, float)):
                            # Every connection from one address shares its link, as on a real
                            # network: each chunk waits its turn.
                            with origin.lock:
                                now = time.monotonic()
                                done_at = max(now, origin._link_free_at.get(client, 0)) + len(piece) / speed
                                origin._link_free_at[client] = done_at
                            if done_at > now:
                                time.sleep(done_at - now)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = "http://127.0.0.1:%d/file.bin" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
