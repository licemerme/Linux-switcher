# ipc.py - JSON-lines over a unix socket in $XDG_RUNTIME_DIR.
# Request: {"cmd": "...", "params": {...}}
# Reply:   {"ok": true, "data": ...} or {"ok": false, "error": "..."}
# Push:    {"event": "correct", ...} after the client sends {"cmd":"subscribe"}
from __future__ import annotations

import json
import os
import socket
import threading


class IPCServer:
    # Single-threaded accept loop; one daemon thread per client.
    def __init__(self, path, dispatch, log=None):
        self.path = path
        self.dispatch = dispatch
        self.log = log or (lambda m: None)
        self._stop = threading.Event()
        self._subs = set()
        self._lock = threading.Lock()
        self._srv = None

    def start(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        s.listen(8)
        s.settimeout(0.5)
        self._srv = s
        threading.Thread(target=self._loop, name="ipc", daemon=True).start()
        self.log("ipc: listening on " + self.path)

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        buf = b""
        try:
            conn.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle(conn, line)
        finally:
            with self._lock:
                self._subs.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, conn, line):
        try:
            req = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(conn, {"ok": False, "error": "bad json"})
            return
        cmd = req.get("cmd", "")
        if cmd == "subscribe":
            with self._lock:
                self._subs.add(conn)
            self._send(conn, {"ok": True, "data": {"subscribed": True}})
            return
        try:
            data = self.dispatch(cmd, req.get("params") or {})
            self._send(conn, {"ok": True, "data": data})
        except KeyError as e:
            self._send(conn, {"ok": False, "error": "unknown command: " + str(e)})
        except Exception as e:
            self._send(conn, {"ok": False, "error": type(e).__name__ + ": " + str(e)})

    @staticmethod
    def _send(conn, obj):
        try:
            conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

    def broadcast(self, obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            subs = list(self._subs)
        for c in subs:
            try:
                c.sendall(line)
            except OSError:
                with self._lock:
                    self._subs.discard(c)

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except (OSError, AttributeError):
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def call(path, cmd, params=None, timeout=5.0):
    # Blocking one-shot request (used by the CLI and by the tray UI).
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        req = {"cmd": cmd, "params": params or {}}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "ipc error"))
        return reply.get("data")
    finally:
        s.close()
