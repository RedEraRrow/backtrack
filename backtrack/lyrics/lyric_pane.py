"""The lyric pane: what is on screen at a moment of a track.

Four rules:

1.  THE AUDIO POSITION IS THE ONLY CLOCK.  Nothing counts lines, remembers where
    it was, or nudges itself by an offset.  A seek is not an event to handle; it
    is just a different number arriving next tick, so scrubbing needs no special
    case and cannot desynchronise.

2.  THE TIMELINE TILES THE TRACK.  Beats are contiguous and never overlap: each
    one ends exactly where the next begins.  Every instant therefore belongs to
    exactly one beat, and finding it is one bisect with no tie to break.  This is
    an invariant established once, when the timeline is built, and asserted.

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
from dataclasses import dataclass

from backtrack import tuning as tune
from backbone.prompt import core as _pc
from backbone import ui
from backbone.ui import Colors as C

LINE, DIRECTION, SILENCE = 'line', 'direction', 'silence'


@dataclass(frozen=True)
class Beat:
    """One thing that is on screen for one stretch of the track."""
    start: float
    end: float
    kind: str = LINE
    speaker: str = ''          # who is talking
    line: int = -1             # the script line it is part of (-1: none), shared by its segments
    aside: str = ''            # the manner it's spoken in: the line's note, or a mid-line tone note since
    text: str = ''             # what they say, markdown intact

    @property
    def blank(self) -> bool:
        return not (self.text or self.speaker)


@dataclass(frozen=True)
class Geometry:
    """Where the pane is and how big: everything the renderer needs of the screen."""
    row: int
    col: int
    width: int
    bottom: int
    # The row to hold the current line on (the art's middle, beside it), or
    # None to run from the top. Only ever moves the words down, never up.
    centre: int | None = None

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.row + 1)


def handover(start: float, end: float, next_start: float, keep_whole: bool,
             floor: float = 0.0, lead: float = tune.LYRIC_LEAD_IN_S) -> float:
    """The instant the next beat takes over from one running start-end.

    It sits a short run-up (`lead`: Settings → Lyrics early by) before
    `next_start`, as a line arriving exactly on its first word can arrive too
    late to read; 0 puts every line on its first word. The run-up comes
    out of any silence first, then out of this beat's own tail, which stays on
    screen dimmed and never gives up more than half of itself. A direction
    (`keep_whole`) gives up nothing: it is short, it is what's being read, and the
    line after it needs no run-up. Never earlier than `floor`.
    """
    earliest = end if keep_whole else end - min(lead, (end - start) / 2)
    earliest = max(earliest, start, floor)
    return max(floor, min(next_start, max(earliest, next_start - lead)))


def _tile(beats: list[Beat], duration: float, lead: float = tune.LYRIC_LEAD_IN_S) -> list[Beat]:
    """Turn a sorted run of beats into a tiling of the track.

    The boundary between two beats is ONE number (the instant the second takes
    over from the first), so it is computed once. Deriving it twice, once as an
    end and again as a start, is how a beat ends up owning a moment its neighbour
    also claims, or neither of them does.

    Each boundary is `handover`'s.
    """
    if not beats:
        return []
    beats = sorted(beats, key=lambda b: (b.start, b.end))

    bounds = [0.0]                                  # the track starts somewhere
    for i, b in enumerate(beats[:-1]):
        bounds.append(handover(b.start, b.end, beats[i + 1].start, b.kind == DIRECTION, bounds[-1], lead))
    bounds.append(max(duration, beats[-1].end, bounds[-1]))

    return [Beat(**{**b.__dict__, 'start': bounds[i], 'end': bounds[i + 1]})
            for i, b in enumerate(beats)]


class Timeline:
    """A track's beats, tiled, with the one lookup everything uses."""

    def __init__(self, beats: list[Beat], duration: float = 0.0, lead: float = tune.LYRIC_LEAD_IN_S):
        self.beats = _tile(beats, duration, lead)
        self._ends = [b.end for b in self.beats]
        # Song lyrics name no one: their words then take the speaker column too.
        self.has_speakers = any(b.speaker for b in self.beats)
        # The invariant the rest of the module relies on, checked once, here.
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

