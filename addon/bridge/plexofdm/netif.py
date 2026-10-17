"""The computer's network adapters, and whether each one reaches the internet.

Python's standard library has no list of adapters, so each platform asks its own tool: `ip` or
the kernel on Linux, `ifconfig` and `networksetup` on macOS, PowerShell on Windows.
"""

import ipaddress
import json
import logging
import re
import socket
import struct
import subprocess
import sys
import threading
import time

log = logging.getLogger("plexofdm.netif")

TOOL_TIMEOUT_S = 6

# Adapters that rarely reach the internet on their own: VPN tunnels, virtual machine and container
# bridges, Apple's peer-to-peer Wi-Fi. Leaving them out is only a default (include_virtual).
VIRTUAL_NAME = re.compile(
    r"^(lo|docker|br-|veth|virbr|vmnet|vboxnet|zt|tailscale|wg|tun|tap|utun|ipsec|ppp|"
    r"awdl|llw|anpi|ap\d|bridge|gif|stf|p2p|dummy|kube|flannel|cni|cali|vEthernet)",
    re.IGNORECASE,
)
VIRTUAL_DESCRIPTION = re.compile(
    r"hyper-v|virtual|vmware|virtualbox|tap-windows|wintun|wireguard|tailscale|zerotier|"
    r"openvpn|loopback|teredo|isatap|bluetooth|vpn",
    re.IGNORECASE,
)


class Network:
    """One adapter: its device name, the name people know it by, and its usable addresses."""

    def __init__(self, id, name, kind, addresses, virtual=False, index=None):
        self.id = id
        self.name = name
        self.kind = kind
        # [(address, family)], family being 4 or 6.
        self.addresses = addresses
        self.virtual = virtual
        self.index = index
        self.test = False

    def families(self):
        return {family for _, family in self.addresses}

    def to_json(self):
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "addresses": [a for a, _ in self.addresses],
            "virtual": self.virtual,
        }

    def key(self):
        return (self.id, tuple(self.addresses))


def usable_address(address):
    """Not loopback or link-local: those never lead to the internet."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return None
    return (str(ip), ip.version)


def classify(text):
    name = text.lower()
    if re.search(r"wi-?fi|wireless|wlan|802\.11|airport|^wl", name):
        return "wifi"
    if re.search(r"rndis|remote ndis|tether|apple mobile device|iphone|ipad|android|usb|^enx|^en\w*u\d", name):
        return "usb"
    if re.search(r"cellular|mobile broadband|wwan|^ww|lte|5g", name):
        return "cellular"
    if re.search(r"ethernet|^en|^eth|lan", name):
        return "ethernet"
    return "other"


# --- Linux -----------------------------------------------------------------------------------

def _run(args):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TOOL_TIMEOUT_S,
        **_hidden_window(),
    )


def _hidden_window():
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def _linux_from_ip():
    out = _run(["ip", "-j", "addr", "show"])
    if out.returncode != 0:
        raise OSError(out.stderr)
    nets = []
    for link in json.loads(out.stdout):
        flags = link.get("flags", [])
        if "UP" not in flags or "LOOPBACK" in flags:
            continue
        if link.get("operstate") == "DOWN":
            continue
        addresses = []
        for info in link.get("addr_info", []):
            if info.get("scope") not in (None, "global"):
                continue
            usable = usable_address(info.get("local", ""))
            if usable:
                addresses.append(usable)
        if addresses:
            dev = link["ifname"]
            nets.append(Network(dev, dev, classify(dev), addresses, bool(VIRTUAL_NAME.match(dev)), link.get("ifindex")))
    return nets


def _linux_from_kernel():
    """IPv4 through ioctl, IPv6 from /proc: for systems without iproute2."""
    import fcntl

    SIOCGIFFLAGS = 0x8913
    SIOCGIFADDR = 0x8915
    IFF_UP, IFF_LOOPBACK, IFF_RUNNING = 0x1, 0x8, 0x40
    by_dev = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for index, dev in socket.if_nameindex():
            req = struct.pack("256s", dev.encode()[:15])
            try:
                flags = struct.unpack("H", fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, req)[16:18])[0]
            except OSError:
                continue
            if not flags & IFF_UP or not flags & IFF_RUNNING or flags & IFF_LOOPBACK:
                continue
            by_dev[dev] = (index, [])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
                usable = usable_address(addr)
                if usable:
                    by_dev[dev][1].append(usable)
            except OSError:
                pass
    finally:
        s.close()
    try:
        with open("/proc/net/if_inet6", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6 or parts[5] not in by_dev or int(parts[3], 16) != 0:
                    continue
                raw = parts[0]
                addr = ":".join(raw[i:i + 4] for i in range(0, 32, 4))
                usable = usable_address(addr)
                if usable:
                    by_dev[parts[5]][1].append(usable)
    except OSError:
        pass
    return [
        Network(dev, dev, classify(dev), addrs, bool(VIRTUAL_NAME.match(dev)), index)
        for dev, (index, addrs) in by_dev.items()
        if addrs
    ]


def _linux():
    try:
        return _linux_from_ip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return _linux_from_kernel()


# --- macOS -----------------------------------------------------------------------------------

def _mac_hardware_ports():
    """networksetup's "Hardware Port" names (Wi-Fi, iPhone USB). Read by position, not label,
    since the labels are translated on Macs in other languages (as Plexo does)."""
    names = {}
    try:
        out = _run(["networksetup", "-listallhardwareports"]).stdout
    except (OSError, subprocess.SubprocessError):
        return names
    for block in re.split(r"\n\s*\n", out):
        values = [line.split(":", 1)[1].strip() for line in block.splitlines() if ":" in line]
        if len(values) >= 2 and re.match(r"^[a-z]+\d+$", values[1]):
            names[values[1]] = values[0]
    return names


def _mac():
    out = _run(["ifconfig"]).stdout
    ports = _mac_hardware_ports()
    nets = []
    current = None
    for line in out.splitlines():
        header = re.match(r"^([A-Za-z0-9]+): flags=\w+<([^>]*)>", line)
        if header:
            dev, flags = header.group(1), header.group(2).split(",")
            current = None
            if "UP" in flags and "RUNNING" in flags and "LOOPBACK" not in flags:
                name = ports.get(dev, dev)
                current = Network(dev, name, classify(name if dev in ports else dev), [],
                                  bool(VIRTUAL_NAME.match(dev)) and dev not in ports)
                try:
                    current.index = socket.if_nametoindex(dev)
                except OSError:
                    pass
                nets.append(current)
            continue
        if current is None:
            continue
        m = re.match(r"^\s+inet6?\s+(\S+)", line)
        if m:
            usable = usable_address(m.group(1))
            if usable and "deprecated" not in line and usable not in current.addresses:
                current.addresses.append(usable)
        if re.match(r"^\s+status: inactive", line):
            current.addresses = []
    return [n for n in nets if n.addresses]


# --- Windows ---------------------------------------------------------------------------------

_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$adapters = @(Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } |
  Select-Object Name, InterfaceDescription, InterfaceIndex, NdisPhysicalMedium, Virtual)
$ips = @(Get-NetIPAddress -AddressState Preferred |
  Select-Object InterfaceIndex, IPAddress, @{n='Family'; e={ [int]$_.AddressFamily }})
@{ adapters = $adapters; ips = $ips } | ConvertTo-Json -Compress -Depth 4
"""


