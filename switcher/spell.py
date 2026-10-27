"""spell.py — thin wrapper around hunspell's pipe mode (-a).

Why: the .dic word list stores only base forms ("продолжать"), while real text
is full of inflected forms ("продолжай", "написал", "running", "typed").
Expanding the hunspell affix rules ourselves is a separate project, so we just
ask hunspell. One long-running process per dictionary, ispell -a protocol,
about 0.3 ms per word.

PROTOCOL GOTCHA: ispell -a treats every input LINE as a piece of TEXT and
answers once per TOKEN it finds in that line. A query that contains punctuation
produces SEVERAL answer lines and silently desynchronises the pipe, after which
every answer belongs to the previous query and words get corrected at random.
Therefore only pure alphabetic strings are ever sent. Everything else is
reported as "not a word" without asking hunspell, because a candidate with
punctuation cannot be a dictionary word anyway.

If hunspell is missing or dies, callers fall back to the plain word set.
"""
from __future__ import annotations

import subprocess

CACHE_LIMIT = 50000


class HunspellPipe:
    """Query hunspell for a single word with a persistent subprocess."""

    def __init__(self, lang):
        self.lang = lang
        self.proc = None
        self.cache = {}
        self._open()

    def _open(self):
        try:
            self.proc = subprocess.Popen(
                ["hunspell", "-d", self.lang, "-a", "-i", "UTF-8"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            self.proc.stdout.readline()   # banner line
        except (OSError, ValueError):
            self.proc = None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def check(self, word):
        """True when hunspell accepts the word, including affixed forms."""
        if not word or not word.isalpha():
            # Punctuation would split the input line into several tokens and
            # desynchronise the pipe. It cannot be a real word either.
            return False
        hit = self.cache.get(word)
        if hit is not None:
            return hit
        if not self.alive():
            return False
        try:
            self.proc.stdin.write(word + "\n")
            self.proc.stdin.flush()
            # hunspell -a answers ONE non-empty line per token, then a BLANK
            # separator line. Reading a single line (the old code) left the
            # separator in the pipe, so the NEXT query read "" and the pipe was
            # discarded after the first word -- every later check returned
            # False. Drain through the separator; keep the first answer line.
            line = None
            for _ in range(10):
                raw = self.proc.stdout.readline()
                if not raw:
                    self.proc = None
                    return False
                if raw.strip():
                    if line is None:
                        line = raw
                    continue
                break
            line = line or ""
        except (OSError, ValueError):
            self.proc = None
            return False
        if not line:
            self.proc = None
            return False
        ok = line.startswith("*") or line.startswith("+") or line.startswith("-")
        if len(self.cache) > CACHE_LIMIT:
            self.cache.clear()
        self.cache[word] = ok
        return ok

    def selftest(self, good, bad):
        """Sanity check right after start. A broken or desynced pipe would make
        decisions random, so we disable it instead of guessing."""
        ok = self.check(good) and not self.check(bad)
        if not ok:
            self.close()
        return ok

    def close(self):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except (OSError, ValueError):
                pass
            self.proc = None
