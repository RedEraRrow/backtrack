"""The lyric pane: what is on screen at a moment of a track.

Rewritten around four rules, because the previous version broke each of them and
every bug traced back to one of the breaks.

1.  THE AUDIO POSITION IS THE ONLY CLOCK.  Nothing counts lines, remembers where
    it was, or nudges itself by an offset.  A seek is not an event to handle; it
    is just a different number arriving next tick, so scrubbing needs no special
    case and cannot desynchronise.

2.  THE TIMELINE TILES THE TRACK.  Beats are contiguous and never overlap: each
    one ends exactly where the next begins.  Every instant therefore belongs to
    exactly one beat, and finding it is one bisect with no tie to break.  Gaps
    are what made the old display show the next line during a silence, and the
    fix was patching windows after the fact, repeatedly.  Here it is an invariant
    established once, when the timeline is built, and asserted.

3.  A FRAME IS A PURE FUNCTION OF (TIME, GEOMETRY).  No incremental drawing, no
    "has the index changed" test.  Redrawing is decided by comparing the frame to
    the one on screen, so a resize repaints for the same reason a new line does:
    the frame is different.  No keypress required, and nothing to invalidate.

4.  ONE TIMELINE FOR EVERY SOURCE.  A dialogue transcript, SYLT and USLT differ
    only in how they are read off disk.  They become the same `Beat` list and
    share one resolver and one renderer, so they cannot drift apart in behaviour.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from src import tuning as tune
from src.utils import prompt_core as _pc
from src.utils import ui_utils
from src.utils.ui_utils import Colors as C

LINE, DIRECTION, SILENCE = 'line', 'direction', 'silence'


@dataclass(frozen=True)
class Beat:
    """One thing that is on screen for one stretch of the track."""
    start: float
    end: float
    kind: str = LINE
    speaker: str = ''          # who is talking
    aside: str = ''            # the script's manner note for the whole line
    text: str = ''             # what they say, markdown intact
    before: tuple = ()         # directions that introduce it
    after: tuple = ()          # directions that interrupt or follow it

    @property
    def blank(self) -> bool:
        return not (self.text or self.before or self.after or self.speaker)


@dataclass(frozen=True)
class Geometry:
    """Where the pane is and how big — everything the renderer needs of the screen."""
    row: int
    col: int
    width: int
    bottom: int

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.row + 1)


def _tile(beats: list[Beat], duration: float) -> list[Beat]:
    """Turn a sorted run of beats into a tiling of the track.

    The boundary between two beats is ONE number — the instant the second takes
    over from the first — so it is computed once. Deriving it twice, once as an
    end and again as a start, is how a beat ends up owning a moment its neighbour
    also claims, or neither of them does.

    Each boundary sits a short run-up BEFORE the beat that follows it, because a
    line arriving exactly on its first word arrives too late to read. Where there
    is silence the run-up comes out of the silence; where there is none it comes
    out of the tail of the line before, which stays on screen (dimmed) while its
    last word finishes. A direction gives up nothing: it is short, it is the thing
    being read, and the line after it needs no run-up because the eye is already
    on the text column.
    """
    if not beats:
        return []
    beats = sorted(beats, key=lambda b: (b.start, b.end))
    lead = tune.LYRIC_LEAD_IN_S

    bounds = [0.0]                                  # the track starts somewhere
    for i, b in enumerate(beats[:-1]):
        nxt = beats[i + 1].start
        if b.kind == DIRECTION:
            earliest = b.end                        # keeps its whole beat
        else:
            earliest = b.end - min(lead, (b.end - b.start) / 2)
        earliest = max(earliest, b.start, bounds[-1])
        bounds.append(max(bounds[-1], min(nxt, max(earliest, nxt - lead))))
    bounds.append(max(duration, beats[-1].end, bounds[-1]))

    return [Beat(**{**b.__dict__, 'start': bounds[i], 'end': bounds[i + 1]})
            for i, b in enumerate(beats)]


class Timeline:
    """A track's beats, tiled, with the one lookup everything uses."""

    def __init__(self, beats: list[Beat], duration: float = 0.0):
        self.beats = _tile(beats, duration)
        self._ends = [b.end for b in self.beats]
        # The invariant the rest of the module relies on, checked once rather
        # than assumed in a dozen places.
        assert all(a.end == b.start for a, b in zip(self.beats, self.beats[1:])), \
            "timeline is not contiguous"

    def __bool__(self) -> bool:
        return bool(self.beats)

    def index_at(self, elapsed: float) -> int:
        """Which beat owns this instant. Clamped, so before the first and after
        the last resolve to the first and last rather than to nothing."""
        if not self.beats:
            return -1
        return min(bisect.bisect_right(self._ends, elapsed), len(self.beats) - 1)


# --- building one from each source ------------------------------------------

def from_chunks(chunks: list[dict], times: list[tuple], duration: float) -> Timeline:
    """From the dialogue transcript, via `lyrics._chunks_from_segments`."""
    beats = []
    for c, (a, b) in zip(chunks, times):
        kind = SILENCE if c.get('is_air') else (DIRECTION if c.get('is_stage') else LINE)
        beats.append(Beat(start=float(a), end=float(b), kind=kind,
                          speaker=(c.get('speaker') or '').strip(),
                          aside=(c.get('stage_dir') or '').strip() if kind != DIRECTION else '',
                          text=(c.get('stage_dir') if kind == DIRECTION
                                else c.get('text') or '').strip(),
                          before=tuple(c.get('pre') or ()),
                          after=tuple(c.get('cues') or ())))
    return Timeline(beats, duration)


