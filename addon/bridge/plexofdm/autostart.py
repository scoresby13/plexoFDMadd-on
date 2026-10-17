"""Starting the bridge at login, so FDM can resume a download after the computer restarts.

FDM only runs the add-on when a link is added. A download it resumes later goes straight to the
bridge's address, which needs the bridge running. Off by default; switched on from the status page.
"""

import os
import subprocess
import sys

NAME = "PlexoFDMBridge"
LABEL = "org.plexo-fdm.bridge"


def command():
    script = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plexo_fdm.py"))
    exe = sys.executable
    if sys.platform == "win32":
        windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(windowless):
            exe = windowless
    return [exe, script, "serve"]


def _mac_plist():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LABEL + ".plist")


def _linux_desktop():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "plexo-fdm-bridge.desktop")


def _xml(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _desktop_quote(arg):
    # The Desktop Entry spec: quote with double quotes, escaping " ` $ and \.
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in arg)
    return '"%s"' % escaped


def set_enabled(enabled):
    cmd = command()
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        path = _mac_plist()
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            args = "".join("<string>%s</string>" % _xml(a) for a in cmd)
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>'
                    "<key>Label</key><string>%s</string>"
                    "<key>ProgramArguments</key><array>%s</array>"
                    "<key>RunAtLoad</key><true/>"
                    "</dict></plist>\n" % (LABEL, args)
                )
        elif os.path.exists(path):
            os.remove(path)
    else:
        path = _linux_desktop()
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    "[Desktop Entry]\nType=Application\nName=Plexo bridge for FDM\n"
                    "Comment=Lets Free Download Manager use every network at once\n"
                    "Exec=%s\nTerminal=false\nX-GNOME-Autostart-enabled=true\n"
                    % " ".join(_desktop_quote(a) for a in cmd)
                )
        elif os.path.exists(path):
            os.remove(path)
