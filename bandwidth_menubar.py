#!/usr/bin/env python3
"""
bandwidth_menubar.py — Little Snitch's cheaper, uglier cousin.

Sits in your macOS menu bar and shows:
  - a live ↓/↑ total-traffic meter in the menu bar title itself, always
    visible, no clicking required (the "Bandwidth+" replacement part)
  - the top bandwidth-hogging apps in the dropdown, refreshed every few
    seconds, built on top of `nettop` (built into macOS)

No kernel extensions, no $45.

Setup (run in Terminal):
    pip3 install rumps
    python3 bandwidth_menubar.py

Click the menu bar meter to see the full top-5 list. Click "Quit" to exit.

Note: nettop only shows full detail for processes it can see without root.
For your own everyday apps (Chrome, Slack, Dropbox, etc.) that's normally
plenty. If entries look suspiciously sparse, try running the underlying
command with sudo in Terminal instead:
    sudo nettop -x -P -J bytes_in,bytes_out -l 1

The menu bar meter itself doesn't use nettop at all — it reads the raw
interface counters (`netstat -ib`) for whichever interface currently
holds the default route, which is the same technique standalone meter
apps use and needs no elevated privileges.
"""

import os
import subprocess
import re
import sys
import time
import rumps

APP_NAME = "NetSucker"
PREFS_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
LOGIN_ITEM_ASKED_FLAG = os.path.join(PREFS_DIR, "asked_login_item")

REFRESH_SECONDS = 3   # how often to poll
TOP_N = 5             # how many apps to show
MIN_KBPS = 0.5         # ignore anything below this — nobody cares about 3 bytes/sec


