"""intercept.py — full keyboard interception: EVIOCGRAB + 1:1 re-emit via uinput.

WHY
---
A passive reader (see capture.py) can never correct a word safely: while it
deletes and re-types the word, a fast typist keeps typing and the two streams
interleave, so the text gets corrupted. That is exactly why the old design had
to ABORT every correction when the user did not pause.

Punto Switcher solves this by owning the keyboard. We do the same:

  * grab every physical keyboard (EVIOCGRAB), so the apps stop seeing them
    directly,
  * re-emit every event 1:1 through our own uinput device,
  * keep a character buffer and, when a word boundary arrives, optionally HOLD
    the boundary event, rewrite the word (backspaces + layout switch + retype)
    and only then release the boundary and everything queued behind it.

The event loop is single threaded and synchronous. While a correction runs, the
user's next keys simply queue in the kernel evdev buffer and are delivered a few
tens of milliseconds later: the Punto "micro pause", without any corruption.

SAFETY
------
If this process dies the kernel drops the grab automatically (fd close), so the
real keyboard works again. We additionally ungrab explicitly in stop()/finally.

NOTE: the virtual device is named "switcher-keyboard"; capture.keyboard_paths()
blacklists that name so a restart never grabs our own output device.
"""
from __future__ import annotations

import errno
import select
import time

from evdev import InputDevice, UInput, ecodes

from .capture import KEY2US, keyboard_paths
from .core import US2RU
from .injector import US_SHIFTED

BOUNDARY = frozenset({
    ecodes.KEY_SPACE, ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.KEY_TAB,
})

# How often to look for keyboards that came (back) while we were running.
RESCAN_EVERY = 2.0

# Modifier keys: they never invalidate the word buffer.
_MODS = frozenset({
    ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
    ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
    ecodes.KEY_CAPSLOCK,
})

# Codes our injector may emit that a physical keyboard might not advertise.
_EXTRA_CAPS = {
    ecodes.KEY_LEFTCTRL, ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTALT,
    # Super/Start is often missing from Bluetooth keyboards' caps; without it
    # uinput refuses the key and the Win tap dies inside the daemon.
    ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
    ecodes.KEY_BACKSPACE, ecodes.KEY_ENTER, ecodes.KEY_SPACE, ecodes.KEY_TAB,
}


# A swallowed key whose physical release never arrives must not stay dead.
SWALLOW_TTL = 2.0
# --debug: warn when a key stays pressed on the virtual device this long (a
# leaked press is what makes the session treat a modifier as held forever).
STUCK_WARN = 10.0


def _name(code):
    """Human name of a key code for the log ("KEY_LEFTCTRL")."""
    try:
        return ecodes.KEY[code]
    except (KeyError, IndexError, TypeError):
        return str(code)


_CTRL_ALT_META = frozenset({
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
    ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
})


# Modifiers we stage (never forward) until we know they do not start a hotkey.
_HOLD_MODS = frozenset({
    ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
    ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
    ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
    ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
})


# Shifted US symbols on non-letter keys: the interceptor buffers US POSITIONS,
# so Shift+6 arrives as "^" and Shift+/ as "?". visible() then turns them into
# RU punctuation/letters via SHIFT_US2RU — no per-layout branching needed here.
SHIFTED_KEY2US = {code: ch for ch, code in US_SHIFTED.items()
                  if not ch.isalpha()}


