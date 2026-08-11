"""Local IPC for multi-window shared playback sessions (feature #14, Phase 2).

One process **hosts** a session (owns the VLC player, Phase 1's ``PlaybackSession``)
and runs a :class:`SessionServer` on a per-session Unix socket under
``$CONFIG_DIR/sessions/``. Other windows **discover** live sessions
(:func:`list_sessions`), pick one, and connect a :class:`SessionClient` to mirror
the host's now-playing snapshots and (Phase 2b) send transport/queue commands.

Each host also writes a small JSON registry file so the launch chooser can label
sessions (and show what's playing) without opening a connection. Everything is a
local Unix socket in the user's own config dir — no network exposure.

This module is pure transport + discovery; it holds no playback logic, so it can
be unit-tested headlessly with a fake command handler / snapshot source. macOS &
Linux (AF_UNIX); a Windows loopback-TCP fallback is a later concern.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time

from src.config import CONFIG_DIR

SESSIONS_DIR = CONFIG_DIR / "sessions"

_BROADCAST_INTERVAL_S = 0.25
_CONNECT_TIMEOUT_S = 0.4


# ---------------------------------------------------------------------------
# Wire protocol: newline-delimited JSON objects
# ---------------------------------------------------------------------------

def _send(sock: socket.socket, obj: dict) -> None:
    """Write one JSON message (newline-terminated) to a socket."""
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def _iter_messages(sock: socket.socket):
    """Yield decoded JSON messages from a socket until it closes."""
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue


# ---------------------------------------------------------------------------
# Registry / discovery
# ---------------------------------------------------------------------------

def _socket_path(session_id: str) -> str:
    return str(SESSIONS_DIR / f"{session_id}.sock")


def _registry_path(session_id: str) -> str:
    return str(SESSIONS_DIR / f"{session_id}.json")


def _pid_alive(pid: int) -> bool:
    """Whether a process with ``pid`` is still running."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists but not ours to signal
    except OSError:
        return False
    return True


