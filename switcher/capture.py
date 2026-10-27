"""capture.py — read the physical keyboard via evdev and emit characters.

We observe raw KEY_* codes (US-position based). Combined with the CURRENT
layout we know the visible char the user sees on screen.

Multi-device: each keyboard node gets its own reader thread, shared word buffer.
A physical key press arrives on exactly one device, so no double-counting.

Combo detection (exact match): a table of {frozenset(codes): callback}.
Used for Ctrl+Shift (layout toggle) and Ctrl+Shift+Backspace (rollback).

Run directly to dump what you type:  .venv/bin/python -m switcher.capture
"""
from __future__ import annotations
import os, sys, time, threading, glob
from evdev import InputDevice, ecodes
from .core import US2RU, RU2US, IDENT

# US-position char for each printable KEY code.
KEY2US = {
    ecodes.KEY_A: "a", ecodes.KEY_B: "b", ecodes.KEY_C: "c", ecodes.KEY_D: "d",
    ecodes.KEY_E: "e", ecodes.KEY_F: "f", ecodes.KEY_G: "g", ecodes.KEY_H: "h",
    ecodes.KEY_I: "i", ecodes.KEY_J: "j", ecodes.KEY_K: "k", ecodes.KEY_L: "l",
    ecodes.KEY_M: "m", ecodes.KEY_N: "n", ecodes.KEY_O: "o", ecodes.KEY_P: "p",
    ecodes.KEY_Q: "q", ecodes.KEY_R: "r", ecodes.KEY_S: "s", ecodes.KEY_T: "t",
    ecodes.KEY_U: "u", ecodes.KEY_V: "v", ecodes.KEY_W: "w", ecodes.KEY_X: "x",
    ecodes.KEY_Y: "y", ecodes.KEY_Z: "z",
    ecodes.KEY_SEMICOLON: ";", ecodes.KEY_APOSTROPHE: "'",
    ecodes.KEY_LEFTBRACE: "[", ecodes.KEY_RIGHTBRACE: "]",
    ecodes.KEY_GRAVE: "`",
    ecodes.KEY_COMMA: ",", ecodes.KEY_DOT: ".",
    ecodes.KEY_SLASH: "/", ecodes.KEY_MINUS: "-",
    ecodes.KEY_0: "0", ecodes.KEY_1: "1", ecodes.KEY_2: "2",
    ecodes.KEY_3: "3", ecodes.KEY_4: "4", ecodes.KEY_5: "5",
    ecodes.KEY_6: "6", ecodes.KEY_7: "7", ecodes.KEY_8: "8",
    ecodes.KEY_9: "9",
}

# Device name blacklist (case-insensitive substring). Gravastar M2 mouse has a
# "keyboard" event node (thumb buttons) but does not type text — exclude it.
DEVICE_BLACKLIST = ("gravastar", "switcher")

# Auto-repeat suppression window (ms).
REPEAT_SUPPRESS_MS = 200


def keyboard_paths():
    """Pick event devices that look like a real keyboard (not blacklisted)."""
    out = []
    for dev in sorted(glob.glob("/dev/input/event*")):
        try:
            d = InputDevice(dev)
            name = d.name.lower()
            if any(bl in name for bl in DEVICE_BLACKLIST):
                del d
                continue
            caps = d.capabilities()  # {ev_type: [codes]}
            keycodes = set(caps.get(ecodes.EV_KEY, []))
            del d
            if ecodes.KEY_A in keycodes and ecodes.KEY_SPACE in keycodes:
                out.append(dev)
        except Exception:
            continue
    return out


def us_char_for(code):
    return KEY2US.get(code)


