"""core.py — heart of Switcher Linux. Pure logic, no I/O.

Cross-layout remapping (QWERTY <-> ЙЦУКЕН by physical key position),
dictionary loading from system hunspell files, and the Punto-Switcher decision.

Self-test:  .venv/bin/python -m switcher.core
"""
from __future__ import annotations
import os, re

# ---------------- Cross-layout mapping ----------------
# key = char as typed in the US layout (the physical key position),
# value = char as typed in the RU layout on the SAME physical key.
US2RU = {
    "q": "й", "w": "ц", "e": "у", "r": "к", "t": "е", "y": "н", "u": "г",
    "i": "ш", "o": "щ", "p": "з",
    "a": "ф", "s": "ы", "d": "в", "f": "а", "g": "п", "h": "р", "j": "о",
    "k": "л", "l": "д", ";": "ж", "[": "х", "]": "ъ", "'": "э",
    "z": "я", "x": "ч", "c": "с", "v": "м", "b": "и", "n": "т", "m": "ь",
    ",": "б", ".": "ю", "/": ".", "`": "ё",
    # Shifted symbols on the same physical keys (US -> RU, standard YTsUKEN).
    # The interceptor buffers shifted US chars, so Shift+6 arrives as "^";
    # visible() must turn it into ":" instead of leaving a bare digit.
    "!": "!", "@": "\u0022", "#": "№", "$": ";", "%": "%",
    "^": ":", "&": "?", "*": "*", "(": "(", ")": ")",
    "_": "_", "+": "+",
}
RU2US = {v: k for k, v in US2RU.items()}

# Shifted US symbols on keys that carry LETTERS in RU (Saneek's key map):
#   [ -> х   ] -> ъ   ; -> ж   ' -> э   , -> б   . -> ю   / -> .
#   { -> х   } -> ъ   : -> ж   " -> э   < -> б   > -> ю   ? -> ,
#   \| -> /   ~ -> ё
# Kept SEPARATE from US2RU on purpose: merging ":" -> "ж" would overwrite
# RU2US["ж"], which must stay ";" for the ru -> us remap.
SHIFT_US2RU = {
    "{": "х", "}": "ъ", ":": "ж", "<": "б", ">": "ю",
    "?": ",", "|": "/", "~": "ё",
    "\u0022": "э",
}
IDENT = frozenset("0123456789")  # digits: same in both layouts
RU_LETTERS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def remap(word, src, dst):
    """Reinterpret the word (typed while src layout active) under dst layout.
    src/dst in {'us','ru'}. Returns None if any char has no counterpart.

    EVERY mapped char is remapped, letters AND punctuation: on the same
    physical key the RU layout shows punctuation where US shows a letter
    (ж on the semicolon key, х on [, ъ on ], э on apostrophe, б on comma,
    ю on dot, ё on backtick). A word typed as "ghjljk" plus the semicolon
    key is "продолжай" and must be looked up that way. Digits and unmapped
    chars pass through unchanged.
    """
    fwd = US2RU if src == "us" else RU2US
    out = []
    for ch in word:
        key = fwd.get(ch.lower())
        if key is None:
            out.append(ch)
            continue
        out.append(key.upper() if ch.isupper() else key)
    return "".join(out)


# ---------------- Dictionary loading ----------------
def _words_from_text(text: str) -> set:
    ws = set()
    for line in text.split("\n")[1:]:
        w = line.split("/", 1)[0].split("\t", 1)[0].strip()
        if w and re.fullmatch(r"[a-zA-Zа-яА-ЯёЁ]+", w):
            ws.add(w.lower())
    return ws