def from_chunks(chunks: list[dict], times: list[tuple], duration: float,
                lead: float = tune.LYRIC_LEAD_IN_S) -> Timeline:
    """From DialoguePlaybackState's chunks."""
    beats = []
    for c, (a, b) in zip(chunks, times):
        kind = SILENCE if c.get('is_air') else (DIRECTION if c.get('is_stage') else LINE)
        beats.append(Beat(start=float(a), end=float(b), kind=kind,
                          speaker=(c.get('speaker') or '').strip(),
                          line=int(c.get('line', -1)),
                          aside=(c.get('stage_dir') if kind != DIRECTION else c.get('aside') or '').strip(),
                          text=(c.get('stage_dir') if kind == DIRECTION
                                else c.get('text') or '').strip()))
    return Timeline(beats, duration, lead)


def from_sylt(entries: list[tuple], duration: float, lead: float = tune.LYRIC_LEAD_IN_S) -> Timeline:
    """From SYLT: timestamped lines, so each runs until the next one starts."""
    beats = []
    for i, (text, ms) in enumerate(entries):
        start = ms / 1000.0
        end = (entries[i + 1][1] / 1000.0 if i + 1 < len(entries)
               else start + tune.LYRIC_FABRICATED_END_MS / 1000.0)
        beats.append(Beat(start=start, end=end, text=(text or '').strip()))
    return Timeline(beats, duration, lead)


def from_uslt(lines: list[str], line_times: list[tuple], duration: float,
              lead: float = tune.LYRIC_LEAD_IN_S) -> Timeline:
    """From USLT: unsynced lyrics paced over the track by word count."""
    beats = [Beat(start=float(a), end=float(b), text=(t or '').strip())
             for t, (a, b) in zip(lines, line_times)]
    return Timeline(beats, duration, lead)


def timeline_for(path: str, audio, duration: float, source: str = 'auto',
                 lead: float = tune.LYRIC_LEAD_IN_S) -> tuple:
    """This track's lyrics as one timeline, from where the `lyrics_source`
    setting says (lyrics.files.SOURCES); (the timeline or None, what it was
    read from, whether its timing is a guess). Whichever it is, it becomes the
    same beats, so nothing downstream asks which kind of lyrics these are.

    Where each comes from, richest first: a transcript beside the track (.md,
    timed by its .json) knows speakers and directions; synced lyrics (SYLT, or
    a .lrc, subtitles or Whisper .json beside the track) know real timings; unsynced words (USLT, a
    .txt) only the words, paced across the track."""
    import os
    from backbone.log import quietly
    from backtrack.lyrics import bkts, files as lyric_files, formats as ly

    def transcript():
        state = ly.DialoguePlaybackState(path, track_duration=duration)
        if not state.is_active():
            return None
        guess = state.timing_source == 'estimated'
        if guess and source == 'synced':
            return None
        return (from_chunks(state.expanded_chunks, state.line_times, duration, lead),
                list(dict.fromkeys(bkts.label(p) for p in (state.md_path, state.json_path) if p)), guess)

    beside: list = []

    def file_lines() -> tuple:
        """The lyrics file beside the track and its lines, read once."""
        if not beside:
            found = lyric_files.sidecar(path)
            lines = []
            if found:
                with quietly():
                    lines = [(t, ms) for t, ms in lyric_files.read_file(found) if t]
            beside.append((found, lines))
        return beside[0]

    def paced(words: list, name: str) -> tuple:
        return from_uslt(words, ly.build_uslt_line_times(words, duration), duration, lead), [name], True

    def file_synced():
        found, lines = file_lines()
        timed = sorted(((t, ms) for t, ms in lines if ms is not None), key=lambda e: e[1])
        return (from_sylt(timed, duration, lead), [os.path.basename(found)], False) if timed else None

    def file_words():
        found, lines = file_lines()
        if not lines or any(ms is not None for _t, ms in lines):
            return None                                     # none, or timed (file_synced's)
        return paced([t for t, _ms in lines], os.path.basename(found))

    def sylt():
        timed = ly._parse_sylt(audio) if audio else []
        return (from_sylt(timed, duration, lead), ['embedded SYLT'], False) if timed else None

    def uslt():
        words = [line for line, _ in ly._parse_uslt(audio)] if audio else []
        return paced(words, 'embedded USLT') if words else None

    order = {
        'files':    (transcript, file_synced, file_words, sylt, uslt),
        'embedded': (sylt, uslt),
        'synced':   (transcript, sylt, file_synced),
    }.get(source, (transcript, sylt, file_synced, uslt, file_words))
    for get in order:
        found = get()
        if found:
            return found
    return None, [], False


# --- drawing -----------------------------------------------------------------

