"""The bridge as FDM's proxy: each connection FDM opens leaves through one of the networks.

FDM already splits a download into several connections and moves work to the connections that
finish first. With FDM's proxy set to the bridge, each of those connections, an HTTPS tunnel
(CONNECT) or a plain HTTP request, goes out through the network with the fewest of them open.
So one download's connections are spread over every network, and a faster network ends up
carrying more of the file. This needs nothing from FDM's add-on API, only its proxy setting.

It never breaks a download: with no network usable, or a destination on this computer, the
connection is made the ordinary way.
"""

import ipaddress
import logging
import socket
import threading
from urllib.parse import urlsplit

from .routing import open_socket

log = logging.getLogger("plexofdm.proxy")

CONNECT_TIMEOUT_S = 15
# A tunnel nobody has sent anything through for this long is closed.
IDLE_TIMEOUT_S = 300
CHUNK = 64 * 1024
HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "trailer", "upgrade"}


class Tunnels:
    """How many proxied connections each network carries now."""

    def __init__(self):
        self.lock = threading.Lock()
        self.open = {}

    def order(self, nets, rates):
        """Networks to try for a new connection: the one carrying fewest first, then the faster."""
        with self.lock:
            return sorted(nets, key=lambda n: (self.open.get(n.id, 0), -rates.get(n.id, 0)))

    def add(self, net_id, delta):
        with self.lock:
            self.open[net_id] = max(0, self.open.get(net_id, 0) + delta)

    def snapshot(self):
        with self.lock:
            return dict(self.open)


def _is_local(host):
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if ip.is_loopback:
            return True
    return False


def connect(bridge, host, port):
    """A socket to host:port and the network carrying it (None when made the ordinary way)."""
    nets = bridge.monitor.usable()
    # Test networks are loopback addresses, so there a local destination is routed too.
    if not _is_local(host) or any(n.test for n in nets):
        errors = []
        for net in bridge.tunnels.order(nets, bridge.rates):
            try:
                return open_socket(net, host, port, CONNECT_TIMEOUT_S), net.id
            except OSError as error:
                errors.append("%s: %s" % (net.name, error))
                bridge.monitor.report_failure(net.id)
        if errors:
            log.info("No network reached %s:%d (%s); connecting the ordinary way", host, port, "; ".join(errors))
    return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S), None


def relay(bridge, client, client_reader, upstream, net_id):
    """Copies bytes both ways until either side closes. Bytes coming back count for `net_id`."""
    upstream.settimeout(IDLE_TIMEOUT_S)
    client.settimeout(IDLE_TIMEOUT_S)
    if net_id:
        bridge.tunnels.add(net_id, 1)

    def outbound():
        try:
            while True:
                data = client_reader(CHUNK)
                if not data:
                    break
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    sender = threading.Thread(target=outbound, daemon=True)
    sender.start()
    try:
        while True:
            data = upstream.recv(CHUNK)
            if not data:
                break
            client.sendall(data)
            if net_id:
                bridge.count(net_id, len(data))
    except OSError:
        pass
    finally:
        if net_id:
            bridge.tunnels.add(net_id, -1)
        for sock in (upstream, client):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        upstream.close()
        sender.join(5)


def handle_connect(handler, bridge):
    """CONNECT host:port: an HTTPS tunnel."""
    host, _, port = handler.path.rpartition(":")
    host = host.strip("[]")
    try:
        port = int(port)
    except ValueError:
        return handler.send_error(400, "Bad CONNECT target")
    if _is_local(host) and port == bridge.port:
        return handler.send_error(403, "That is the bridge itself")
    try:
        upstream, net_id = connect(bridge, host, port)
    except OSError as error:
        return handler.send_error(502, "Couldn't reach %s: %s" % (host, error))
    handler.close_connection = True
    handler.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    # read1: whatever the client sent already sits in the handler's buffer, so read through it.
    relay(bridge, handler.connection, handler.rfile.read1, upstream, net_id)


def handle_absolute(handler, bridge):
    """GET http://host/path: a plain HTTP request, sent on as one request per connection."""
    target = urlsplit(handler.path)
    if target.scheme.lower() != "http" or not target.hostname:
        return handler.send_error(400, "Only http:// requests can be proxied this way")
    port = target.port or 80
    if _is_local(target.hostname) and port == bridge.port:
        return handler.send_error(403, "That is the bridge itself")
    try:
        upstream, net_id = connect(bridge, target.hostname, port)
    except OSError as error:
        return handler.send_error(502, "Couldn't reach %s: %s" % (target.hostname, error))
    path = target.path or "/"
    if target.query:
        path += "?" + target.query
    head = ["%s %s HTTP/1.1" % (handler.command, path)]
    for key, value in handler.headers.items():
        if key.lower() not in HOP_HEADERS:
            head.append("%s: %s" % (key, value))
    head.append("Connection: close")
    body = b""
    length = int(handler.headers.get("Content-Length") or 0)
    if length:
        body = handler.rfile.read(length)
    handler.close_connection = True
    try:
        upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
    except OSError as error:
        upstream.close()
        return handler.send_error(502, str(error))
    relay(bridge, handler.connection, lambda n: b"", upstream, net_id)


# --- SOCKS5 (RFC 1928): what FDM's torrent engine can send its peer connections through -------

SOCKS_VERSION = 5


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("The client closed the connection")
        data += chunk
    return data


def _socks_reply(sock, code):
    # Bound address 0.0.0.0:0: clients don't need it for CONNECT.
    sock.sendall(bytes([SOCKS_VERSION, code, 0, 1, 0, 0, 0, 0, 0, 0]))


def handle_socks(sock, bridge):
    """A SOCKS5 CONNECT, without authentication (the bridge only listens on this computer)."""
    sock.settimeout(30)
    version, count = _recv_exact(sock, 2)
    methods = _recv_exact(sock, count)
    if version != SOCKS_VERSION or 0 not in methods:
        sock.sendall(bytes([SOCKS_VERSION, 0xFF]))
        return
    sock.sendall(bytes([SOCKS_VERSION, 0]))
    version, command, _, kind = _recv_exact(sock, 4)
    if kind == 1:
        host = socket.inet_ntop(socket.AF_INET, _recv_exact(sock, 4))
    elif kind == 4:
        host = socket.inet_ntop(socket.AF_INET6, _recv_exact(sock, 16))
    elif kind == 3:
        host = _recv_exact(sock, _recv_exact(sock, 1)[0]).decode("idna")
    else:
        return _socks_reply(sock, 8)  # Address type not supported
    port = int.from_bytes(_recv_exact(sock, 2), "big")
    if command != 1:
        return _socks_reply(sock, 7)  # Only CONNECT: BIND and UDP ASSOCIATE aren't offered
    if _is_local(host) and port == bridge.port:
        return _socks_reply(sock, 2)
    try:
        upstream, net_id = connect(bridge, host, port)
    except OSError:
        return _socks_reply(sock, 4)  # Host unreachable
    _socks_reply(sock, 0)
    relay(bridge, sock, sock.recv, upstream, net_id)
