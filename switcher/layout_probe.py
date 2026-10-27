"""layout_probe.py — read the LIVE system keyboard layout at startup.

`initial_layout()` in main.py previously only read kxkbrc LayoutList[0] = the
layout KDE STARTS with. If the user left the session on another layout (or
toggled it manually), cap.layout drifts and every word is misinterpreted until
the first manual Ctrl+Shift. This module fixes the start-of-run desync.

Sources, in order (first that yields a valid layout wins):
  1) DBus org.kde.KeyboardLayouts (KWin 6, object /Layouts):
       getLayout      -> uint index of the ACTIVE group (authoritative, live)
       getLayoutsList -> a(sss) list of (short id, name, full) in group order
     This is what actually drives the on-screen layout on Wayland.
  2) `setxkbmap -query`  ->  "layout: <id>"  (XWayland; usually in sync)
  3) kxkbrc LayoutList[0]   (fallback = previous behaviour)
  4) "us".

Read helpers never write state. The write helpers (`layout_index_map`,
`dbus_set_layout`) let the caller switch the LIVE layout synchronously via DBus
instead of emitting a uinput Ctrl+Shift and waiting ~450ms for KDE to apply it
asynchronously — that is the fast-toggle path used by main.py.
"""
from __future__ import annotations
import os, re, subprocess, configparser


def kxkbrc_order() -> list[str]:
    """LayoutList from ~/.config/kxkbrc, lowercased, in order (index == XKB group)."""
    p = os.path.expanduser("~/.config/kxkbrc")
    try:
        cp = configparser.ConfigParser()
        cp.read(p)
        lst = [x.strip().lower() for x in cp.get("Layout", "LayoutList",
                                                  fallback="").split(",") if x.strip()]
        return lst
    except Exception:
        return []


def _busctl(*args, timeout=5):
    """Run `busctl --user call ...`; return (rc, stdout, stderr) or None on hard error."""
    try:
        r = subprocess.run(["busctl", "--user", "call", *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _dbus_kde() -> tuple[str, str] | None:
    """Source 1: live group index + ordered id list from KWin 6.
    Returns (layout_id, source) or None if DBus/parse fails.

    busctl raw format (confirmed on Fedora 44 KDE 6):
      getLayout       ->  u 1
      getLayoutsList  ->  a(sss) 2 "ru" "" "Русская" "us" "" "English (US)"
    Each struct in a(sss) has 3 quoted fields: id, short, full.
    We take every 3rd quoted string (the id).
    """
    dest, path, iface = "org.kde.keyboard", "/Layouts", "org.kde.KeyboardLayouts"

    # 1) ordered list of layout ids
    res = _busctl(dest, path, iface, "getLayoutsList")
    if res is None:
        return None
    rc, out, _ = res
    if rc != 0:
        return None
    # Extract all quoted strings from the line (handles escaped bytes too).
    # Format: a(sss) N "id1" "" "full1" "id2" "" "full2" ...
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', out)
    # Each struct = 3 fields; layout id is field 0 of each struct.
    ids = [quoted[i].lower() for i in range(0, len(quoted), 3)]
    if not ids:
        return None

    # 2) active group index: busctl prints  "u 1"
    res = _busctl(dest, path, iface, "getLayout")
    idx = 0
    if res is not None:
        rc, out, _ = res
        if rc == 0:
            m = re.search(r'\bu\s+(\d+)', out)
            if m:
                try:
                    idx = int(m.group(1))
                except ValueError:
                    idx = 0
    if not (0 <= idx < len(ids)):
        return None
    return ids[idx], "dbus(kde)"


def _setxkbmap_layout() -> str | None:
    """Source 2: parse `setxkbmap -query` for 'layout: <id>'."""
    try:
        r = subprocess.run(["setxkbmap", "-query"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"^layout:\s*(\S+)", r.stdout, re.MULTILINE)
    if m:
        first = m.group(1).strip().lower()
        if first:
            return first
    return None


# --- write / set helpers (fast toggle path) -------------------------------
def layout_index_map() -> dict[str, int]:
    """Map layout id (lowercase) -> XKB group index, from getLayoutsList.
    Callers cache it (the list doesn't change within a session). Returns {} if
    DBus/busctl is unavailable (non-KDE) -> caller falls back to uinput toggle."""
    dest, path, iface = "org.kde.keyboard", "/Layouts", "org.kde.KeyboardLayouts"
    res = _busctl(dest, path, iface, "getLayoutsList")
    if res is None:
        return {}
    rc, out, _ = res
    if rc != 0:
        return {}
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', out)
    ids = [quoted[i].lower() for i in range(0, len(quoted), 3)]
    return {ids[i]: i for i in range(len(ids))}


def dbus_set_layout(index: int) -> bool | None:
    """Synchronously set the active layout group by index (DBus, KDE 6).
    Returns True on success, False on failure, None if busctl is unavailable.
    The method is SYNC: by the time it returns, KDE has applied the change,
    so the ~450ms SETTLE wait of the uinput path is no longer needed."""
    dest, path, iface = "org.kde.keyboard", "/Layouts", "org.kde.KeyboardLayouts"
    res = _busctl(dest, path, iface, "setLayout", "u", str(index))
    if res is None:
        return None
    rc, out, _ = res
    if rc != 0:
        return False
    return "true" in out.lower()


def current_layout() -> tuple[str, str]:
    """Detect the live layout. Returns (layout, source) where source is one of
    'dbus(kde)', 'setxkbmap', 'kxkbrc', 'default' so callers can log origin."""
    db = _dbus_kde()
    if db:
        return db
    sx = _setxkbmap_layout()
    if sx:
        return sx, "setxkbmap"
    order = kxkbrc_order()
    if order:
        return order[0], "kxkbrc"
    return "us", "default"


if __name__ == "__main__":
    print(f"--- each source, individually ---")
    print("dbus(kde):   ", _dbus_kde())
    print("setxkbmap:   ", _setxkbmap_layout())
    print("kxkbrc order:", kxkbrc_order())
    print("index map:   ", layout_index_map())
    lay, src = current_layout()
    print(f"\n=== chosen: layout={lay!r} source={src!r} ===")
