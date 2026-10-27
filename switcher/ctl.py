# ctl.py - tiny CLI for the daemon IPC socket.
#   python -m switcher.ctl status
#   python -m switcher.ctl pause | resume | reload | ping | shutdown
#   python -m switcher.ctl learned | blocked | config
#   python -m switcher.ctl log 80
#   python -m switcher.ctl learned.add layout=us word=ult
#   python -m switcher.ctl learned.del layout=us word=ult
#   python -m switcher.ctl blocked.add layout=ru word=да
#   python -m switcher.ctl config.set min_len=2 debug=true
#
# Exclusions ("leave this program alone completely"):
#   python -m switcher.ctl exclude                     # both lists + active one
#   python -m switcher.ctl exclude.add pattern=dota2 mode=focus
#   python -m switcher.ctl exclude.del pattern=dota2 mode=focus
from __future__ import annotations

import json
import sys

from . import config as cfg
from .ipc import call


def _params(args):
    # Remaining args are key=value pairs; values are parsed as JSON when
    # possible, otherwise kept as strings.
    out = {}
    for a in args:
        if "=" not in a:
            raise SystemExit("expected key=value, got: " + a)
        k, v = a.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["status"]
    cmd = argv[0]
    rest = argv[1:]

    if cmd in ("learned", "blocked", "exclude"):
        cmd = cmd + ".list"
    elif cmd == "config":
        cmd = "config.get"
    elif cmd == "log":
        keep = rest
        rest = []
        cmd = "log.tail"
        if keep:
            rest = ["lines=" + keep[0]]

    params = _params(rest)
    try:
        data = call(cfg.paths.socket, cmd, params)
    except (FileNotFoundError, ConnectionError):
        print("daemon is not running (socket: " + cfg.paths.socket + ")")
        return 2
    except RuntimeError as e:
        print("error: " + str(e))
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
