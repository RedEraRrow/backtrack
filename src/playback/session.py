"""Shared playback session — one self-driving VLC player that persists across
screens (feature #14, Phase 1: single-window background audio).

The problem the old design had: ``music_player`` created the VLC player as a
local variable and stopped it in its ``finally``, so audio could not outlive the
player view. This module promotes the player to a process-wide **session**: it
owns the VLC instance/player, the current track, and the queue, and a background
tick advances the queue on track-end and logs history — independent of whichever
screen is foregrounded. The full player view (``playback.music_player``) attaches
to render and control it; *leaving the view no longer stops the audio*, only an
explicit stop does.

Only audio + queue + lifecycle live here. Lyric parsing and the rich rendering
stay in the view, which reads ``session.audio`` and reloads when the track
changes. Phase 2 (multi-window) will make this session the thing an IPC layer
shares, so it is deliberately the single source of truth.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import vlc
from mutagen.id3 import ID3
import mutagen.id3

from src.history import log_listening_history
from src.music_library import get_song_duration

# vlc.State attributes are dynamic; expose safe aliases (mirrors playback.py).
_VLC_STATE_PAUSED = getattr(vlc.State, 'Paused', None)
_VLC_STATE_ERROR = getattr(vlc.State, 'Error', None)
_VLC_STATE_ENDED = getattr(vlc.State, 'Ended', None)
_VLC_STATE_STOPPED = getattr(vlc.State, 'Stopped', None)

_VLC_PLAY_SETTLE_S = 0.3
_TICK_INTERVAL_S = 0.1

# Repeat modes for the queue.
REPEAT_OFF = 'off'
REPEAT_ONE = 'one'
REPEAT_ALL = 'all'


# ---------------------------------------------------------------------------
# Low-level audio primitives (moved here so the session is the lowest layer;
# the view imports them from here).
# ---------------------------------------------------------------------------

def _new_instance() -> vlc.Instance:
    """Create the single reusable libvlc instance for the process."""
    inst = vlc.Instance('--no-video', '--quiet')
    assert inst is not None
    return inst


def _handle_seek(mp, elapsed: float, duration: float, seek_amount: float) -> None:
    """Seek the player by ``seek_amount`` seconds, clamped to the track bounds."""
    target = max(0.0, min(elapsed + seek_amount, max(duration - 0.5, 0.0)))
    mp.set_time(int(target * 1000))


def _apply_equalizer(mp, audio) -> bool:
    """Apply the file's EQU2 equalisation to playback via libvlc's equaliser.

    Each stored (frequency, gain) point is snapped to the nearest libvlc band, so
    absent bands stay flat. Returns True if an equaliser was applied.
    """
    try:
        frames = audio.getall('EQU2')
    except Exception:
        frames = []
    adjustments = [pt for fr in frames for pt in (getattr(fr, 'adjustments', None) or [])]
    if not adjustments:
        return False
    try:
        eq = vlc.AudioEqualizer()
        count = vlc.libvlc_audio_equalizer_get_band_count()
        band_freqs = [vlc.libvlc_audio_equalizer_get_band_frequency(i) for i in range(count)]
        for freq, gain in adjustments:
            if not freq or freq <= 0:
                continue

            def _ratio(k: int) -> float:
                """Distance of band k from freq as a >=1 ratio, for nearest-band matching."""
                r = band_freqs[k] / freq
                return r if r >= 1.0 else 1.0 / r
            idx = min(range(count), key=_ratio)
            eq.set_amp_at_index(float(max(-20.0, min(20.0, gain))), idx)  # type: ignore[reportOptionalMemberAccess]
        return bool(mp.set_equalizer(eq) == 0)
    except Exception:
        return False


def _set_stderr_to_null() -> int:
    """Redirect fd 2 to /dev/null to silence VLC's native stderr spam; returns the
    saved original fd (for :func:`_restore_stderr`)."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    return old_stderr


def _restore_stderr(old_stderr: int) -> None:
    """Restore fd 2 from the value saved by :func:`_set_stderr_to_null`."""
    os.dup2(old_stderr, 2)
    os.close(old_stderr)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

