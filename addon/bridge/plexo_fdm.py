"""Plexo bridge for Free Download Manager: the command FDM's add-on runs.

  register --url URL [...]  Starts the bridge if needed and asks it to take a download. Prints
                            one line of JSON: {"ok": true, "url": <bridge link>, ...} or
                            {"ok": false, "reason": ..., "message": ...}.
  serve                     Runs the bridge in this process.
  start | stop | status     Starts it in the background, stops it, or prints what it's doing.
  networks                  Lists the networks found and whether each reaches the internet.
  ui                        Opens the status page in a browser.
"""

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plexofdm import VERSION  # noqa: E402
from plexofdm.config import Config, daemon_info, data_dir, data_file, load_or_create_key  # noqa: E402

# Never through a proxy: the bridge is on this computer.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _port(config=None):
    info = daemon_info()
    if info and info.get("port"):
        return int(info["port"])
    return int((config or Config())["port"])


def api(path, body=None, port=None, timeout=60):
    port = port or _port()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method="POST" if data else "GET")
    req.add_header("X-Plexo-Key", load_or_create_key())
    req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            answer = json.loads(error.read().decode("utf-8"))
        except ValueError:
            answer = {}
        raise RuntimeError(answer.get("message") or str(error))


def diag(message):
    """A line in addon.log in the data folder: every time FDM runs this script, what it was
    asked and how it went, so a link FDM downloaded without the bridge can be explained."""
    try:
        path = data_file("addon.log")
        if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def redact(url):
    """The link without its query: those often carry tokens."""
    return str(url).split("?", 1)[0]


def _tail(path, lines=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return ""


def ping(port, timeout=1.5):
    try:
        return api("/api/ping", port=port, timeout=timeout)
    except (OSError, ValueError, RuntimeError):
        return None


def spawn():
    """Starts `serve` in the background, outliving the process FDM launched."""
    script = os.path.abspath(__file__)
    exe = sys.executable
    # Whatever the bridge prints before its own log is set up (an import error, a crash) lands here.
    errors = open(data_file("bridge-stderr.log"), "ab")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": errors,
              "cwd": data_dir(), "close_fds": True}
    diag("starting the bridge: %s %s serve" % (exe, script))
    if sys.platform == "win32":
        windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(windowless):
            exe = windowless
        DETACHED_PROCESS, NEW_GROUP, NO_WINDOW, BREAKAWAY = 0x8, 0x200, 0x08000000, 0x01000000
        flags = DETACHED_PROCESS | NEW_GROUP | NO_WINDOW
        try:
            # Out of FDM's job object, if it has one, so FDM ending the script doesn't end this.
            return subprocess.Popen([exe, script, "serve"], creationflags=flags | BREAKAWAY, **kwargs)
        except OSError:
            return subprocess.Popen([exe, script, "serve"], creationflags=flags, **kwargs)
    return subprocess.Popen([exe, script, "serve"], start_new_session=True, **kwargs)


def ensure_running(config):
    port = _port(config)
    alive = ping(port)
    if alive and alive.get("version") == VERSION:
        return port
    if alive:
        # An older bridge from before the add-on updated.
        try:
            api("/api/shutdown", {}, port=port, timeout=5)
        except (OSError, ValueError, RuntimeError):
            pass
        for _ in range(30):
            if not ping(port, 0.5):
                break
            time.sleep(0.1)
    port = int(config["port"])
    proc = spawn()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ping(port, 0.5):
            diag("bridge is up on port %d (pid %s)" % (port, proc.pid))
            return port
        if proc.poll() is not None:
            break
        time.sleep(0.15)
    details = _tail(data_file("bridge-stderr.log")) or _tail(data_file("bridge.log"))
    code = proc.poll()
    raise RuntimeError("The bridge didn't start (%s)%s" % (
        "exit code %s" % code if code is not None else "no answer on port %d after 15 s" % port,
        ": " + details if details else ""))


