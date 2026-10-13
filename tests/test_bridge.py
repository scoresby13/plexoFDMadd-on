"""Tests for the bridge, against a local test server. Loopback addresses stand in for networks:
the test server paces each source address as one link, so 127.0.0.2 and 127.0.0.3 behave like a
slow and a fast network. Linux only (other systems don't route all of 127.0.0.0/8 to loopback)."""

import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "..", "addon", "bridge")
sys.path.insert(0, BRIDGE)
sys.path.insert(0, HERE)

os.environ["PLEXO_FDM_HOME"] = tempfile.mkdtemp(prefix="plexo-fdm-test-")

from origin import Origin  # noqa: E402
from plexofdm import engine, probe, server  # noqa: E402
from plexofdm.config import Config  # noqa: E402

KEY = "test-key-" + "x" * 32
MB = 1000 * 1000
MIB = 1024 * 1024
_ports = iter(range(48100, 48200))

TWO_NETWORKS = [{"id": "slow", "name": "Slow", "addresses": ["127.0.0.2"]},
                {"id": "fast", "name": "Fast", "addresses": ["127.0.0.3"]}]


def start_bridge(**settings):
    config = Config()
    config._values.update({"test_networks": TWO_NETWORKS, "streams_per_network": 4})
    config._values.update(settings)
    port = next(_ports)
    bridge = server.Bridge(config, KEY, port)
    httpd = server.Server(("127.0.0.1", port), server.make_handler(bridge))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    bridge.monitor.wait_for_checks()
    return bridge, httpd


def call(bridge, path, body=None, key=KEY, host=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (bridge.port, path),
                                 data=None if body is None else json.dumps(body).encode())
    if key:
        req.add_header("X-Plexo-Key", key)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def register(bridge, url, **extra):
    status, answer = call(bridge, "/api/register", dict({"url": url}, **extra))
    assert status == 200, answer
    return answer


