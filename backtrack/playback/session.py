"""One process-wide mpv player, current track and queue that outlive any screen.
A background tick advances the queue and logs history. Joined windows drive it
over ipc via RemoteSession.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from types import SimpleNamespace

from backtrack.playback.libmpv import LOAD_ERROR as _MPV_LOAD_ERROR, mpv, player
from backtrack.playback import coreaudio
from backtrack import tuning as tune
from mutagen.id3 import ID3

from backtrack.history import log_listening_history
from backtrack import books, theme
from backtrack.id3.tag_formats import kind as tag_kind, load_id3, open_tags, parse_gain, texts
from backtrack.music_library import (chapter_at, drop_moved, get_song_duration,
                                    is_audiobook, library_entry, quality_text, track_title, first_text)
from backbone.files import write_text_atomic
from backbone.log import log, quietly

_TICK_INTERVAL_S = tune.TICK_INTERVAL_S
# ⏮ grace window: within the first few seconds of a track, previous steps back
# to the previous queue item; past it, it restarts the current one (the standard
# music-player behaviour).
_PREV_RESTART_AFTER_S = tune.PREV_RESTART_AFTER_S

# Repeat modes for the queue.
REPEAT_OFF = 'off'
REPEAT_ONE = 'one'
REPEAT_ALL = 'all'

# What happens after a track picked from a list (the `after_pick` setting):
# it plays alone, the rest of its list follows, or the queue carries on after it.
AFTER_PICK = ('stop', 'list', 'queue')

# Queue edits that change the queue itself, so undo can take them back.
_UNDOABLE = ('add', 'play_picked', 'move', 'remove', 'clear_upcoming', 'shuffle_upcoming')
_UNDO_DEPTH = tune.UNDO_DEPTH

# The queue is kept between runs (Resume on the home menu): saved after every
# edit and track change, on stop, and every SAVE_QUEUE_EVERY_S while playing.
_SAVE_QUEUE_EVERY_S = tune.SAVE_QUEUE_EVERY_S


def _queue_file():
    from backtrack.config import CONFIG_DIR
    return CONFIG_DIR / "queue.json"


def saved_queue() -> dict | None:
    """The queue a previous run left, for Resume, or None. Tracks that have
    moved since are left out; a broken file is removed (and logged)."""
    try:
        data = json.loads(_queue_file().read_text(encoding="utf-8"))
        queue, titles = list(data["queue"]), list(data.get("titles") or [])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("queue.json unreadable, dropped: %s", exc)
        forget_saved_queue()
        return None
    keep = [i for i, p in enumerate(queue) if isinstance(p, str) and os.path.isfile(p)]
    if not keep:
        forget_saved_queue()
        return None
    paths = [queue[i] for i in keep]
    names = [titles[i] if i < len(titles) else os.path.basename(queue[i]) for i in keep]
    playing = data.get("path")
    index = paths.index(playing) if playing in paths else max(0, min(int(data.get("index") or 0), len(paths) - 1))
    return {"queue": paths, "titles": names, "index": index,
            "elapsed": float(data.get("elapsed") or 0) if playing in paths else 0.0,
            "mode": data.get("mode") if data.get("mode") in (REPEAT_OFF, REPEAT_ONE, REPEAT_ALL) else REPEAT_OFF}


def repoint_saved_queue(moves: dict) -> None:
    """Follow renamed files (old path → new) in the queue a previous run left."""
    try:
        data = json.loads(_queue_file().read_text(encoding="utf-8"))
        queue = list(data["queue"])
    except FileNotFoundError:
        return
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("queue.json unreadable, renames not followed: %s", exc)
        return
    moved = {**data, "queue": [moves.get(p, p) for p in queue], "path": moves.get(data.get("path"), data.get("path"))}
    if moved != data:
        try:
            write_text_atomic(_queue_file(), json.dumps(moved))
        except OSError as exc:
            log.warning("queue.json not updated for renamed files: %s", exc)


def forget_saved_queue() -> None:
    with quietly():
        _queue_file().unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Low-level audio primitives.
# ---------------------------------------------------------------------------

def player_tags(path: str) -> ID3:
    """The tags the player shows and applies (details, credits, lyrics, gain):
    the file's own ID3 (an MP3, WAV or AIFF), else an ID3 view of any other
    file's tags, so one way of drawing and playing serves every format."""
    k = tag_kind(path)
    try:
        if k == 'id3':
            return load_id3(path)
        if k != 'unsupported':
            return _id3_view(path)
    except Exception as exc:
        log.info("tags unreadable in %s: %s", path, exc)
    return ID3()


# The library's stand-ins for a missing value, which aren't shown as values.
_UNSET = ('', '0', 'Unknown Artist', 'Unknown Album', 'Unknown Genre', 'Unknown Year')


def _id3_view(path: str) -> ID3:
    """An m4a/FLAC/Ogg/Opus file's tags as ID3 frames: the library's reading of
    them, plus its embedded lyrics (LRC text becomes timed SYLT) and ReplayGain.
    ponytail: a read-only view, never saved (it has no file name, so a save fails loudly)."""
    from mutagen import id3 as f
    from backtrack.lyrics.formats import parse_lrc_text
    e, view = library_entry(path), ID3()

    def add(frame, value, split=False):
        if str(value or '').strip() not in _UNSET:
            view.add(frame(encoding=3, text=str(value).split('; ') if split else [str(value)]))

    for frame, key, split in ((f.TIT2, 'title', False), (f.TPE1, 'artist', True), (f.TPE2, 'album_artist', True),
                              (f.TALB, 'album', False), (f.TCON, 'genre', True), (f.TCOM, 'composer', True),
                              (f.TEXT, 'lyricist', True), (f.TDRC, 'year', False), (f.TIT3, 'work', False),
                              (f.TSST, 'disc_subtitle', False), (f.MVNM, 'movement_name', False)):
        add(frame, e.get(key), split)
    for frame, num, total in ((f.TRCK, 'track', 'total_tracks'), (f.TPOS, 'disc', 'total_discs'),
                              (f.MVIN, 'movement_number', 'total_movements')):
        if str(e.get(num) or '0') != '0':
            add(frame, f"{e[num]}/{e[total]}" if str(e.get(total) or '0') != '0' else e[num])
    if e.get('credits'):
        view.add(f.TMCL(encoding=3, people=[[role, name] for name, role in e['credits']]))

    tags = open_tags(path)[0]
    if lyrics := "\n".join(texts(tags, 'lyrics')):
        lines = parse_lrc_text(lyrics)
        if any(ms is not None for _t, ms in lines):
            view.add(f.SYLT(encoding=3, lang='eng', format=2, type=1,
                            text=[(t, ms) for t, ms in lines if ms is not None]))
        else:
            view.add(f.USLT(encoding=3, lang='eng', desc='', text=lyrics))
    if gain := texts(tags, 'replaygain_track_gain'):
        view.add(f.TXXX(encoding=3, desc='REPLAYGAIN_TRACK_GAIN', text=[gain[0]]))
    return view


