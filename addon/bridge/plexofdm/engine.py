"""Fetching one file over several networks at once, the way Plexo does, for FDM to read in order.

FDM reads a download from the bridge as one or more byte ranges ("front requests"), one per FDM
connection. The bridge cuts the file into fixed blocks and keeps, for each front request, a
window of blocks ahead of where FDM is reading. Every network runs its own streams, each pinned
to that network. A stream takes the next block nobody has taken, fetches it with an HTTP range
request, and takes another, so a fast network fetches more blocks than a slow one. Bytes reach
FDM as soon as they arrive in order.

When there is nothing left to take, an idle stream races a block another stream is fetching too
slowly, from where that one got to. Whichever finishes first wins (Plexo's "no slow finish").
"""

import http.client
import logging
import socket
import threading
import time
from collections import defaultdict
from urllib.parse import urljoin, urlsplit

from .routing import Client, NoRouteError

log = logging.getLogger("plexofdm.engine")

KIB = 1024
MIB = 1024 * KIB
READ_CHUNK = 64 * KIB

REDIRECTS = (301, 302, 303, 307, 308)
# A server turning streams away: it wants fewer connections from this address.
REFUSALS = (429, 503)
# Retries before a block, and so the download, gives up.
MAX_BLOCK_FAILURES = 24
# A worker with nothing to do for this long, and no front request open, closes its connection.
IDLE_EXIT_S = 20
# A front request that gets no new bytes for this long is closed, so FDM retries it.
FRONT_STALL_S = 90
# A lowered stream ceiling rises by one this often, as in Plexo.
RECOVER_S = 60
# Racing a slow block only pays when there is a good amount of it left.
RACE_MIN_BYTES = 256 * KIB
RACE_MIN_AGE_S = 2.0
STALLED_S = 5.0