def _windows():
    out = _run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SCRIPT])
    data = json.loads(out.stdout.strip().lstrip("﻿"))

    def as_list(value):
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    by_index = {}
    for ip in as_list(data.get("ips")):
        usable = usable_address(str(ip.get("IPAddress", "")))
        if usable:
            by_index.setdefault(ip.get("InterfaceIndex"), []).append(usable)
    nets = []
    for adapter in as_list(data.get("adapters")):
        addresses = by_index.get(adapter.get("InterfaceIndex"), [])
        if not addresses:
            continue
        name = adapter.get("Name") or str(adapter.get("InterfaceIndex"))
        description = adapter.get("InterfaceDescription") or ""
        medium = adapter.get("NdisPhysicalMedium")
        # NDIS media: 1 = wireless LAN, 9 = native 802.11, 8 = WWAN, 14 = Ethernet (802.3).
        if medium in (1, 9):
            kind = "wifi"
        elif medium == 8:
            kind = "cellular"
        else:
            kind = classify(description) if classify(description) != "other" else classify(name)
            if kind == "other" and medium == 14:
                kind = "ethernet"
        virtual = bool(adapter.get("Virtual")) or bool(VIRTUAL_DESCRIPTION.search(description)) \
            or bool(VIRTUAL_NAME.match(name))
        nets.append(Network(name, name, kind, addresses, virtual, adapter.get("InterfaceIndex")))
    return nets


def _fallback():
    """Addresses only, without names: what the resolver knows of this computer."""
    seen = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            usable = usable_address(info[4][0])
            if usable and usable not in seen:
                seen.append(usable)
    except OSError:
        pass
    return [Network(addr, addr, "other", [(addr, fam)]) for addr, fam in seen]


def discover(test_networks=None):
    if test_networks:
        nets = []
        for item in test_networks:
            addresses = [(a, 6 if ":" in a else 4) for a in item.get("addresses", [])]
            net = Network(item["id"], item.get("name", item["id"]), item.get("kind", "other"), addresses)
            net.test = True
            nets.append(net)
        return nets
    try:
        if sys.platform.startswith("linux"):
            nets = _linux()
        elif sys.platform == "darwin":
            nets = _mac()
        elif sys.platform == "win32":
            nets = _windows()
        else:
            nets = _fallback()
    except Exception as error:  # A tool that's missing or refuses mustn't stop downloads.
        log.warning("Couldn't list networks (%s), using the fallback", error)
        nets = _fallback()
    nets.sort(key=lambda n: n.id)
    return nets


