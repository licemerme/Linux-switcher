"""focus.py - best-effort answer to "which process owns the keyboard now".

WHY
---
An exclusion entry ("do not touch this program at all, give it the raw
keyboard") needs to know what the user is looking at. On a Wayland session the
only window the daemon can identify without compositor cooperation is an
XWayland one - and that covers exactly the interesting case: games (Dota 2 is a
native X11 binary running under XWayland, Proton games too). KWin keeps
_NET_ACTIVE_WINDOW up to date for X11 clients, and such a window carries
_NET_WM_PID and WM_CLASS.

A native Wayland window is invisible to this probe. We then report ok=False
("unknown"), which the daemon treats as "no exclusion". That is why an entry
may also use the "running" mode: it does not care about focus at all, it just
asks whether a process with that name exists.

No X server at all (pure Wayland, no Xlib) degrades the same way: ok=False.

Everything here is read-only and cheap: one X property round-trip (~0.1 ms) or
a /proc name scan (~1-3 ms, cached for a second).
"""
from __future__ import annotations

import os
import re
import subprocess
import time

# A dead X connection is retried now and then (Xwayland can restart).
_RECONNECT_AFTER = 30.0
# How long a /proc scan result is reused (find_running runs in a poll loop).
_RUNNING_TTL = 1.0

# The xprop fallback spawns processes, so its answer is reused for a moment.
_XPROP_TTL = 1.0
_xprop_cache = (0.0, None)

_xconn = None
_xnext_try = 0.0
_running_cache = (0.0, None, None)   # (timestamp, key, hit)

# Never let a pattern match the daemon itself (the user will type "python").
_SELF = os.getpid()


# ---------------------------------------------------------------- X11 probe
def _xlib_display():
    global _xconn, _xnext_try
    if _xconn is not None:
        return _xconn
    now = time.time()
    if now < _xnext_try:
        return None
    try:
        from Xlib import display as xdisplay
    except Exception:
        # python-xlib missing: remember it for a long time, xprop takes over.
        _xnext_try = now + 300.0
        return None
    try:
        _xconn = xdisplay.Display()
    except Exception:
        _xconn = None
        _xnext_try = now + _RECONNECT_AFTER
    return _xconn


def _drop_xlib(again=True):
    """Forget the X connection. again=False reconnects on the next call.

    A window that vanishes between "_NET_ACTIVE_WINDOW" and the property read
    throws BadWindow, which python-xlib surfaces on the NEXT call: that used to
    make us blind for 30 seconds (the reconnect backoff), and during those 30
    seconds the daemon believed "no window matches" and grabbed the keyboard
    back in the middle of a game. A vanished window is not a broken server, so
    the connection is simply re-opened at once.
    """
    global _xconn, _xnext_try
    try:
        if _xconn is not None:
            _xconn.close()
    except Exception:
        pass
    _xconn = None
    _xnext_try = 0.0 if again is False else time.time() + _RECONNECT_AFTER