def _connectable(sock_path: str) -> bool:
    """Whether a Unix socket is currently accepting connections."""
    if not os.path.exists(sock_path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(_CONNECT_TIMEOUT_S)
    try:
        s.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def list_sessions() -> list[dict]:
    """Every live session advertised in the registry (newest first).

    Probes each entry's socket; removes registry/socket files for sessions whose
    host has gone (stale after a crash), so the chooser only shows joinable ones.
    """
    try:
        entries = sorted(SESSIONS_DIR.glob("*.json"))
    except OSError:
        return []
    live: list[dict] = []
    for reg in entries:
        try:
            info = json.loads(reg.read_text("utf-8"))
        except (OSError, ValueError):
            _cleanup(reg.stem)
            continue
        sock_path = info.get("socket") or _socket_path(info.get("id", ""))
        if _connectable(sock_path):
            live.append(info)
        elif not _pid_alive(int(info.get("pid", 0) or 0)):
            _cleanup(info.get("id", reg.stem))
    live.sort(key=lambda i: i.get("started_at", 0), reverse=True)
    return live


def _cleanup(session_id: str) -> None:
    """Remove a session's registry + socket files (best effort)."""
    for p in (_registry_path(session_id), _socket_path(session_id)):
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Host side
# ---------------------------------------------------------------------------

class SessionServer:
    """Serves one hosted session: accepts client connections, broadcasts
    now-playing snapshots, and forwards received commands to a handler.

    ``snapshot_provider()`` returns the current now-playing dict (or None);
    ``command_handler(name, args)`` applies a client command to the host session;
    ``label_provider()`` returns a short human label for the registry.
    """

    def __init__(self, session_id: str, *, snapshot_provider, command_handler,
                 label_provider=None, started_at: float = 0.0) -> None:
        self.session_id = session_id
        self.socket_path = _socket_path(session_id)
        self._snapshot = snapshot_provider
        self._on_command = command_handler
        self._label = label_provider or (lambda: "Session")
        self._started_at = started_at or time.time()
        self._srv: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Bind the socket, write the registry, and spawn the accept/broadcast threads."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.remove(self.socket_path)          # clear any stale socket
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.socket_path)
        srv.listen(8)
        srv.settimeout(0.3)
        self._srv = srv
        self._write_registry()
        for target in (self._accept_loop, self._broadcast_loop):
            t = threading.Thread(target=target, name=f"ipc-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)

    def _write_registry(self) -> None:
        snap = None
        try:
            snap = self._snapshot()
        except Exception:
            snap = None
        info = {
            "id": self.session_id,
            "label": self._label(),
            "socket": self.socket_path,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "now_playing": snap,
        }
        try:
            with open(_registry_path(self.session_id), "w", encoding="utf-8") as f:
                json.dump(info, f)
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._srv is not None:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self._clients.add(conn)
            t = threading.Thread(target=self._client_loop, args=(conn,), daemon=True)
            t.start()

    def peer_count(self) -> int:
        """Number of client windows currently connected to this hosted session."""
        with self._clients_lock:
            return len(self._clients)

    def _client_loop(self, conn: socket.socket) -> None:
        try:
            for msg in _iter_messages(conn):
                if msg.get("t") == "cmd":
                    try:
                        self._on_command(msg.get("name", ""), msg.get("args") or {})
                    except Exception:
                        pass
        finally:
            with self._clients_lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _broadcast_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._snapshot()
            except Exception:
                snap = None
            msg = {"t": "snapshot", "data": snap}
            with self._clients_lock:
                dead = []
                for c in self._clients:
                    try:
                        _send(c, msg)
                    except OSError:
                        dead.append(c)
                for c in dead:
                    self._clients.discard(c)
            self._write_registry()             # keep the chooser label fresh
            self._stop.wait(_BROADCAST_INTERVAL_S)

    def stop(self) -> None:
        """Shut down the server and remove this session's registry/socket files."""
        self._stop.set()
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        _cleanup(self.session_id)


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

class SessionClient:
    """Connects to a hosted session: mirrors its now-playing snapshots and sends
    commands. ``on_snapshot(dict|None)`` fires on each update; ``on_disconnect()``
    fires when the host goes away (used later for hand-off)."""

    def __init__(self, socket_path: str, *, on_snapshot=None, on_disconnect=None) -> None:
        self.socket_path = socket_path
        self._on_snapshot = on_snapshot
        self._on_disconnect = on_disconnect
        self._sock: socket.socket | None = None
        self._latest: dict | None = None
        self._latest_at: float = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = False

    def connect(self) -> bool:
        """Connect and start the receiver thread. Returns success."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(_CONNECT_TIMEOUT_S)
        try:
            s.connect(self.socket_path)
        except OSError:
            s.close()
            return False
        s.settimeout(None)
        self._sock = s
        self.connected = True
        threading.Thread(target=self._recv_loop, name="ipc-client", daemon=True).start()
        return True

    def _recv_loop(self) -> None:
        assert self._sock is not None
        for msg in _iter_messages(self._sock):
            if self._stop.is_set():
                break
            if msg.get("t") == "snapshot":
                with self._lock:
                    self._latest = msg.get("data")
                    self._latest_at = time.time()
                if self._on_snapshot:
                    try:
                        self._on_snapshot(self._latest)
                    except Exception:
                        pass
        self.connected = False
        if not self._stop.is_set() and self._on_disconnect:
            try:
                self._on_disconnect()
            except Exception:
                pass

    def latest(self) -> dict | None:
        """The most recent now-playing snapshot from the host (or None)."""
        with self._lock:
            return self._latest

    def latest_at(self) -> float:
        """Wall-clock time the latest snapshot arrived (for elapsed interpolation)."""
        with self._lock:
            return self._latest_at

    def send(self, name: str, args: dict | None = None) -> bool:
        """Send a command to the host. Returns False if the link is down."""
        if self._sock is None or not self.connected:
            return False
        try:
            _send(self._sock, {"t": "cmd", "name": name, "args": args or {}})
            return True
        except OSError:
            self.connected = False
            return False

    def close(self) -> None:
        """Stop the receiver and close the connection."""
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.connected = False