def _speaker_rows(beat: Beat, width: int, active: bool, named: bool = True) -> list[str]:
    """The speaker column: name, and the manner note under it, wrapped. Not
    `named`, the note alone: a tone change partway through a line already named."""
    from backtrack.lyrics.formats import _dir_rows
    if not beat.speaker:
        return []
    name = C.BOLD + ui.truncate_text(beat.speaker, width) + C.RESET
    rows = [name if active else f"{C.DIM}{ui.truncate_text(beat.speaker, width)}{C.RESET}"] if named else []
    # clipped too: a single word wider than the column can't wrap
    return rows + [ui.clip_ansi(r, width) for r in _dir_rows(beat.aside, width, C.DIM)]


def _labels_line(timeline: Timeline, shown: list[int], cur: int, idx: int) -> bool:
    """Whether beat `idx` carries its line's speaker and aside. A line split into
    several beats names them once: beside the current beat when it is one of
    them, so the name moves with the line being spoken, else the first shown."""
    line = timeline.beats[idx].line
    if line < 0:
        return True
    run = [i for i in shown if timeline.beats[i].line == line]
    return idx == (cur if cur in run else run[0])


def _tone_changes(timeline: Timeline, idx: int) -> bool:
    """Whether beat `idx` is spoken in a different manner from the part of its
    line before it (a mid-line tone note), so its aside shows unnamed."""
    beats, line = timeline.beats, timeline.beats[idx].line
    j = idx - 1
    while j >= 0 and beats[j].line < 0:
        j -= 1
    return line >= 0 and j >= 0 and beats[j].line == line and bool(beats[idx].aside) \
        and beats[j].aside != beats[idx].aside


def _text_rows(beat: Beat, width: int, active: bool) -> list[str]:
    """The text column: what is said, or the direction that is this beat."""
    from backtrack.lyrics.formats import _dir_rows, _md_rows
    base = '' if active else C.DIM
    if beat.kind == DIRECTION:
        return _dir_rows(beat.text, width, base, active)
    return (_md_rows(beat.text, width, base=base, active=active) or ['']) if beat.text else []


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

    # Sized to the pane, never past it: a speaker column only where there's
    # room for it beside a readable line.
    pad = geom.width - 2
    spk_w = max(12, min(pad // 3, 26)) if timeline.has_speakers and pad >= 40 else 0
    txt_w = max(1, pad - spk_w - 5) if spk_w else max(1, pad - 3)

    shown = [i for i in (cur - 1, cur, cur + 1) if 0 <= i < len(timeline.beats)]
    body: list[str] = []
    cur_mid = 0                            # body row at the current beat's middle
    for idx in shown:
        beat = timeline.beats[idx]
        if beat.kind == SILENCE or beat.blank:
            if idx == cur:
                cur_mid = len(body)
                body.append('')
            continue
        active = idx == cur
        left = []
        if spk_w and _labels_line(timeline, shown, cur, idx):
            left = _speaker_rows(beat, spk_w, active)
        elif spk_w and _tone_changes(timeline, idx):
            left = _speaker_rows(beat, spk_w, active, named=False)
        right = _text_rows(beat, txt_w, active)
        rows = max(len(left), len(right))
        if active:
            cur_mid = len(body) + (rows - 1) // 2
        for r in range(rows):
            lhs = left[r] if r < len(left) else ''
            rhs = right[r] if r < len(right) else ''
            bar = f"{C.FRAME}│{C.RESET}" if right else ' '
            gutter = ' ' * max(1, spk_w + 2 - ui.visual_len(lhs)) if spk_w else ''
            body.append(f"{lhs}{gutter}{bar} {rhs}")
        body.append('')
    if geom.centre is not None:
        body = [''] * max(0, geom.centre - geom.row - cur_mid) + body
    return (body + blank)[:geom.height]


class Pane:
    """Draws the pane, and only the rows of it that have changed.

    Holding the last frame is the whole of its state, and it is a picture of the
    screen rather than a position in the track, so it can never disagree with
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
        new = frame(self.timeline, elapsed, geom)
        old = self._shown
        for i, line in enumerate(new):
            if i < len(old) and old[i] == line:
                continue
            # Through the painter, cell by cell, padded to the pane's width: the
            # rest of the row (the art beside it, a box's border) isn't touched.
            # Clipped to the pane first: nothing it draws reaches the border.
            line = ui.clip_ansi(line, geom.width)
            out.write(_pc.screen_span_paint(geom.row + i, geom.col,
                                            line + ' ' * max(0, geom.width - ui.visual_len(line))))
        self._shown = new