class PlaybackSession:
    """Owns the process's VLC player, the current track, and the queue.

    Thread-safety: every method that touches the player or queue takes ``_lock``;
    the background tick and the foreground view both call through the same guarded
    methods, so only one advances the queue at a time. The view sets
    :attr:`view_attached` while it renders so the background tick backs off.
    """

    def __init__(self) -> None:
        self._instance: vlc.Instance | None = None
        self.mp = None                       # vlc.MediaPlayer | None
        self.file_path: str | None = None
        self.audio = None                    # mutagen ID3 of the current track (for the view + EQ)
        self.duration: float = 0.0
        self.track_start: float = 0.0        # wall-clock start, for history logging
        self.is_grouping: bool = False
        self._history_logged: bool = True    # guard so each track logs at most once
        self._old_stderr: int | None = None

        # Queue.
        self.queue: list[str] = []           # absolute file paths
        self.titles: list[str] = []          # display titles, parallel to queue
        self.index: int = 0
        self.mode: str = REPEAT_OFF

        # A monotonically-increasing counter bumped whenever the current track
        # changes, so an attached view can cheaply detect "the track advanced".
        self.generation: int = 0

        self.view_attached: bool = False
        # Which window currently has the full player view open (its process
        # token), or None. Enforces "one player view at a time" across windows.
        self.view_holder: str | None = None
        self._lock = threading.RLock()
        self._tick_thread: threading.Thread | None = None
        self._tick_stop = threading.Event()
        self._server = None                  # ipc.SessionServer once advertised (#14 Phase 2)
        self._session_id: str | None = None  # reused across a hand-off to keep the socket

    # -- lifecycle ----------------------------------------------------------

    def is_active(self) -> bool:
        """Whether a track is currently loaded (playing or paused)."""
        return self.mp is not None and self.file_path is not None

    def _ensure_instance(self) -> vlc.Instance:
        if self._instance is None:
            self._instance = _new_instance()
        if self._old_stderr is None:
            self._old_stderr = _set_stderr_to_null()
        return self._instance

    def start(self, path: str, queue: list[str] | None = None,
              titles: list[str] | None = None, index: int = 0,
              mode: str | None = None, is_grouping: bool = False) -> bool:
        """Start playing ``path`` (replacing any current track/queue). ``queue`` is
        the full list of paths this play belongs to (an album/selection) so
        track-end can auto-advance; ``index`` is ``path``'s position in it."""
        with self._lock:
            if queue is None:
                queue = [path]
                index = 0
            self.queue = list(queue)
            # Display titles for the queue view: use what the caller supplied
            # (e.g. play_queue's library titles), else read each file's own title
            # — NOT the bare filename (which made a single-track play show its
            # file name in the queue, #14).
            self.titles = list(titles) if titles else [self._title_for(p) for p in self.queue]
            self.index = max(0, min(index, len(self.queue) - 1))
            if mode is not None:
                self.mode = mode
            self.is_grouping = is_grouping
            ok = self._load(self.queue[self.index])
            if ok:
                self._ensure_advertised()
            return ok

    def _load(self, path: str) -> bool:
        """Load and begin playing a single track, logging the previous one first.
        Returns False if the file can't be read."""
        with self._lock:
            # Finish accounting for whatever was playing.
            self._log_history()
            try:
                audio = ID3(path)
            except (FileNotFoundError, OSError, mutagen.id3.ID3NoHeaderError):  # type: ignore[reportPrivateImportUsage]
                try:
                    audio = ID3()
                except Exception:
                    return False
            try:
                duration = get_song_duration(path)
            except Exception:
                duration = 0.0

            inst = self._ensure_instance()
            media = inst.media_new(path)
            if self.mp is None:
                self.mp = inst.media_player_new()
            assert self.mp is not None
            self.mp.set_media(media)

            self.file_path = path
            self.audio = audio
            self.duration = duration if duration and duration > 0 else 0.0
            self._history_logged = False
            self.generation += 1
            gen = self.generation
            mp = self.mp
            need_vlc_len = not self.duration

            mp.play()
            _apply_equalizer(mp, audio)     # right after play() so it takes (#74)
            self.track_start = time.time()

        # Outside the lock: only settle + probe VLC for a length when mutagen
        # couldn't give us a duration (rare now get_song_duration covers MP4 too).
        # Keeps the 0.3s VLC settle from freezing every lock-guarded op
        # (now_playing / seek / volume / tick / IPC dispatch) on each track change.
        if need_vlc_len:
            time.sleep(_VLC_PLAY_SETTLE_S)
            vlc_len = mp.get_length()
            d = vlc_len / 1000.0 if vlc_len > 0 else 999.0
            with self._lock:
                if self.generation == gen:   # still the same track
                    self.duration = d
        return True

    # -- multi-window advertising (#14 Phase 2) ---------------------------

    def _session_label(self) -> str:
        """A short human label for the session registry / join chooser."""
        np = self.now_playing()
        if not np:
            return "Session"
        return np.get('album') or np.get('artist') or np.get('title') or "Session"

    def _ensure_advertised(self) -> None:
        """Start advertising this session over IPC so other windows can join —
        unless this process has itself joined someone else's session."""
        if self._server is not None or is_client():
            return
        try:
            from src.playback import ipc
            sid = self._session_id or uuid.uuid4().hex[:8]
            self._session_id = sid
            self._server = ipc.SessionServer(
                sid,
                snapshot_provider=self.now_playing,
                command_handler=self._handle_remote_command,
                label_provider=self._session_label,
            )
            self._server.start()
        except Exception:
            self._server = None

    def _handle_remote_command(self, name: str, args: dict) -> None:
        """Apply a command received from a joined client window to this session."""
        if name == 'play':
            self.start(args['path'], queue=args.get('queue'), titles=args.get('titles'),
                       index=int(args.get('index', 0)), mode=args.get('mode'))
        elif name == 'pause':
            self.pause_toggle()
        elif name == 'next':
            self.next(manual=True)
        elif name == 'prev':
            self.prev()
        elif name == 'seek':
            self.seek(float(args.get('delta', 0)))
        elif name == 'set_volume':
            self.set_volume(int(args.get('vol', self.get_volume())))
        elif name == 'stop':
            self.stop()
        elif name == 'enqueue':
            self.enqueue(args['path'], args.get('title'))
        elif name == 'play_next':
            self.play_next(args['path'], args.get('title'))
        elif name == 'acquire_view':
            self.acquire_view(args.get('token', ''))
        elif name == 'release_view':
            self.release_view(args.get('token', ''))

    def _title_for(self, path: str) -> str:
        """A display title for a queued file — its ID3 title, else the file name
        (no extension). Used when the caller didn't supply queue titles."""
        try:
            fr = ID3(path).get('TIT2')
            if fr is not None and getattr(fr, 'text', None):
                s = str(fr.text[0]).strip()
                if s:
                    return s
        except Exception:
            pass
        return os.path.splitext(os.path.basename(path))[0]

    def _log_history(self) -> None:
        """Log the currently-loaded track to listening history, at most once."""
        if self.file_path and not self._history_logged:
            try:
                log_listening_history(self.file_path, self.track_start, time.time())
            except Exception:
                pass
            self._history_logged = True

    def stop(self) -> None:
        """Stop playback and clear the current track (the queue is kept so the UI
        can still show it). Audio ends; history for the current track is logged."""
        with self._lock:
            self._log_history()
            if self.mp is not None:
                try:
                    self.mp.stop()
                except Exception:
                    pass
            self.file_path = None
            self.audio = None
            self.duration = 0.0
            self.generation += 1

    def shutdown(self) -> None:
        """Fully tear down the session (on app exit): stop audio and restore stderr."""
        with self._lock:
            self._tick_stop.set()
            if self._server is not None:
                try:
                    self._server.stop()
                except Exception:
                    pass
                self._server = None
            self.stop()
            if self._old_stderr is not None:
                try:
                    _restore_stderr(self._old_stderr)
                except Exception:
                    pass
                self._old_stderr = None

    # -- transport ----------------------------------------------------------

    def pause_toggle(self) -> None:
        with self._lock:
            if self.mp is not None:
                self.mp.pause()

    def is_paused(self) -> bool:
        return self.mp is not None and self.mp.get_state() == _VLC_STATE_PAUSED

    def seek(self, seconds: float) -> None:
        with self._lock:
            if self.mp is not None:
                _handle_seek(self.mp, self.elapsed(), self.duration, seconds)

    def set_volume(self, vol: int) -> int:
        with self._lock:
            vol = max(0, min(100, int(vol)))
            if self.mp is not None:
                self.mp.audio_set_volume(vol)
            return vol

    def get_volume(self) -> int:
        return self.mp.audio_get_volume() if self.mp is not None else 0

    def elapsed(self) -> float:
        """Elapsed seconds into the current track (0 if not playing)."""
        if self.mp is None:
            return 0.0
        ms = self.mp.get_time()
        return ms / 1000.0 if ms >= 0 else 0.0

    def latest_at(self) -> float:
        """When the current state was sampled — 'now' for the live local player.
        Present so the local session and the remote proxy share one interface."""
        return time.time()

    def seek_to(self, seconds: float) -> None:
        """Seek to an absolute position (used when a new host resumes a track)."""
        with self._lock:
            if self.mp is not None:
                self.mp.set_time(int(max(0.0, seconds) * 1000))

    def next(self, *, manual: bool = True) -> str | None:
        """Advance to the next queued track. ``manual`` ignores repeat-one (an
        explicit skip). Returns the new path, or None if the queue is exhausted
        (which stops playback)."""
        with self._lock:
            if not self.queue:
                return None
            nxt = self.index + 1
            if self.mode == REPEAT_ONE and not manual:
                nxt = self.index
            elif nxt >= len(self.queue):
                if self.mode == REPEAT_ALL:
                    nxt = 0
                else:
                    self.stop()
                    return None
            self.index = nxt
            self._load(self.queue[self.index])
            return self.file_path

    def enqueue(self, path: str, title: str | None = None) -> int:
        """Append a track to the end of the queue. Returns the new queue length."""
        with self._lock:
            self.queue.append(path)
            self.titles.append(title or self._title_for(path))
            return len(self.queue)

    def play_next(self, path: str, title: str | None = None) -> None:
        """Insert a track to play immediately after the current one."""
        with self._lock:
            pos = self.index + 1
            self.queue.insert(pos, path)
            self.titles.insert(pos, title or self._title_for(path))

    def prev(self) -> str | None:
        """Go to the previous queued track (or restart the current one)."""
        with self._lock:
            if not self.queue:
                return None
            self.index = max(0, self.index - 1)
            self._load(self.queue[self.index])
            return self.file_path

    # -- background tick ----------------------------------------------------

    def tick(self) -> str | None:
        """Advance the session's state machine once: if the current track has
        ended, log it and move to the next (or stop). Safe to call from either the
        background thread or the foreground view loop. Returns ``'changed'`` when
        the track advanced, ``'stopped'`` when the queue finished, else None."""
        with self._lock:
            if not self.is_active() or self.mp is None:
                return None
            state = self.mp.get_state()
            ended = state in (_VLC_STATE_ENDED, _VLC_STATE_STOPPED, _VLC_STATE_ERROR)
            if not ended and self.duration and self.elapsed() >= self.duration:
                ended = True
            if not ended:
                return None
            result = self.next(manual=False)
            if result is None:
                return 'stopped'
            # next() advanced, repeated, or wrapped — in every case the track
            # (re)loaded, so the view must refresh.
            return 'changed'

    def start_background_tick(self) -> None:
        """Launch the daemon thread that advances the queue when no view is
        attached (idempotent)."""
        if self._tick_thread is not None and self._tick_thread.is_alive():
            return
        self._tick_stop.clear()
        t = threading.Thread(target=self._tick_loop, name='playback-tick', daemon=True)
        self._tick_thread = t
        t.start()

    def _tick_loop(self) -> None:
        from src.utils import ui_utils
        while not self._tick_stop.is_set():
            try:
                # The attached view drives ticking itself (so it can reload lyric
                # state on change); the background thread only advances when the
                # player is running unattended.
                if self.is_active() and not self.view_attached:
                    self.tick()
                    # Keep any menu's now-playing box live (clock + auto-advance)
                    # without waiting for a keystroke there (#14).
                    ui_utils.pulse_now_playing()
            except Exception:
                pass
            time.sleep(_TICK_INTERVAL_S)

    # -- snapshot for the now-playing bar / views ---------------------------

    def now_playing(self) -> dict | None:
        """A snapshot of the current track for the now-playing bar / views, or
        None when nothing is loaded."""
        with self._lock:
            if not self.is_active() or self.mp is None:
                return None
            title = artist = album = ''
            if self.audio is not None:
                t = self.audio.get('TIT2')
                a = self.audio.get('TPE1')
                al = self.audio.get('TALB')
                title = str(t.text[0]) if (t and getattr(t, 'text', None)) else ''
                artist = str(a.text[0]) if (a and getattr(a, 'text', None)) else ''
                album = str(al.text[0]) if (al and getattr(al, 'text', None)) else ''
            if not title:
                title = os.path.splitext(os.path.basename(self.file_path or ''))[0]
            return {
                'title': title,
                'artist': artist,
                'album': album,
                'elapsed': self.elapsed(),
                'duration': self.duration,
                'paused': self.is_paused(),
                'volume': self.get_volume(),
                'index': self.index,
                'count': len(self.queue),
                'file_path': self.file_path,
                'generation': self.generation,
                'view_holder': self.view_holder,
                # Full queue state so a joined window can take over on host-loss (#14 2d).
                'queue': list(self.queue),
                'titles': list(self.titles),
                'mode': self.mode,
                'is_grouping': self.is_grouping,
            }

    # -- player-view lock (one open view across all windows) ---------------

    def acquire_view(self, token: str) -> bool:
        """Claim the player-view lock for ``token``. Succeeds if free or already
        ours; fails if another window holds it."""
        with self._lock:
            if self.view_holder is None or self.view_holder == token:
                self.view_holder = token
                return True
            return False

    def release_view(self, token: str) -> None:
        """Release the player-view lock if ``token`` holds it."""
        with self._lock:
            if self.view_holder == token:
                self.view_holder = None