def plan_block_size(total, networks, streams, min_block, max_block):
    """Plexo's plan: about two blocks per stream, within [min_block, max_block]."""
    if total <= 0:
        return max_block
    target_blocks = max(1, networks) * max(1, streams) * 2
    return int(max(min_block, min(max_block, -(-total // target_blocks))))


def parse_content_range(value):
    """'bytes 0-99/1234' -> (0, 99, 1234). Total is None for '*'."""
    if not value:
        return None
    try:
        unit, _, rest = value.strip().partition(" ")
        if unit.lower() != "bytes":
            return None
        span, _, total = rest.partition("/")
        first, _, last = span.partition("-")
        return int(first), int(last), (None if total.strip() == "*" else int(total))
    except ValueError:
        return None


def same_version(a, b):
    """ETags compared weakly: a CDN may add W/ to the same version."""
    def norm(tag):
        tag = tag.strip()
        return tag[2:] if tag.startswith("W/") else tag
    return norm(a) == norm(b)


class StatusError(Exception):
    def __init__(self, status, message=None):
        super().__init__(message or "The server answered %d" % status)
        self.status = status


class Refused(StatusError):
    pass


class LinkError(StatusError):
    """The link itself no longer works (expired, removed, forbidden)."""


class FileChanged(Exception):
    pass


class Attempt:
    """One stream fetching a block, from `start` (relative to the block) to its end."""

    __slots__ = ("block", "net_id", "start", "pos", "started", "last_data", "cancelled", "racer", "sock")

    def __init__(self, block, net_id, start, racer=False):
        self.block = block
        self.net_id = net_id
        self.start = start
        self.pos = start
        self.started = time.monotonic()
        self.last_data = self.started
        self.cancelled = False
        self.racer = racer
        self.sock = None

    def cancel(self):
        """Stops it, and wakes its stream if it's waiting on a connection that went quiet."""
        self.cancelled = True
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def rate(self, now):
        elapsed = now - self.started
        return (self.pos - self.start) / elapsed if elapsed > 0 else 0.0


class Block:
    __slots__ = ("index", "start", "end", "size", "buf", "attempts", "history", "done", "dead",
                 "failures", "retry_after", "by_net")

    def __init__(self, index, start, end):
        self.index = index
        self.start = start
        self.end = end  # inclusive
        self.size = end - start + 1
        self.buf = None
        self.attempts = []  # running now
        self.history = []  # every attempt, running or not: what each one wrote is kept
        self.done = False
        self.dead = False
        self.failures = 0
        self.retry_after = 0.0
        self.by_net = defaultdict(int)

    def coverage(self):
        """How many bytes from the block's start are in, without a gap."""
        covered = 0
        for attempt in sorted(self.history, key=lambda a: a.start):
            if attempt.start > covered:
                break
            covered = max(covered, attempt.pos)
        return covered

    def resume_point(self):
        """Where a new attempt picks up: the furthest any attempt got, when they join up."""
        return self.coverage()


class FrontRequest:
    """One FDM connection reading bytes [start, end] of the file."""

    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.cursor = start
        self.closed = False


class Session:
    """One download: what FDM reads from /d/<token>, and everything fetching it."""

    def __init__(self, bridge, token, meta):
        self.bridge = bridge
        self.token = token
        self.meta = meta
        self.size = int(meta["size"])
        self.block_size = int(meta["block_size"])
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.blocks = {}
        self.requests = []
        self.workers = {}  # (net_id, slot) -> thread
        self.allocated = 0
        self.error = None
        self.resolved = meta.get("resolved") or meta["url"]
        self.last_resolve = 0.0
        self.ceilings = {}  # net_id -> {"limit": n, "since": t}
        self.cooldown = {}  # net_id -> monotonic time the network may be used again
        self.net_failures = defaultdict(int)
        self.bytes_by_net = defaultdict(int)
        self.rates = {}
        self.served = 0
        self.last_served_at = 0.0
        self.last_activity = time.monotonic()
        self._sample = (time.monotonic(), {})

    # --- What FDM reads --------------------------------------------------------------------

    def serve(self, start, end, write):
        """Streams bytes [start, end] to `write`, in order, as they arrive."""
        req = FrontRequest(start, end)
        with self.cv:
            if self.error:
                raise FileChanged(self.error)
            self.requests.append(req)
            self.last_activity = time.monotonic()
            self._ensure_workers()
            self.cv.notify_all()
        try:
            while req.cursor <= end:
                with self.cv:
                    idx = req.cursor // self.block_size
                    waited_from = time.monotonic()
                    while True:
                        if self.error:
                            raise FileChanged(self.error)
                        block = self.blocks.get(idx)
                        if block is not None and block.buf is not None:
                            if block.dead:
                                raise ConnectionError("Block %d couldn't be fetched" % idx)
                            available = block.start + block.coverage()
                            if available > req.cursor:
                                break
                        if time.monotonic() - waited_from > FRONT_STALL_S:
                            raise TimeoutError("No network delivered data for %d s" % FRONT_STALL_S)
                        self.cv.wait(1.0)
                    upto = min(available - 1, end)
                    buf = block.buf
                    offset = req.cursor - block.start
                    count = upto - req.cursor + 1
                with memoryview(buf) as view:
                    write(view[offset:offset + count])
                with self.cv:
                    req.cursor = upto + 1
                    self.served += count
                    self.last_served_at = self.last_activity = time.monotonic()
                    if req.cursor > block.end or req.cursor > end:
                        self._collect()
                        self.cv.notify_all()
        finally:
            with self.cv:
                req.closed = True
                if req in self.requests:
                    self.requests.remove(req)
                self._collect()
                self.cv.notify_all()

    # --- Which block next ------------------------------------------------------------------

    def _limit(self, req):
        """The last byte this request will likely read. FDM opens a new connection for the
        second half of a range and stops the first one there, so blocks past another request's
        start belong to that request."""
        limit = req.end
        for other in self.requests:
            if other is not req and not other.closed and req.cursor < other.start <= limit:
                limit = other.start - 1
        return limit

    def _window_blocks(self):
        cfg = self.bridge.config
        nets = max(1, len({n for n, _ in self.workers}))
        streams = cfg["streams_per_network"] * nets
        per_request = (cfg["memory_limit_mb"] * MIB) // (self.block_size * max(1, len(self.requests)))
        return int(max(2, min(streams * 2, per_request)))

    def _block(self, idx):
        block = self.blocks.get(idx)
        if block is None:
            start = idx * self.block_size
            block = Block(idx, start, min(start + self.block_size, self.size) - 1)
            self.blocks[idx] = block
        if block.buf is None:
            block.buf = bytearray(block.size)
            self.allocated += block.size
        return block

    def _claim(self, net_id, now):
        """The block a stream on `net_id` should fetch now, nearest to where FDM reads first."""
        memory_limit = self.bridge.config["memory_limit_mb"] * MIB
        window = self._window_blocks()
        best = None
        for req in self.requests:
            if req.closed:
                continue
            first = req.cursor // self.block_size
            last = min(self._limit(req) // self.block_size, first + window - 1)
            busy = 0
            for depth, idx in enumerate(range(first, last + 1)):
                block = self.blocks.get(idx)
                if block is not None:
                    if block.attempts:
                        busy += 1
                    if block.done or block.dead or block.attempts or block.retry_after > now:
                        continue
                size = block.size if block is not None else self.block_size
                if depth > 0 and self.allocated + (0 if block is not None and block.buf is not None else size) > memory_limit:
                    break
                key = (depth, busy)
                if best is None or key < best[0]:
                    best = (key, idx)
                break
        if best is None:
            return None
        block = self._block(best[1])
        attempt = Attempt(block, net_id, block.resume_point())
        block.attempts.append(attempt)
        block.history.append(attempt)
        return attempt

    def _race(self, net_id, now):
        """With nothing left to take: a block another stream is fetching too slowly, raced
        from where that stream has got to."""
        my_rate = self._stream_rate(net_id)
        best = None
        heads = {req.cursor // self.block_size for req in self.requests if not req.closed}
        for block in self.blocks.values():
            if block.done or block.dead or len(block.attempts) != 1 or block.buf is None:
                continue
            slow = block.attempts[0]
            remaining = block.size - max(slow.pos, block.coverage())
            if remaining < RACE_MIN_BYTES or now - slow.started < RACE_MIN_AGE_S:
                continue
            stalled = now - slow.last_data > STALLED_S
            if slow.net_id == net_id and not stalled:
                continue
            eta = remaining / max(slow.rate(now), 1.0)
            if not stalled:
                if my_rate > 0:
                    if eta < 3.0 or eta < 2 * (remaining / my_rate + 0.5):
                        continue
                elif eta < 8.0:
                    continue
            key = (0 if block.index in heads else 1, -eta)
            if best is None or key < best[0]:
                best = (key, block, slow)
        if best is None:
            return None
        _, block, slow = best
        attempt = Attempt(block, net_id, max(slow.pos, block.coverage()), racer=True)
        block.attempts.append(attempt)
        block.history.append(attempt)
        return attempt

    def _stream_rate(self, net_id):
        streams = sum(1 for b in self.blocks.values() for a in b.attempts if a.net_id == net_id)
        return self.rates.get(net_id, 0.0) / max(1, streams)

    def _collect(self):
        """Lets go of blocks no open request will read."""
        keep = set()
        for req in self.requests:
            if req.closed:
                continue
            first = req.cursor // self.block_size
            last = self._limit(req) // self.block_size
            for block in self.blocks.values():
                if first <= block.index <= last:
                    keep.add(block.index)
        for idx in [i for i in self.blocks if i not in keep]:
            block = self.blocks.pop(idx)
            for attempt in block.attempts:
                attempt.cancel()
            if block.buf is not None:
                self.allocated -= block.size
                block.buf = None

    # --- Streams ---------------------------------------------------------------------------

    def _stream_limit(self, net_id):
        limit = self.bridge.config["streams_per_network"]
        ceiling = self.ceilings.get(net_id)
        return min(limit, ceiling["limit"]) if ceiling else limit

    def _ensure_workers(self):
        """Starts the streams each usable network should run (called with the lock held)."""
        if self.error or not self.requests:
            return
        for net in self.bridge.monitor.usable():
            for slot in range(self._stream_limit(net.id)):
                key = (net.id, slot)
                if key not in self.workers:
                    thread = threading.Thread(target=self._worker, args=(net.id, slot),
                                              name="stream-%s-%s-%d" % (self.token[:6], net.id, slot), daemon=True)
                    self.workers[key] = thread
                    thread.start()

    def networks_changed(self):
        with self.cv:
            self._ensure_workers()
            self.cv.notify_all()

    def _worker(self, net_id, slot):
        client = None
        idle_since = time.monotonic()
        backoff = 0.0
        try:
            while True:
                with self.cv:
                    attempt = None
                    while attempt is None:
                        now = time.monotonic()
                        net = self.bridge.monitor.get(net_id)
                        usable = net is not None and any(n.id == net_id for n in self.bridge.monitor.usable())
                        if self.error or not usable or slot >= self._stream_limit(net_id):
                            return
                        if not self.requests and now - idle_since > IDLE_EXIT_S:
                            return
                        if now >= self.cooldown.get(net_id, 0) and backoff <= 0:
                            attempt = self._claim(net_id, now) or self._race(net_id, now)
                            if attempt:
                                break
                        wait = 0.5 if backoff <= 0 else min(backoff, 0.5)
                        self.cv.wait(wait)
                        backoff -= wait
                if client is None or client.net.key() != net.key():
                    if client:
                        client.close()
                    client = Client(net, timeout=30)
                try:
                    reusable = self._fetch(client, attempt)
                    if not reusable:
                        client.close()
                    backoff = 0.0
                    with self.cv:
                        self.net_failures[net_id] = 0
                except Exception as error:
                    client.close()
                    backoff = self._failed(attempt, error, slot)
                finally:
                    with self.cv:
                        if attempt in attempt.block.attempts:
                            attempt.block.attempts.remove(attempt)
                        self.cv.notify_all()
                    idle_since = time.monotonic()
        finally:
            if client:
                client.close()
            with self.cv:
                self.workers.pop((net_id, slot), None)
                self.cv.notify_all()

    def _failed(self, attempt, error, slot):
        """Books a failed attempt. Returns how long this stream waits before the next one."""
        net_id = attempt.net_id
        block = attempt.block
        now = time.monotonic()
        with self.cv:
            if attempt.cancelled or block.done:
                return 0.0
            if isinstance(error, FileChanged):
                self._fail_session(str(error))
                return 0.0
            block.failures += 1
            if block.failures >= MAX_BLOCK_FAILURES and len(block.attempts) <= 1:
                block.dead = True
                self._fail_session("A part of the file couldn't be fetched after %d tries (%s)" % (block.failures, error))
                return 0.0
            serving = now - self.last_served_at < 10
            if isinstance(error, Refused) or (isinstance(error, LinkError) and error.status == 403 and serving):
                # The server wants fewer connections from this network: drop to the streams it
                # accepted, then win one back each RECOVER_S.
                running = len({k for k in self.workers if k[0] == net_id})
                limit = max(1, min(running - 1, self._stream_limit(net_id)))
                self.ceilings[net_id] = {"limit": limit, "since": now}
                log.info("%s refused a stream on %s (%s); %d streams now", self.meta.get("host"), net_id, error, limit)
                block.retry_after = now + 1.0
                return 5.0 if slot < limit else 0.0
            if isinstance(error, LinkError):
                block.retry_after = now + 2.0
                if now - self.last_resolve > 30:
                    self.last_resolve = now
                    threading.Thread(target=self._resolve_again, daemon=True).start()
                return 2.0
            if isinstance(error, NoRouteError):
                # This network can't reach the host at all (e.g. IPv6-only host): park it.
                self.cooldown[net_id] = now + 300
                return 0.0
            failures = self._network_failed(net_id, now)
            log.debug("Stream on %s failed: %s", net_id, error)
            return min(8.0, 0.5 * 2 ** min(failures, 4))

    def _network_failed(self, net_id, now):
        """Counts a failure against a network (lock held). From the third in a row, it rests."""
        self.net_failures[net_id] += 1
        failures = self.net_failures[net_id]
        if failures >= 3:
            self.cooldown[net_id] = now + min(60, 5 * 2 ** (failures - 3))
            self.bridge.monitor.report_failure(net_id)
        return failures

    def _fail_session(self, message):
        if self.error:
            return
        self.error = message
        log.warning("Download %s stopped: %s", self.meta.get("filename"), message)
        self.cv.notify_all()

    def _resolve_again(self):
        """Signed links often redirect to a short-lived URL: follow the original link again."""
        from .probe import resolve

        for net in self.bridge.monitor.usable():
            try:
                final = resolve(net, self.meta["url"], self.headers_for)
                with self.cv:
                    self.resolved = final
                    self.cv.notify_all()
                log.info("Followed %s again", self.meta["url"])
                return
            except Exception:
                continue

    def headers_for(self, url):
        from .probe import build_headers

        return build_headers(self.meta, url)

    def _fetch(self, client, attempt):
        """Fetches the rest of `attempt`'s block. Returns whether the connection can be reused."""
        block = attempt.block
        first = block.start + attempt.start
        with self.cv:
            target = self.resolved
        for _ in range(6):
            url = urlsplit(target)
            headers = self.headers_for(target)
            headers["Range"] = "bytes=%d-%d" % (first, block.end)
            headers["Accept-Encoding"] = "identity"
            resp = client.request(url, headers)
            if resp.status in REDIRECTS and resp.getheader("Location"):
                resp.read()
                target = urljoin(target, resp.getheader("Location"))
                with self.cv:
                    self.resolved = target
                continue
            break
        else:
            raise StatusError(310, "Too many redirects")

        whole = False
        if resp.status == 206:
            span = parse_content_range(resp.getheader("Content-Range"))
            if span is None or span[0] != first:
                raise StatusError(206, "The server sent a different part of the file")
            if span[2] is not None and span[2] != self.size:
                raise FileChanged("The file changed on the server (its size is now %d bytes)" % span[2])
        elif resp.status == 200:
            length = resp.getheader("Content-Length")
            if first != 0 or (length is not None and int(length) != self.size):
                raise StatusError(200, "The server ignored the byte range")
            whole = True
        elif resp.status in REFUSALS:
            raise Refused(resp.status)
        elif resp.status in (401, 403, 404, 410):
            raise LinkError(resp.status)
        else:
            raise StatusError(resp.status)

        etag = resp.getheader("ETag")
        if self.meta.get("etag") and etag and not same_version(etag, self.meta["etag"]):
            raise FileChanged("The file changed on the server (new ETag)")
        modified = resp.getheader("Last-Modified")
        if not self.meta.get("etag") and self.meta.get("last_modified") and modified \
                and modified != self.meta["last_modified"]:
            raise FileChanged("The file changed on the server (new Last-Modified)")

        net_id = attempt.net_id
        with self.cv:
            buf = block.buf
            if buf is None or attempt.cancelled or block.done:
                return False
            attempt.sock = client.socket()
        view = memoryview(buf)
        try:
            while True:
                with self.cv:
                    if attempt.cancelled or block.done:
                        return False
                    pos = attempt.pos
                if pos >= block.size:
                    break
                try:
                    n = resp.readinto(view[pos:min(pos + READ_CHUNK, block.size)])
                except http.client.IncompleteRead as error:
                    raise ConnectionError("The server closed the connection early") from error
                if not n:
                    raise ConnectionError("The server closed the connection early")
                now = time.monotonic()
                with self.cv:
                    attempt.pos += n
                    attempt.last_data = now
                    block.by_net[net_id] += n
                    self.bytes_by_net[net_id] += n
                    self.bridge.count(net_id, n)
                    if not block.done and block.coverage() >= block.size:
                        block.done = True
                        for other in block.attempts:
                            if other is not attempt:
                                other.cancel()
                                if now - other.last_data > STALLED_S:
                                    # Lost a race having gone quiet: a network that keeps
                                    # doing that is rested like one that can't connect.
                                    self._network_failed(other.net_id, now)
                    self.cv.notify_all()
        finally:
            view.release()
        return not whole and resp.isclosed()

    # --- Bookkeeping -----------------------------------------------------------------------

    def tick(self, now):
        """Once a second: rates, and stream ceilings recovering."""
        with self.cv:
            then, before = self._sample
            elapsed = max(0.001, now - then)
            current = dict(self.bytes_by_net)
            for net_id, total in current.items():
                rate = (total - before.get(net_id, 0)) / elapsed
                previous = self.rates.get(net_id, rate)
                self.rates[net_id] = 0.6 * rate + 0.4 * previous
            self._sample = (now, current)
            for net_id, ceiling in list(self.ceilings.items()):
                if now - ceiling["since"] >= RECOVER_S:
                    ceiling["limit"] += 1
                    ceiling["since"] = now
                    if ceiling["limit"] >= self.bridge.config["streams_per_network"]:
                        del self.ceilings[net_id]
            self._ensure_workers()

    def status(self):
        with self.cv:
            active = defaultdict(int)
            for block in self.blocks.values():
                for attempt in block.attempts:
                    active[attempt.net_id] += 1
            return {
                "token": self.token,
                "filename": self.meta.get("filename"),
                "host": self.meta.get("host"),
                "size": self.size,
                "served": self.served,
                "connections": len([r for r in self.requests if not r.closed]),
                "error": self.error,
                "bytes_by_network": dict(self.bytes_by_net),
                "rate_by_network": {k: round(v) for k, v in self.rates.items()},
                "streams_by_network": dict(active),
                "buffered": self.allocated,
                "active": bool(self.requests),
            }