# --- Reachability ----------------------------------------------------------------------------

# Anycast addresses that answer on 443 almost everywhere. Connecting is enough: no data is sent.
ANCHORS = {4: [("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443)],
           6: [("2606:4700:4700::1111", 443), ("2001:4860:4860::8888", 443)]}


def check_internet(net, timeout=4.0):
    """Round trip in ms through `net` to the first anchor that answers, or None."""
    from .routing import bind_socket

    for family in sorted(net.families()):
        for host, port in ANCHORS[family]:
            for local, fam in net.addresses:
                if fam != family:
                    continue
                s = socket.socket(socket.AF_INET6 if family == 6 else socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                try:
                    bind_socket(s, net, local, family)
                    start = time.monotonic()
                    s.connect((host, port))
                    return round((time.monotonic() - start) * 1000)
                except OSError:
                    continue
                finally:
                    s.close()
    return None


class NetworkMonitor:
    """The networks as they are now: looked at every few seconds, since nothing tells a process
    when an adapter appears, drops, or gets a new address."""

    POLL_S = 3
    RECHECK_S = 30

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._nets = {}
        # id -> {"online": bool|None, "latency": ms, "checked": monotonic, "key": Network.key()}
        self._health = {}
        self._listeners = []
        self._changed = threading.Event()
        self.refresh()
        threading.Thread(target=self._loop, name="network-monitor", daemon=True).start()

    def on_change(self, callback):
        self._listeners.append(callback)

    def _loop(self):
        while True:
            self._changed.wait(self.POLL_S)
            self._changed.clear()
            try:
                self.refresh()
            except Exception:
                log.exception("Network refresh failed")

    def report_failure(self, net_id):
        """A stream on this network couldn't connect: look again soon rather than in 30 s."""
        with self._lock:
            health = self._health.get(net_id)
            if health and health.get("online"):
                health["checked"] = 0
        self._changed.set()

    def refresh(self):
        cfg = self.config.snapshot()
        found = {n.id: n for n in discover(cfg.get("test_networks"))}
        changed = False
        with self._lock:
            if {k: n.key() for k, n in found.items()} != {k: n.key() for k, n in self._nets.items()}:
                changed = True
            self._nets = found
            for gone in set(self._health) - set(found):
                del self._health[gone]
            to_check = []
            now = time.monotonic()
            for net in found.values():
                health = self._health.get(net.id)
                if health is None or health["key"] != net.key():
                    health = {"online": None, "latency": None, "checked": 0, "key": net.key()}
                    self._health[net.id] = health
                    changed = True
                if now - health["checked"] >= self.RECHECK_S and not health.get("checking"):
                    health["checking"] = True
                    to_check.append(net)
        for net in to_check:
            threading.Thread(target=self._check, args=(net, cfg), daemon=True).start()
        if changed:
            self._notify()

    def _check(self, net, cfg):
        if net.test or not cfg.get("connectivity_check", True):
            latency, online = None, True
        else:
            latency = check_internet(net)
            online = latency is not None
        with self._lock:
            health = self._health.get(net.id)
            if health is None or health["key"] != net.key():
                return
            was = health["online"]
            health.update(online=online, latency=latency, checked=time.monotonic(), checking=False)
        if was != online:
            log.info("Network %s (%s) is %s", net.name, net.id, "online" if online else "offline")
            self._notify()

    def _notify(self):
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                log.exception("Network listener failed")

    def all(self):
        """Every network with its state, for the status page."""
        cfg = self.config.snapshot()
        with self._lock:
            out = []
            for net in self._nets.values():
                health = self._health.get(net.id, {})
                item = net.to_json()
                item["online"] = health.get("online")
                item["latency_ms"] = health.get("latency")
                item["enabled"] = self._eligible(net, cfg, ignore_health=True)
                out.append(item)
            return out

    def _eligible(self, net, cfg, ignore_health=False):
        if net.id in cfg.get("disabled_networks", []):
            return False
        if net.virtual and not cfg.get("include_virtual"):
            # Switched on by name, a virtual adapter is used anyway.
            if net.id not in cfg.get("enabled_networks", []):
                return False
        if ignore_health:
            return True
        return bool(self._health.get(net.id, {}).get("online"))

    def usable(self):
        """The networks downloads may use now: switched on and online."""
        cfg = self.config.snapshot()
        with self._lock:
            return [n for n in self._nets.values() if self._eligible(n, cfg)]

    def wait_for_checks(self, timeout=6.0):
        """At start, reachability checks are still running: give them a moment."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                pending = [h for h in self._health.values() if h["online"] is None]
            if not pending:
                return
            time.sleep(0.1)

    def get(self, net_id):
        with self._lock:
            return self._nets.get(net_id)