# The process-wide singleton. Import and use `SESSION` everywhere.
SESSION = PlaybackSession()

# A stable per-window identity for the player-view lock (#14 Phase 2c).
_PROCESS_TOKEN = uuid.uuid4().hex[:8]


def my_token() -> str:
    """This window's identity for claiming the player-view lock."""
    return _PROCESS_TOKEN


class RemoteSession:
    """Client-side proxy (#14 Phase 2b): presents the same control surface as
    :class:`PlaybackSession`, but forwards each action to the host over the IPC
    link and reads now-playing from the link's mirrored snapshots. Commands are
    fire-and-forget; the resulting state comes back via the next snapshot."""

    def __init__(self, link) -> None:
        self._link = link

    def now_playing(self) -> dict | None:
        return self._link.latest()

    def is_active(self) -> bool:
        return self._link.latest() is not None

    def get_volume(self) -> int:
        np = self._link.latest()
        return int(np.get('volume', 0)) if np else 0

    def start(self, path: str, queue: list | None = None, titles: list | None = None,
              index: int = 0, mode: str | None = None, is_grouping: bool = False) -> bool:
        return self._link.send('play', {'path': path, 'queue': queue, 'titles': titles,
                                        'index': index, 'mode': mode})

    def enqueue(self, path: str, title: str | None = None) -> int:
        self._link.send('enqueue', {'path': path, 'title': title})
        return 0                              # host owns the real length

    def play_next(self, path: str, title: str | None = None) -> None:
        self._link.send('play_next', {'path': path, 'title': title})

    def pause_toggle(self) -> None:
        self._link.send('pause')

    def next(self, *, manual: bool = True) -> str | None:
        self._link.send('next')
        return None

    def prev(self) -> str | None:
        self._link.send('prev')
        return None

    def seek(self, seconds: float) -> None:
        self._link.send('seek', {'delta': seconds})

    def set_volume(self, vol: int) -> int:
        self._link.send('set_volume', {'vol': int(vol)})
        return int(vol)

    def stop(self) -> None:
        self._link.send('stop')

    def acquire_view(self, token: str) -> None:
        self._link.send('acquire_view', {'token': token})

    def release_view(self, token: str) -> None:
        self._link.send('release_view', {'token': token})

    def latest_at(self) -> float:
        return self._link.latest_at()