def load_dict(path: str):
    """Load a hunspell .dic. Auto-detects encoding. Returns (word_set, encoding)."""
    if not os.path.exists(path):
        return set(), None
    data = open(path, "rb").read()
    best, best_enc, best_n = set(), None, -1
    for enc in ("utf-8", "koi8-r", "cp1251"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        ws = _words_from_text(text)
        if len(ws) > best_n:
            best, best_enc, best_n = ws, enc, len(ws)
    return best, best_enc


def default_dictionaries():
    d = "/usr/share/hunspell"
    ru, e1 = load_dict(os.path.join(d, "ru_RU.dic"))
    en, e2 = load_dict(os.path.join(d, "en_US.dic"))
    return ru, en, (e1, e2)


_MISSING = object()


def split_trail(w: str) -> tuple[str, str]:
    """Split a visible word into (alpha_part, trailing_punct).
    'tot.' -> ('tot', '.'). Shared by decide() and the teach path so a
    learned-key is always the bare letters, never the terminator."""
    last_alpha = -1
    for i in range(len(w) - 1, -1, -1):
        if w[i].isalpha():
            last_alpha = i
            break
    if last_alpha < 0:
        return w, ""
    return w[:last_alpha + 1], w[last_alpha + 1:]


# ---------------- The decision ----------------
# Vowels for the "is this really a word" sanity check. y counts: my, by, sty.
_VOWELS = frozenset("аеёиоуыэюяaeiouy")


def _has_vowel(s):
    # hunspell-en happily accepts "jr", "dr", "sth": they are dictionary
    # abbreviations (Jr., Dr.). A Russian word mistyped in the US layout never
    # turns into a vowelless string, so this kills the false positives.
    return any(c in _VOWELS for c in s)


class Switcher:
    """Decides, for a finished word, whether to correct it and switch layout."""

    def __init__(self, ru_words, en_words, min_len=3, learned=None, blocked=None,
                 ru_check=None, en_check=None):
        self.ru = set(ru_words)
        self.en = set(en_words)
        # Dictionary autocorrection never goes below 3 letters: 2-letter
        # "words" are noise ("ок" -> "jr", "lf" -> "да"). Taught pairs are
        # looked up BEFORE the length guards, so learned 2-letter words work.
        self.min_len = max(3, int(min_len))
        # learned: {layout: {visible_alpha: target_str}}
        #   target_str -> force correct to that word (OVERRIDES dict + collision)
        # Mutated in place by main.py (teach) and persisted to learned.json.
        self.learned = learned if learned is not None else {}
        # blocked: {layout: {visible_alpha: True}}
        #   the user said "do not touch this word", ever. Wins over everything.
        # Separate file (blocked.json) on purpose: it is a deny-list, not a
        # dictionary, and it must survive wiping the learned words.
        self.blocked = blocked if blocked is not None else {}
        # Optional smarter checks (hunspell knows inflected forms).
        # Default: plain set membership.
        self._ru_ok = ru_check if ru_check is not None else (lambda w: w in self.ru)
        self._en_ok = en_check if en_check is not None else (lambda w: w in self.en)

    def _learned(self, layout: str, visible: str):
        """Return the taught target for (layout, visible), or _MISSING."""
        m = self.learned.get(layout)
        if m is None:
            return _MISSING
        return m.get(visible, _MISSING)

    def _is_blocked(self, layout: str, visible: str) -> bool:
        m = self.blocked.get(layout)
        return bool(m and m.get(visible))

    def decide(self, typed: str, current_layout: str):
        """typed: the visible word as the user sees it (in `current_layout`).
        Returns (action, corrected):
            action in {'switch-rus', 'switch-eng', 'none'}
        Rule: the visible word is NOT a real word in the current layout, but IS
        a real word once reinterpreted in the other layout -> correct + switch.

        Trailing punctuation (e.g. "." typed as "?" in RU) is separated for
        the dict lookup but preserved (remapped) in the correction.
        """
        w = typed.strip().lower()
        # Must contain at least one letter (avoids pure-digit/punct words)
        if not any(c.isalpha() for c in w):
            return ("none", None)

        # Codes, passwords and version strings ("ок6", "abc123") are not words.
        if any(c.isdigit() for c in w):
            return ("none", None)

        # Separate trailing non-alpha chars (punctuation) for dict lookup.
        # e.g. "tot." -> alpha_part="tot", trail="."
        alpha_part, trail = split_trail(w)

        # EXPLICIT USER DECISIONS FIRST (blocked / learned). They MUST beat the
        # min_len guard: otherwise a taught 2-letter word ("ye" -> "ну") lands
        # in learned.json and is then silently ignored forever, because the
        # generic length filter returns "none" before the lookup runs.
        if self._is_blocked(current_layout, alpha_part):
            return ("none", None)
        e = self._learned(current_layout, alpha_part)
        if e is not _MISSING:
            if e is None:
                return ("none", None)
            action = "switch-rus" if current_layout == "us" else "switch-eng"
            return (action, e + trail)

        # Generic length guard: only for dictionary-driven (unlearned) words.
        # This is what keeps "lf" -> "да" and other 2-letter noise out.
        # The per-variant length check lives in the loop below: the "trailing
        # punctuation" may in fact be the last letter of the word.
        if len(w) < self.min_len:
            return ("none", None)

        # The trailing punctuation is kept EXACTLY as the user saw it: the
        # injector re-types that symbol on the physical key of the TARGET
        # layout, we never remap it.
        #
        # EXCEPT that several US keys carry a LETTER in RU:
        #     "." -> ю   "," -> б   ";" -> ж   "'" -> э
        #     "[" -> х   "]" -> ъ   "`" -> ё
        # so the last character of a Russian word typed on the US layout looks
        # like punctuation and split_trail() eats it:
        #     "gjkyjcnm." -> полностью   (the dot is ю!)
        #     "yj;"       -> нож          (the semicolon is ж!)
        # Hence: try every split point, from "all of the tail is punctuation"
        # (the old behaviour, tried first, so real punctuation wins) to "the
        # tail belongs to the word". Variant i moves trail[:i] back in.
        if current_layout == "us":
            other, action = "ru", "switch-rus"
            want_ok, have_ok = self._ru_ok, self._en_ok
        else:
            other, action = "us", "switch-eng"
            want_ok, have_ok = self._en_ok, self._ru_ok
        for i in range(0, len(trail) + 1):
            probe = alpha_part + trail[:i]
            if len(probe) < self.min_len:
                continue
            cand = remap(probe, current_layout, other)
            if not cand:
                continue
            c_alpha, c_trail = split_trail(cand)
            if (want_ok(c_alpha) and not have_ok(probe)
                    and _has_vowel(c_alpha)):
                # remap() is 1:1, so the result has exactly the length of what
                # the user typed: the daemon's backspace count stays right.
                return (action, c_alpha + c_trail + trail[i:])
        return ("none", None)


if __name__ == "__main__":
    ru, en, encs = default_dictionaries()
    sw = Switcher(ru, en)
    print(f"dicts: ru={len(ru)} en={len(en)} encs={encs}")
    print("'rjhjkm' cur=us ->", sw.decide("rjhjkm", "us"))
    print("'hello'  cur=us ->", sw.decide("hello", "us"))
    print("'далее'  cur=ru ->", sw.decide("далее", "ru"))
    print("'как'    cur=ru ->", sw.decide("как", "ru"))
    print("'world'  cur=ru ->", sw.decide("world", "ru"))  # should be none
    print("'tot.'   cur=us ->", sw.decide("tot.", "us"))    # trailing punct
    print("'tot?'   cur=us ->", sw.decide("tot?", "us"))    # trailing punct 2
    # The trailing "punctuation" that is really a RU letter (see decide()):
    print("'gjkyjcnm.' cur=us ->", sw.decide("gjkyjcnm.", "us"))  # полностью
    print("'yj;'    cur=us ->", sw.decide("yj;", "us"))          # нож
    print("'wdwef.' cur=ru ->", sw.decide("wdwef.", "ru"))  # EN word + punct on RU
    print("roundtrip rjhjkm us->ru =", remap("rjhjkm", "us", "ru"))
    # learned overrides: teach tot->еще (beats EN 'tot'), block a on ru.
    sw2 = Switcher(ru, en, learned={"us": {"tot": "еще"}, "ru": {"a": None}})
    print("learned tot->еще  cur=us ->", sw2.decide("tot", "us"))
    print("learned block    cur=ru ->", sw2.decide("a", "ru"))