def download(url):
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def fdm_download(url, size, segments=4):
    """Reads the file the way FDM does: one open-ended connection first, then more connections
    for the later parts, each earlier connection stopping where the next begins."""
    out = bytearray(size)
    host, port = url.split("/")[2].split(":")
    path = "/" + url.split("/", 3)[3]
    bounds = [size * i // segments for i in range(segments)] + [size]
    errors = []

    def segment(i):
        try:
            conn = http.client.HTTPConnection(host, int(port), timeout=120)
            conn.request("GET", path, headers={"Range": "bytes=%d-" % bounds[i]})
            resp = conn.getresponse()
            assert resp.status == 206, resp.status
            pos, stop = bounds[i], bounds[i + 1]
            while pos < stop:
                chunk = resp.read(min(256 * 1024, stop - pos))
                assert chunk, "connection ended early at %d" % pos
                out[pos:pos + len(chunk)] = chunk
                pos += len(chunk)
            conn.close()  # Mid-response, as FDM does when it reaches the next part.
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=segment, args=(0,))]
    threads[0].start()
    time.sleep(0.3)
    for i in range(1, segments):
        threads.append(threading.Thread(target=segment, args=(i,)))
        threads[-1].start()
    for t in threads:
        t.join(180)
    if errors:
        raise errors[0]
    return bytes(out)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Units(unittest.TestCase):
    def test_parse_range(self):
        self.assertEqual(server.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(server.parse_range("bytes=500-", 1000), (500, 999))
        self.assertEqual(server.parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(server.parse_range("bytes=900-5000", 1000), (900, 999))
        self.assertEqual(server.parse_range("bytes=0-1,5-6", 1000), (0, 1))
        self.assertEqual(server.parse_range("bytes=1000-", 1000), "invalid")
        self.assertIsNone(server.parse_range("items=0-1", 1000))
        self.assertIsNone(server.parse_range(None, 1000))

    def test_content_range(self):
        self.assertEqual(engine.parse_content_range("bytes 0-0/1234"), (0, 0, 1234))
        self.assertEqual(engine.parse_content_range("bytes 5-9/*"), (5, 9, None))
        self.assertIsNone(engine.parse_content_range("garbage"))

    def test_block_plan(self):
        # About two blocks per stream, within the limits.
        self.assertEqual(engine.plan_block_size(1024 * MIB, 2, 8, 512 * 1024, 4 * MIB), 4 * MIB)
        self.assertEqual(engine.plan_block_size(16 * MIB, 2, 8, 512 * 1024, 4 * MIB), 512 * 1024)
        self.assertEqual(engine.plan_block_size(64 * MIB, 2, 8, 512 * 1024, 4 * MIB), 2 * MIB)

    def test_coverage_joins_attempts(self):
        block = engine.Block(0, 0, 999)
        slow = engine.Attempt(block, "a", 0)
        slow.pos = 300
        racer = engine.Attempt(block, "b", 250)
        racer.pos = 1000
        block.history = [racer, slow]
        self.assertEqual(block.coverage(), 1000)
        slow.pos = 200  # A gap between the two: only the first part counts.
        self.assertEqual(block.coverage(), 200)

    def test_cookies_only_for_their_domain(self):
        cookies = [{"domain": ".example.com", "name": "a", "value": "1"},
                   {"domain": "other.org", "name": "b", "value": "2"},
                   {"domain": "example.com", "name": "c", "value": "3", "isSecure": True}]
        self.assertEqual(probe.cookie_header(cookies, "http://dl.example.com/f"), "a=1")
        self.assertEqual(probe.cookie_header(cookies, "https://example.com/f"), "a=1; c=3")

    def test_filenames(self):
        self.assertEqual(probe.filename_from('attachment; filename="a b.iso"', "http://x/y"), "a b.iso")
        self.assertEqual(probe.filename_from("attachment; filename*=UTF-8''%E2%82%AC.zip", "http://x/y"), "€.zip")
        self.assertEqual(probe.filename_from(None, "http://x/dir/file%20name.tar.gz?x=1"), "file name.tar.gz")
        self.assertEqual(probe.filename_from('attachment; filename="../../evil.exe"', "http://x/"), "evil.exe")
        self.assertEqual(probe.filename_from(None, "http://x/"), "download")


class Downloads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge, cls.httpd = start_bridge(min_size_mb=4)

    def test_combines_two_networks(self):
        origin = Origin(size=24 * MB, speeds={"127.0.0.2": 2 * MB, "127.0.0.3": 6 * MB})
        try:
            answer = register(self.bridge, origin.url)
            self.assertTrue(answer["ok"], answer)
            self.assertEqual(answer["filename"], "test file.bin")
            self.assertEqual(sorted(answer["networks"]), ["Fast", "Slow"])
            began = time.monotonic()
            data = download(answer["url"])
            elapsed = time.monotonic() - began
            self.assertEqual(sha(data), origin.sha256)
            slow, fast = origin.served_by["127.0.0.2"], origin.served_by["127.0.0.3"]
            # Both networks carried the file, the fast one more of it.
            self.assertGreater(slow, 2 * MB)
            self.assertGreater(fast, slow)
            # Faster than the fast network alone could have managed (24 MB at 6 MB/s = 4 s).
            self.assertLess(elapsed, 3.9)
        finally:
            origin.close()

    def test_fdm_style_segments(self):
        origin = Origin(size=20 * MB, speeds={"127.0.0.2": 8 * MB, "127.0.0.3": 8 * MB})
        try:
            answer = register(self.bridge, origin.url)
            data = fdm_download(answer["url"], answer["size"], segments=4)
            self.assertEqual(sha(data), origin.sha256)
            session = self.bridge.sessions[answer["url"].split("/")[4]]
            time.sleep(0.2)
            self.assertEqual(session.allocated, 0)  # Every block let go once read.
            # Blocks FDM never read (past where a connection stopped) cost little.
            fetched = sum(origin.served_by.values())
            self.assertLess(fetched, 20 * MB * 1.35)
        finally:
            origin.close()

    def test_head_and_ranges(self):
        origin = Origin(size=6 * MB)
        try:
            answer = register(self.bridge, origin.url)
            req = urllib.request.Request(answer["url"], method="HEAD")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.headers["Content-Length"], str(6 * MB))
                self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
            req = urllib.request.Request(answer["url"], headers={"Range": "bytes=1000-1999"})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 206)
                self.assertEqual(resp.read(), origin.data[1000:2000])
            req = urllib.request.Request(answer["url"], headers={"Range": "bytes=-10"})
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.read(), origin.data[-10:])
        finally:
            origin.close()

    def test_stalled_network_is_raced(self):
        origin = Origin(size=12 * MB, speeds={"127.0.0.2": "stall", "127.0.0.3": 10 * MB})
        try:
            answer = register(self.bridge, origin.url)
            began = time.monotonic()
            data = download(answer["url"])
            self.assertEqual(sha(data), origin.sha256)
            # Without racing, a stalled stream holds its block until its 30 s read timeout.
            self.assertLess(time.monotonic() - began, 20)
        finally:
            origin.close()

    def test_refused_streams(self):
        origin = Origin(size=10 * MB, speeds={"127.0.0.2": 5 * MB, "127.0.0.3": 5 * MB})
        try:
            answer = register(self.bridge, origin.url)
            origin.refuse["127.0.0.3"] = 6
            data = download(answer["url"])
            self.assertEqual(sha(data), origin.sha256)
        finally:
            origin.close()

    def test_redirects_and_cookies(self):
        origin = Origin(size=6 * MB)
        try:
            cookies = [{"domain": "127.0.0.1", "name": "session", "value": "s3cret"},
                       {"domain": "example.com", "name": "other", "value": "no"}]
            answer = register(self.bridge, origin.url.replace("/file.bin", "/redirect"), cookies=cookies,
                              headers={"User-Agent": "TestAgent/1"})
            self.assertTrue(answer["ok"], answer)
            data = download(answer["url"])
            self.assertEqual(sha(data), origin.sha256)
            sent = {cookie for _, _, cookie in origin.requests}
            self.assertEqual(sent, {"session=s3cret"})
        finally:
            origin.close()

    def test_changed_file_is_refused(self):
        origin = Origin(size=8 * MB, speeds={"127.0.0.2": 4 * MB, "127.0.0.3": 4 * MB})
        try:
            answer = register(self.bridge, origin.url)
            origin.etag = '"v2"'
            with self.assertRaises(Exception):
                data = download(answer["url"])
                if len(data) != 8 * MB:
                    raise IOError("short")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                download(answer["url"])
            self.assertEqual(caught.exception.code, 409)
            # Registering again sees the new version, under a new link.
            again = register(self.bridge, origin.url)
            self.assertNotEqual(again["url"], answer["url"])
            self.assertEqual(sha(download(again["url"])), origin.sha256)
        finally:
            origin.close()

    def test_declines_what_wont_gain(self):
        no_ranges = Origin(size=6 * MB, ranges=False)
        small = Origin(size=1 * MB)
        try:
            self.assertEqual(register(self.bridge, no_ranges.url)["reason"], "ranges")
            self.assertEqual(register(self.bridge, small.url)["reason"], "small")
            self.assertEqual(register(self.bridge, small.url, size=1000)["reason"], "small")
            forced = register(self.bridge, small.url + "#plexo")
            self.assertTrue(forced["ok"], forced)
            self.assertEqual(sha(download(forced["url"])), small.sha256)
            self.assertEqual(register(self.bridge, "ftp://example.com/x")["reason"], "unsupported")
            own = "http://127.0.0.1:%d/d/abc/x" % self.bridge.port
            self.assertEqual(register(self.bridge, own)["reason"], "unsupported")
        finally:
            no_ranges.close()
            small.close()

    def test_api_is_guarded(self):
        status, _ = call(self.bridge, "/api/register", {"url": "http://x/"}, key=None)
        self.assertEqual(status, 401)
        status, _ = call(self.bridge, "/api/state", key="wrong")
        self.assertEqual(status, 401)
        status, _ = call(self.bridge, "/api/state", host="attacker.example:%d" % self.bridge.port)
        self.assertEqual(status, 403)
        status, state = call(self.bridge, "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual({n["id"] for n in state["networks"]}, {"slow", "fast"})
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.bridge.port) as resp:
            self.assertIn(KEY, resp.read().decode())

    def test_unknown_download(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            download("http://127.0.0.1:%d/d/nope/file.bin" % self.bridge.port)
        self.assertEqual(caught.exception.code, 404)


class NetworkChoice(unittest.TestCase):
    def test_one_network_is_not_enough_unless_forced(self):
        bridge, _ = start_bridge(min_size_mb=1, test_networks=TWO_NETWORKS[:1])
        origin = Origin(size=4 * MB)
        try:
            self.assertEqual(register(bridge, origin.url)["reason"], "networks")
            self.assertTrue(register(bridge, origin.url + "#plexo")["ok"])
        finally:
            origin.close()

    def test_switched_off_network_is_not_used(self):
        bridge, _ = start_bridge(min_size_mb=1, min_networks=1)
        status, _ = call(bridge, "/api/config", {"disabled_networks": ["slow"]})
        self.assertEqual(status, 200)
        origin = Origin(size=6 * MB)
        try:
            answer = register(bridge, origin.url)
            self.assertEqual(answer["networks"], ["Fast"])
            self.assertEqual(sha(download(answer["url"])), origin.sha256)
            self.assertNotIn("127.0.0.2", origin.served_by)
        finally:
            origin.close()
            bridge.config.update({"disabled_networks": []})

    def test_auto_off_only_takes_marked_links(self):
        bridge, _ = start_bridge(min_size_mb=1, auto=False)
        origin = Origin(size=4 * MB)
        try:
            self.assertEqual(register(bridge, origin.url)["reason"], "disabled")
            self.assertTrue(register(bridge, origin.url + "#plexo")["ok"])
        finally:
            origin.close()


class CommandLine(unittest.TestCase):
    """What FDM runs: the register command starts the bridge in the background on first use."""

    def test_register_starts_the_bridge(self):
        home = tempfile.mkdtemp(prefix="plexo-fdm-cli-")
        port = next(_ports)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"port": port, "test_networks": TWO_NETWORKS, "min_size_mb": 1}, f)
        env = dict(os.environ, PLEXO_FDM_HOME=home)
        script = os.path.join(BRIDGE, "plexo_fdm.py")
        origin = Origin(size=5 * MB)
        try:
            # Small or switched off: answered without starting anything.
            out = subprocess.run([sys.executable, script, "register", "--url", origin.url, "--size", "10"],
                                 capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(json.loads(out.stdout)["reason"], "small")
            self.assertFalse(os.path.exists(os.path.join(home, "daemon.json")))

            cookies = os.path.join(home, "cookies.json")
            with open(cookies, "w") as f:
                json.dump([{"domain": "127.0.0.1", "name": "k", "value": "v"}], f)
            out = subprocess.run([sys.executable, script, "register", "--url", origin.url,
                                  "--cookies-file", cookies, "--user-agent", "FDM/6"],
                                 capture_output=True, text=True, env=env, timeout=60)
            answer = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertTrue(answer["ok"], (answer, out.stderr))
            self.assertFalse(os.path.exists(cookies))  # Deleted once read.
            self.assertEqual(sha(download(answer["url"])), origin.sha256)
            self.assertTrue(os.path.exists(os.path.join(home, "daemon.json")))
        finally:
            subprocess.run([sys.executable, script, "stop"], env=env, timeout=30, capture_output=True)
            origin.close()


if __name__ == "__main__":
    unittest.main()