# When this window has JOINED another window's session (#14 Phase 2), the local
# SESSION stays idle; now-playing mirrors the host and control routes through a
# RemoteSession proxy. Set by the launch chooser.
_client_link = None                          # ipc.SessionClient | None
_remote: RemoteSession | None = None


def set_client_link(link) -> None:
    """Mark this process as a client of a remote session (or clear it)."""
    global _client_link, _remote
    _client_link = link
    _remote = RemoteSession(link) if link is not None else None


def is_client() -> bool:
    """Whether this window is joined to (mirroring) another window's session."""
    return _client_link is not None


def has_other_windows() -> bool:
    """True when another Backtrack window is part of this session right now —
    either we're a joined client (the host is another window) or we host and a
    client is connected. Used to pin the player view open while a separate browse
    window exists: you can't leave the player back to browse until the other
    window closes, keeping the two windows specialised (browse vs player) (#14)."""
    if _client_link is not None:
        return _client_link.connected
    srv = SESSION._server
    return srv is not None and srv.peer_count() > 0


def active_session():
    """The session interface the UI should drive: the local :data:`SESSION` when
    hosting, or the :class:`RemoteSession` proxy when joined to another window."""
    return _remote if _remote is not None else SESSION


def current_now_playing() -> dict | None:
    """The now-playing snapshot to display here: the remote host's when joined,
    otherwise this process's own session."""
    if _client_link is not None:
        return _client_link.latest()
    return SESSION.now_playing()