def _new_player():
    """The process's one player for the session."""
    if mpv is None:
        raise RuntimeError(f"mpv couldn't be loaded ({_MPV_LOAD_ERROR}); see `backtrack doctor`")
    return player()


def _seek(mp, seconds: float) -> None:
    """Move to `seconds` in, exactly (in an MP3 too). A seek mpv can't make
    (the track not open yet) is logged and dropped."""
    try:
        mp.seek(max(0.0, seconds), 'absolute', 'exact')
    except Exception as exc:                 # noqa: BLE001 (python-mpv raises SystemError and others)
        log.warning("seek to %.1f s failed: %s", seconds, exc)


def _handle_seek(mp, elapsed: float, duration: float, seek_amount: float) -> None:
    """Seek the player by ``seek_amount`` seconds, clamped to the track bounds."""
    _seek(mp, max(0.0, min(elapsed + seek_amount, max(duration - tune.SEEK_END_MARGIN_S, 0.0))))


def _audio_filters(audio) -> str:
    """The file's EQU2 bands and RVA2 master-channel gain as mpv's audio
    filters: each band an equalizer at its own frequency, the gain a volume
    ahead of them, the two combined rather than either replacing the other.
    Without this an RVA2 frame (the gain tag the trimmer's ReplayGain
    operation writes) would have no audible effect in backtrack's own player,
    the one place it's most obviously supposed to show. '' when the file has
    neither."""
    try:
        equ2_frames = audio.getall('EQU2')
    except Exception:
        equ2_frames = []
    adjustments = [pt for fr in equ2_frames for pt in (getattr(fr, 'adjustments', None) or [])]

    try:
        rva2_frames = audio.getall('RVA2')
    except Exception:
        rva2_frames = []
    preamp_gain = next((fr.gain for fr in rva2_frames if getattr(fr, 'channel', None) == 1), None)
    if preamp_gain is None:                  # ReplayGain from any tagger, and every non-MP3 format
        preamp_gain = next((parse_gain(fr.text[0]) for fr in audio.getall('TXXX')
                            if fr.desc.upper() == 'REPLAYGAIN_TRACK_GAIN' and fr.text), None)

    def db(gain: float) -> float:
        return float(max(-tune.EQ_GAIN_LIMIT_DB, min(tune.EQ_GAIN_LIMIT_DB, gain)))
    parts = [f"volume=volume={db(preamp_gain):g}dB"] if preamp_gain is not None else []
    parts += [f"equalizer=f={freq:g}:t=o:w=1:g={db(gain):g}" for freq, gain in adjustments if freq and freq > 0]
    return f"lavfi=[{','.join(parts)}]" if parts else ""


# Takeover (the `takeover` setting): music plays with the output to itself, at
# each file's own rate, its samples untouched: no filters, and mpv's volume at
# 100: the volume keys set the output's own instead (coreaudio.volume). Audiobooks never take over
# (their speed changes resample them anyway). On macOS that's mpv's
# coreaudio_exclusive; on Linux, PipeWire's exclusive mode. The macOS output's
# stereo float (libmpv._MAC_OUTPUT) stays: float holds a 16- or 24-bit sample
# exactly, so nothing is lost by it.
_TAKEOVER = {'audio-exclusive': 'yes', 'volume': '100'}
# mpv's sample formats (planar or not) as the Info panel says them.
_SAMPLE_FORMATS = {'u8': "8-bit", 's16': "16-bit", 's32': "32-bit", 's64': "64-bit",
                   'float': "32-bit float", 'double': "64-bit float"}


def _takeover_on() -> bool:
    """The takeover setting, as saved: read fresh, so a change made in any
    window counts."""
    from backtrack.config import load_config, setting
    return bool(setting(load_config(), 'takeover'))


def _rate_note():
    """Where takeover notes the output's rate before changing it (coreaudio)."""
    from backtrack.config import CONFIG_DIR
    return CONFIG_DIR / "takeover_rate.json"


# What the session reads of mpv's state, kept as mpv reports it (observed on
# its own thread), so reading it never waits on mpv: while mpv opens an output
# (takeover's exclusive one, a device change, a new rate) it answers nothing for
# up to half a second, and a screen asking it then would freeze.
_WATCHED = ('time-pos', 'core-idle', 'pause', 'idle-active', 'duration', 'audio-out-params', 'audio-device')


def _file_options(**options: str) -> str:
    """mpv's per-file options for loadfile, which hold only while that file
    plays: each value length-quoted (%n%), so a filter graph's commas and
    brackets stay its own."""
    return ",".join(f"{k}=%{len(v.encode())}%{v}" for k, v in options.items())


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def _load_stored_volume() -> int | None:
    """The volume saved in config, or None when none is stored / it can't be read."""
    try:
        from backtrack.config import load_config
        raw = load_config().get('volume')
        return None if raw is None else clamp_volume(raw)
    except Exception:
        return None


def clamp_volume(value) -> int:
    """Any reported volume → a sane 0-100 int, so a snapshot from another
    window carrying something odd can never render as "-1"."""
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, v))


def next_sleep_mode(mode):
    """The sleep timer setting after `mode`: off, each of SLEEP_MINUTES, end of chapter, off."""
    steps = [None, *tune.SLEEP_MINUTES, 'chapter']
    return steps[(steps.index(mode) + 1) % len(steps)] if mode in steps else steps[1]


