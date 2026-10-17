"""Connections that leave through one chosen network.

Each socket is bound to one of the network's own addresses, so its packets go out through that
network. Windows and macOS route by source address. Linux picks the way out from the routing
table alone, so there the socket is also pinned to the device itself with SO_BINDTODEVICE
(unprivileged since Linux 5.7), as Plexo does.
"""

import http.client
import ipaddress
import logging
import os
import socket
import ssl
import sys
import threading

log = logging.getLogger("plexofdm.routing")

SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
IP_BOUND_IF = 25  # macOS: <netinet/in.h>
IPV6_BOUND_IF = 125

_device_binding = {"supported": sys.platform.startswith("linux"), "warned": False}


def _is_loopback(address):
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


def bind_socket(sock, net, local_address, family):
    """Ties `sock` to `net` before it connects."""
    if not net.test and not _is_loopback(local_address):
        if sys.platform.startswith("linux") and _device_binding["supported"]:
            try:
                sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, net.id.encode() + b"\0")
            except OSError as error:
                # Kernels before 5.7 only let root do this. Source addresses still work where
                # each network has its own route (policy routing, or one default route each).
                _device_binding["supported"] = False
                if not _device_binding["warned"]:
                    _device_binding["warned"] = True
                    log.warning("Can't pin sockets to a network device (%s); binding source addresses only", error)
        elif sys.platform == "darwin" and net.index:
            try:
                if family == 6:
                    sock.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, net.index)
                else:
                    sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, net.index)
            except OSError:
                pass
    if family == 6:
        sock.bind((local_address, 0, 0, 0))
    else:
        sock.bind((local_address, 0))


# By network and host, the remote address that last connected: where the next connection starts.
_last_good = {}
_last_good_lock = threading.Lock()


def open_socket(net, host, port, timeout):
    """A TCP connection to host:port through `net`, trying each address DNS gives that the
    network has a matching address family for."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ConnectionError("Couldn't look up %s: %s" % (host, error))
    families = net.families()
    routes = []
    for family, _, _, _, sockaddr in infos:
        fam = 6 if family == socket.AF_INET6 else 4
        if fam not in families:
            continue
        for local, local_fam in net.addresses:
            if local_fam == fam and (fam, sockaddr[0], local) not in [(r[0], r[1][0], r[2]) for r in routes]:
                routes.append((fam, sockaddr, local))
    if not routes:
        raise NoRouteError("%s has no address that can reach %s" % (net.name, host))
    memory = (net.id, host, port)
    with _last_good_lock:
        preferred = _last_good.get(memory)
    routes.sort(key=lambda r: 0 if (r[1][0], r[2]) == preferred else 1)

    last_error = None
    for fam, sockaddr, local in routes:
        sock = socket.socket(socket.AF_INET6 if fam == 6 else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            bind_socket(sock, net, local, fam)
            sock.connect(sockaddr)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with _last_good_lock:
                _last_good[memory] = (sockaddr[0], local)
            return sock
        except OSError as error:
            sock.close()
            last_error = error
            with _last_good_lock:
                if _last_good.get(memory) == (sockaddr[0], local):
                    del _last_good[memory]
    raise ConnectionError("Couldn't connect to %s through %s: %s" % (host, net.name, last_error))


class NoRouteError(ConnectionError):
    """The network has no address of a family the server has (an IPv6-only host on an IPv4-only
    network). Says nothing about the network's health."""


_ssl_context = None


def ssl_context():
    """The system's certificates, or a bundle found on disk where Python ships without them
    (python.org builds on macOS)."""
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):
        candidates = []
        try:
            import certifi  # type: ignore

            candidates.append(certifi.where())
        except ImportError:
            pass
        candidates += ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                       "/etc/pki/tls/certs/ca-bundle.crt", "/usr/local/etc/openssl/cert.pem",
                       "/opt/homebrew/etc/openssl@3/cert.pem"]
        for path in candidates:
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(path)
                    break
                except (OSError, ssl.SSLError):
                    continue
    _ssl_context = ctx
    return ctx


class RoutedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, net, host, port=None, timeout=30):
        super().__init__(host, port, timeout=timeout)
        self.net = net

    def connect(self):
        self.sock = open_socket(self.net, self.host, self.port, self.timeout)


class RoutedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, net, host, port=None, timeout=30):
        ctx = ssl_context()
        super().__init__(host, port, timeout=timeout, context=ctx)
        self.net = net
        self.plexo_context = ctx

    def connect(self):
        sock = open_socket(self.net, self.host, self.port, self.timeout)
        try:
            self.sock = self.plexo_context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class Client:
    """One stream's connection to one server through one network. Requests reuse a single
    kept-alive connection, so a stream pays for DNS, TCP and TLS and slow start only once."""

    def __init__(self, net, timeout=30):
        self.net = net
        self.timeout = timeout
        self._conn = None
        self._origin = None

    def request(self, url, headers):
        """GET `url` (a urllib SplitResult). Returns the response with its headers read."""
        origin = (url.scheme, url.hostname, url.port)
        if self._conn is not None and self._origin != origin:
            self.close()
        retried = False
        while True:
            fresh = self._conn is None
            if fresh:
                cls = RoutedHTTPSConnection if url.scheme == "https" else RoutedHTTPConnection
                self._conn = cls(self.net, url.hostname, url.port, timeout=self.timeout)
                self._origin = origin
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            try:
                self._conn.request("GET", path, headers=headers)
                return self._conn.getresponse()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as error:
                # A kept-alive connection the server closed while it sat idle only fails once
                # used: one more go on a new connection.
                self.close()
                if fresh or retried:
                    raise ConnectionError(str(error) or type(error).__name__)
                retried = True
            except (OSError, http.client.HTTPException):
                self.close()
                raise

    def socket(self):
        return self._conn.sock if self._conn is not None else None

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._origin = None