def from_sylt(entries: list[tuple], duration: float) -> Timeline:
    """From SYLT: timestamped lines, so each runs until the next one starts."""
    beats = []
    for i, (text, ms) in enumerate(entries):
        start = ms / 1000.0
        end = (entries[i + 1][1] / 1000.0 if i + 1 < len(entries)
               else start + tune.LYRIC_FABRICATED_END_MS / 1000.0)
        beats.append(Beat(start=start, end=end, text=(text or '').strip()))
    return Timeline(beats, duration)


def from_uslt(lines: list[str], line_times: list[tuple], duration: float) -> Timeline:
    """From USLT: unsynced lyrics paced over the track by word count."""
    beats = [Beat(start=float(a), end=float(b), text=(t or '').strip())
             for t, (a, b) in zip(lines, line_times)]
    return Timeline(beats, duration)


# --- drawing -----------------------------------------------------------------

def _speaker_rows(beat: Beat, width: int, active: bool) -> list[str]:
    """The speaker column: name, and the script's manner note under it."""
    if not beat.speaker:
        return []
    name = C.BOLD + ui_utils.truncate_text(beat.speaker, width) + C.RESET
    rows = [name if active else f"{C.DIM}{ui_utils.truncate_text(beat.speaker, width)}{C.RESET}"]
    if beat.aside:
        rows.append(f"{C.DIM}{C.ITALIC}"
                    f"{ui_utils.truncate_text('(' + beat.aside + ')', width)}{C.RESET}")
    return rows


def _text_rows(beat: Beat, width: int, active: bool) -> list[str]:
    """The text column: what is introduced, what is said, what interrupts.

    In that order, always. A direction that introduces a line reads above it —
    the chime sounds before the announcement — and one that interrupts reads
    below. Getting this backwards put the effect before its cause.
    """
    from src.lyrics.lyrics import _dir_rows, _md_rows
    base = '' if active else C.DIM
    rows: list[str] = []
    for d in beat.before:
        rows += _dir_rows(d, width, base, active)
    if beat.kind == DIRECTION:
        rows += _dir_rows(beat.text, width, base, active)
    elif beat.text:
        rows += _md_rows(beat.text, width, base=base, active=active) or ['']
    for d in beat.after:
        rows += _dir_rows(d, width, base, active)
    return rows


def frame(timeline: Timeline, elapsed: float, geom: Geometry) -> list[str]:
    """The pane's contents at this instant: exactly `geom.height` lines.

    Pure. Same time and same geometry always give the same frame, which is what
    lets the caller decide to repaint by comparing frames instead of tracking
    what it drew last.
    """
    blank = [''] * geom.height
    if not timeline or geom.height <= 0:
        return blank
    cur = timeline.index_at(elapsed)
    if cur < 0:
        return blank

    pad = geom.width - 2
    spk_w = max(12, min(pad // 3, 26))
    txt_w = max(20, pad - spk_w - 5)

    out: list[str] = [f"{C.DIM}{'─' * max(0, geom.width)}{C.RESET}", '']
    for idx in (cur - 1, cur, cur + 1):
        if not 0 <= idx < len(timeline.beats):
            continue
        beat = timeline.beats[idx]
        if beat.kind == SILENCE or beat.blank:
            if idx == cur:
                out.append('')
            continue
        active = idx == cur
        left = _speaker_rows(beat, spk_w, active)
        right = _text_rows(beat, txt_w, active)
        for r in range(max(len(left), len(right))):
            lhs = left[r] if r < len(left) else ''
            rhs = right[r] if r < len(right) else ''
            bar = f"{C.DIM}│{C.RESET}" if rhs else ' '
            out.append(f"{lhs}{' ' * max(1, spk_w + 2 - ui_utils.visual_len(lhs))}"
                       f"{bar} {rhs}")
        out.append('')
    return (out + blank)[:geom.height]


class Pane:
    """Draws the pane, and only the rows of it that have changed.

    Holding the last frame is the whole of its state, and it is a picture of the
    screen rather than a position in the track — so it can never disagree with
    the audio about where we are. A resize, a seek and an ordinary tick all take
    the same path: build the frame, compare, write the differences.
    """

    def __init__(self, timeline: Timeline):
        self.timeline = timeline
        self._shown: list[str] = []
        self._geom: Geometry | None = None

    def paint(self, out, elapsed: float, geom: Geometry, force: bool = False) -> None:
        if geom.height <= 0:
            return
        if force or geom != self._geom:
            self._shown = []               # geometry moved: nothing on screen is trusted
            self._geom = geom
            _pc.screen_forget_rows(geom.row, geom.bottom)
        new = frame(self.timeline, elapsed, geom)
        old = self._shown
        for i, line in enumerate(new):
            if i < len(old) and old[i] == line:
                continue
            out.write(f"\033[{geom.row + i};{geom.col}H\033[K{line}")
        self._shown = new