class Interceptor:
    """Owns the physical keyboards and the virtual pass-through device."""

    def __init__(self, dev_paths, init_layout="us", name="switcher-keyboard"):
        self.paths = list(dev_paths)
        self.devices = [InputDevice(p) for p in self.paths]
        self.layout = init_layout            # "us" or "ru"
        self.buffer = []                     # RAW us-position chars of the word
        self.shift = False                   # tracked: KEY2US is unshifted
        self.on_word = None                  # callback(raw_list, layout, term_code) -> bool
        self._combos = {}                    # frozenset(codes) -> callback()
        self._combo_codes = set()
        self._held = set()
        self._pending_mods = []              # staged modifier presses
        self._in_combo = False
        # code -> [events_to_drop, timestamp]. The timestamp matters: a
        # counter whose key release will never reach us (we lost the grab in
        # between) must expire instead of eating that key's next press - a
        # "dead A/F key" until the daemon restarts.
        self._swallow = {}
        self.debug = False                   # --debug: log key flow
        self._stop = False
        self.correcting = False              # True while we rewrite a word
        self._mods = set()                   # ctrl/alt/meta currently held
        self._mods_swallowed = set()         # modifiers eaten by our hotkey
        self.log = print                     # main() swaps in the file logger
        self.rescan = True                   # re-scan /dev/input for new keyboards
        self.scan = keyboard_paths           # swap out in tests
        # Exclusion mode. The daemon sets suppress_requested from its focus
        # watcher; the loop below applies it. Kept as a plain flag set from
        # another thread on purpose: only this loop may touch the devices.
        self.suppress_requested = False
        self.suppressed = False
        # Codes WE hold on the virtual device. When the keyboard goes back to
        # the system, everything in here must be released first: X11 tracks key
        # state per device, so a press from our uinput device that is never
        # released here stays logically down forever (the game then behaves as
        # if Shift were held - "everything runs through Shift").
        self._down = {}                       # code -> press timestamp
        self._events_while_suppressed = False
        # Called once when we take the keyboard back: KWin owns the layout in
        # the meantime, so the daemon has to look it up again (see daemon.py).
        self.on_resume = None

        caps = set()
        for d in self.devices:
            caps.update(d.capabilities().get(ecodes.EV_KEY, []))
        caps.update(_EXTRA_CAPS)
        self._ui = UInput({ecodes.EV_KEY: sorted(caps)}, name=name)

    # -- public ------------------------------------------------------------
    @property
    def ui(self):
        return self._ui

    def add_combo(self, codes, callback, swallow=False):
        key = frozenset(codes)
        self._combos[key] = (callback, swallow)
        self._combo_codes.update(key)

    def stop(self):
        self._stop = True

    def reset_buffer(self):
        self.buffer.clear()

    # -- low level ---------------------------------------------------------
    def _emit(self, code, value):
        # Track what the virtual device holds (see self._down).
        if value == 1:
            self._down[code] = time.time()
        elif value == 0:
            self._down.pop(code, None)
        self._ui.write(ecodes.EV_KEY, code, value)
        self._ui.syn()

    def _grab(self):
        ok = 0
        for d in self.devices:
            try:
                d.grab()
                ok += 1
            except Exception:
                pass  # no permission on this one, keep the rest alive
        return ok

    def _ungrab(self):
        ok = 0
        for d in self.devices:
            try:
                d.ungrab()
                ok += 1
            except OSError as exc:
                # EINVAL just means "this fd holds no grab" (hurtful on a
                # repeat), anything else is worth a line in the log.
                if getattr(exc, "errno", None) != errno.EINVAL:
                    self.log("ungrab failed on %s: %s"
                             % (getattr(d, "path", d), exc))
            except Exception as exc:
                self.log("ungrab failed on %s: %s"
                         % (getattr(d, "path", d), exc))
        return ok

    def _prune(self):
        """Drop devices that disappeared (BT off) so select() stays usable."""
        alive = []
        for d in self.devices:
            try:
                select.select([d], [], [], 0)
                alive.append(d)
            except (OSError, ValueError):
                try:
                    d.close()
                except Exception:
                    pass
        self.devices = alive

    # -- device bookkeeping ------------------------------------------------
    def _drop(self, dev, why):
        """A keyboard went away (BT switched host, slept, unplugged)."""
        # The releases of keys we are holding will never arrive through THIS
        # device any more: hand them over or they stay stuck for the session.
        self._release_held("keyboard %s gone" % why)
        try:
            dev.ungrab()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass
        try:
            self.devices.remove(dev)
        except ValueError:
            pass
        self.log(f"keyboard disconnected ({why}): {dev.path if hasattr(dev, 'path') else dev}")

    def _rescan(self):
        """Pick up keyboards that came (back) while we were running."""
        if not self.rescan:
            return
        try:
            paths = self.scan()
        except Exception:
            return
        known = set(d.path for d in self.devices)
        added = 0
        for p in paths:
            if p in known:
                continue
            try:
                d = InputDevice(p)
                if not self.suppressed:
                    # While suppressed we track the device but leave it alone:
                    # grabbing here would put us back in the way of the game.
                    d.grab()
                self.devices.append(d)
                added += 1
                self.log(f"keyboard connected: {p} ({d.name})")
            except Exception:
                pass
        if added:
            self.log(f"forwarding on {len(self.devices)} keyboard(s)")

    # -- main loop ---------------------------------------------------------
    def run(self):
        try:
            if self.suppress_requested:
                # Started while an excluded program owns the screen (the daemon
                # polls focus before the first grab): stay out of the way.
                self._set_suppressed(True)
            elif not self._grab():
                raise RuntimeError("could not grab any keyboard: add user to group input")
            last_scan = 0.0
            last_watch = 0.0
            stuck = set()
            while not self._stop:
                if self.suppress_requested != self.suppressed:
                    self._set_suppressed(self.suppress_requested)
                if not self.devices:
                    # No keyboard right now (BT switched to another host, went
                    # to sleep, unplugged). Do NOT exit: wait and look again.
                    # Scanning never runs while a keyboard is alive, because
                    # walking /dev/input costs tens of ms and would show up as
                    # input lag.
                    if time.time() - last_scan > RESCAN_EVERY:
                        last_scan = time.time()
                        self._rescan()
                    time.sleep(0.2)
                    continue
                try:
                    # 0.25 s, not 0.5: the loop must notice an exclusion change
                    # (ungrab / re-grab) quickly, and this is the only wakeup.
                    r, _, _ = select.select(self.devices, [], [], 0.25)
                except (OSError, ValueError):
                    self._prune()
                    continue
                if self.debug:
                    now = time.time()
                    if now - last_watch > 1.0:
                        last_watch = now
                        for code in list(self._down):
                            if now - self._down[code] > STUCK_WARN and code not in stuck:
                                stuck.add(code)
                                self.log("WARNING: %s has been held on the virtual "
                                         "keyboard for %.0f s"
                                         % (_name(code), now - self._down[code]))
                        for code in list(stuck):
                            if code not in self._down:
                                stuck.discard(code)
                for dev in list(r):
                    try:
                        # dev.read() is lazy: the OSError may come from the
                        # iteration, not from the call.
                        for ev in dev.read():
                            self._dispatch(ev)
                    except OSError as e:
                        self._drop(dev, e)
                    except ValueError:
                        self._drop(dev, "closed")
        finally:
            self._release_held("shutdown")
            self._ungrab()
            for d in self.devices:
                try:
                    d.close()
                except Exception:
                    pass
            try:
                self._ui.close()
            except Exception:
                pass

    # -- exclusion ---------------------------------------------------------
    def _release_held(self, why):
        """Let go of everything we are holding, and forget pending state.

        Called whenever the keyboard leaves our hands (an exclusion starts, the
        device disconnects). A key WE pressed on the virtual device that never
        gets its release stays logically down for the whole session: the user
        then types into a "held Ctrl/Alt" and single keys look dead - and only
        closing the uinput device clears it (which is why stopping the daemon
        used to "fix" it). Presses that were merely staged are dropped, never
        forwarded: the program has not seen them.
        """
        self.buffer.clear()
        self._held.clear()
        self._in_combo = False
        self._mods.clear()
        self._mods_swallowed.clear()
        # Pending swallow counters (a hotkey or a held-back Tab whose release
        # we were still waiting for): that release will never arrive here, and
        # a stale counter would eat the next press of that key.
        self._swallow.clear()
        self._pending_mods.clear()
        released = []
        for code in sorted(self._down):
            self._emit(code, 0)
            released.append(_name(code))
        self._down.clear()
        if released:
            self.log("released keys still held on the virtual keyboard (%s): %s"
                     % (why, " ".join(released)))

    def _set_suppressed(self, want):
        """Own the keyboard, or get out of its way completely.

        want=True  - an excluded program (a game) is in charge: ungrab every
                     keyboard, so the kernel delivers the events straight to it
                     with zero added latency and no modifier staging, and drop
                     all word state. The loop keeps READING the devices and
                     throwing the events away (see _dispatch): an ungrab does
                     not stop delivery to our fd, it only lets others read too.
        want=False - grab again.
        """
        want = bool(want)
        if want == self.suppressed:
            return
        if want:
            # Staged modifiers are DROPPED, not flushed: the program never saw
            # those presses, so forwarding them here would hand it half a
            # keystroke (press from us, release from the physical device).
            # Whatever we already pressed on our uinput device must be
            # released now (see _release_held).
            self._release_held("exclusion")
            n = self._ungrab()
            self.suppressed = True
            self._events_while_suppressed = False
            self.log("exclusion ON: keyboard released (%d device(s)), "
                     "not intercepting" % n)
            return
        self.suppressed = False
        # The layout could have been switched (by KWin, since the game had the
        # raw keyboard): our idea of it is stale and decide() would read the
        # wrong text for the first word after the game. One DBus read, once.
        if self.on_resume is not None:
            try:
                self.on_resume()
            except Exception as exc:
                self.log("resume hook failed: " + str(exc))
        # A keyboard may have appeared while we were away (BT reconnected in
        # the middle of a game): scan first, then take what is there.
        self._rescan()
        n = self._grab()
        self.log(f"exclusion OFF: grabbed {n} keyboard(s) again")

    def _dispatch(self, ev):
        if self.suppressed:
            # THE most important line of the exclusion feature.
            #
            # EVIOCGRAB is not a valve: ungrab() only makes the events reach
            # the OTHER readers as well, our fd keeps receiving the very same
            # stream. Without this early return the game got every key twice -
            # once from the physical device (the kernel lets it through now)
            # and once from our uinput, with modifier staging and word
            # buffering on top. In the field that looked like: Shift stuck
            # down, F-keys and Super dead, Alt+Tab not switching, hero not
            # moving (right-click with a stuck Shift queues an order instead
            # of moving).
            #
            # So: read and drop. Reading keeps the kernel buffers empty and the
            # device liveness visible, and the resume needs no drain.
            return
        if ev.type == ecodes.EV_KEY:
            self._key(ev.code, ev.value)
        # EV_SYN: we syn ourselves after every event.
        # EV_MSC and EV_LED: scan codes and leds, not needed downstream.

    # -- modifier staging ---------------------------------------------------
    def _flush_mods(self):
        """Forward the modifier presses we were holding back. Called as soon as
        we know they are not the start of one of our hotkeys, so the app sees
        Ctrl/Alt/Shift/Meta in the right order, right before the next key."""
        if not self._pending_mods:
            return
        if self.debug:
            self.log("flush mods: " + " ".join(_name(c) for c in self._pending_mods))
        for c in self._pending_mods:
            self._emit(c, 1)
        self._pending_mods.clear()

    def _try_combo(self, code):
        """Track the pressed key and fire a hotkey if the set matches exactly.
        Returns True when the key is ours and must not reach the app."""
        if code not in self._combo_codes:
            return False
        if code in self._held:
            return False
        self._held.add(code)
        cb, swallow = self._combos.get(frozenset(self._held), (None, False))
        if cb is None:
            return False
        self._in_combo = True
        self.buffer.clear()
        cb()
        # The hotkey's modifiers belong to us: a later tap must not leak to
        # the app as a stray Ctrl/Alt press.
        self._mods_swallowed.update(self._pending_mods)
        self._pending_mods.clear()
        if swallow:
            # Our own hotkey: the app and KWin must not see this key at all.
            self._swallow[code] = [self._swallow.get(code, [0, 0.0])[0] + 1,
                                   time.time()]
            if self.debug:
                self.log("KEY %s swallowed (hotkey)" % _name(code))
            # Its release is eaten in step 1, so drop it from the combo set now:
            # otherwise _held never empties and _in_combo sticks at True.
            self._held.discard(code)
            return True
        return False

    def _key(self, code, value):
        # Track Shift ourselves. KEY2US is the UNSHIFTED table, so without this
        # Shift+6 ("^" in US, ":" in RU) would enter the buffer as "6" and the
        # word "ок:" would be corrected to "jr6".
        if code in (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT):
            self.shift = bool(value)

        # 1) a held-back key: its press was dropped, drop release and repeats too
        pending = self._swallow.get(code)
        if pending and time.time() - pending[1] > SWALLOW_TTL:
            # The release we were waiting for never arrived (the grab was lost
            # in between): forget it, otherwise this key stays dead.
            self.log("swallow counter for %s expired - that key's release was "
                     "never seen" % _name(code))
            del self._swallow[code]
            pending = None
        if pending:
            if value == 0:
                pending[0] -= 1
                if not pending[0]:
                    del self._swallow[code]
                # The press was never forwarded, but this IS its release, so it
                # must still clear the combo bookkeeping. Otherwise a swallowed
                # hotkey leaves its key in _held forever, _in_combo stays True,
                # and every later keystroke is forwarded WITHOUT buffering ->
                # zero corrections until restart (the Ctrl+Alt+Z dead-state).
                self._held.discard(code)
                if not self._held:
                    self._in_combo = False
            return

        is_mod = code in _HOLD_MODS

        # 2) modifier state for the shortcut-versus-text decisions
        if code in _CTRL_ALT_META:
            if value in (1, 2):
                self._mods.add(code)
            elif value == 0:
                self._mods.discard(code)

        # 3) releases
        if value == 0:
            self._held.discard(code)
            if not self._held:
                self._in_combo = False
            if code in self._mods_swallowed:
                # Its press belonged to our hotkey and was never forwarded.
                self._mods_swallowed.discard(code)
                return
            if self.debug and code in _MODS:
                self.log("KEY %s up" % _name(code))
            if is_mod and code in self._pending_mods:
                # Still staged: a modifier tapped on its own (Win -> Start,
                # Alt -> menu, Ctrl+Shift -> layout toggle) must still reach
                # the session. Replay the staged set as presses, then let
                # this release through — previously both were dropped and
                # the Super key went dead for as long as the daemon ran.
                self._flush_mods()
                self._emit(code, 0)
                return
            self._emit(code, 0)
            return

        # 4) modifier presses are held back until we know what they start
        if is_mod:
            if code not in self._pending_mods:
                self._pending_mods.append(code)
                if self.debug:
                    self.log("KEY %s down (staged)" % _name(code))
            self._try_combo(code)
            return

        # 5) a normal key may complete one of our hotkeys
        if value == 1 and self._try_combo(code):
            return

        # 6) not ours: release the staged modifiers first, then pass the key on
        self._flush_mods()

        if self._in_combo:
            if not self._held:
                # Safety net: a lost modifier release (BT drop, unplug) would
                # otherwise keep us permanently inside a hotkey and silently
                # disable word buffering forever.
                self._in_combo = False
            else:
                # Inside a fired hotkey: forward, but do not treat it as text.
                self._emit(code, value)
                return

        # 7) word boundary: maybe correct before the boundary is delivered
        if code in BOUNDARY:
            held = False
            if value == 1 and self._mods:
                self.buffer.clear()  # a shortcut, not a word
            if value == 1 and self.buffer and not self.correcting:
                raw = list(self.buffer)
                self.buffer.clear()
                held = bool(self.on_word and self.on_word(raw, self.layout, code))
            if held:
                # The terminator is handled by us: re-emit it and drop the
                # physical press/release pair that is still on its way.
                self._swallow[code] = [self._swallow.get(code, [0, 0.0])[0] + 1,
                                       time.time()]
                if self.debug:
                    self.log("KEY %s held back (boundary, correction ran)"
                             % _name(code))
                self._emit(code, 1)
                self._emit(code, 0)
                return
            self._emit(code, value)
            return

        # 8) backspace pops the buffer
        if code == ecodes.KEY_BACKSPACE:
            if value == 1 and self._mods:
                self.buffer.clear()
            elif value == 1 and self.buffer:
                self.buffer.pop()
            self._emit(code, value)
            return

        # 9) printable characters build the word
        us = KEY2US.get(code)
        if us is not None:
            if self.shift and not us.isalpha():
                us = SHIFTED_KEY2US.get(code, us)
            if value in (1, 2) and not self._mods:
                self.buffer.append(us)   # raw: mapping happens at flush time
            self._emit(code, value)
            return

        # 10) anything else (arrows, Esc, Delete, F-keys...) moves the caret:
        #     the buffered word is stale now.
        if value == 1 and code not in _MODS:
            self.buffer.clear()
        self._emit(code, value)



def passthrough(paths):
    """Debug mode: grab and re-emit, no word logic. Ctrl+C to stop."""
    it = Interceptor(paths)
    print("passthrough on", paths, "- type anything. Ctrl+C to stop.")
    try:
        it.run()
    except KeyboardInterrupt:
        it.stop()