def sample_nettop():
    """
    Run one nettop snapshot and return {app_name: (bytes_in, bytes_out)}.
    nettop reports CUMULATIVE bytes since it started tracking each process,
    so the caller diffs two snapshots to get an actual rate.
    """
    try:
        # -L (capital) is what actually produces CSV output; -l (lowercase)
        # produces a human-readable "raw form" table with no commas at all,
        # so the header/column parsing below would never match anything.
        out = subprocess.check_output(
            ["nettop", "-x", "-P", "-J", "bytes_in,bytes_out", "-L", "1"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode("utf-8", errors="ignore")
    except Exception:
        return {}

    lines = out.strip().splitlines()
    if not lines:
        return {}

    header_idx = None
    header_cols = []
    for i, line in enumerate(lines):
        if line.startswith("time,"):
            header_idx = i
            header_cols = line.split(",")
            break
    if header_idx is None:
        return {}

    try:
        in_idx = header_cols.index("bytes_in")
        out_idx = header_cols.index("bytes_out")
        # nettop's CSV header is "time,,bytes_in,bytes_out," — the process
        # name column right after "time" has no header name of its own.
        name_idx = header_cols.index("time") + 1
    except ValueError:
        return {}

    data = {}
    for line in lines[header_idx + 1:]:
        parts = line.split(",")
        if len(parts) <= max(in_idx, out_idx, name_idx):
            continue
        # process field looks like "Google Chrome Helper.1234" -> strip the pid
        name = re.sub(r"\.\d+$", "", parts[name_idx]).strip()
        if not name:
            continue
        try:
            b_in = int(parts[in_idx])
            b_out = int(parts[out_idx])
        except ValueError:
            continue
        prev_in, prev_out = data.get(name, (0, 0))
        # merge helper processes (Chrome Helper, Chrome Helper (Renderer), ...) into one line
        data[name] = (prev_in + b_in, prev_out + b_out)
    return data


def default_interface():
    """Return the name of the interface currently holding the default route (e.g. "en0"), or None."""
    try:
        out = subprocess.check_output(
            ["route", "-n", "get", "default"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            return line.split(":", 1)[1].strip()
    return None


def interface_bytes(iface):
    """Return cumulative (bytes_in, bytes_out) for one interface from `netstat -ib`, or None."""
    if not iface:
        return None
    try:
        out = subprocess.check_output(
            ["netstat", "-ib"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
    except Exception:
        return None

    lines = out.strip().splitlines()
    if not lines:
        return None
    header_cols = lines[0].split()
    try:
        name_idx = header_cols.index("Name")
        in_idx = header_cols.index("Ibytes")
        out_idx = header_cols.index("Obytes")
    except ValueError:
        return None

    for line in lines[1:]:
        parts = line.split()
        if len(parts) <= max(name_idx, in_idx, out_idx):
            continue
        # netstat prints one row per interface/address combo; the first
        # row for a given interface (the link-layer one) already carries
        # the full cumulative counters, so grab it and stop.
        if parts[name_idx] != iface:
            continue
        try:
            return int(parts[in_idx]), int(parts[out_idx])
        except ValueError:
            return None
    return None


def hardware_port_name(iface):
    """Map a BSD interface name (e.g. "en0") to its macOS hardware port label (e.g. "Wi-Fi"), or None."""
    if not iface:
        return None
    try:
        out = subprocess.check_output(
            ["networksetup", "-listallhardwareports"], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
    except Exception:
        return None

    port = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and line.split(":", 1)[1].strip() == iface:
            return port
    return None


def nic_details(iface):
    """Return {ip, mac, media, status} for one interface from `ifconfig`, skipping fields it lacks."""
    if not iface:
        return {}
    try:
        out = subprocess.check_output(
            ["ifconfig", iface], stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
    except Exception:
        return {}

    info = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("ether "):
            info["mac"] = line.split()[1]
        elif line.startswith("inet "):
            info["ip"] = line.split()[1]
        elif line.startswith("media:"):
            info["media"] = line.split(":", 1)[1].strip()
        elif line.startswith("status:"):
            info["status"] = line.split(":", 1)[1].strip()
    return info


def _fmt_rate(kbps):
    if kbps >= 1024:
        return f"{kbps / 1024:.1f}MB/s"
    return f"{kbps:.0f}KB/s"


def _menu_line(text):
    """
    Plain strings passed to rumps become callback-less MenuItems, which
    macOS renders disabled — greyed out / lighter text. A no-op callback
    keeps the item inert but enabled, so it renders at full text strength
    (and still adapts correctly to light/dark mode, unlike a hardcoded color).
    """
    return rumps.MenuItem(text, callback=lambda _: None)


class BandwidthApp(rumps.App):
    def __init__(self):
        super().__init__("↓– ↑–", quit_button="Quit")
        self.prev = {}
        self.prev_time = time.time()
        self.prev_iface = None
        self.prev_iface_bytes = None
        self.prev_iface_time = time.time()
        self.menu = [_menu_line("Warming up…")]
        self.timer = rumps.Timer(self.refresh, REFRESH_SECONDS)
        self.timer.start()

        # NSAlert needs the Cocoa run loop already going to behave as a real
        # modal (otherwise it resolves instantly with no chance to click
        # anything), so fire it shortly after .run() has started instead of
        # before it.
        self.login_prompt_timer = rumps.Timer(self._show_login_prompt_once, 1)
        self.login_prompt_timer.start()

    def _show_login_prompt_once(self, _):
        self.login_prompt_timer.stop()
        maybe_prompt_for_login_item()

    def _meter_title(self, now):
        """↓/↑ total-traffic meter for the menu bar title, from interface counters."""
        iface = default_interface()
        sample = interface_bytes(iface)
        if sample is None:
            self.prev_iface = None
            self.prev_iface_bytes = None
            return "↓– ↑–"

        if self.prev_iface_bytes is None or iface != self.prev_iface:
            self.prev_iface = iface
            self.prev_iface_bytes = sample
            self.prev_iface_time = now
            return "↓– ↑–"

        p_in, p_out = self.prev_iface_bytes
        b_in, b_out = sample
        dt = max(now - self.prev_iface_time, 0.1)
        down_kbps = max(b_in - p_in, 0) / dt / 1024
        up_kbps = max(b_out - p_out, 0) / dt / 1024

        self.prev_iface_bytes = sample
        self.prev_iface_time = now

        return f"↓{_fmt_rate(down_kbps)} ↑{_fmt_rate(up_kbps)}"

    def _nic_menu_item(self):
        """A submenu item with the active interface's hardware name, IP, MAC and link status."""
        iface = self.prev_iface
        if not iface:
            return _menu_line("No active interface")

        hw = hardware_port_name(iface) or iface
        details = nic_details(iface)

        item = rumps.MenuItem(f"{hw} ({iface})")
        item.add(_menu_line(f"IP: {details.get('ip', '—')}"))
        item.add(_menu_line(f"MAC: {details.get('mac', '—')}"))
        link = details.get("media", "—")
        if details.get("status"):
            link += f"  ({details['status']})"
        item.add(_menu_line(f"Link: {link}"))
        return item

    def refresh(self, _):
        now = time.time()
        self.title = self._meter_title(now)
        current = sample_nettop()
        dt = max(now - self.prev_time, 0.1)

        rates = []
        for name, (b_in, b_out) in current.items():
            p_in, p_out = self.prev.get(name, (b_in, b_out))
            d_in = max(b_in - p_in, 0)
            d_out = max(b_out - p_out, 0)
            kbps = (d_in + d_out) / dt / 1024
            if kbps > MIN_KBPS:
                rates.append((name, kbps, d_in / dt / 1024, d_out / dt / 1024))

        rates.sort(key=lambda r: r[1], reverse=True)
        top = rates[:TOP_N]

        self.prev = current
        self.prev_time = now

        self.menu.clear()

        self.menu.add(self._nic_menu_item())
        self.menu.add(rumps.separator)

        if not top:
            self.menu.add(_menu_line("Nothing meaningfully phoning home right now"))
            return

        for name, kbps, down, up in top:
            self.menu.add(_menu_line(f"{name}: {kbps:.0f} KB/s  (↓{down:.0f} ↑{up:.0f})"))
        self.menu.add(rumps.separator)
        self.menu.add(_menu_line(f"refreshing every {REFRESH_SECONDS}s"))


def running_as_bundled_app():
    """True when running as the packaged .app (py2app sets sys.frozen), not `python3 bandwidth_menubar.py`."""
    return getattr(sys, "frozen", None) == "macosx_app"


def app_bundle_path():
    """Filesystem path to the running .app bundle, or None if not bundled."""
    try:
        from Foundation import NSBundle
        return NSBundle.mainBundle().bundlePath()
    except Exception:
        return None


def add_to_login_items(app_path):
    script = (
        'tell application "System Events" to make login item at end '
        f'with properties {{path:"{app_path}", hidden:false, name:"{APP_NAME}"}}'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, timeout=10)
        return True
    except Exception:
        return False


def maybe_prompt_for_login_item():
    """On the very first launch of the packaged app, ask once whether to start at login."""
    if os.path.exists(LOGIN_ITEM_ASKED_FLAG):
        return
    try:
        os.makedirs(PREFS_DIR, exist_ok=True)
        open(LOGIN_ITEM_ASKED_FLAG, "w").close()
    except Exception:
        pass

    if not running_as_bundled_app():
        return
    app_path = app_bundle_path()
    if not app_path:
        return

    response = rumps.alert(
        title=f"Launch {APP_NAME} at login?",
        message=f"{APP_NAME} can start automatically every time you log in.",
        ok="Add to Login Items",
        cancel="Not Now",
    )
    if response == 1:
        add_to_login_items(app_path)


if __name__ == "__main__":
    BandwidthApp().run()
