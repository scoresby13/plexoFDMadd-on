"""Where the bridge keeps its files, and the settings people can change."""

import json
import os
import secrets
import sys
import threading

DEFAULT_PORT = 47913

DEFAULTS = {
    # The port FDM's download links point at. It stays fixed so links survive a restart.
    "port": DEFAULT_PORT,
    # Route large downloads through the bridge without being asked. When off, only links ending
    # in #plexo go through it.
    "auto": True,
    # Smaller files gain little from splitting, so FDM keeps them.
    "min_size_mb": 16,
    # With fewer networks than this online, there is nothing to combine, so FDM keeps the
    # download (a link ending in #plexo still goes through the bridge).
    "min_networks": 2,
    # Streams each network runs for each download, as Plexo starts with.
    "streams_per_network": 8,
    # Blocks are held in memory until FDM reads them, so they're smaller than Plexo's 8 MB.
    "max_block_kb": 4096,
    "min_block_kb": 512,
    # The most a download may hold in blocks FDM hasn't read yet.
    "memory_limit_mb": 256,
    # Networks (by device name) people switched off.
    "disabled_networks": [],
    # Virtual adapters people switched on anyway.
    "enabled_networks": [],
    # VPNs, virtual machine and container adapters rarely lead anywhere new.
    "include_virtual": False,
    # Check each network can reach the internet before using it.
    "connectivity_check": True,
    # Start the bridge when the person logs in, so paused FDM downloads can resume after a restart.
    "autostart": False,
    # Tests only: networks to use instead of the computer's, as
    # [{"id": "a", "name": "A", "addresses": ["127.0.0.2"]}].
    "test_networks": None,
}


def data_dir():
    override = os.environ.get("PLEXO_FDM_HOME")
    if override:
        path = override
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        path = os.path.join(base, "PlexoFDM")
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "PlexoFDM")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        path = os.path.join(base, "plexo-fdm")
    os.makedirs(path, exist_ok=True)
    return path


def data_file(name):
    return os.path.join(data_dir(), name)


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def write_json(path, value):
    """Written to a temporary file and renamed, so a crash never leaves half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


class Config:
    def __init__(self):
        self._lock = threading.Lock()
        self._path = data_file("config.json")
        stored = read_json(self._path, {})
        self._values = dict(DEFAULTS)
        if isinstance(stored, dict):
            self._values.update({k: v for k, v in stored.items() if k in DEFAULTS})

    def __getitem__(self, key):
        with self._lock:
            return self._values[key]

    def snapshot(self):
        with self._lock:
            return dict(self._values)

    def update(self, changes):
        """Applies the known, well-typed keys of `changes` and saves them. Returns what changed."""
        applied = {}
        with self._lock:
            for key, value in changes.items():
                if key not in DEFAULTS or key in ("test_networks", "port"):
                    continue
                default = DEFAULTS[key]
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, int):
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
                elif isinstance(default, list):
                    if not isinstance(value, list):
                        continue
                    value = [str(v) for v in value]
                self._values[key] = value
                applied[key] = value
            self._values["streams_per_network"] = max(1, min(32, self._values["streams_per_network"]))
            self._values["min_networks"] = max(1, min(8, self._values["min_networks"]))
            self._values["min_size_mb"] = max(0, self._values["min_size_mb"])
            self._values["memory_limit_mb"] = max(32, self._values["memory_limit_mb"])
            stored = {k: v for k, v in self._values.items() if v != DEFAULTS[k] or k in applied}
            write_json(self._path, stored)
        return applied


def daemon_info():
    """The running bridge's port and key, as the bridge last wrote them."""
    return read_json(data_file("daemon.json"), None)


def load_or_create_key():
    """The secret that every call to the bridge's API carries, so a web page can't drive it."""
    path = data_file("key")
    try:
        with open(path, "r", encoding="ascii") as f:
            key = f.read().strip()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = secrets.token_hex(24)
    with open(path, "w", encoding="ascii") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key