def out(value):
    if "reason" in value or "url" in value:
        diag("-> %s" % (value.get("message") or value.get("url")))
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def cmd_register(args):
    config = Config()
    url = args.url
    force = args.force or url.lower().endswith("#plexo")
    cookies = []
    if args.cookies_file:
        try:
            with open(args.cookies_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except (OSError, ValueError, RuntimeError):
            cookies = []
        try:
            os.remove(args.cookies_file)  # It holds the person's cookies: don't leave it around.
        except OSError:
            pass
    # Answer the common cases without starting anything.
    if not force and not config["auto"]:
        return out({"ok": False, "reason": "disabled", "message": "Automatic mode is off"})
    if not force and args.size is not None and 0 <= args.size < config["min_size_mb"] * 1024 * 1024:
        return out({"ok": False, "reason": "small", "message": "Too small to split"})
    headers = {}
    if args.user_agent:
        headers["User-Agent"] = args.user_agent
    if args.referer:
        headers["Referer"] = args.referer
    port = ensure_running(config)
    result = api("/api/register", {
        "url": url,
        "force": force,
        "size": args.size if args.size is not None else -1,
        "headers": headers,
        "cookies": cookies,
    }, port=port, timeout=90)
    out(result)


def cmd_serve(args):
    from plexofdm import server

    handler = logging.handlers.RotatingFileHandler(data_file("bridge.log"), maxBytes=1024 * 1024, backupCount=2,
                                                   encoding="utf-8")
    handlers = [handler]
    if args.verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config()
    port = args.port or int(config["port"])
    if ping(port):
        logging.info("A bridge is already running on port %d", port)
        return
    try:
        server.run(config, load_or_create_key(), port)
    except OSError as error:
        logging.error("Couldn't listen on port %d: %s", port, error)
        sys.exit(1)


def cmd_start(args):
    port = ensure_running(Config())
    out({"ok": True, "ui": "http://127.0.0.1:%d/" % port})


def cmd_stop(args):
    try:
        out(api("/api/shutdown", {}, timeout=5))
    except (OSError, ValueError, RuntimeError) as error:
        out({"ok": False, "message": "The bridge isn't running (%s)" % error})


def cmd_status(args):
    try:
        print(json.dumps(api("/api/state", timeout=5), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        out({"ok": False, "message": "The bridge isn't running (%s)" % error})


def cmd_networks(args):
    from plexofdm.netif import check_internet, discover

    config = Config()
    for net in discover(config["test_networks"]):
        latency = check_internet(net)
        state = "online, %d ms" % latency if latency is not None else "no internet"
        flag = " (virtual, off by default)" if net.virtual else ""
        addresses = ", ".join(a for a, _ in net.addresses)
        print("%-12s %-22s %-9s %s  [%s]%s" % (net.id, net.name[:22], net.kind, addresses, state, flag))


def cmd_ui(args):
    import webbrowser

    port = ensure_running(Config())
    webbrowser.open("http://127.0.0.1:%d/" % port)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="plexo_fdm", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register")
    p.add_argument("--url", required=True)
    p.add_argument("--force", action="store_true")
    p.add_argument("--size", type=int)
    p.add_argument("--cookies-file")
    p.add_argument("--user-agent")
    p.add_argument("--referer")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_serve)

    for name, func in (("start", cmd_start), ("stop", cmd_stop), ("status", cmd_status),
                       ("networks", cmd_networks), ("ui", cmd_ui)):
        sub.add_parser(name).set_defaults(func=func)

    args = parser.parse_args(argv)
    if args.command != "serve":
        diag("%s %s | python %s %s | script %s" % (
            args.command, redact(getattr(args, "url", "") or ""), sys.version.split()[0], sys.executable,
            os.path.abspath(__file__)))
    try:
        args.func(args)
    except Exception as error:
        import traceback

        diag("failed: %s\n%s" % (error, traceback.format_exc()))
        if args.command == "register":
            out({"ok": False, "reason": "error", "message": str(error)})
        else:
            raise


if __name__ == "__main__":
    main()