class KeyboardCapture:
    """Multi-device keyboard capture with a shared word buffer."""

    def __init__(self, dev_paths, on_word, init_layout="us"):
        self.dev_paths = list(dev_paths)
        self.on_word = on_word              # callback(word_str, layout, term_code, post_flush_buffer)
        self.devices = [InputDevice(p) for p in self.dev_paths]
        self.buffer = []
        self.layout = init_layout           # "us" or "ru"
        self._stop = threading.Event()
        self._threads = []
        self._last_press = {}
        self._held = set()
        self._combo_table = {}             # frozenset(codes) -> callback
        # Monotonic count of printable chars typed since capture start.
        # Never reset. Each flushed word snapshots it as its `baseline`;
        # the worker aborts if self._total_chars > baseline (user typed a new
        # char after that word's terminator, possibly across later flushes).
        self._total_chars = 0
        self.baseline = 0
        self.correction_active = False
        self.toggle_suppression_until = 0.0  # was undefined -> AttributeError on any combo key
        self.post_flush_buffer = []

    def set_layout(self, lay):
        self.layout = lay

    def add_combo(self, codes, callback):
        self._combo_table[frozenset(codes)] = callback

    # -- main loop ---------------------------------------------------------
    def _run(self, device):
        for event in device.read_loop():
            if self._stop.is_set():
                break
            if event.type == ecodes.EV_KEY:
                ts_f = event.timestamp()  # float seconds since epoch
                self._handle(event.code, event.value, ts_f)

    def _handle(self, code, value, ts_f):
        # ts_f = event.timestamp() — уже float секунд с epoch.
        ts_f = float(ts_f)
        # Combo tracking (exact-match dispatch).
        # Fire only on the FIRST press of each key (guard against auto-repeat
        # re-firing), and only when the full held-set exactly equals a combo.
        if code in self._combo_members():
            if value == 1:
                if code not in self._held:
                    self._held.add(code)
                    combo = frozenset(self._held)
                    cb = self._combo_table.get(combo)
                    if cb is not None:
                        if time.time() < self.toggle_suppression_until:
                            return
                        cb()
            else:
                self._held.discard(code)
        # Boundary keys: flush word (pass which boundary key ended it)
        if code in (ecodes.KEY_SPACE, ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.KEY_TAB):
            if value == 1 and not self.correction_active:
                self._flush(code)
            return
        # Backspace: pop last char
        if code == ecodes.KEY_BACKSPACE:
            if value == 1 and self.buffer:
                self.buffer.pop()
            return
        # Printable char
        us = us_char_for(code)
        if us is None:
            return
        if value == 1:
            prev = self._last_press.get(code)
            if prev is not None and (ts_f - prev) * 1000 < REPEAT_SUPPRESS_MS:
                return
            self._last_press[code] = ts_f
            if self.layout == "ru" and us in US2RU:
                ch = US2RU[us]
            else:
                ch = us
            self.buffer.append(ch)
            self._total_chars += 1

    def chars_since(self, baseline: int) -> int:
        """Printable chars typed since the given snapshot. If > 0, the user
        typed a new char after that word's terminator -> a correction for that
        word would clobber later text."""
        return max(0, self._total_chars - baseline)

    def _combo_members(self):
        # cache-free: union of all combo key-codes
        s = set()
        for k in self._combo_table:
            s |= set(k)
        return s

    def _flush(self, term_code):
        if self.buffer:
            w = "".join(self.buffer)
            self.buffer.clear()
            self.baseline = self._total_chars
            self.post_flush_buffer = []
            # Snapshot the monotonic counter NOW (= chars up to & incl. this
            # word). Any char typed after this terminator grows it -> abort.
            self.on_word(w, self.layout, term_code, self.baseline, self.post_flush_buffer)

    def start(self):
        for dev in self.devices:
            t = threading.Thread(target=self._run, args=(dev,), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)


if __name__ == "__main__":
    def show(w, lay, term=None, baseline=None):
        print(f"[{lay}] {w!r} +{term}")
    devs = keyboard_paths()
    print("keyboards:", devs)
    if not devs:
        print("none found (check input group + device blacklist)")
        sys.exit(1)
    cap = KeyboardCapture(devs, show, init_layout="us")
    cap.add_combo([ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT], lambda: print("TOGGLE"))
    cap.add_combo([ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_BACKSPACE],
                  lambda: print("ROLLBACK"))
    cap.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cap.stop()