def _pid_ok(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if 0 < pid < 4194304 and os.path.isdir("/proc/" + str(pid)):
        return pid
    return None


def _active_via_xlib():
    d = _xlib_display()
    if d is None:
        return None
    try:
        from Xlib import X
        root = d.screen().root
        prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"),
                                      X.AnyPropertyType)
        if not prop or not prop.value:
            return {"ok": False, "source": "xlib", "reason": "no-window",
                    "why": "no _NET_ACTIVE_WINDOW"}
        wid = int(prop.value[0])
        if wid <= 0:
            return {"ok": False, "source": "xlib", "reason": "no-window",
                    "why": "no active window"}
        win = d.create_resource_object("window", wid)
        pid = None
        p = win.get_full_property(d.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
        if p and p.value:
            pid = _pid_ok(p.value[0])
        classes = []
        c = win.get_full_property(d.intern_atom("WM_CLASS"), X.AnyPropertyType)
        if c and c.value:
            raw = c.value
            if isinstance(raw, bytes):
                classes = [x.decode("utf-8", "replace")
                           for x in raw.split(b"\x00") if x]
            else:
                classes = [str(raw)]
        title = ""
        t = win.get_full_property(d.intern_atom("_NET_WM_NAME"),
                                  d.intern_atom("UTF8_STRING"))
        if t and t.value:
            title = t.value if isinstance(t.value, str) else \
                t.value.decode("utf-8", "replace")
        if pid is None and not classes:
            # The focused window is a native Wayland client: X knows its id at
            # best, never who owns it. reason="wayland" is a POSITIVE answer -
            # some other window has the focus - and callers may act on it.
            return {"ok": False, "source": "xlib", "reason": "wayland",
                    "wid": hex(wid),
                    "why": "active window is not an X11 client"}
        return {"ok": True, "source": "xlib", "wid": hex(wid), "pid": pid,
                "wm_class": classes, "title": title}
    except Exception as e:
        # reason="error": we cannot tell what is focused. That is NOT the same
        # as "another program is in front", so callers must not grab the
        # keyboard back on it.
        _drop_xlib(again=False)
        return {"ok": False, "source": "xlib", "reason": "error",
                "wid": locals().get("wid"),
                "why": type(e).__name__ + ": " + str(e)}


def _xprop(*args):
    try:
        out = subprocess.run(["xprop", *args], capture_output=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or b"").decode("utf-8", "replace")


def _active_via_xprop():
    # Fallback for systems without python-xlib. Two subprocesses, so it is
    # used only when the Xlib probe is unavailable - and then cached, to keep
    # the poll loop from spawning xprop several times a second.
    global _xprop_cache
    now = time.time()
    ts, cached = _xprop_cache
    if cached is not None and (now - ts) < _XPROP_TTL:
        return cached
    got = _active_via_xprop_uncached()
    _xprop_cache = (now, got)
    return got


def _active_via_xprop_uncached():
    txt = _xprop("-root", "_NET_ACTIVE_WINDOW")
    m = re.search(r"#\s*(0x[0-9a-fA-F]+)", txt)
    if not m:
        return {"ok": False, "source": "xprop", "reason": "no-window",
                "why": "no _NET_ACTIVE_WINDOW"}
    wid = int(m.group(1), 16)
    if wid <= 0:
        return {"ok": False, "source": "xprop", "reason": "no-window",
                "why": "no active window"}
    txt = _xprop("-id", hex(wid), "_NET_WM_PID", "WM_CLASS", "_NET_WM_NAME")
    pid = None
    m = re.search(r"_NET_WM_PID\(CARDINAL\)\s*=\s*(\d+)", txt)
    if m:
        pid = _pid_ok(m.group(1))
    classes = []
    m = re.search(r'WM_CLASS\(STRING\)\s*=\s*"([^"]*)"(?:,\s*"([^"]*)")?', txt)
    if m:
        classes = [x for x in (m.group(1), m.group(2)) if x]
    title = ""
    m = re.search(r'_NET_WM_NAME\(UTF8_STRING\)\s*=\s*"([^"]*)"', txt)
    if m:
        title = m.group(1)
    if pid is None and not classes:
        # xprop on a destroyed/stale id prints an error and yields nothing:
        # that is "cannot tell", not "a Wayland window is in front".
        if "no such window" in txt.lower() or "BadWindow" in txt:
            return {"ok": False, "source": "xprop", "reason": "error",
                    "wid": hex(wid), "why": "window vanished"}
        return {"ok": False, "source": "xprop", "reason": "wayland",
                "wid": hex(wid),
                "why": "active window is not an X11 client"}
    return {"ok": True, "source": "xprop", "wid": hex(wid), "pid": pid,
            "wm_class": classes, "title": title}


def active_window():
    """The focused window as a dict. ok=False means "cannot tell"."""
    if _xlib_display() is not None:
        got = _active_via_xlib()
    else:
        got = _active_via_xprop()
    if got is None:
        got = {"ok": False, "source": None, "reason": "error",
               "why": "no X11 connection"}
    return got


# ------------------------------------------------------------- /proc probing
def _name_of(pid):
    """Lower-case names a process answers to (comm, argv0, exe)."""
    names = set()
    try:
        with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as f:
            names.add(f.read().strip().lower())
    except OSError:
        pass
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read().split(b"\x00")
        if raw and raw[0]:
            arg0 = raw[0].decode("utf-8", "replace")
            names.add(arg0.lower())
            base = os.path.basename(arg0)
            if base:
                names.add(base.lower())
    except OSError:
        pass
    try:
        names.add(os.path.basename(os.readlink("/proc/%d/exe" % pid)).lower())
    except OSError:
        pass
    return {n for n in names if n}


def _ppid(pid):
    try:
        with open("/proc/%d/status" % pid, encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def proc_names(pid, depth=4):
    """Names of the process AND its ancestors.

    A game window is often created by a launcher/wrapper process, and the
    _NET_WM_PID may point at a helper. Walking up a few levels makes
    "dota2" match even when the window belongs to its child process.
    """
    names = set()
    cur = _pid_ok(pid)
    for _ in range(depth + 1):
        if not cur:
            break
        got = _name_of(cur)
        if not got:
            break
        names |= got
        cur = _pid_ok(_ppid(cur))
    return names


def match_names(patterns, names):
    """Return the first pattern that hits one of the names (substring)."""
    for pat in patterns:
        p = str(pat).strip().lower()
        if not p:
            continue
        for nm in names:
            if p in nm:
                return pat
    return None


def window_matches(patterns, win):
    """The matching exclusion pattern for that window, or None."""
    if not patterns or not win or not win.get("ok"):
        return None
    names = set()
    for c in win.get("wm_class") or []:
        if c:
            names.add(str(c).lower())
    pid = win.get("pid")
    if pid:
        names |= proc_names(pid)
    if win.get("title"):
        names.add(str(win["title"]).lower())
    return match_names(patterns, names)


def _running_names():
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == _SELF:
            continue
        yield _name_of(pid)


def find_running(patterns):
    """The first pattern whose process is alive right now, or None.

    Cached for _RUNNING_TTL: the daemon polls focus several times per second,
    and walking /proc every tick is pointless - a game does not vanish in the
    middle of a keystroke.
    """
    global _running_cache
    key = tuple(sorted(str(p).strip().lower() for p in patterns if str(p).strip()))
    if not key:
        return None
    ts, ckey, hit = _running_cache
    now = time.time()
    if ckey == key and (now - ts) < _RUNNING_TTL:
        return hit
    hit = None
    originals = list(patterns)
    for names in _running_names():
        hit = match_names(originals, names)
        if hit:
            break
    _running_cache = (now, key, hit)
    return hit


def normalize(patterns):
    """Clean a user list: strings only, stripped, de-duplicated (case-insens.)."""
    out, seen = [], set()
    for p in patterns or []:
        if not isinstance(p, str):
            continue
        s = p.strip()
        if not s or len(s) > 120:
            continue
        if any(bad in s for bad in ('"', "\n", "\r")):
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return out
