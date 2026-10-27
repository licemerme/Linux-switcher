"""injector.py — send corrected keys back via a virtual keyboard (uinput).

No root needed on this box: /dev/uinput is user-writable. We create a virtual
"switcher-keyboard" device, then can: delete N chars, type a string, and fire
the layout-toggle key combo (Ctrl+Shift — the same combo KDE binds to
"Switch to Next Keyboard Layout").

IMPORTANT: every KEY code we may emit must be in the UInput capability list,
otherwise the kernel silently drops the event. KEY_LEFTCTRL was missing before
and that's why toggle_layout() was a no-op.

Self-test:  .venv/bin/python -m switcher.injector
"""
from __future__ import annotations
import time
from evdev import UInput, ecodes
from .core import US2RU

# KEY code -> us char (US-position).
US_BY_CODE = {
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
_CODE_BY_US = {v: k for k, v in US_BY_CODE.items()}

# char -> KEY code, with optional shift (for the US layout).
# NOTE: US_BY_CODE is code->char, so dict(US_BY_CODE) keeps code->char and
# lookups by LETTER return None (that silently dropped every US char). Use the
# inverted _CODE_BY_US (char->code) instead.
US_CHAR2CODE = dict(_CODE_BY_US)          # lowercase / digits / base symbols
US_SHIFTED = {
    "Q": ecodes.KEY_Q, "W": ecodes.KEY_W, "E": ecodes.KEY_E, "R": ecodes.KEY_R,
    "T": ecodes.KEY_T, "Y": ecodes.KEY_Y, "U": ecodes.KEY_U, "I": ecodes.KEY_I,
    "O": ecodes.KEY_O, "P": ecodes.KEY_P,
    "A": ecodes.KEY_A, "S": ecodes.KEY_S, "D": ecodes.KEY_D, "F": ecodes.KEY_F,
    "G": ecodes.KEY_G, "H": ecodes.KEY_H, "J": ecodes.KEY_J, "K": ecodes.KEY_K,
    "L": ecodes.KEY_L, "Z": ecodes.KEY_Z, "X": ecodes.KEY_X, "C": ecodes.KEY_C,
    "V": ecodes.KEY_V, "B": ecodes.KEY_B, "N": ecodes.KEY_N, "M": ecodes.KEY_M,
    "_": ecodes.KEY_MINUS, "!": ecodes.KEY_1, "@": ecodes.KEY_2,
    "#": ecodes.KEY_3, "$": ecodes.KEY_4, "%": ecodes.KEY_5,
    "^": ecodes.KEY_6, "&": ecodes.KEY_7, "*": ecodes.KEY_8,
    "(": ecodes.KEY_9, ")": ecodes.KEY_0,
    "}": ecodes.KEY_RIGHTBRACE, ":": ecodes.KEY_SEMICOLON,
    '"': ecodes.KEY_APOSTROPHE, "?": ecodes.KEY_SLASH,
    "|": ecodes.KEY_BACKSLASH, "~": ecodes.KEY_GRAVE,
    "{": ecodes.KEY_LEFTBRACE, "<": ecodes.KEY_COMMA, ">": ecodes.KEY_DOT,
    "+": ecodes.KEY_EQUAL,
}

# RU char -> (KEY code, modifier) for the target RU layout.
# modifier: None = no extra key, "shift" = hold Shift, "altgr" = hold AltGr.
# Letters: unshifted physical key (same position as the US char that maps to it).
_RU_CHAR2CODE: dict[str, tuple[int, str | None]] = {}
for _us, _ru in US2RU.items():
    if _ru.isalpha():
        _RU_CHAR2CODE[_ru] = (_CODE_BY_US[_us], None)
# RU punctuation, standard YTsUKEN (xkb ru:rus; verified via photo + digit row):
#   AC05 (US ,): b / Shift B        -> US2RU maps , to b
#   AC06 (US .): yu / Shift YU      -> US2RU maps . to yu
#   AC07 (US /): . / Shift ,        -> US2RU maps / to .
#   AE07 (US 7): ? (Shift)   AE06 (US 6): : (Shift)
#   AE02 (US 2): " (Shift)   AE01 (US 1): ! (Shift)
#   AE11 (US -): - / Shift _
_RU_CHAR2CODE["."] = (ecodes.KEY_SLASH, None)        # SLASH bare = RU period
_RU_CHAR2CODE[","] = (ecodes.KEY_SLASH, "shift")    # Shift+SLASH = RU comma
_RU_CHAR2CODE["/"] = (ecodes.KEY_SLASH, None)        # no bare / in RU: emit period (best effort)
_RU_CHAR2CODE["?"] = (ecodes.KEY_7, "shift")        # Shift+7
_RU_CHAR2CODE[":"] = (ecodes.KEY_6, "shift")        # Shift+6
_RU_CHAR2CODE['"'] = (ecodes.KEY_2, "shift")        # Shift+2
_RU_CHAR2CODE["!"] = (ecodes.KEY_1, "shift")        # Shift+1
_RU_CHAR2CODE["-"] = (ecodes.KEY_MINUS, None)        # MINUS bare
_RU_CHAR2CODE["_"] = (ecodes.KEY_MINUS, "shift")   # Shift+MINUS


class Injector:
    """Virtual keyboard. All timings are conservative: KDE needs a moment to
    apply a layout switch before further keys mean anything."""

    def __init__(self, name: str = "switcher-keyboard", ui=None):
        codes = list(US_BY_CODE) + [
            ecodes.KEY_LEFTCTRL,      # <- MUST be here or toggle_layout is a no-op
            ecodes.KEY_LEFTSHIFT,
            ecodes.KEY_RIGHTALT,      # AltGr for RU punctuation (., ,)
            ecodes.KEY_BACKSPACE,
            ecodes.KEY_ENTER,
            ecodes.KEY_SPACE,
            ecodes.KEY_TAB,
        ]
        self.ui = ui if ui is not None else UInput({ecodes.EV_KEY: codes}, name=name)

    # -- low-level ---------------------------------------------------------
    def _press(self, code):
        self.ui.write(ecodes.EV_KEY, code, 1)
        self.ui.syn()

    def _release(self, code):
        self.ui.write(ecodes.EV_KEY, code, 0)
        self.ui.syn()

    # -- typing ------------------------------------------------------------
    def _char(self, ch: str, layout: str):
        """Type one char assuming `layout` is the ACTIVE system layout."""
        if layout == "ru":
            entry = _RU_CHAR2CODE.get(ch)
            if entry is not None:
                code, mod = entry
                # Determine which modifier keys to hold:
                #   uppercase letter -> Shift
                #   mod == "shift"   -> Shift (punctuation like ?, :, ")
                #   mod == "altgr"   -> AltGr (reserved; not used on xkb ru:rus)
                use_shift = ch.isupper() or mod == "shift"
                use_altgr = mod == "altgr"
                if use_shift:
                    self._press(ecodes.KEY_LEFTSHIFT)
                if use_altgr:
                    self._press(ecodes.KEY_RIGHTALT)
                self._press(code); self._release(code)
                if use_altgr:
                    self._release(ecodes.KEY_RIGHTALT)
                if use_shift:
                    self._release(ecodes.KEY_LEFTSHIFT)
                return
            # unknown RU char: skip (log upstream)
            return
        # us layout
        code = US_CHAR2CODE.get(ch)
        if code is not None:
            self._press(code); self._release(code)
            return
        if ch in US_SHIFTED:
            self._press(ecodes.KEY_LEFTSHIFT)
            self._press(US_SHIFTED[ch]); self._release(US_SHIFTED[ch])
            self._release(ecodes.KEY_LEFTSHIFT)
            return
        # unknown char: skip

    def type_string(self, s: str, layout: str, gap: float = 0.02):
        for ch in s:
            self._char(ch, layout)
            time.sleep(gap)

    def backspace(self, n: int, gap: float = 0.02):
        time.sleep(0.05)  # settle: let the field register focus/state
        for _ in range(n):
            self._press(ecodes.KEY_BACKSPACE)
            self._release(ecodes.KEY_BACKSPACE)
            time.sleep(gap)

    def type_term(self, code):
        """Reprint the boundary key (space/enter/tab) that ended a corrected word."""
        self._press(code)
        self._release(code)

    def toggle_layout(self, combo=(ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT),
                     hold: float = 0.02):
        """Emit the KDE 'next layout' combo. KDE applies the switch slightly
        later — caller must sleep(SETTLE) before typing."""
        for c in combo:
            self._press(c)
        time.sleep(hold)
        for c in reversed(combo):
            self._release(c)

    def correct(self, wrong_len: int, new_word: str, layout: str):
        """Delete `wrong_len` chars, then type `new_word` (active layout = layout)."""
        self.backspace(wrong_len)
        time.sleep(0.05)
        self.type_string(new_word, layout)


if __name__ == "__main__":
    inj = Injector()
    inj.type_string("king", "us")
    print("typed 'king' on a virtual keyboard; try typing in any field.")
