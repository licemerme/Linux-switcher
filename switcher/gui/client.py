# gui/client.py - thin IPC client used by the tray UI.
# The GUI never touches learned.json / blocked.json: the daemon is the single
# writer, so every edit goes through these calls.
from __future__ import annotations

import os

from .. import config as cfg
from ..ipc import call


class DaemonError(RuntimeError):
    # Daemon is not running (or refused the command).
    pass


def _call(name, params=None):
    try:
        return call(cfg.paths.socket, name, params or {}, timeout=2.0)
    except RuntimeError as e:
        # The daemon answered, but rejected the command.
        raise DaemonError(str(e) + " [" + cfg.paths.socket + "]") from e
    except OSError as e:
        # ENOENT / ECONNREFUSED / EACCES / timeout - all of them mean the same
        # thing to the UI, but the message must carry the socket path: a wrong
        # XDG_RUNTIME_DIR is the usual reason for a phantom "daemon down".
        raise DaemonError("no connection to " + cfg.paths.socket + ": " + str(e)) from e


def status():
    return _call("status")


def set_paused(value):
    return _call("resume" if not value else "pause")


def shutdown():
    """Ask the daemon to exit. It answers, then closes the socket."""
    return _call("shutdown")


def learned_add(layout, word, target=None):
    p = {"layout": layout, "word": word}
    if target:
        p["target"] = target
    return _call("learned.add", p)


def learned_del(layout, word):
    return _call("learned.del", {"layout": layout, "word": word})


def blocked_add(layout, word):
    return _call("blocked.add", {"layout": layout, "word": word})


def blocked_del(layout, word):
    return _call("blocked.del", {"layout": layout, "word": word})


def reload_dicts():
    return _call("reload")


def exclude_list():
    return _call("exclude.list")


def exclude_add(pattern, mode):
    return _call("exclude.add", {"pattern": pattern, "mode": mode})


def exclude_del(pattern, mode):
    return _call("exclude.del", {"pattern": pattern, "mode": mode})


def config_set(**kwargs):
    return _call("config.set", kwargs)


def log_tail(lines=100):
    return _call("log.tail", {"lines": int(lines)})


def diagnose():
    # Everything needed to explain a phantom "daemon is not running".
    path = cfg.paths.socket
    info = {
        "socket": path,
        "exists": os.path.exists(path),
        "runtime_dir": cfg.paths.runtime_dir,
        "xdg": os.environ.get("XDG_RUNTIME_DIR"),
    }
    try:
        info["listing"] = sorted(x for x in os.listdir(cfg.paths.runtime_dir) if "switch" in x)
    except OSError:
        info["listing"] = []
    try:
        _call("ping")
        info["ok"] = True
    except DaemonError as e:
        info["ok"] = False
        info["error"] = str(e)
    return info
