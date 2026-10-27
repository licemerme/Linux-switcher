"""procs.py - "what is running that a human might want to exclude", best effort.

WHY
---
The exclusions dialog cannot ask the compositor "which app is on screen": on
Wayland that information does not exist for us (see focus.py). So the dialog
offers a list of RUNNING APPLICATIONS built from signals we do have, and the
user can always type a name by hand - the list is a convenience, not the API.

WHY NOT JUST EVERYTHING
-----------------------
A raw /proc dump is 180 processes of one user and 95% of it is noise
(dbus-broker, akonadi agents, kwin, render helpers). Three signals pick the
applications out of it:

  * Steam: the game process inherits SteamAppId (570 for Dota 2). The title
    comes from steamapps/appmanifest_<id>.acf, the pattern from the deepest
    non-wrapper process of that tree ("dota2").
  * Desktop launch: Plasma starts an app in its own systemd scope
    (app-org.kde.dolphin@<uuid>.scope). Scopes ending in @autostart.service are
    autostart services, not apps, and are dropped. The scope root is the app,
    its children (btop inside konsole) are not.
  * X11 window: _NET_CLIENT_LIST gives WM_CLASS + _NET_WM_PID for every
    XWayland window, i.e. exactly the set for which the focus mode works.
    Games live here.

Everything is grouped by NAME (dota2 has five processes, brave has thirty) and
carries whether it owns an X11 window, because the dialog uses that to pick the
right exclusion mode: focus works only for X11/XWayland windows, running works
for everything.

The whole scan costs ~20 ms and is cached for a couple of seconds; it runs in
the tray process on the user's request (opening the dialog), never in the
daemon's event loop.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import time

# Reuse the clock the dialog has: a dropdown opened twice in a row must not
# re-walk /proc.
_TTL = 2.0
_cache = (0.0, None, None)          # (timestamp, all_user, result)

# Process names that are helpers of an application, never the application.
_WRAPPERS = frozenset({
    "bwrap", "srt-bwrap", "pv-adverb", "reaper", "srt-logger", "bash", "sh",
    "dash", "cat", "xdg-dbus-proxy", "glycin-image-rs", "steam", "kitten",
    "steamwebhelper", "gameoverlayui", "steam-runtime-l", "python", "python3",
    "env", "wine", "wine64", "wineserver", "pressure-vessel",
})

# Desktop infrastructure: present in the user's session, never an exclusion.
_INFRA = (
    "switcher", "hunspell", "kwin", "plasmashell", "ksmserver", "kded", "kaccess",
    "dbus-", "systemd", "pipewire", "wireplumber", "xdg-desktop-portal",
    "xdg-document-portal", "xdg-permission-store", "xdg-dbus-proxy", "at-spi",
    "kactivitymanagerd", "gmenudbusmenuproxy", "xembedsniproxy", "kwalletd",
    "ksecretd", "akonadi", "baloo", "baloorunner", "obexd", "xsettingsd",
    "imsettings", "ksystemstats", "kglobalacceld", "plasma-", "plasma_",
    "kdeconnectd", "kunifiedpush", "seapplet", "abrt-applet", "discovernotifi",
    "kalendarac", "xwaylandvideobridge", "mysqld", "agent", "srt-logger",
    "reaper", "steamwebhelper", "sd-pam", "catatonit", "startplasma",
    "ssh-agent", "p11-kit", "dconf", "hermes", "polkit", "flatpak-portal",
    "flatpak-session", "uresourced", "org_kde_powerde", "chrome_crashpad",
    "qtwebengineproc",
)

_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")


# ------------------------------------------------------------------ /proc bits
def _read(pid, name):
    try:
        with open("/proc/%d/%s" % (pid, name), "rb") as f:
            return f.read()
    except OSError:
        return b""


def _comm(pid):
    return _read(pid, "comm").decode("utf-8", "replace").strip()


def _cmdline(pid):
    return [x.decode("utf-8", "replace")
            for x in _read(pid, "cmdline").split(b"\x00") if x]


def _environ(pid):
    out = {}
    for item in _read(pid, "environ").split(b"\x00"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def _ppid(pid):
    for line in _read(pid, "status").decode("utf-8", "replace").splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    return 0


def _own(pid):
    # Sockets, ttys: cheap facts about what the process is attached to.
    out = []
    try:
        names = os.listdir("/proc/%d/fd" % pid)
    except OSError:
        return out
    for n in names[:128]:
        try:
            out.append(os.readlink("/proc/%d/fd/%s" % (pid, n)))
        except OSError:
            pass
    return out


def _uid(pid):
    try:
        return os.stat("/proc/%d" % pid).st_uid
    except OSError:
        return -1


def _scope(pid):
    # systemd app scope of that process ("" when it has none).
    text = _read(pid, "cgroup").decode("utf-8", "replace")
    for part in text.split("/"):
        if part.startswith("app-"):
            return part.strip()
    return ""


def _depth(pid, limit=40):
    depth, cur = 0, pid
    while cur > 1 and depth < limit:
        cur = _ppid(cur)
        depth += 1
    return depth


# ------------------------------------------------------------- app names
def _unescape(scope_id):
    # systemd escapes the unit name: brave-browser -> brave\x2dbrowser.
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), scope_id)


def _scope_app_id(scope):
    """"app-org.kde.dolphin@<uuid>.scope" -> "org.kde.dolphin".

    Plasma names them app-<id>@<uuid>.scope or app-<id>-<pid>.scope, and the
    id itself is escaped ("app-brave\x2dbrowser"), so both the escaping and
    the instance suffix have to go before any .desktop lookup.
    """
    text = _unescape(scope)
    if text.startswith("app-"):
        text = text[len("app-"):]
    text = text.split("@")[0]
    text = re.sub(r"(-\d+)?(\.scope|\.service)$", "", text)
    return text


def _desktop_names():
    """{app_id: Name} from .desktop files, plus {wm:<class>: Name}."""
    out = {}
    dirs = ["/usr/share/applications", "/usr/local/share/applications",
            "/var/lib/flatpak/exports/share/applications"]
    for env in ("XDG_DATA_HOME", "XDG_DATA_DIRS"):
        for base in (os.environ.get(env) or "").split(":"):
            if base:
                dirs.append(os.path.join(base, "applications"))
    dirs.append(os.path.expanduser("~/.local/share/applications"))
    dirs.append(os.path.expanduser(
        "~/.local/share/flatpak/exports/share/applications"))
    for d in dict.fromkeys(dirs):
        try:
            files = glob.glob(os.path.join(d, "*.desktop"))
        except OSError:
            continue
        for path in files:
            app_id = os.path.basename(path)[:-len(".desktop")]
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read(8192)
            except OSError:
                continue
            name = re.search(r"^Name=(.+)$", text, re.M)
            if not name:
                continue
            title = name.group(1).strip()
            out.setdefault(app_id, title)
            wm = re.search(r"^StartupWMClass=(.+)$", text, re.M)
            if wm:
                out.setdefault("wm:" + wm.group(1).strip().lower(), title)
    return out


def _steam_titles():
    """{app_id: "Dota 2"} from the installed Steam manifests."""
    out = {}
    patterns = ("~/.local/share/Steam/steamapps/appmanifest_*.acf",
                "~/.steam/steam/steamapps/appmanifest_*.acf",
                "~/.steam/root/steamapps/appmanifest_*.acf")
    for pat in patterns:
        for path in glob.glob(os.path.expanduser(pat)):
            m = re.search(r"appmanifest_(\d+)\.acf", path)
            if not m:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read(8192)
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if name:
                out.setdefault(m.group(1), name.group(1))
    return out


# ------------------------------------------------------------- X11 windows
def _x11_windows():
    """[{pid, wm_class, title}] for every X11/XWayland window, or []."""
    try:
        from Xlib import X
        from Xlib import display as xdisplay
    except Exception:
        return _x11_windows_xprop()
    try:
        d = xdisplay.Display()
        root = d.screen().root
        prop = root.get_full_property(d.intern_atom("_NET_CLIENT_LIST"),
                                      X.AnyPropertyType)
        if not prop or not prop.value:
            return []
        out = []
        utf8 = d.intern_atom("UTF8_STRING")
        for wid in list(prop.value)[:64]:
            try:
                w = d.create_resource_object("window", int(wid))
                pid_p = w.get_full_property(d.intern_atom("_NET_WM_PID"),
                                            X.AnyPropertyType)
                cls_p = w.get_full_property(d.intern_atom("WM_CLASS"),
                                            X.AnyPropertyType)
                name_p = w.get_full_property(d.intern_atom("_NET_WM_NAME"), utf8)
                classes = []
                if cls_p and cls_p.value:
                    classes = [c.decode("utf-8", "replace")
                               for c in cls_p.value.split(b"\x00") if c]
                title = ""
                if name_p and name_p.value:
                    title = (name_p.value if isinstance(name_p.value, str)
                             else name_p.value.decode("utf-8", "replace"))
                out.append({
                    "pid": int(pid_p.value[0]) if pid_p and pid_p.value else None,
                    "wm_class": classes, "title": title,
                })
            except Exception:
                continue
        return out
    except Exception:
        return _x11_windows_xprop()


def _xprop(*args):
    try:
        res = subprocess.run(["xprop", *args], capture_output=True, timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (res.stdout or b"").decode("utf-8", "replace")


def _x11_windows_xprop():
    # Fallback without python-xlib: 1 + N xprop calls, only on dialog open.
    ids = re.findall(r"0x[0-9a-fA-F]+", _xprop("-root", "_NET_CLIENT_LIST"))
    out = []
    for wid in ids[:64]:
        txt = _xprop("-id", wid, "_NET_WM_PID", "WM_CLASS", "_NET_WM_NAME")
        pid = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", txt)
        classes = []
        cls = re.search(r'WM_CLASS\(STRING\)\s*=\s*(.+)', txt)
        if cls:
            classes = [x for x in re.findall(r'"([^"]*)"', cls.group(1)) if x]
        title = ""
        name = re.search(r'_NET_WM_NAME\(UTF8_STRING\)\s*=\s*"(.*)"\s*$', txt, re.M)
        if name:
            title = name.group(1)
        out.append({"pid": int(pid.group(1)) if pid else None,
                    "wm_class": classes, "title": title})
    return out


# ------------------------------------------------------------- the scan
def _scan():
    """One pass over /proc: uid-filtered records with the signals we need."""
    me = os.getpid()
    rows = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return rows
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me or _uid(pid) != os.getuid():
            continue
        argv = _cmdline(pid)
        if any("switcher" in a for a in argv[:3]):
            continue        # our own daemon/tray: never an exclusion candidate
        env = _environ(pid)
        fd = _own(pid)
        rows[pid] = {
            "pid": pid,
            "comm": _comm(pid),
            "argv": argv,
            "scope": _scope(pid),
            "steam": env.get("SteamAppId") or env.get("SteamGameId"),
            "tty": any(x.startswith("/dev/pts/") for x in fd),
            "depth": _depth(pid),
            "screen": bool(env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")),
        }
    return rows


def _is_infra(name):
    # Both directions: the kernel truncates comm to 15 characters, so
    # "kactivitymanage" must still match the full "kactivitymanagerd".
    low = (name or "").lower()
    if not low:
        return False
    return any(low.startswith(p) or p.startswith(low) for p in _INFRA)


def _label(title, pattern):
    if title and title.lower() != pattern.lower():
        return title + " (" + pattern + ")"
    return pattern


def candidates(all_user=False):
    """Running programs worth offering as an exclusion.

    Each entry: {"pattern", "label", "source", "count", "x11"}.
    all_user=True drops the "is it an application" heuristics (an escape hatch
    for launchers that hide the real process), but still removes our own
    processes, desktop infrastructure and terminal children.
    """
    global _cache
    now = time.time()
    ts, key, cached = _cache
    if key == all_user and (now - ts) < _TTL:
        return cached

    rows = _scan()
    if not rows:
        _cache = (now, all_user, [])
        return []

    steam_titles = _steam_titles()
    desk = _desktop_names()
    windows = _x11_windows()

    # pid -> window, and the set of pids that own a window
    by_pid = {}
    for w in windows:
        if w.get("pid"):
            by_pid.setdefault(w["pid"], w)
    x11_pids = set(by_pid)

    groups = {}          # pattern(lower) -> entry
    claimed = set()      # pids already explained by an app/steam/x11 group

    def add(pattern, title, source, pid, count=1, x11=False):
        if not pattern:
            return
        key_low = pattern.lower()
        ent = groups.get(key_low)
        if ent is None:
            groups[key_low] = {
                "pattern": pattern, "title": title, "source": source,
                "count": count, "x11": x11, "pid": pid,
            }
            return
        ent["count"] += count
        ent["x11"] = ent["x11"] or x11
        if not ent["title"] and title:
            ent["title"] = title
        if source == "steam" and ent["source"] != "steam":
            ent["source"] = "steam"
            if title:
                ent["title"] = title

    # (A) Steam games: one entry per app id, named after the deepest real binary
    steam = {}
    for r in rows.values():
        if r["steam"]:
            steam.setdefault(r["steam"], []).append(r)
    for app_id, members in steam.items():
        real = [r for r in members if r["comm"].lower() not in _WRAPPERS]
        pick = max(real or members, key=lambda r: r["depth"])
        add(pick["comm"], steam_titles.get(app_id, ""), "steam", pick["pid"],
            count=len(members),
            x11=any(r["pid"] in x11_pids for r in members))
        claimed.update(r["pid"] for r in members)

    # (B) Desktop-launched applications: their own systemd scope, root process
    scopes = {}
    for r in rows.values():
        sc = r["scope"]
        if not sc or "@autostart" in sc:
            continue
        scopes.setdefault(sc, []).append(r)
    for sc, members in scopes.items():
        app_id = _scope_app_id(sc)
        plain = app_id[len("flatpak-"):] if app_id.startswith("flatpak-") else app_id
        # Steam has its own branch above (per game, with the real title), and
        # its scope is a launcher that owns nearly every game process: showing
        # it here would be the "steam" trap.
        if app_id.startswith("steam") or "steam" == plain:
            continue
        # The application is the most frequent non-wrapper binary of the scope
        # (brave appears 30 times, its shell wrapper once), and among equals
        # the shallowest one (konsole, not the btop running inside it).
        real = [r for r in members if r["comm"].lower() not in _WRAPPERS]
        if real:
            counts = {}
            for r in real:
                counts[r["comm"]] = counts.get(r["comm"], 0) + 1
            best = max(counts.values())
            pick = min((r for r in real if counts[r["comm"]] == best),
                       key=lambda r: (r["depth"], r["pid"]))
            name = pick["comm"]
        else:
            pick = min(members, key=lambda r: (r["depth"], r["pid"]))
            name = plain.rsplit(".", 1)[-1] or pick["comm"]
        title = desk.get(plain) or desk.get(app_id) or desk.get("wm:" + name.lower()) or ""
        source = "flatpak" if app_id.startswith("flatpak-") else "app"
        add(name, title, source, pick["pid"], count=len(members),
            x11=any(r["pid"] in x11_pids for r in members))
        claimed.update(r["pid"] for r in members)

    # (C) X11/XWayland windows: exactly what the focus mode can see
    for pid, w in by_pid.items():
        r = rows.get(pid)
        classes = [c for c in (w.get("wm_class") or []) if c]
        pattern = (classes[-1] if classes else (r["comm"] if r else "")).lower()
        if not pattern:
            continue
        title = w.get("title") or desk.get("wm:" + pattern) or ""
        add(pattern, title, "x11", pid, x11=True)

    # (D) escape hatch: any user session process that survived the filters
    if all_user:
        for r in rows.values():
            name = r["comm"]
            if not name or r["tty"] or name.lower() in _WRAPPERS:
                continue
            if r["pid"] in claimed:
                continue    # already part of an application above
            add(name, desk.get("wm:" + name.lower()) or "", "any", r["pid"])

    out = []
    for ent in groups.values():
        name = ent["pattern"]
        if _is_infra(name) or _is_infra(ent.get("title") or ""):
            continue
        if not all_user and not (ent["source"] in ("steam", "app", "flatpak")
                                or ent["x11"]):
            continue
        ent["label"] = _label(ent["title"], ent["pattern"])
        out.append(ent)
    out.sort(key=lambda e: (e["source"] != "steam", (e["label"] or "").lower()))
    result = out
    _cache = (now, all_user, result)
    return result
