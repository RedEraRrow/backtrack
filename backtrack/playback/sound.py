"""A track's sound, for the panel that draws it (the visualiser).

ffmpeg decodes the track once, in the background, and it's reduced as it
arrives to a few small arrays indexed by time: frequency bands, left and
right levels and peaks, and a quiet copy of the waveform. Drawing a frame is
then a lookup at the player's own clock, so it stays with the audio through a
pause, a seek or a change of speed, and the playback itself is untouched.
A panel shows what's been decoded so far; a long episode is ready in seconds.
"""
from __future__ import annotations

import subprocess
import threading

import numpy as np

from backbone.log import log

RATE = 22050            # decoded at, in Hz: enough for every band drawn
FPS = 30                # analysis frames a second
HOP = RATE // FPS       # samples a frame moves on
WINDOW = 2048           # samples each spectrum is taken over
BANDS = 48              # frequency bands, log-spaced 40 Hz – 10 kHz
SCOPE_STEP = 2          # the waveform kept at RATE / SCOPE_STEP, for the oscilloscope
FLOOR_DB = -70.0        # what reads as silence

_EDGES = np.unique(np.clip(np.round(np.geomspace(40, 10000, BANDS + 1) * WINDOW / RATE).astype(int),
                           1, WINDOW // 2))
_HANN = np.hanning(WINDOW).astype(np.float32)


def _db_byte(x: np.ndarray, ref: float = 1.0) -> np.ndarray:
    """Amplitudes as 0-255, FLOOR_DB → 0 and full scale → 255."""
    db = 20 * np.log10(np.maximum(x / ref, 1e-9))
    return np.clip((db - FLOOR_DB) / -FLOOR_DB * 255, 0, 255).astype(np.uint8)


class Sound:
    """One track's analysis, filling in as ffmpeg decodes it."""

    def __init__(self, path: str, duration: float, ffmpeg: str):
        n = int((max(duration, 1.0) + 10) * FPS)
        self.path = path
        self.frames = 0                                  # analysed so far
        self.expected = max(1, int(duration * FPS))
        self.done = False
        self.bands = np.zeros((n, len(_EDGES) - 1), np.uint8)
        self.levels = np.zeros((n, 4), np.uint8)         # rms L, rms R, peak L, peak R
        self.scope = np.zeros(n * HOP // SCOPE_STEP + HOP, np.int8)
        self._proc = None
        self._stop = False
        threading.Thread(target=self._run, args=(ffmpeg,), daemon=True, name="sound").start()

    def _grow(self, frames: int) -> None:
        if frames <= len(self.levels):
            return
        n = frames + 30 * FPS
        self.bands = np.resize(self.bands, (n, self.bands.shape[1]))
        self.levels = np.resize(self.levels, (n, 4))
        self.scope = np.resize(self.scope, n * HOP // SCOPE_STEP + HOP)

    def _run(self, ffmpeg: str) -> None:
        cmd = [ffmpeg, '-v', 'error', '-nostdin', '-i', self.path, '-map', '0:a:0',
               '-ac', '2', '-ar', str(RATE), '-f', 'f32le', '-']
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            log.warning("couldn't start ffmpeg for %s: %s", self.path, exc)
            self.done = True
            return
        block = HOP * FPS * 4                            # four seconds at a time
        tail = np.zeros(WINDOW - HOP, np.float32)        # the previous block's end, for the first windows
        try:
            while not self._stop:
                raw = self._proc.stdout.read(block * 8)
                if not raw:
                    break
                st = np.frombuffer(raw[:len(raw) // 8 * 8], np.float32).reshape(-1, 2)
                n = len(st) // HOP
                if not n:
                    continue
                st = st[:n * HOP]
                mono = st.mean(axis=1)
                f0 = self.frames
                self._grow(f0 + n)
                # Spectrum: a window ending at each frame's end, Hann-shaped.
                ext = np.concatenate([tail, mono])
                wins = np.lib.stride_tricks.sliding_window_view(ext, WINDOW)[::HOP][:n]
                mag = np.abs(np.fft.rfft(wins * _HANN, axis=1)) / (WINDOW / 4)
                csum = np.cumsum(mag ** 2, axis=1)
                energy = (csum[:, _EDGES[1:] - 1] - csum[:, _EDGES[:-1] - 1]) / np.diff(_EDGES)
                self.bands[f0:f0 + n] = _db_byte(np.sqrt(energy))
                # Levels: each frame's RMS and peak, left and right.
                fr = st.reshape(n, HOP, 2)
                self.levels[f0:f0 + n, 0:2] = _db_byte(np.sqrt((fr ** 2).mean(axis=1)))
                self.levels[f0:f0 + n, 2:4] = _db_byte(np.abs(fr).max(axis=1))
                s0 = f0 * HOP // SCOPE_STEP
                q = np.clip(mono[::SCOPE_STEP] * 127, -127, 127).astype(np.int8)
                self.scope[s0:s0 + len(q)] = q
                tail = ext[-(WINDOW - HOP):]
                self.frames = f0 + n
        except Exception as exc:                          # a half-read file: what's done stays
            log.warning("sound analysis of %s stopped: %s", self.path, exc)
        finally:
            self.done = True
            try:
                self._proc.kill()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop = True
        try:
            if self._proc:
                self._proc.kill()
        except Exception:
            pass

    def frame(self, t: float) -> int | None:
        """The analysis frame for time `t`, None when it isn't decoded yet."""
        i = int(t * FPS)
        return i if 0 <= i < self.frames else None

    def wave(self, t: float, n: int) -> np.ndarray | None:
        """`n` samples of the waveform ending at `t` (-127..127), None if not decoded."""
        i = int(t * RATE / SCOPE_STEP)
        if i > self.frames * HOP // SCOPE_STEP or i < n:
            return None
        return self.scope[i - n:i]

    @property
    def progress(self) -> float:
        return 1.0 if self.done else min(1.0, self.frames / self.expected)


_current: list = []          # recent analyses, oldest first: the one before, this one, the next


def available() -> bool:
    from backtrack.trim.engine import _resolve_ffmpeg
    return bool(_resolve_ffmpeg())


def for_track(path: str, duration: float) -> Sound | None:
    """The track's analysis, started now if it isn't already; None without ffmpeg."""
    for s in _current:
        if s.path == path:
            return s
    from backtrack.trim.engine import _resolve_ffmpeg
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg or not path:
        return None
    for other in [o for o in _current if not o.done]:      # a half-done one (skipped past): dropped, not kept short
        other.stop()
        _current.remove(other)
    s = Sound(path, duration, ffmpeg)
    _current.append(s)
    while len(_current) > 3:
        _current.pop(0).stop()
    return s


def prefetch(path: str, duration: float) -> None:
    """Start the next track's analysis, so its visualiser is ready the moment
    it starts. One decode at a time: only once nothing else is decoding."""
    if path and all(s.done for s in _current) and not any(s.path == path for s in _current):
        for_track(path, duration)