class PlaybackSession:
    """Owns the process's mpv player, the current track, and the queue.

    Thread-safety: every method that touches the player or queue takes ``_lock``;
    the background tick and the foreground view both call through the same guarded
    methods, so only one advances the queue at a time. The view sets
    :attr:`view_attached` while it renders so the background tick backs off.
    """

    def __init__(self) -> None:
        self.mp = None                       # mpv.MPV | None, made on first play
        self._entry_id = None                # mpv's playlist entry for the track loaded
        self._ended_id = None                # the entry mpv last said ended (played out or failed)
        self._started_id = None              # the entry mpv last started
        self._next = None                    # (entry, queue position) mpv has lined up to follow, gapless
        self._seen: dict = {}                # mpv's _WATCHED properties, as it last reported them
        self._last_snapshot: dict | None = None   # now_playing's, for while the session is busy
        self.taken_over: bool = False        # the track playing has the output to itself
        self.file_path: str | None = None
        self.audio = None                    # mutagen ID3 of the current track (for the view + EQ)
        self.duration: float = 0.0
        self.entry: dict = {}                # the current track's library details
        self.chapters: list = []             # its [[start s, title], ...], if any
        self.is_book: bool = False           # it's in a directory set to Audiobooks
        self.rate: float = 1.0               # playback speed (books only; music plays at 1)
        self.sleep_mode = None               # sleep timer: None, minutes, or 'chapter'
        self._sleep_deadline: float = 0.0    # monotonic time a minutes timer runs out
        self.track_start: float = 0.0        # wall-clock start, for history logging
        self.is_grouping: bool = False
        self._history_logged: bool = True    # guard so each track logs at most once

        # The volume is kept here and given to each player: one level, kept
        # from the last run, whatever the player is doing.
        _stored = _load_stored_volume()
        self._volume: int = 100 if _stored is None else _stored   # 100: mpv's own level, unchanged
        self._config: dict | None = None      # the app's live config dict, if bound

        # Queue.
        self.queue: list[str] = []           # absolute file paths
        self.titles: list[str] = []          # display titles, parallel to queue
        self.index: int = 0
        self.mode: str = REPEAT_OFF
        self._undo: list[tuple] = []         # (queue, titles, path playing) before each edit
        self._saved_at: float = 0.0          # when the queue was last saved for Resume

        # A monotonically-increasing counter bumped whenever the current track
        # changes, so an attached view can cheaply detect "the track advanced".
        self.generation: int = 0

        self.view_attached: bool = False
        # Which window currently has the full player view open (its process
        # token), or None. Enforces "one player view at a time" across windows.
        self.view_holder: str | None = None
        self._lock = threading.RLock()
        self._clock = [0.0, 0.0, None]       # elapsed()'s own clock: base seconds, when, mpv's last reading
        self._tick_thread: threading.Thread | None = None
        self._tick_stop = threading.Event()
        self._server = None                  # ipc.SessionServer once advertised (#14)
        self._session_id: str | None = None  # reused across a hand-off to keep the socket

    # -- lifecycle ----------------------------------------------------------

    def is_active(self) -> bool:
        """Whether a track is currently loaded (playing or paused)."""
        return self.mp is not None and self.file_path is not None

    def _ensure_player(self):
        if self.mp is None:
            coreaudio.restore_rate(_rate_note())     # a rate a crashed run left changed
            self.mp = _new_player()
            self.mp.event_callback('end-file')(self._file_ended)
            self.mp.event_callback('start-file')(self._file_started)
            self._seen = {}
            for name in _WATCHED:
                self.mp.observe_property(name, self._observed)
        return self.mp

    def _observed(self, name: str, value) -> None:
        """mpv reporting one of _WATCHED, on its own thread; never takes the lock."""
        self._seen[name] = value

    def _mpv(self, name: str):
        """mpv's `name` (one of _WATCHED) as it last reported it; asked of mpv
        only until the first report (and of a stand-in player in tests)."""
        seen = self._seen
        return seen[name] if name in seen else getattr(self.mp, name.replace('-', '_'))

    def _file_started(self, event) -> None:
        """mpv's start-file, on its own thread: notes the entry, so tick() can
        see mpv moved on to the one lined up (_next). Never takes the lock."""
        self._started_id = event.as_dict(decoder=mpv.lazy_decoder).get('playlist_entry_id')

    def _file_ended(self, event) -> None:
        """mpv's end-file, on its own thread: notes an entry that played to its
        end or couldn't be played, for tick() to move on from. It never takes
        the lock (a caller holding it may be waiting on mpv)."""
        e = event.as_dict(decoder=mpv.lazy_decoder)
        if e.get('reason') == 'error':
            log.warning("mpv couldn't play entry %s (%s): %s", e.get('playlist_entry_id'), self.file_path,
                        e.get('file_error'))
        if e.get('reason') in ('eof', 'error'):
            self._ended_id = e.get('playlist_entry_id')

    def _ended(self) -> bool:
        """Whether the track loaded has played out or failed."""
        return self._entry_id is not None and self._ended_id == self._entry_id

    def start(self, path: str, queue: list[str] | None = None,
              titles: list[str] | None = None, index: int = 0,
              mode: str | None = None, is_grouping: bool = False,
              start_at: float | None = None) -> bool:
        """Start playing ``path`` (replacing any current track/queue). ``queue`` is
        the full list of paths this play belongs to (an album/selection) so
        track-end can auto-advance; ``index`` is ``path``'s position in it.
        ``start_at`` is where to begin; None resumes a started audiobook."""
        with self._lock:
            if queue is None:
                queue = [path]
                index = 0
            self.queue = list(queue)
            # Display titles for the queue view: use what the caller supplied
            # (e.g. play_queue's library titles), else read each file's own title,
            # not the bare filename, so a single-track play doesn't show its file
            # name in the queue (#14).
            self.titles = list(titles) if titles else [self._title_for(p) for p in self.queue]
            self.index = max(0, min(index, len(self.queue) - 1))
            if mode is not None:
                self.mode = mode
            self.is_grouping = is_grouping
            if start_at is None:
                start_at = self._book_resume()
            ok = self._load(self.queue[self.index], start_at)
            if ok:
                self._ensure_advertised()
            return ok

    def _book_resume(self) -> float:
        """Where the queue item about to play should start: a started audiobook
        resumes at its saved file and spot (the queue moves to that file), and
        anything else starts at 0."""
        path = self.queue[self.index]
        point = books.resume_point(path) if is_audiobook(path) else None
        if point is None:
            return 0.0
        saved, at = point
        if saved in self.queue:
            self.index = self.queue.index(saved)
        else:
            self.queue[self.index] = saved
            if self.index < len(self.titles):
                self.titles[self.index] = self._title_for(saved)
        if at >= 1:
            from backbone import ui
            ui.show_status(f"Resumed at {ui.format_time(int(at))}", tune.TOAST_MEDIUM_S)
        return at

    def _begin(self, path: str) -> int:
        """The session's side of a new track, whoever started it (_load, or
        mpv moving on by itself, _adopt): the last one logged and its book spot
        kept, then this one's tags, length, chapters and speed. Returns the
        track's generation."""
        self._log_history()
        self._save_book()
        audio = player_tags(path)
        try:
            duration = get_song_duration(path)
        except Exception:
            duration = 0.0
        self.file_path = path
        self.audio = audio
        self.entry = library_entry(path)
        with quietly():
            theme.apply(path)              # art mode: this cover's colours
        self.chapters = self.entry.get('chapters') or []
        self.is_book = is_audiobook(path)
        self.rate = books.rate_for(path) if self.is_book else 1.0
        self.duration = duration if duration and duration > 0 else 0.0
        self.taken_over = _takeover_on() and not self.is_book
        self._history_logged = False
        self.generation += 1
        self.track_start = time.time()
        return self.generation

    def _track_options(self, path: str, start_at: float = 0.0) -> str:
        """`path`'s own settings for mpv, held while it plays: its filters
        (_audio_filters), a book's speed, takeover's (_TAKEOVER) for music
        when it's on, and where to start."""
        book = is_audiobook(path)
        over = _takeover_on() and not book
        opts = {'af': "" if over else _audio_filters(player_tags(path)),
                'speed': f"{books.rate_for(path) if book else 1.0:g}"}
        if over:
            coreaudio.remember_rate(_rate_note(), self._output_uid())
            opts.update(_TAKEOVER)
        if start_at:
            opts['start'] = f"{start_at:.2f}"
        return _file_options(**opts)

    def reload(self) -> None:
        """Play the track again from where it is, with settings changed since
        it began (takeover, the output device), and line up the next with them."""
        if self.mp is not None:                  # outside the lock: mpv takes a moment to switch
            from backtrack.playback.libmpv import use_saved_device
            use_saved_device(self.mp)
        with self._lock:
            if self.is_active():
                self._load(self.file_path, self.elapsed())

    def _output_uid(self) -> str:
        """The CoreAudio UID of the output chosen (mpv's "coreaudio/<UID>"),
        '' for the system default."""
        name = str(self._mpv('audio-device') or "") if self.mp is not None else ""
        return name.split("/", 1)[1] if name.startswith("coreaudio/") else ""

    def _level(self) -> float:
        """The volume the track playing is at: fixed at 100 when taken over."""
        return 100 if self.taken_over else self._volume

    def _device_volume(self) -> float | None:
        """The output's own volume (0-1) while takeover has it, which the
        volume keys move then; None otherwise, or when the output has none."""
        if not (self.is_active() and self.taken_over):
            return None
        return coreaudio.volume(coreaudio.output(self._output_uid()))

    def volume_fixed(self) -> bool:
        """Whether the volume keys do nothing now: takeover has an output
        with no volume of its own."""
        return self.is_active() and self.taken_over and self._device_volume() is None

    def output_note(self) -> str:
        """What reaches the output, for the Info panel's Output line, laid out
        like the file's Quality line (quality_text) after its codec: always
        PCM, as mpv decodes everything first; then "→ Bluetooth" when the
        output is (macOS re-encodes it there, and doesn't say in what). ''
        before mpv has opened the output."""
        out = (self._mpv('audio-out-params') or {}) if self.is_active() else {}
        if not out:
            return ""
        depth = _SAMPLE_FORMATS.get(str(out.get('format')).rstrip('p'), "")
        note = " · ".join(["PCM", quality_text(out.get('samplerate') or 0, depth, out.get('channel-count') or 0)])
        return note + (" → Bluetooth" if coreaudio.is_bluetooth(coreaudio.output(self._output_uid())) else "")

    def _load(self, path: str, start_at: float = 0.0) -> bool:
        """Load and begin playing a single track `start_at` seconds in, logging
        the previous one first, and line up the one after it (_line_up)."""
        with self._lock:
            mp = self._ensure_player()
            gen = self._begin(path)
            need_length = not self.duration
            mp.volume = self._volume
            mp.pause = False
            self._ended_id = self._next = None
            self._entry_id = mp.command('loadfile', path, 'replace', -1,
                                        self._track_options(path, start_at))['playlist_entry_id']
            self._line_up()
            self._save_queue()

        # Only wait for mpv's length when mutagen couldn't give one (rare now
        # get_song_duration covers MP4 too). Released here, but every caller
        # (start/next/jump/prev) holds the reentrant lock around _load, so in
        # that rare case the wait does hold up other lock users.
        # ponytail: move the wait off the caller's lock if it ever matters.
        if need_length:
            try:
                mp.wait_for_property('duration', timeout=tune.LENGTH_WAIT_S)
            except TimeoutError:
                log.info("no length for %s from its tags or mpv", path)
            d = mp.duration or tune.DURATION_FALLBACK_S
            with self._lock:
                if self.generation == gen:   # still the same track
                    self.duration = d
        return True

    def _line_up(self) -> None:
        """Give mpv the track that plays after this one, so it goes straight on
        with no gap (gapless): whatever was lined up before goes. Called after
        every load and every change to what follows. Nothing is lined up at the
        end of the queue, or for a file moved since it was queued: tick() then
        moves on as it always has."""
        if self.mp is None or self._entry_id is None:
            return
        with quietly():
            self.mp.command('playlist-clear')          # all but the track playing
        self._next = None
        nxt = self._next_index(manual=False)
        if nxt is None or not os.path.isfile(self.queue[nxt]):
            return
        path = self.queue[nxt]
        try:
            entry = self.mp.command('loadfile', path, 'append', -1, self._track_options(path))['playlist_entry_id']
        except Exception as exc:                       # noqa: BLE001 (no gapless this once: tick moves on)
            log.warning("couldn't line up %s: %s", path, exc)
            return
        self._next = (entry, nxt)

    def _adopt(self) -> bool:
        """mpv moved on to the track lined up: the session follows (the queue
        position, the track's details, history), and lines up the next.
        Whether it had."""
        if self._next is None or self._started_id != self._next[0]:
            return False
        entry, self.index = self._next
        self._begin(self.queue[self.index])
        self._entry_id, self._next = entry, None
        if not self.duration:
            self.duration = (self._mpv('duration') if self.mp else 0) or tune.DURATION_FALLBACK_S
        self._line_up()
        self._save_queue()
        return True

    # -- multi-window advertising (#14) ---------------------------

    def _session_label(self) -> str:
        """A short human label for the session registry / join chooser."""
        np = self.now_playing()
        if not np:
            return "Session"
        return np.get('album') or np.get('artist') or np.get('title') or "Session"

    def _ensure_advertised(self) -> None:
        """Start advertising this session over IPC so other windows can join,
        unless this process has itself joined someone else's session."""
        if self._server is not None or is_client():
            return
        try:
            from backtrack.playback import ipc
            sid = self._session_id or uuid.uuid4().hex[:tune.ID_SLICE_LEN]
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
                       index=int(args.get('index', 0)), mode=args.get('mode'),
                       start_at=args.get('start_at'))
        elif name == 'pause':
            self.pause_toggle()
        elif name == 'next':
            self.next(manual=True)
        elif name == 'prev':
            self.prev()
        elif name == 'seek':
            self.seek(float(args.get('delta', 0)))
        elif name == 'seek_to':
            self.seek_to(float(args.get('at', 0)))
        elif name == 'set_rate':
            self.set_rate(float(args.get('rate', 1.0)))
        elif name == 'sleep':
            self.cycle_sleep()
        elif name == 'set_volume':
            self.set_volume(int(args.get('vol', self.get_volume())))
        elif name == 'stop':
            self.stop()
        elif name == 'reload':
            self.reload()
        elif name == 'enqueue':
            self.enqueue(args['path'], args.get('title'))
        elif name == 'play_next':
            self.play_next(args['path'], args.get('title'))
        elif name == 'queue_edit':
            args = dict(args)
            self.edit(args.pop('op', ''), **args)
        elif name == 'acquire_view':
            self.acquire_view(args.get('token', ''))
        elif name == 'release_view':
            self.release_view(args.get('token', ''))

    def _title_for(self, path: str) -> str:
        """A display title for a queued file when the caller supplied none."""
        return track_title(path, read_tags=True)

    def _log_history(self) -> None:
        """Log the currently-loaded track to listening history, at most once."""
        if self.file_path and not self._history_logged:
            with quietly():
                log_listening_history(self.file_path, self.track_start, time.time())
            self._history_logged = True

    def _save_queue(self) -> None:
        """Keep the queue and where it got to, for Resume next run. Only the
        window playing the audio saves: a joined one mirrors it."""
        if is_client() or not self.queue:
            return
        i = max(0, min(self.index, len(self.queue) - 1))
        data = {"queue": self.queue, "titles": self.titles, "index": i, "path": self.queue[i],
                "elapsed": round(self.elapsed(), 1) if self.is_active() else 0.0, "mode": self.mode}
        with quietly():
            write_text_atomic(_queue_file(), json.dumps(data))
        self._saved_at = time.monotonic()

    def _save_book(self) -> None:
        """Keep where the audiobook playing got to (books.json). Nothing in its
        first second, so a track just loaded can't wipe a saved spot; at its end,
        the book moves on to its next file."""
        if is_client() or not self.is_book or not self.file_path or self.mp is None:
            return
        at = self.elapsed()
        at_end = (self._ended()
                  or bool(self.duration and at >= self.duration - tune.SEEK_END_MARGIN_S))
        if at < 1 and not at_end:
            return
        with quietly():
            books.remember(self.file_path, at, self.rate, at_end)

    def stop(self) -> None:
        """Stop playback and clear the current track (the queue is kept so the UI
        can still show it). Audio ends; history for the current track is logged."""
        with self._lock:
            if self.is_active():
                self._save_queue()           # where it stopped, for Resume
                self._save_book()
            self._log_history()
            if self.mp is not None:
                with quietly():
                    self.mp.stop()
            self._entry_id = self._next = None
            self.file_path = None
            self.audio = None
            self.duration = 0.0
            self.generation += 1
            with quietly():
                theme.apply()                  # nothing playing: the chosen colours

    def shutdown(self) -> None:
        """Fully tear down the session (on app exit): stop audio, close the player."""
        with self._lock:
            self._tick_stop.set()
            if self._server is not None:
                with quietly():
                    self._server.stop()
                self._server = None
            self.stop()
            if self.mp is not None:
                with quietly():
                    self.mp.terminate()          # mpv gives the output back, at its own rate
                self.mp = None
                _rate_note().unlink(missing_ok=True)

    # -- transport ----------------------------------------------------------

    def pause_toggle(self) -> None:
        with self._lock:
            if self.mp is not None:
                self.mp.cycle('pause')
                self._save_book()

    def track(self) -> SimpleNamespace:
        """The current track and queue, read together under the lock: another
        thread switching tracks between separate reads could pair one track's
        path with another's tag and duration."""
        with self._lock:
            return SimpleNamespace(
                generation=self.generation, file_path=self.file_path or "",
                audio=self.audio, duration=self.duration, is_grouping=self.is_grouping,
                chapters=list(self.chapters), is_book=self.is_book, rate=self.rate,
                titles=list(self.titles), index=self.index, queue=list(self.queue))

    def is_paused(self) -> bool:
        return self.mp is not None and bool(self._mpv('pause'))

    def seek(self, seconds: float) -> None:
        with self._lock:
            if self.mp is not None:
                _handle_seek(self.mp, self.elapsed(), self.duration, seconds)

    def bind_config(self, config: dict) -> None:
        """Adopt the app's live config dict.

        The volume is written into it as well as to disk, so a long-held dict
        being saved later (on quit, or from the settings menu) can't put a stale
        level back over the one just chosen.
        """
        with self._lock:
            self._config = config
            config['volume'] = self._volume

    def _persist_volume(self, vol: int) -> None:
        """Save the volume for the next launch, best effort: an unwritable config
        must never interrupt playback."""
        if self._config is not None:
            self._config['volume'] = vol
        with quietly():
            from backtrack.config import load_config, update_config
            if load_config().get('volume') != vol:
                update_config({'volume': vol})

    def set_volume(self, vol: int) -> int:
        """Set the volume: backtrack's own, or under takeover the output's
        (the samples stay untouched; backtrack's own level is kept for later)."""
        with self._lock:
            vol = clamp_volume(vol)
            if self._device_volume() is not None:
                coreaudio.set_volume(coreaudio.output(self._output_uid()), vol / 100)
                return vol
            # Remembered even with no player yet, so a level chosen before
            # playback isn't silently lost: `play` re-applies it.
            changed = vol != self._volume
            self._volume = vol
            if self.mp is not None and not self.taken_over:
                self.mp.volume = vol
        if changed:
            self._persist_volume(vol)        # outside the lock: it touches disk
        return vol

    def get_volume(self) -> int:
        """The current volume, always 0-100: under takeover, the output's own."""
        device = self._device_volume()
        return self._volume if device is None else round(device * 100)

    def elapsed(self) -> float:
        """Elapsed seconds into the current track (0 if not playing), smooth.

        mpv's reading moves in uneven steps as audio is decoded, so read raw
        it jitters, and the lyrics with it. While playing, the clock runs on its
        own from the system clock at the playback rate, eased a little towards
        each new reading (CLOCK_EASE), and starts again from mpv's reading
        after a jump (CLOCK_JUMP_S: a seek, a stall) or while paused or
        loading (core-idle)."""
        if self.mp is None:
            return 0.0
        at = self._mpv('time-pos')
        if at is None:
            return 0.0
        now, clock = time.monotonic(), self._clock         # [base seconds, when, last reading]
        rate = self.rate or 1.0
        if self._mpv('core-idle') or clock[2] is None:
            clock[:] = [at, now, at]
            return at
        if at != clock[2]:
            error = at - (clock[0] + (now - clock[1]) * rate)
            if abs(error) > tune.CLOCK_JUMP_S:
                clock[:] = [at, now, at]
                return at
            clock[0] += error * tune.CLOCK_EASE
            clock[2] = at
        return clock[0] + (now - clock[1]) * rate

    def latest_at(self) -> float:
        """When the current state was sampled: 'now' for the live local player.
        Present so the local session and the remote proxy share one interface."""
        return time.time()

    def seek_to(self, seconds: float) -> None:
        """Seek to an absolute position (a chapter picked from the panel)."""
        with self._lock:
            if self.mp is not None:
                _seek(self.mp, seconds)

    def set_rate(self, rate: float) -> float | None:
        """Set an audiobook's speed (kept for that book); None for music."""
        with self._lock:
            if not self.is_book or self.mp is None:
                return None
            self.rate = round(min(max(rate, tune.RATE_MIN), tune.RATE_MAX), 1)
            self.mp.speed = self.rate
            self._save_book()
            self._line_up()
            return self.rate

    def cycle_sleep(self):
        """Step the sleep timer on (next_sleep_mode); returns the new setting."""
        with self._lock:
            self.sleep_mode = next_sleep_mode(self.sleep_mode)
            if isinstance(self.sleep_mode, int):
                self._sleep_deadline = time.monotonic() + self.sleep_mode * 60
            if self.sleep_mode is None and self.mp is not None:
                self.mp.volume = self._level()               # undo a fade in progress
            return self.sleep_mode

    def sleep_left(self) -> float | None:
        """Real seconds until the sleep timer pauses, or None when it's off. At
        end of chapter that's to the next chapter, or the end of the track."""
        if self.sleep_mode is None:
            return None
        if self.sleep_mode != 'chapter':
            return max(0.0, self._sleep_deadline - time.monotonic())
        at = self.elapsed()
        ci = chapter_at(self.chapters, at)
        end = self.chapters[ci + 1][0] if 0 <= ci < len(self.chapters) - 1 else self.duration
        return max(0.0, (end - at) / (self.rate or 1.0))

    def _sleep_tick(self) -> bool:
        """Fade over the timer's last seconds, then pause. True when it paused."""
        left = self.sleep_left()
        if left is None or self.mp is None:
            return False
        if self.is_paused():
            if self.sleep_mode != 'chapter' and left <= 0:
                self.sleep_mode = None          # ran out while already paused
            return False
        if left > tune.SLEEP_END_EARLY_S:
            if left < tune.SLEEP_FADE_S:
                self.mp.volume = self._level() * left / tune.SLEEP_FADE_S
            return False
        self.mp.pause = True
        self.mp.volume = self._level()
        self.sleep_mode = None
        self._save_book()
        self._save_queue()
        log.info("sleep timer paused %s at %.0fs", self.file_path, self.elapsed())
        from backbone import ui
        ui.show_status("Sleep timer: paused", tune.TOAST_LONG_S)
        return True

    def next(self, *, manual: bool = True) -> str | None:
        """Advance to the next queued track. ``manual`` ignores repeat-one (an
        explicit skip). Returns the new path, or None if the queue is exhausted
        (which stops playback). A manual skip in a file with chapters goes to
        the next chapter, until the last."""
        with self._lock:
            self._adopt()                    # mpv may have moved on since the last tick
            if manual and self.mp is not None:
                ci = chapter_at(self.chapters, self.elapsed())
                if 0 <= ci < len(self.chapters) - 1:
                    _seek(self.mp, self.chapters[ci + 1][0])
                    return self.file_path
            while True:
                if not self.queue:
                    self.stop()
                    forget_saved_queue()
                    return None
                nxt = self._next_index(manual)
                if nxt is None:
                    if album := self._queue_end_album():
                        self.queue, self.titles = album, [self._title_for(p) for p in album]
                        nxt = 0
                    else:
                        self.stop()
                        forget_saved_queue()     # played to the end: nothing to resume
                        return None
                if drop_moved([self.queue[nxt]]):
                    break
                # Moved or renamed since it was queued: it leaves the queue, and
                # the advance carries on to whatever followed it.
                del self.queue[nxt]
                if nxt < len(self.titles):
                    del self.titles[nxt]
                self.index = nxt - 1
                manual = True
            self.index = nxt
            self._load(self.queue[self.index])
            return self.file_path

    def _next_index(self, manual: bool) -> int | None:
        """The queue position that plays after this one (`manual`: a skip,
        which ignores repeat-one), or None at the end of the queue with repeat
        off. Changes nothing."""
        if self.mode == REPEAT_ONE and not manual:
            return self.index
        if self.index + 1 < len(self.queue):
            return self.index + 1
        return 0 if self.mode == REPEAT_ALL and self.queue else None

    def _queue_end_album(self) -> list[str]:
        """What follows the end of the queue when Settings says a random album
        does (not after an audiobook): its tracks, else nothing."""
        from backtrack.config import setting
        from backtrack.music_library import _live_config, live_library, random_album
        if setting(_live_config(), 'queue_end') != 'random_album' or self.is_book:
            return []
        library = live_library() or []
        return random_album([t['path'] for t in library], library, avoid=self.file_path)

    def jump(self, index: int) -> str | None:
        """Play queue position `index` now (a click in the queue pane). Returns
        the new path, or None if `index` is out of range."""
        with self._lock:
            if not 0 <= index < len(self.queue):
                return None
            self.index = index
            self._load(self.queue[self.index])
            return self.file_path

    def enqueue(self, path: str, title: str | None = None) -> int:
        """Append a track to the end of the queue. Returns the new queue length."""
        with self._lock:
            self.edit('add', paths=[path], titles=[title] if title else None, where='end')
            return len(self.queue)

    def play_next(self, path: str, title: str | None = None) -> None:
        """Insert a track to play immediately after the current one."""
        self.edit('add', paths=[path], titles=[title] if title else None, where='next')

    # -- queue editing ------------------------------------------------------

    def edit(self, op: str, **args) -> bool:
        """Every change to the queue goes through here, from this window, a
        joined one (the `queue_edit` ipc command) or the CLI. Returns whether
        anything changed. The ops are the `_edit_*` methods below."""
        with self._lock:
            self._adopt()                    # mpv may have moved on since the last tick
            fn = getattr(self, f"_edit_{op}", None)
            if fn is None:
                log.warning("unknown queue edit %r", op)
                return False
            before = (list(self.queue), list(self.titles), self.file_path)
            try:
                changed = bool(fn(**args))
            except (TypeError, ValueError, IndexError) as exc:
                log.warning("queue edit %s%r failed: %s", op, args, exc)
                return False
            if changed and op in _UNDOABLE:
                self._undo.append(before)
                del self._undo[:-_UNDO_DEPTH]
            if changed:
                self._line_up()
                self._save_queue()
                log.debug("queue edit %s: %d queued, at %d", op, len(self.queue), self.index)
            return changed

    def _at(self, pos: int, path: str) -> bool:
        """Whether queue position `pos` still holds `path`: an edit sent from
        another window can arrive after the queue moved on (an auto-advance),
        and must not land on a different track."""
        if 0 <= pos < len(self.queue) and self.queue[pos] == path:
            return True
        log.info("queue edit ignored: position %d no longer holds %s", pos, path)
        return False

    def _album_end(self) -> int:
        """Queue position of the last track in the run that shares the current
        track's album (and album artist): where "after this album" inserts."""
        def key(p):
            e = library_entry(p)
            return ((e.get('album') or '').casefold(), (e.get('album_artist') or '').casefold())
        end = self.index
        if not self.queue:
            return -1
        here = key(self.queue[self.index])
        while end + 1 < len(self.queue) and key(self.queue[end + 1]) == here:
            end += 1
        return end

    def _edit_add(self, paths: list, titles: list | None = None, where: str = 'end',
                  shuffled: bool = False) -> bool:
        """Insert tracks: next, at the end, or after the current album; shuffled
        among themselves first when asked, but kept together."""
        items = list(zip(paths, titles or [None] * len(paths)))
        if not items:
            return False
        if shuffled:
            random.shuffle(items)
        items = [(p, t or self._title_for(p)) for p, t in items]
        if where == 'next':
            pos = self.index + 1
        elif where == 'end':
            pos = len(self.queue)
        elif where == 'after_album':
            pos = self._album_end() + 1
        else:
            raise ValueError(f"where={where!r}")
        pos = max(0, min(pos, len(self.queue)))
        self.queue[pos:pos] = [p for p, _ in items]
        self.titles[pos:pos] = [t for _, t in items]
        return True

    def _edit_play_picked(self, paths: list, index: int, then: str = 'list',
                          titles: list | None = None) -> bool:
        """Play a track picked from a list: alone (`stop`), with the rest of its
        list after it (`list`), or now and then back to the queue (`queue`;
        with nothing playing that's the same as `list`)."""
        titles = list(titles) if titles else [self._title_for(p) for p in paths]
        path, title = paths[index], titles[index]
        if then == 'stop':
            return self.start(path, queue=[path], titles=[title])
        if then == 'queue' and self.is_active():
            self.queue.insert(self.index + 1, path)
            self.titles.insert(self.index + 1, title)
            self.index += 1
            return self._load(path, self._book_resume())
        return self.start(path, queue=list(paths), titles=titles, index=index)

    def _edit_jump(self, pos: int, path: str) -> bool:
        if not self._at(pos, path):
            return False
        self.index = pos
        return self._load(path)

    def _edit_move(self, pos: int, delta: int, path: str) -> bool:
        """Swap the track at `pos` with its neighbour above (-1) or below (+1)."""
        j = pos + delta
        if not self._at(pos, path) or not 0 <= j < len(self.queue):
            return False
        for lst in (self.queue, self.titles):
            lst[pos], lst[j] = lst[j], lst[pos]
        if self.index in (pos, j):
            self.index = j if self.index == pos else pos
        return True

    def _edit_remove(self, pos: int, path: str) -> bool:
        """Drop a track. Removing the one playing moves on to the next."""
        if not self._at(pos, path):
            return False
        del self.queue[pos]
        del self.titles[pos]
        if pos < self.index:
            self.index -= 1
        elif pos == self.index:
            if self.index < len(self.queue):
                self._load(self.queue[self.index])
            else:
                self.index = max(0, len(self.queue) - 1)
                self.stop()
        return True

    def _edit_clear_upcoming(self) -> bool:
        if len(self.queue) <= self.index + 1:
            return False
        del self.queue[self.index + 1:]
        del self.titles[self.index + 1:]
        return True

    def _edit_shuffle_upcoming(self) -> bool:
        """Shuffle what's still to play; audiobook files keep their places."""
        spots = [i for i in range(self.index + 1, min(len(self.queue), len(self.titles)))
                 if not is_audiobook(self.queue[i])]
        if len(spots) < 2:
            return False
        items = [(self.queue[i], self.titles[i]) for i in spots]
        random.shuffle(items)
        for i, (p, t) in zip(spots, items):
            self.queue[i], self.titles[i] = p, t
        return True

    def _edit_undo(self) -> bool:
        """Put the queue back as it was before the last edit. The track playing
        keeps playing: the cursor goes to it in the restored queue."""
        if not self._undo:
            return False
        queue, titles, _was = self._undo.pop()
        self.queue, self.titles = list(queue), list(titles)
        if self.file_path in self.queue:
            spots = [i for i, p in enumerate(self.queue) if p == self.file_path]
            self.index = min(spots, key=lambda i: abs(i - self.index))
        else:
            self.index = max(0, min(self.index, len(self.queue) - 1))
        return True

    def prev(self) -> str | None:
        """⏮: past the first ``_PREV_RESTART_AFTER_S`` seconds, restart the current
        track; within that window step back to the previous queue item (restarting
        this one anyway when it is the first). In a file with chapters the same
        goes for its chapters, back to the first. Returns the now-loaded path."""
        with self._lock:
            self._adopt()                    # mpv may have moved on since the last tick
            if not self.queue:
                return None
            if self.chapters and self.mp is not None:
                at = self.elapsed()
                ci = chapter_at(self.chapters, at)
                if at - self.chapters[ci][0] >= _PREV_RESTART_AFTER_S:
                    _seek(self.mp, self.chapters[ci][0])
                    return self.file_path
                if ci > 0:
                    _seek(self.mp, self.chapters[ci - 1][0])
                    return self.file_path
            if (self.index > 0 and self.mp is not None
                    and self.elapsed() >= _PREV_RESTART_AFTER_S):
                # Seek rather than reload: no audible gap, and pause state holds.
                _seek(self.mp, 0)
                return self.file_path
            self.index = max(0, self.index - 1)
            self._load(self.queue[self.index])
            return self.file_path

    # -- background tick ----------------------------------------------------

    def tick(self) -> str | None:
        """Advance the session's state machine once: if the current track has
        ended, log it and move to the next (or stop). Safe to call from either the
        background thread or the foreground view loop. Returns ``'changed'`` when
        the track advanced, ``'stopped'`` when the queue finished, ``'slept'``
        when the sleep timer paused it, else None."""
        with self._lock:
            if not self.is_active() or self.mp is None:
                return None
            if self._adopt():
                return 'changed'
            ended = self._ended()
            if self._next is not None and not (ended and self._mpv('idle-active')):
                # mpv goes on to the one lined up by itself, and _adopt follows;
                # unless it stopped first (a file that failed at once can end
                # before the next is lined up): then the session moves on.
                ended = False
            elif not ended and self.duration and self.elapsed() >= self.duration:
                ended = True
            if not ended:
                if self._sleep_tick():
                    return 'slept'
                if time.monotonic() - self._saved_at > _SAVE_QUEUE_EVERY_S:
                    self._save_queue()
                    self._save_book()
                return None
            result = self.next(manual=False)
            if result is None:
                return 'stopped'
            # next() advanced, repeated, or wrapped: in every case the track
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
        from backbone import ui
        while not self._tick_stop.is_set():
            with quietly():
                # The attached view drives ticking itself (so it can reload lyric
                # state on change); the background thread only advances when the
                # player is running unattended.
                if self.is_active() and not self.view_attached:
                    self.tick()
                    # Keep any menu's now-playing box live (clock + auto-advance)
                    # without waiting for a keystroke there (#14).
                    ui.pulse_footer()
            time.sleep(_TICK_INTERVAL_S)

    # -- snapshot for the now-playing bar / views ---------------------------

    def now_playing(self) -> dict | None:
        """A snapshot of the current track for the now-playing bar / views, or
        None when nothing is loaded. While the session is busy (a track
        loading, mpv switching outputs), the last one: a screen drawing it
        never waits on the session."""
        if not self._lock.acquire(timeout=tune.NOW_PLAYING_WAIT_S):
            return self._last_snapshot
        try:
            self._last_snapshot = self._snapshot()
        finally:
            self._lock.release()
        return self._last_snapshot

    def _snapshot(self) -> dict | None:
        with self._lock:
            if not self.is_active() or self.mp is None:
                return None
            title = artist = album = ''
            if self.audio is not None:
                def _one(fr) -> str:
                    """A single-identity frame's value (title, album)."""
                    return first_text(fr)

                def _all(fr) -> str:
                    """Every value of a list-like frame, in storage form ('; '-joined).

                    The UI renders it through `format_tag_values`, so a duet credit
                    reaches the now-playing bar whole instead of losing everyone
                    after the first name.
                    """
                    if not (fr and getattr(fr, 'text', None)):
                        return ''
                    return "; ".join(v for v in (str(x).strip() for x in fr.text) if v)

                title = _one(self.audio.get('TIT2'))
                artist = _all(self.audio.get('TPE1'))
                album = _one(self.audio.get('TALB'))
            if not title:
                title = track_title(self.file_path or '')
            return {
                'title': title,
                'artist': artist,
                'album': album,
                'elapsed': self.elapsed(),
                'duration': self.duration,
                'paused': self.is_paused(),
                'volume': clamp_volume(self.get_volume()),
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
                'rate': self.rate,
                'is_book': self.is_book,
                'sleep_mode': self.sleep_mode,
                'sleep_left': self.sleep_left(),
                'volume_fixed': self.volume_fixed(),
                'output': self.output_note(),
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

# A stable per-window identity for the player-view lock (#14).
_PROCESS_TOKEN = uuid.uuid4().hex[:tune.ID_SLICE_LEN]


def my_token() -> str:
    """This window's identity for claiming the player-view lock."""
    return _PROCESS_TOKEN


class RemoteSession:
    """Client-side proxy (#14): presents the same control surface as
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
        return clamp_volume(np.get('volume', 0)) if np else 0

    def start(self, path: str, queue: list | None = None, titles: list | None = None,
              index: int = 0, mode: str | None = None, is_grouping: bool = False,
              start_at: float | None = None) -> bool:
        return self._link.send('play', {'path': path, 'queue': queue, 'titles': titles,
                                        'index': index, 'mode': mode, 'start_at': start_at})

    def enqueue(self, path: str, title: str | None = None) -> int:
        self._link.send('enqueue', {'path': path, 'title': title})
        return 0                              # host owns the real length

    def play_next(self, path: str, title: str | None = None) -> None:
        self._link.send('play_next', {'path': path, 'title': title})

    def edit(self, op: str, **args) -> bool:
        """A queue edit (see PlaybackSession.edit), applied by the host; the
        result comes back in the next snapshot."""
        return self._link.send('queue_edit', {'op': op, **args})

    def pause_toggle(self) -> None:
        self._link.send('pause')

    def next(self, *, manual: bool = True) -> str | None:
        self._link.send('next')
        return None

    def prev(self) -> str | None:
        self._link.send('prev')
        return None

    def seek(self, seconds: float) -> None:
        """Ask the host to seek by ``seconds``; the new position arrives in a snapshot."""
        self._link.send('seek', {'delta': seconds})

    def seek_to(self, seconds: float) -> None:
        self._link.send('seek_to', {'at': seconds})

    def set_rate(self, rate: float) -> float | None:
        """Ask the host for a speed; it confirms (or ignores, for music) in a snapshot."""
        np = self._link.latest() or {}
        rate = round(min(max(rate, tune.RATE_MIN), tune.RATE_MAX), 1)
        self._link.send('set_rate', {'rate': rate})
        return rate if np.get('is_book') else None

    def cycle_sleep(self):
        self._link.send('sleep')
        return next_sleep_mode((self._link.latest() or {}).get('sleep_mode'))

    def set_volume(self, vol: int) -> int:
        """Ask the host to set the volume; returns the clamped level asked for."""
        vol = clamp_volume(vol)
        self._link.send('set_volume', {'vol': vol})
        return vol            # the host confirms via the next snapshot

    def stop(self) -> None:
        self._link.send('stop')

    def reload(self) -> None:
        self._link.send('reload')

    def volume_fixed(self) -> bool:
        return bool((self._link.latest() or {}).get('volume_fixed'))

    def acquire_view(self, token: str) -> None:
        self._link.send('acquire_view', {'token': token})

    def release_view(self, token: str) -> None:
        self._link.send('release_view', {'token': token})

    def latest_at(self) -> float:
        """When the last mirrored snapshot arrived (for elapsed interpolation)."""
        return self._link.latest_at()


# When this window has JOINED another window's session (#14), the local
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
    """True when another Backtrack window is part of this session right now:
    either we're a joined client (the host is another window) or we host and a
    client is connected. Used to pin the player view open while a separate browse
    window exists: you can't leave the player back to browse until the other
    window closes, keeping the two windows specialised (browse vs player) (#14)."""
    link = _client_link                  # the receiver thread can clear it mid-check
    if link is not None:
        return link.connected
    srv = SESSION._server
    return srv is not None and srv.peer_count() > 0


class AppLink:
    """This window's own session as a CLI command typed at its `:` line sees
    it: the calls a SessionClient makes on a running session, applied here (or
    sent on to the session this window joined)."""

    def send(self, name: str, args: dict | None = None) -> bool:
        if is_client():
            return _client_link.send(name, args or {})
        SESSION._handle_remote_command(name, args or {})
        if name == 'play':
            SESSION.start_background_tick()
        return True

    def latest(self):
        return active_session().now_playing()

    def close(self) -> None:
        pass


def active_session():
    """The session interface the UI should drive: the local :data:`SESSION` when
    hosting, or the :class:`RemoteSession` proxy when joined to another window."""
    remote = _remote                     # the receiver thread can clear it mid-check
    return remote if remote is not None else SESSION


def current_now_playing() -> dict | None:
    """The now-playing snapshot to display here: the remote host's when joined,
    otherwise this process's own session."""
    link = _client_link                  # the receiver thread can clear it mid-check
    if link is not None:
        return link.latest()
    return SESSION.now_playing()


# ---------------------------------------------------------------------------
# Host hand-off (#14): when a joined window's host disappears, the
# surviving windows elect one to take over and resume near the same spot.
# ---------------------------------------------------------------------------

def attempt_handoff(session_id: str, socket_path: str) -> None:
    """A joined window's host went away (quit or crashed). Elect a new host: the
    winner (an exclusive lock file) rebuilds playback from the last snapshot and
    re-hosts on the *same* socket; losers reconnect to the new host. Runs on the
    dead client link's receiver thread."""
    from backtrack.playback import ipc
    link = _client_link
    snap = link.latest() if link is not None else None
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
        with quietly():
            old.close()
    if not snap or not snap.get('file_path'):
        return                                   # nothing to resume → session ends
    SESSION._session_id = session_id             # keep the socket so losers reconnect
    SESSION.start(snap['file_path'],
                  queue=snap.get('queue') or [snap['file_path']],
                  titles=snap.get('titles'),
                  index=int(snap.get('index', 0)),
                  mode=snap.get('mode'),
                  is_grouping=bool(snap.get('is_grouping', False)),
                  start_at=float(snap.get('elapsed') or 0.0))
    with quietly():
        if snap.get('paused'):
            SESSION.pause_toggle()
    SESSION.start_background_tick()


def client_link(socket_path: str, session_id: str):
    """A (not yet connected) link to the session host, set up the one way every
    join uses: each mirrored snapshot repaints this window's now-playing box, and
    losing the host starts the hand-off election."""
    from backtrack.playback import ipc
    from backbone import ui
    return ipc.SessionClient(
        socket_path,
        on_snapshot=lambda _snap: ui.pulse_footer(),
        on_disconnect=lambda: attempt_handoff(session_id, socket_path),
    )


def _rejoin_after_handoff(session_id: str, socket_path: str) -> None:
    """Lose the election → wait for the winner to re-host, then reconnect to it."""
    from backtrack.playback import ipc
    for _ in range(tune.HANDOFF_POLL_TRIES):     # grace period for the winner to rebind
        time.sleep(tune.HANDOFF_POLL_INTERVAL_S)
        if ipc._connectable(socket_path):
            link = client_link(socket_path, session_id)
            if link.connect():
                set_client_link(link)
                return
    set_client_link(None)                        # winner never appeared → session gone
