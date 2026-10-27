"""main.py — Switcher Linux, INTERCEPT mode.

We own the keyboard (see intercept.py): grab the physical devices, forward
every event through our own uinput device, and rewrite a mistyped word
synchronously inside the event loop. Because we own the stream, a correction
can never race with the user's typing: keys pressed during a correction just
queue in the kernel for a few tens of ms and then come out in the right
layout. That is how Punto Switcher feels.

Hotkeys:
  Ctrl+Shift / Meta+Alt+K / Meta+Alt+L   layout switch (KDE does it, we resync)
  Ctrl+Alt+Z                             rollback last correction, ban the word
  Ctrl+Alt+T                             fix the last word and remember it
  Ctrl+Alt+P                             pause / resume autocorrection
  Ctrl+C                                 quit
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

from evdev import ecodes

from . import config as cfg
from . import focus as focus_probe
from .capture import keyboard_paths
from .core import US2RU, SHIFT_US2RU
from .core import Switcher, default_dictionaries, remap, split_trail
from .injector import Injector
from .intercept import Interceptor
from .ipc import IPCServer, call as ipc_call
from .layout_probe import current_layout, dbus_set_layout, layout_index_map
from .spell import HunspellPipe

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Effective user config (config.toml). CFG is a plain dict so the IPC layer can
# mutate it in place; timing knobs take effect on the NEXT correction only.
CFG = cfg.ensure_files()
LOG_PATH = cfg.paths.log
LEARNED_PATH = cfg.paths.learned
BLOCKED_PATH = cfg.paths.blocked

# How often the exclusion watcher looks at the focused window. 0.3 s is a
# compromise: fast enough that a game never sees our pass-through for long,
# cheap enough to be invisible (one X property round-trip per tick).
EXCLUSION_POLL = 0.3
# How long "not excluded" has to hold before the keyboard is grabbed back -
# and it has to be the SAME window the whole time. The focus probe blinks
# during ordinary use: Alt+Tab makes KWin bounce the focus through several
# windows (the switcher itself is a Wayland surface, i.e. "unknown"), a load
# screen changes the window, an overlay steals it for a moment. Grabbing the
# keyboard back in the middle of such a chord is how the game ends up with a
# stuck Alt/Tab and how the KDE switcher loses its release. 3 s of a stable
# window is longer than any window-switch gesture and still feels instant when
# the user really moved to another program.
EXCLUSION_LEAVE_DWELL = 3.0

SETTLE = CFG["settle"]            # uinput Ctrl+Shift fallback: wait for KDE
SETTLE_DBUS = CFG["settle_dbus"]  # DBus setLayout is synchronous
GAP_BS = CFG["gap_bs"]            # backspace gap (we own the stream)
GAP_TYPE = CFG["gap_type"]        # typed char gap


def load_json(path):
    """Load a JSON dict (learned words / blocked words). {} on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_json(data, path):
    """Persist a JSON dict (atomic write: tmp + rename)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        _log(f"save_json FAILED ({path}): {e}")


def _log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def visible(raw, layout):
    """Raw us-position chars -> what the user really sees in that layout."""
    if layout == "ru":
        return "".join(US2RU.get(c) or SHIFT_US2RU.get(c) or c for c in raw)
    return "".join(raw)


def check_words(words):
    """Diagnostic mode: python -m switcher.main --check <word> [<word> ...]
    Shows what remap produces and what every check answers, without touching
    the keyboard or the layout."""
    ru, en, encs = default_dictionaries()
    ru_sp = HunspellPipe("ru_RU")
    en_sp = HunspellPipe("en_US")
    print("hunspell ru:", ru_sp.selftest("привет", "ъъъъъ"),
          "en:", en_sp.selftest("hello", "zzzzq"))
    sw = Switcher(ru, en,
                  ru_check=lambda w: w in ru or ru_sp.check(w),
                  en_check=lambda w: w in en or en_sp.check(w))
    for w in words:
        wl = w.lower()
        to_ru = remap(wl, "us", "ru")
        to_en = remap(wl, "ru", "us")
        print(f"word {w!r}")
        print(f"  us->ru {to_ru!r}  in_set={to_ru in ru}  hunspell={ru_sp.check(to_ru)}")
        print(f"  ru->us {to_en!r}  in_set={to_en in en}  hunspell={en_sp.check(to_en)}")
        print(f"  decide(us) {sw.decide(w, 'us')}")
        print(f"  decide(ru) {sw.decide(w, 'ru')}")



def main():
    ipc_srv = None          # set after the callbacks are wired (late-bound)
    ru, en, encs = default_dictionaries()
    if "--check" in sys.argv:
        idx = sys.argv.index("--check")
        check_words(sys.argv[idx + 1:])
        return

    _log(f"dicts ru={len(ru)} ({encs[0]}) en={len(en)} ({encs[1]})")

    devs = keyboard_paths()
    if not devs:
        _log("no readable keyboard. Are you in the 'input' group?")
        print("        sudo usermod -aG input $USER  then re-login.")
        sys.exit(1)
    _log(f"keyboards: {devs}")


    learned = load_json(LEARNED_PATH)
    blocked = load_json(BLOCKED_PATH)

    # Migrate the old format: banned words used to be stored as null values in
    # learned.json. Move them to blocked.json once and clean learned.json.
    moved = 0
    for lay, words in list(learned.items()):
        if not isinstance(words, dict):
            learned[lay] = {}
            continue
        for key, val in list(words.items()):
            if val is None:
                words.pop(key, None)
                blocked.setdefault(lay, {})[key] = True
                moved += 1
    if moved:
        save_json(learned, LEARNED_PATH)
        save_json(blocked, BLOCKED_PATH)
        _log(f"migrated {moved} banned word(s) to {BLOCKED_PATH}")

    total = sum(len(v) for v in learned.values())
    n_block = sum(len(v) for v in blocked.values())
    _log(f"learned: {total} word(s) from {LEARNED_PATH}")
    _log(f"blocked: {n_block} word(s) from {BLOCKED_PATH}")

    ru_sp = HunspellPipe("ru_RU")
    en_sp = HunspellPipe("en_US")
    ru_ok_pipe = ru_sp.selftest("привет", "ъъъъъ")
    en_ok_pipe = en_sp.selftest("hello", "zzzzq")
    _log(f"hunspell: ru={'on' if ru_ok_pipe else 'OFF'} en={'on' if en_ok_pipe else 'OFF'}")
    sw = Switcher(ru, en, learned=learned, blocked=blocked, min_len=int(CFG["min_len"]),
                  ru_check=lambda w: w in ru or ru_sp.check(w),
                  en_check=lambda w: w in en or en_sp.check(w))
    lay0, lay0_src = current_layout()
    _log(f"initial layout: {lay0} (source: {lay0_src})")

    # Refuse to start twice: two grabbers fight over the keyboard and the
    # second one dies on grab() while leaving the first without a socket.
    try:
        if ipc_call(cfg.paths.socket, "ping", timeout=1.0).get("pong"):
            _log("another daemon already owns " + cfg.paths.socket + " - exiting")
            return
    except Exception:
        pass   # no socket / stale socket / refused -> we are free to start

    it = Interceptor(devs, init_layout=lay0)
    it.log = _log
    inj = Injector(ui=it.ui)

    lay_map = layout_index_map()
    _log(f"layout map: {lay_map if lay_map else 'none (uinput fallback)'}")

    last_correction = {}
    last_flushed = {}
    state = {"paused": ("--paused" in sys.argv)}   # --paused: forward only, no corrections
    state["debug"] = "--debug" in sys.argv
    state["gen"] = 0      # bump on every user toggle to drop stale resolvers

    # ------------------------------------------------------------------ layout
    def set_layout(target):
        """Switch the system layout and our own idea of it, synchronously."""
        if lay_map and target in lay_map:
            if dbus_set_layout(lay_map[target]):
                time.sleep(SETTLE_DBUS)
                it.layout = target
                _log(f"layout (dbus) -> {target}")
                return
            _log(f"dbus setLayout -> {target} failed, using uinput")
        inj.toggle_layout()
        time.sleep(SETTLE)
        it.layout = target
        _log(f"layout (uinput) -> {target}")

    def resync_layout(reason=""):
        """Ask KDE what the real layout is (we may have lost track)."""
        try:
            lay, src = current_layout()
        except Exception:
            return
        if lay != it.layout:
            _log(f"layout resync {it.layout} -> {lay} ({src}) {reason}")
        it.layout = lay

    def on_toggle():
        # Ctrl+Shift is OURS now: the interceptor stages the modifiers, so
        # neither KWin nor the focused app ever sees this chord. One call,
        # synchronous, deterministic.
        target = "us" if it.layout == "ru" else "ru"
        set_layout(target)
        threading.Timer(0.40, resync_layout, args=("after ctrl+shift",)).start()

    def on_resync():
        threading.Timer(0.35, resync_layout, args=("after kde combo",)).start()

    # ------------------------------------------------------------- correction
    def on_word(raw, layout, term_code):
        """raw is the list of us-position chars buffered by the interceptor.
        Runs synchronously inside the event loop. Returns True when the
        terminator was already re-emitted by us, so the loop must drop the
        physical press/release pair."""
        word = visible(raw, layout)
        action, fixed = sw.decide(word, layout)
        if action != "none":
            # A correction is expensive anyway: verify the live layout first.
            # If the user switched behind our back (Ctrl+Shift+letter, a KDE
            # shortcut), our tracked layout is stale, so recompute the word.
            real, src = current_layout()
            if real != layout:
                _log(f"layout drift: assumed {layout}, real {real} ({src})")
                layout = real
                it.layout = real
                word = visible(raw, layout)
                action, fixed = sw.decide(word, layout)
        last_flushed.update(word=word, layout=layout, term_code=term_code)
        if state["debug"]:
            _log(f"FLUSH {word!r} ({layout}) -> {action} {fixed!r}")
        if state["paused"] or action == "none":
            return False
        target = "ru" if action == "switch-rus" else "us"
        _log(f"CORRECT {word!r} ({layout}) -> {fixed!r} ({target})")
        it.correcting = True
        try:
            # The terminator is still held back by the interceptor, so only
            # the word itself is on screen.
            inj.backspace(len(word), gap=GAP_BS)
            set_layout(target)
            inj.type_string(fixed, target, gap=GAP_TYPE)
        finally:
            it.correcting = False
        last_correction.update(
            wrong=word, wrong_layout=layout, fixed=fixed,
            fixed_layout=target, term_code=term_code,
        )
        _log("CORRECT done")
        if ipc_srv is not None:
            ipc_srv.broadcast({"event": "correct", "word": word, "fixed": fixed,
                               "from": layout, "to": target})
        return True

    # --------------------------------------------------------------- rollback
    def on_rollback(ban=False):
        # Ctrl+Alt+Z reverts the last correction and, pressed again, re-applies
        # it: the two forms simply swap places, like a toggle.
        lc = last_correction
        if not lc:
            _log("rollback: nothing to revert")
            return
        if it.buffer:
            # The user has already started a new word: the tail of the screen
            # is not ours any more, deleting there would eat their text.
            _log("rollback: word in progress, skipped")
            return
        was, was_layout = lc["wrong"], lc["wrong_layout"]
        now, now_layout = lc["fixed"], lc["fixed_layout"]
        _log(f"rollback: {now!r} -> {was!r} (layout {was_layout})")
        if ban:
            vis, _trail = split_trail(was.lower())
            if vis:
                sw.blocked.setdefault(was_layout, {})[vis] = True
                save_json(sw.blocked, BLOCKED_PATH)
                _log(f"blocked forever: {vis!r}")
        it.correcting = True
        try:
            # On screen right now: the "fixed" form plus its terminator.
            inj.backspace(len(now) + 1, gap=GAP_BS)
            if it.layout != was_layout:
                set_layout(was_layout)
            inj.type_string(was, was_layout, gap=GAP_TYPE)
            inj.type_term(lc["term_code"])
        finally:
            it.correcting = False
        # Swap so the next press flips it back.
        lc["wrong"], lc["wrong_layout"] = now, now_layout
        lc["fixed"], lc["fixed_layout"] = was, was_layout
        _log("rollback done")


    def on_ban():
        on_rollback(ban=True)

    def on_teach():
        lf = dict(last_flushed)
        if not lf:
            _log("teach: no word yet")
            return
        word, layout, term_code = lf["word"], lf["layout"], lf["term_code"]
        if last_correction and last_correction.get("wrong") == word:
            _log(f"teach: {word!r} was already corrected, nothing to do")
            return
        alpha, trail = split_trail(word.lower())
        target = "ru" if layout == "us" else "us"
        conv = remap(alpha, layout, target)
        if not conv:
            _log(f"teach: cannot remap {word!r} ({layout})")
            return
        fixed = conv + trail
        sw.learned.setdefault(layout, {})[alpha] = conv
        save_json(sw.learned, LEARNED_PATH)
        _log(f"teach: {word!r} ({layout}) -> {fixed!r} ({target}), remembered")
        it.correcting = True
        try:
            inj.backspace(len(word) + 1, gap=GAP_BS)
            if it.layout != target:
                set_layout(target)
            inj.type_string(fixed, target, gap=GAP_TYPE)
            inj.type_term(term_code)
        finally:
            it.correcting = False
        last_correction.update(
            wrong=word, wrong_layout=layout, fixed=fixed,
            fixed_layout=target, term_code=term_code,
        )
        _log("teach done")

    def on_pause():
        state["paused"] = not state["paused"]
        _log("autocorrect PAUSED" if state["paused"] else "autocorrect RESUMED")
        if ipc_srv is not None:
            ipc_srv.broadcast({"event": "paused", "value": bool(state["paused"])})

    # ------------------------------------------------------------------ wiring
    it.on_word = on_word
    # Taking the keyboard back happens inside the interceptor loop; the layout
    # may have been switched by KWin while an excluded program owned it.
    it.on_resume = lambda: resync_layout("after exclusion")
    # --debug also traces the key flow (modifiers, swallows, leaked presses):
    # a stuck modifier is invisible otherwise.
    it.debug = bool(state["debug"])
    # Register every left/right modifier combination: the user may well press
    # right Alt or right Ctrl, and exact-match combos would silently miss it.
    ctrls = (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL)
    shifts = (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT)
    alts = (ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT)
    metas = (ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA)

    for c in ctrls:
        for s in shifts:
            it.add_combo([c, s], on_toggle)          # KDE must still see it
    for m in metas:
        for a in alts:
            it.add_combo([m, a, ecodes.KEY_K], on_resync)
            it.add_combo([m, a, ecodes.KEY_L], on_resync)
    for c in ctrls:
        for a in alts:
            it.add_combo([c, a, ecodes.KEY_Z], on_rollback, True)
            it.add_combo([c, a, ecodes.KEY_T], on_teach, True)
            it.add_combo([c, a, ecodes.KEY_B], on_ban, True)
            it.add_combo([c, a, ecodes.KEY_P], on_pause, True)

    # ------------------------------------------------------------------- IPC
    # Single owner of learned.json / blocked.json: the tray UI never writes
    # those files directly, it calls these commands. Avoids two writers.
    def _words_of(d):
        return {k: dict(v) for k, v in d.items()}

    def _norm_word(raw):
        alpha, trail = split_trail(str(raw).strip().lower())
        if not alpha or not alpha.isalpha():
            raise ValueError("word must contain letters only: " + repr(raw))
        return alpha, trail

    def _reload_dictionaries():
        new_l = load_json(LEARNED_PATH)
        new_b = load_json(BLOCKED_PATH)
        sw.learned.clear(); sw.learned.update(new_l)
        sw.blocked.clear(); sw.blocked.update(new_b)
        n_l = sum(len(v) for v in new_l.values())
        n_b = sum(len(v) for v in new_b.values())
        _log("ipc reload: learned=" + str(n_l) + " blocked=" + str(n_b))
        return {"learned": n_l, "blocked": n_b}

    def _learned_add(p):
        layout = p.get("layout")
        if layout not in ("us", "ru"):
            raise ValueError("layout must be us or ru")
        alpha, _trail = _norm_word(p.get("word", ""))
        target = p.get("target")
        if target:
            target, _t2 = _norm_word(target)
        else:
            other = "ru" if layout == "us" else "us"
            target = remap(alpha, layout, other)
            if not target:
                raise ValueError("cannot remap " + repr(alpha))
        sw.learned.setdefault(layout, {})[alpha] = target
        save_json(sw.learned, LEARNED_PATH)
        _log("ipc: learned " + repr(alpha) + " (" + layout + ") -> " + repr(target))
        return {"word": alpha, "target": target, "layout": layout}

    def _learned_del(p):
        layout = p.get("layout")
        alpha, _trail = _norm_word(p.get("word", ""))
        m = sw.learned.get(layout) or {}
        existed = m.pop(alpha, None) is not None
        if existed:
            save_json(sw.learned, LEARNED_PATH)
        _log("ipc: unlearned " + repr(alpha) + " existed=" + str(existed))
        return {"removed": existed}

    def _blocked_add(p):
        layout = p.get("layout")
        if layout not in ("us", "ru"):
            raise ValueError("layout must be us or ru")
        alpha, _trail = _norm_word(p.get("word", ""))
        sw.blocked.setdefault(layout, {})[alpha] = True
        save_json(sw.blocked, BLOCKED_PATH)
        _log("ipc: blocked " + repr(alpha) + " (" + layout + ")")
        return {"word": alpha, "layout": layout}

    def _blocked_del(p):
        layout = p.get("layout")
        alpha, _trail = _norm_word(p.get("word", ""))
        m = sw.blocked.get(layout) or {}
        existed = m.pop(alpha, None) is not None
        if existed:
            save_json(sw.blocked, BLOCKED_PATH)
        _log("ipc: unblocked " + repr(alpha) + " existed=" + str(existed))
        return {"removed": existed}

    def _status():
        return {
            "paused": bool(state["paused"]),
            "layout": it.layout,
            "excluded": excl["active"],
            "excluded_mode": excl["mode"],
            "suppressed": bool(it.suppressed),
            "exclude_focus": list(CFG.get("exclude_focus") or []),
            "exclude_running": list(CFG.get("exclude_running") or []),
            "learned": _words_of(sw.learned),
            "blocked": _words_of(sw.blocked),
            "words_learned": sum(len(v) for v in sw.learned.values()),
            "words_blocked": sum(len(v) for v in sw.blocked.values()),
            "config": dict(CFG),
            "log": LOG_PATH,
            "socket": ipc_srv.path if ipc_srv else None,
        }

    def _log_tail(p):
        try:
            n = int(p.get("lines", 50))
        except (TypeError, ValueError):
            n = 50
        n = max(1, min(n, 500))
        try:
            with open(LOG_PATH, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return {"lines": []}
        return {"lines": [x.rstrip(chr(10)) for x in lines[-n:]]}

    def _config_apply():
        # Knobs that can change without restarting the daemon. min_len has a
        # hard floor of 3: see Switcher.__init__ (2-letter "words" are noise).
        requested = int(CFG["min_len"])
        sw.min_len = max(3, requested)
        if requested < 3:
            _log("config: min_len=" + str(requested) + " ignored, floor is 3")
        # --debug from the command line wins; otherwise the config knob, so the
        # settings window can switch the key-flow trace on and off live.
        it.debug = bool(CFG.get("debug")) or ("--debug" in sys.argv)
        return dict(CFG)

    def _config_set(p):
        changed = {k: v for k, v in (p or {}).items() if k in cfg.DEFAULTS}
        for k, v in changed.items():
            CFG[k] = cfg._coerce(k, v)
        cfg.save(CFG)
        out = _config_apply()
        _log("ipc: config set " + repr(changed))
        # An exclusion edited through config.set must take effect immediately,
        # not on the next tick 0.3 s later.
        try:
            _exclusion_tick()
        except Exception as e:
            _log("exclusion probe error: " + type(e).__name__ + ": " + str(e))
        ipc_srv.broadcast({"event": "config", "value": out})
        return out

    # -------------------------------------------------------------- exclusion
    # A program listed in config.toml is NOT ours. Two ways to name one:
    #   exclude_focus   - only while that program's window is focused
    #                     (X11/XWayland windows only: games; see focus.py)
    #   exclude_running - the whole time the process exists, focus irrelevant
    # While an exclusion is in force the interceptor lets go of the keyboard
    # completely: the kernel hands raw events to the game, with no extra uinput
    # device in the path, no modifier staging and no word buffering. Hotkeys do
    # not work either - there is nothing left to intercept, which is the point.
    excl = {"active": None, "mode": None, "window": None,
            "cand": None, "cand_wid": None, "cand_since": 0.0,
            "flips": [], "unknown": 0}
    stop_flag = threading.Event()
    excl_probe_logged = {"done": False}

    def _patterns():
        return (list(CFG.get("exclude_focus") or []),
                list(CFG.get("exclude_running") or []))

    def _exclusion_tick():
        """One look at the world: are we supposed to be hands-off right now?"""
        pat_f, pat_r = _patterns()
        hit, mode, win, why = None, None, None, ""
        if pat_f:
            win = focus_probe.active_window()
            if not excl_probe_logged["done"]:
                excl_probe_logged["done"] = True
                if win.get("ok"):
                    _log("focus probe: ok, active window pid=" + str(win.get("pid"))
                         + " class=" + repr(win.get("wm_class")))
                else:
                    _log("focus probe: unknown (" + str(win.get("why")) + ")")
            if win.get("ok"):
                hit = focus_probe.window_matches(pat_f, win)
                if hit:
                    mode = "focus"
                excl["unknown"] = 0
            elif win.get("reason") == "wayland":
                # Some OTHER window is in front (a native Wayland one we cannot
                # name). That is a real answer: the excluded program lost the
                # focus, so the switcher may come back.
                excl["unknown"] = 0
            else:
                # The probe could not tell what is focused (an X error, a
                # window that vanished, no X connection at all). That is NOT
                # "another program is in front": grabbing the keyboard back on
                # a blind probe is how the daemon ends up intercepting a game
                # mid-match. Keep whatever state we are in.
                excl["unknown"] = excl.get("unknown", 0) + 1
                if excl["unknown"] == 1:
                    _log("focus probe gave no answer (%s) - keeping the state as is"
                         % (win.get("why") or "unknown"))
                if not excl["active"]:
                    excl["window"] = win
                    return
                hit, mode = excl["active"], excl["mode"]
        if not hit and pat_r:
            hit = focus_probe.find_running(pat_r)
            if hit:
                mode = "running"
        excl["window"] = win
        now = time.time()
        if bool(hit) == bool(excl["active"]):
            if mode != excl["mode"]:
                excl["mode"] = mode
            excl["cand"] = None
            return
        # HYSTERESIS. Entering an exclusion is urgent - a game is about to take
        # the keyboard. Leaving it is not: the probe can answer "no match" for a
        # moment (load screen, an overlay window, an unknown focus on Wayland)
        # and every flip costs a grab/ungrab pair. That churn is what leaves the
        # game with a half-delivered keystroke: while a game is loading or the
        # window changes, the keyboard gets grabbed back and the staging logic
        # starts eating modifiers in the middle of the game.
        # The window that produced the "not excluded" answer. A chord like
        # Alt+Tab walks the focus through several windows within a second, so
        # two different windows must not add up to one stable state.
        cand_wid = None if hit else (win or {}).get("wid")
        if excl["cand"] != hit or cand_wid != excl.get("cand_wid"):
            excl["cand"] = hit
            excl["cand_wid"] = cand_wid
            excl["cand_since"] = now
        if not hit and (now - excl["cand_since"]) < EXCLUSION_LEAVE_DWELL:
            return          # not enough quiet yet: stay as we are
        flips = excl["flips"]
        flips.append(now)
        while flips and now - flips[0] > 10.0:
            flips.pop(0)
        if len(flips) == 4:
            _log("WARNING: exclusion state keeps flipping (%d changes in %.0f s) "
                 "- the focus probe is unstable, staying as quiet as possible"
                 % (len(flips), now - flips[0]))
        prev = excl["active"]
        excl["active"], excl["mode"] = hit, mode
        excl["cand"] = None
        it.suppress_requested = bool(hit)
        if hit:
            _log("exclusion ON: %r (%s) - keyboard released  [window pid=%s class=%r]"
                 % (hit, mode, (win or {}).get("pid"), (win or {}).get("wm_class")))
        elif prev:
            _log("exclusion OFF: %r - interception back on  [probe: %s, wid=%s pid=%s class=%r]"
                 % (prev, why or (win or {}).get("reason") or "ok",
                    (win or {}).get("wid"), (win or {}).get("pid"),
                    (win or {}).get("wm_class")))
        if ipc_srv is not None:
            ipc_srv.broadcast({"event": "excluded", "pattern": hit, "mode": mode})

    def _exclusion_loop():
        while not stop_flag.is_set():
            try:
                _exclusion_tick()
            except Exception as e:
                _log("exclusion probe error: " + type(e).__name__ + ": " + str(e))
            stop_flag.wait(EXCLUSION_POLL)

    def _excl_state():
        pat_f, pat_r = _patterns()
        return {"focus": pat_f, "running": pat_r,
                "active": excl["active"], "active_mode": excl["mode"],
                "suppressed": bool(it.suppressed)}

    def _excl_entry(p):
        # Win over everything. A pattern that matches everything ("py") would
        # silently disable the switcher, so it must be a real name.
        mode = str(p.get("mode") or "focus")
        if mode not in ("focus", "running"):
            raise ValueError("mode must be 'focus' or 'running'")
        cleaned = cfg._clean_list([str(p.get("pattern") or "")])
        if not cleaned:
            raise ValueError("bad pattern: " + repr(p.get("pattern")))
        pat = cleaned[0]
        if len(pat) < 2:
            raise ValueError("pattern too short, 2 characters minimum: " + repr(pat))
        return mode, pat

    def _excl_add(p):
        mode, pat = _excl_entry(p)
        key = "exclude_focus" if mode == "focus" else "exclude_running"
        CFG[key] = cfg._clean_list(list(CFG.get(key) or []) + [pat])
        cfg.save(CFG)
        _log("ipc: exclusion added " + repr(pat) + " (" + mode + ")")
        _exclusion_tick()
        return _excl_state()

    def _excl_del(p):
        mode, pat = _excl_entry(p)
        key = "exclude_focus" if mode == "focus" else "exclude_running"
        low = pat.lower()
        before = list(CFG.get(key) or [])
        CFG[key] = [x for x in before if x.lower() != low]
        removed = len(CFG[key]) != len(before)
        if removed:
            cfg.save(CFG)
        _log("ipc: exclusion removed " + repr(pat) + " (" + mode + ") removed="
             + str(removed))
        _exclusion_tick()
        return _excl_state()

    def dispatch(cmd, p):
        if cmd == "ping":
            return {"pong": True}
        if cmd == "status":
            return _status()
        if cmd == "pause":
            state["paused"] = True
            _log("autocorrect PAUSED (ipc)")
            ipc_srv.broadcast({"event": "paused", "value": True})
            return {"paused": True}
        if cmd == "resume":
            state["paused"] = False
            _log("autocorrect RESUMED (ipc)")
            ipc_srv.broadcast({"event": "paused", "value": False})
            return {"paused": False}
        if cmd == "reload":
            return _reload_dictionaries()
        if cmd == "learned.list":
            return _words_of(sw.learned)
        if cmd == "learned.add":
            return _learned_add(p)
        if cmd == "learned.del":
            return _learned_del(p)
        if cmd == "blocked.list":
            return _words_of(sw.blocked)
        if cmd == "blocked.add":
            return _blocked_add(p)
        if cmd == "blocked.del":
            return _blocked_del(p)
        if cmd == "exclude.list":
            return _excl_state()
        if cmd == "exclude.add":
            return _excl_add(p)
        if cmd == "exclude.del":
            return _excl_del(p)
        if cmd == "config.get":
            return dict(CFG)
        if cmd == "config.set":
            return _config_set(p)
        if cmd == "config.reload":
            new = cfg.load()
            CFG.clear(); CFG.update(new)
            _log("ipc: config reloaded from disk")
            return _config_apply()
        if cmd == "log.tail":
            return _log_tail(p)
        if cmd == "shutdown":
            # Answer FIRST, die second: if we stop the loop here, the socket
            # goes away before the reply is written and the tray turns the
            # clean stop into a phantom "no connection" warning.
            _log("shutdown requested (ipc)")
            threading.Timer(0.25, it.stop).start()
            return {"stopping": True}
        raise KeyError(cmd)

    ipc_srv = IPCServer(cfg.paths.socket, dispatch, log=_log)
    ipc_srv.start()

    # Exclusion watcher: honours an entry that is already in force BEFORE the
    # first grab, so the daemon never even touches the keyboard of a running
    # game. The thread only flips it.suppress_requested; the interceptor loop
    # owns the devices.
    try:
        _exclusion_tick()
    except Exception as e:
        _log("exclusion probe error: " + type(e).__name__ + ": " + str(e))
    threading.Thread(target=_exclusion_loop, name="exclusion", daemon=True).start()
    pat_f, pat_r = _patterns()
    _log("exclusions: focus=" + repr(pat_f) + " running=" + repr(pat_r))

    _log("running (intercept). Ctrl+C=quit  Ctrl+Alt+P=pause  "
         "Ctrl+Alt+Z=rollback  Ctrl+Alt+B=ban  Ctrl+Alt+T=teach")
    try:
        it.run()
    except RuntimeError as e:
        _log(f"FATAL: {e}")
    except KeyboardInterrupt:
        _log("stopped (Ctrl+C).")
    finally:
        stop_flag.set()
        it.stop()
        if ipc_srv is not None:
            ipc_srv.stop()
        ru_sp.close()
        en_sp.close()


if __name__ == "__main__":
    main()