# ---------------------------------------------------------------------------
# Host hand-off (#14 Phase 2d): when a joined window's host disappears, the
# surviving windows elect one to take over and resume near the same spot.
# ---------------------------------------------------------------------------

def attempt_handoff(session_id: str, socket_path: str) -> None:
    """A joined window's host went away (quit or crashed). Elect a new host: the
    winner (an exclusive lock file) rebuilds playback from the last snapshot and
    re-hosts on the *same* socket; losers reconnect to the new host. Runs on the
    dead client link's receiver thread."""
    from src.playback import ipc
    snap = _client_link.latest() if _client_link is not None else None
    lock_path = ipc._host_lock_path(session_id)   # single source; swept by ipc._cleanup
    won = False
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        won = True
    except (FileExistsError, OSError):
        won = False
    if won:
        try:
            _become_host_from(session_id, snap)
        finally:
            try:
                os.remove(lock_path)
            except OSError:
                pass
    else:
        _rejoin_after_handoff(session_id, socket_path)


def _become_host_from(session_id: str, snap: dict | None) -> None:
    """Win the election → stop being a client and re-host the session on the same
    socket, resuming the current track at its last-known position."""
    old = _client_link
    set_client_link(None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    if not snap or not snap.get('file_path'):
        return                                   # nothing to resume → session ends
    SESSION._session_id = session_id             # keep the socket so losers reconnect
    SESSION.start(snap['file_path'],
                  queue=snap.get('queue') or [snap['file_path']],
                  titles=snap.get('titles'),
                  index=int(snap.get('index', 0)),
                  mode=snap.get('mode'),
                  is_grouping=bool(snap.get('is_grouping', False)))
    try:
        SESSION.seek_to(float(snap.get('elapsed') or 0.0))
        if snap.get('paused'):
            SESSION.pause_toggle()
    except Exception:
        pass
    SESSION.start_background_tick()


def _rejoin_after_handoff(session_id: str, socket_path: str) -> None:
    """Lose the election → wait for the winner to re-host, then reconnect to it."""
    from src.playback import ipc
    for _ in range(25):                          # ~2.5 s for the winner to rebind
        time.sleep(0.1)
        if ipc._connectable(socket_path):
            link = ipc.SessionClient(
                socket_path,
                on_disconnect=lambda: attempt_handoff(session_id, socket_path))
            if link.connect():
                set_client_link(link)
                return
    set_client_link(None)                        # winner never appeared → session gone
