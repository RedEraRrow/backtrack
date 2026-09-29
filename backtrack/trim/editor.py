"""Interactive single-track trim editor, a sibling of lyrics/editor.py: its
own private VLC player (so scrubbing never disturbs SESSION or a joined
window), the same raw-mode key loop and mouse-enable pattern, and EDIT mode's
segmented MM:SS.mmm timestamp fields from lyrics.time_fields (`new_edit`,
`edit_key`), shared with the lyrics editor rather than a second parser.

Chrome matches the rest of the app: the same one-line rounded header as
id3.browser's tag list (styled title left, dim facts right), and a full-width
progress bar in playback's own style: the kept region filled like elapsed
playback, a short accent tip at each cut point, brackets and blocks from
`ui.get_progress_bar`. Everything that touches a terminal or VLC lives
in this module; the pure engine (snapping, the ffmpeg cut, backups, silence
detection) is trim.py, imported and never duplicated.

'[' / ']' jump the playhead between silence-detection candidates near the
head and tail, computed lazily on first press, since most
tracks won't need it (a sting-seeded bulk track, or a mark set by ear).
Nothing is marked automatically; i/o still accepts a candidate once it's
been heard. Some material opens cold with no gap to find at all, which
shows a status message instead, not a dead key.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

_vlc = None
try:
    import vlc as _vlc  # type: ignore[import-untyped]
    _HAS_VLC = True
except ImportError:
    _HAS_VLC = False

from mutagen.id3 import ID3

from backtrack.config import load_config
from backtrack.trim import engine as trim
from backtrack.music_library import drop_moved, track_title, first_text
from backbone import ui
from backbone.ui import Colors as C
from backbone.prompt.core import _Widget, _read_key, _wait_for_keypress, _set_raw, _restore_term_attrs, _get_term_attrs
from backbone.prompt import confirm as _confirm
from backbone import prompt as _promptmod
from backtrack.lyrics.time_fields import edit_changed, _EDIT_END, edit_key, edit_seconds, _EDIT_START, new_edit, _render_edit_fields, _ts_parts
from backbone import timefmt
from backtrack.config import setting
from backbone.log import quietly
from backtrack import tuning as tune

# Same shape as AUDITION's nudge scheme (editor_view's _AUD_* steps): a coarse step
# on ',' and '.', and a fine step that here is one frame rather than a fixed
# 0.05s, probed per file, since frame length varies with sample rate.
_COARSE_STEP = 0.25
_JOIN_LEAD = 1.0     # seconds of "about to be cut" heard before the in-point
_JOIN_TAIL = 1.0     # seconds of "just cut" heard after the out-point
_TAIL_PREVIEW = 5.0  # window played by "up to the out-point"


# ---------------------------------------------------------------------------
# Pure state: marks, undo, audition windows, sibling context. No terminal or
# VLC calls below this point: this half is what tests/test_trim_editor.py
# exercises headlessly.
# ---------------------------------------------------------------------------

@dataclass
class Marks:
    in_requested: float | None = None
    in_snapped: float | None = None
    out_requested: float | None = None
    out_snapped: float | None = None


def set_in(marks: Marks, pos_s: float, frame_dur: float) -> tuple:
    """Set the in-point (from the playhead, a click, or typed entry). Returns
    an undo record."""
    undo = ('in', marks.in_requested, marks.in_snapped)
    marks.in_requested = max(0.0, pos_s)
    marks.in_snapped = trim.snap_in_point(marks.in_requested, frame_dur)
    return undo


def set_out(marks: Marks, pos_s: float, frame_dur: float,
            track_length_s: float | None = None) -> tuple:
    """Set the out-point. Returns an undo record. Rounding outward can
    push the snapped value a frame past the file's own end; `track_length_s`
    clamps it back so the out-point never claims to be later than the track
    actually runs."""
    undo = ('out', marks.out_requested, marks.out_snapped)
    marks.out_requested = max(0.0, pos_s)
    snapped = trim.snap_out_point(marks.out_requested, frame_dur)
    if track_length_s is not None:
        snapped = min(snapped, track_length_s)
    marks.out_snapped = snapped
    return undo


def clear_in(marks: Marks) -> tuple:
    undo = ('in', marks.in_requested, marks.in_snapped)
    marks.in_requested = marks.in_snapped = None
    return undo


def clear_out(marks: Marks) -> tuple:
    undo = ('out', marks.out_requested, marks.out_snapped)
    marks.out_requested = marks.out_snapped = None
    return undo


def nudge_in(marks: Marks, delta_s: float, frame_dur: float) -> tuple:
    """Move the in-point by delta (one frame, or the coarse step), re-snapping."""
    base = marks.in_requested if marks.in_requested is not None else 0.0
    return set_in(marks, base + delta_s, frame_dur)


def nudge_out(marks: Marks, delta_s: float, frame_dur: float, track_length_s: float) -> tuple:
    """Move the out-point by delta, re-snapping, never past the track's end."""
    base = marks.out_requested if marks.out_requested is not None else track_length_s
    return set_out(marks, min(base + delta_s, track_length_s), frame_dur, track_length_s)


def apply_undo(marks: Marks, record: tuple) -> None:
    """Restore a mark to its value before the change `record` describes."""
    which, req, snap = record
    if which == 'in':
        marks.in_requested, marks.in_snapped = req, snap
    else:
        marks.out_requested, marks.out_snapped = req, snap


def resulting_duration(marks: Marks) -> float | None:
    """The trimmed file's length, or None until both marks are set (or they
    don't leave anything behind)."""
    if marks.in_snapped is None or marks.out_snapped is None:
        return None
    d = marks.out_snapped - marks.in_snapped
    return d if d > 0 else None


def removed_head(marks: Marks) -> float:
    """Seconds removed from the start."""
    return marks.in_snapped if marks.in_snapped is not None else 0.0


def removed_tail(marks: Marks, track_length_s: float) -> float:
    """Seconds removed from the end."""
    if marks.out_snapped is None:
        return 0.0
    return max(0.0, track_length_s - marks.out_snapped)


def join_clips(marks: Marks, track_length_s: float | None,
               lead_s: float = _JOIN_LEAD, tail_s: float = _JOIN_TAIL) -> list[tuple[float, float, str]]:
    """Two clips straddling each cut point, queued back to back: the material
    just before the in-point and just after the out-point, the edge of what
    gets discarded at each end. Either mark being unset just
    drops that clip. `_aud_next()`-shaped: (lo, hi, label) tuples."""
    clips: list[tuple[float, float, str]] = []
    if marks.in_snapped is not None:
        lo = max(0.0, marks.in_snapped - lead_s)
        clips.append((lo, marks.in_snapped, 'in'))
    if marks.out_snapped is not None:
        hi = marks.out_snapped + tail_s
        if track_length_s is not None:
            hi = min(hi, track_length_s)
        if hi > marks.out_snapped:
            clips.append((marks.out_snapped, hi, 'out'))
    return clips


def sibling_durations(library: list, path: str) -> list[float]:
    """Durations of every other track sharing this one's album+artist, from
    the cached library list: the sanity check against a series' true length.
    No file access: it's a pass over what's already cached."""
    track = next((t for t in library if t.get('path') == path), None)
    if track is None:
        return []
    album, artist = track.get('album'), track.get('artist')
    return [t['duration'] for t in library
            if t is not track and t.get('album') == album and t.get('artist') == artist
            and t.get('duration')]


def _edit_seed(marks: Marks) -> tuple[dict, dict]:
    """The segmented editor seeded from the current marks, and its snapshot."""
    return new_edit(marks.in_requested, marks.out_requested)


def _edit_apply(marks: Marks, edit: dict, edit_orig: dict, frame_dur: float,
                track_length_s: float | None = None) -> list[tuple]:
    """Commit whichever bound(s) changed in the segmented editor. Returns the
    undo record(s) so a single Enter is a single undo step (or two, if both
    bounds changed together)."""
    undos = []
    if edit_changed(edit, edit_orig, _EDIT_START):
        undos.append(set_in(marks, edit_seconds(edit, _EDIT_START), frame_dur))
    if edit_changed(edit, edit_orig, _EDIT_END):
        undos.append(set_out(marks, edit_seconds(edit, _EDIT_END), frame_dur, track_length_s))
    return undos


_edit_field_key = edit_key     # the one segmented-editor key handler (time_fields)


def _progress_bar(width: int, track_length: float, marks: Marks, play_pos: float) -> str:
    """A full-width track bar in playback's own style
    (`ui.get_progress_bar`'s dim brackets and heavy-block fill): the kept
    region (between the marks) filled like the elapsed portion of a normal
    playback bar, with a short accent tip at each cut point (`╺` in, `╸` out).
    The playhead inverts whatever cell it's over rather than adding a glyph."""
    if width <= 2:
        return f"{C.DIM}[{C.RESET}{' ' * max(0, width - 2)}{C.DIM}]{C.RESET}"
    inner = width - 2
    if track_length <= 0:
        return f"{C.DIM}[{C.RESET}{' ' * inner}{C.DIM}]{C.RESET}"

    def _pos(t: float) -> int:
        return max(0, min(inner - 1, int((t / track_length) * inner)))

    in_i = _pos(marks.in_snapped) if marks.in_snapped is not None else None
    out_i = _pos(marks.out_snapped) if marks.out_snapped is not None else None

    cells = [' '] * inner
    styles: list[str] = [''] * inner
    if in_i is not None or out_i is not None:
        lo = in_i if in_i is not None else 0
        hi = out_i if out_i is not None else inner - 1
        for i in range(lo, hi + 1):
            cells[i] = '━'; styles[i] = C.PRIMARY
    if in_i is not None:
        cells[in_i] = '╺'; styles[in_i] = f"{C.ACCENT}{C.BOLD}"
    if out_i is not None:
        cells[out_i] = '╸'; styles[out_i] = f"{C.ACCENT}{C.BOLD}"

    play_i = _pos(play_pos)
    rendered = []
    for i, ch in enumerate(cells):
        if i == play_i:
            rendered.append(f"{C.INVERT}{styles[i]}{ch}{C.RESET}")
        elif styles[i]:
            rendered.append(f"{styles[i]}{ch}{C.RESET}")
        else:
            rendered.append(ch)
    return f"{C.DIM}[{C.RESET}{''.join(rendered)}{C.DIM}]{C.RESET}"


# ---------------------------------------------------------------------------
# The interactive screen.
# ---------------------------------------------------------------------------

def _track_title_artist(path: str) -> tuple[str, str]:
    """(title, artist), falling back to the filename stem when untagged."""
    try:
        tags = ID3(path)
    except Exception:                    # untagged, or unreadable: the file name
        return track_title(path), ""
    return first_text(tags.get('TIT2')) or track_title(path), first_text(tags.get('TPE1'))


def _silence_markers(path: str, track_length: float) -> list[float]:
    """Head + tail silence-boundary candidates: jump targets
    for '[' / ']', not automatic marks. Empty when nothing is found; some
    material opens cold with no gap to detect at all, which is not a failure."""
    cfg = load_config()
    window_s = float(setting(cfg, "trim_scan_window_s"))
    noise_db = float(setting(cfg, "trim_silence_noise_db"))
    min_s = float(setting(cfg, "trim_silence_min_s"))
    markers: set[float] = set()
    with quietly():
        for s, e in trim.detect_silence(path, 0.0, min(window_s, track_length), noise_db, min_s):
            markers.add(round(s, 3)); markers.add(round(e, 3))
        tail_start = max(0.0, track_length - window_s)
        for s, e in trim.detect_silence(path, tail_start, track_length - tail_start, noise_db, min_s):
            markers.add(round(s, 3)); markers.add(round(e, 3))
    return sorted(markers)


# Chapter-affected preview: title · time range · what the cut does to it.
_CHAPTER_PREVIEW_COLUMNS = [
    _promptmod.Column(style='primary', flex=True),
    _promptmod.Column(style='dynamic-dim'),
    _promptmod.Column(style='dynamic-dim', align='right', pin=True),
]


def resolve_chapters(path: str, snapped_in_s: float, snapped_out_s: float
                     ) -> tuple[list[tuple], list[str], int | None] | None:
    """Classify this track's chapters against the cut and ask about anything
    destroyed or straddling; a chapter that survives intact is
    rebased regardless, that part is never optional. Returns
    (chapters, child_order, flags) ready for `trim.commit_trim`'s `chapters`
    kwarg, or None when the file has no chapters at all (nothing to ask)."""
    orig_chapters, child_order, flags = trim.read_chapters(path)
    if not orig_chapters:
        return None

    cut_start_ms = int(round(snapped_in_s * 1000))
    cut_end_ms = int(round(snapped_out_s * 1000))
    # trim.classify_chapters returns flat (element_id, start_ms, end_ms,
    # title, classification) tuples; split each into (chapter, cls) pairs,
    # since every helper below (rebase/clamp) takes the plain 4-tuple.
    classified = [((eid, s, e, t), cls) for eid, s, e, t, cls
                 in trim.classify_chapters(orig_chapters, cut_start_ms, cut_end_ms)]
    affected = [(c, cls) for c, cls in classified if cls != 'kept']

    def _finish(decisions: dict) -> tuple[list[tuple], list[str], int | None]:
        survivors = [d for d in decisions.values() if d is not None]
        surviving_ids = {d[0] for d in survivors}
        new_order = trim.rebuild_ctoc_children(child_order, surviving_ids) if child_order else []
        return survivors, new_order, flags

    if not affected:
        # Nothing to ask about: the rule-driven resolution gives the same answer.
        return trim.apply_chapter_policy(path, snapped_in_s, snapped_out_s, 'clamp')

    def _label(c: tuple) -> str:
        return c[3] or c[0]

    state_label = {'kept': 'survives', 'destroyed': 'lost', 'straddles': 'straddles the cut'}
    rows = [_promptmod.Choice(
                title=_label(c), value=c[0],
                cells=[_label(c), f"{c[1] / 1000:.1f}s → {c[2] / 1000:.1f}s", state_label[cls]])
            for c, cls in classified]
    all_destroyed = len(affected) == len(classified)

    overview = _promptmod.select(
        f"{len(affected)} of {ui.plural(len(classified), 'chapter')} affected by this cut, ↵ to review each:",
        choices=rows, columns=_CHAPTER_PREVIEW_COLUMNS,
        shortcuts={'D': '__discard_all__'},
        extra_hints={'D': 'discard all chapters'},
    )
    if overview == '__discard_all__' or (overview is None and all_destroyed):
        return [], [], flags

    decisions: dict[str, tuple | None] = {}
    for c, cls in classified:
        if cls == 'kept':
            decisions[c[0]] = trim.rebase_chapter(c, cut_start_ms)
            continue
        action = _promptmod.select(
            f"'{_label(c)}' {state_label[cls]}: what should happen to it?",
            choices=[
                _promptmod.Choice(title="Delete this chapter", value='delete'),
                _promptmod.Choice(title="Keep, clamped to the cut boundary", value='clamp'),
                _promptmod.Choice(title="Reassign: type new start-end in seconds", value='reassign'),
            ])
        if action == 'clamp':
            decisions[c[0]] = trim.clamp_chapter(c, cut_start_ms, cut_end_ms)
        elif action == 'reassign':
            raw = _promptmod.text("New start-end in seconds, e.g. '12.5-45.0' (in the trimmed file):")
            try:
                new_start_s, new_end_s = (float(x) for x in (raw or '').split('-', 1))
                decisions[c[0]] = (c[0], int(new_start_s * 1000), int(new_end_s * 1000), c[3])
            except (ValueError, AttributeError):
                ui.show_status("Could not parse that range; chapter deleted instead.")
                decisions[c[0]] = None
        else:   # 'delete', or backed out of the per-chapter choice
            decisions[c[0]] = None

    return _finish(decisions)


def _header_box(title: str, artist: str, track_length: float) -> list[str]:
    """One-line rounded box, same shape as the rest of the app's per-file
    screens (id3.browser's tag list, the bulk-edit header): styled title (+
    dim artist) left, dim facts right, spanning the full terminal width."""
    return _promptmod.rounded_header(title, f" · {artist}" if artist else "",
                                     f"[MP3]  {timefmt.clock(track_length)}")


def _run_marking_screen(
    path: str,
    marks: Marks,
    undo_stack: list[tuple],
    *,
    track_length: float,
    frame_dur: float,
    track_name: str,
    track_artist: str,
    siblings: list[float],
    finish_key: str = 's',
    finish_hint: str = 'commit',
    finish_verb: str = 'commit',
    extra_key: str | None = None,
    extra_hint: str = '',
    strip_lines: list[str] | None = None,
    flags: dict | None = None,
) -> str:
    """Interactive marking/audition/undo screen for one track: the shared
    engine behind both single-track `trim_editor` and the bulk conveyor
    (trim/bulk.py). Mutates `marks`/`undo_stack` in place; never touches disk
    or shows a confirmation: those are the caller's job, since a single track
    and a conveyor group mean different things by "done" (write now vs. record
    and move to the next track).

    Returns `finish_key` once both marks are valid, `extra_key` if pressed
    (the conveyor's skip), or 'ESC': back one track, or leave, at the
    caller's discretion. `strip_lines` is the conveyor's group-state strip,
    prepended above the header. `flags` is a shared dict so the audition
    "approximate" note fires once across a whole group, not
    once per track.
    """
    flags = flags if flags is not None else {}
    active = 'in'          # which mark arrows/,/./i/o/d act on
    edit: dict | None = None
    edit_orig: dict = {}

    playing = False
    play_until = 0.0
    play_pos = 0.0
    aud_queue: list[tuple[float, float, str]] = []
    aud_now: str | None = None

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)

    mp = None
    if _vlc is not None:
        try:
            old_fd = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2); os.close(devnull)
            inst = _vlc.Instance('--no-video', '--quiet')
            mp_i = inst.media_player_new()           # type: ignore[union-attr]
            mp_i.set_media(inst.media_new(path))     # type: ignore[union-attr]
            os.dup2(old_fd, 2); os.close(old_fd)
            mp = mp_i
        except (AttributeError, OSError):
            pass

    w = _Widget(fd)

    def do_preview(start_s: float, dur: float | None = None) -> None:
        nonlocal playing, play_until, play_pos
        if mp is None:
            return
        start_s = max(0.0, start_s)
        mp.set_time(int(start_s * 1000))
        if not mp.is_playing():
            mp.play(); time.sleep(0.15)
        playing = True
        play_until = float('inf') if dur is None else time.time() + dur
        play_pos = start_s

    def do_stop() -> None:
        nonlocal playing
        if mp and mp.is_playing(): mp.pause()
        playing = False

    def _aud_next() -> None:
        nonlocal aud_now
        if not aud_queue:
            aud_now = None; do_stop(); return
        lo, hi, label = aud_queue.pop(0)
        aud_now = label
        do_preview(lo, max(0.1, hi - lo))

    def do_play_from_in() -> None:
        if marks.in_snapped is not None:
            do_preview(marks.in_snapped)

    def do_play_to_out() -> None:
        nonlocal aud_queue
        if marks.out_snapped is not None:
            aud_queue = []
            lo = max(0.0, marks.out_snapped - _TAIL_PREVIEW)
            do_preview(lo, max(0.1, marks.out_snapped - lo))

    def do_join() -> None:
        nonlocal aud_queue
        clips = join_clips(marks, track_length)
        if not clips:
            ui.show_status("Could not audition the join: set both marks first.")
            return
        aud_queue = clips
        _aud_next()
        if not flags.get('join_warned'):
            flags['join_warned'] = True
            ui.show_status(
                "Approximate: a VLC seek isn't sample-accurate. Play back the "
                "written file to check the real join.", duration=tune.STATUS_WARNING_S)

    def do_undo() -> None:
        if undo_stack:
            apply_undo(marks, undo_stack.pop())

    _markers: list[float] | None = None   # computed lazily, most tracks won't need it

    def _get_markers() -> list[float]:
        nonlocal _markers
        if _markers is None:
            _markers = _silence_markers(path, track_length)
        return _markers

    def do_jump_marker(direction: int) -> None:
        """'[' / ']': jump the playhead to the previous/next silence-detection
        candidate and play briefly there; i/o still accepts it."""
        markers = _get_markers()
        if not markers:
            ui.show_status("No silence gaps found near the head or tail.")
            return
        if direction > 0:
            nxt = next((m for m in markers if m > play_pos + 0.05), markers[0])
        else:
            nxt = next((m for m in reversed(markers) if m < play_pos - 0.05), markers[-1])
        do_preview(nxt, dur=2.0)

    def _footer_pairs() -> list[tuple[str, str]]:
        if edit is not None:
            # EDIT sub-mode has its own key set, the same as lyrics_editor's.
            pairs = [('tab/⇧tab', 'field'), ('←→', 'cursor'), ('↑↓', 'adjust')]
            if mp is not None:
                pairs.append(('p', 'grab playhead'))
            pairs += [('↵', 'apply'), ('esc', 'cancel')]
            return pairs
        pairs = [('tab', 'in/out')]
        if mp is not None:                  # marks are taken from the playhead
            pairs.append(('i/o', 'mark'))
        pairs += [('d', 'clear'), ('e', 'type'), ('←→', '1 frame'), (',/.', f'{_COARSE_STEP:g}s')]
        if mp is not None:
            pairs += [('p', 'play in'), ('P', 'play to out'), ('j', 'audition join'),
                     ('[/]', 'silence marker')]
        if undo_stack:
            pairs.append(('u', f'undo ×{len(undo_stack)}'))
        if extra_key:
            pairs.append((extra_key, extra_hint))
        pairs += [(finish_key, finish_hint), ('esc', 'back'), ('q', 'quit')]
        return pairs

    indent = " " * ui.MARGIN_H

    def _mark_line(label: str, key: str, m: Marks) -> str:
        req = m.in_requested if key == 'in' else m.out_requested
        snap = m.in_snapped if key == 'in' else m.out_snapped
        marker = f"{C.ACCENT}▸{C.RESET} " if active == key else "  "
        if req is None:
            return f"{indent}{marker}{label}: {C.DIM}not set{C.RESET}"
        return f"{indent}{marker}{label}: {timefmt.clock(req)} → snapped {C.BOLD}{timefmt.clock(snap)}{C.RESET}"

    def _render() -> tuple[list[str], int, int, dict]:
        out: list[str] = list(strip_lines or []) + list(_header_box(track_name, track_artist, track_length))

        out.append(_mark_line("In ", 'in', marks))
        out.append(_mark_line("Out", 'out', marks))
        out.append("")

        dur = resulting_duration(marks)
        head = removed_head(marks)
        tail = removed_tail(marks, track_length)
        out.append(f"{indent}Resulting duration: {C.BOLD}{timefmt.clock(dur)}{C.RESET}"
                   f" · removed head {head:.1f}s · tail {tail:.1f}s")
        if siblings:
            avg = sum(siblings) / len(siblings)
            out.append(f"{indent}{C.DIM}Siblings: avg {avg:.0f}s over {ui.plural(len(siblings), 'track')}"
                       f" · this track {track_length:.0f}s{C.RESET}")
        out.append("")

        # Bar spans the full width, like every other bar in the app: indent ·
        # play glyph (2 visible cols) · bar · elapsed/total, no fixed cap.
        cols = ui.get_terminal_width()
        avail = max(10, cols - 2 * ui.MARGIN_H)
        timer_plain = f" {timefmt.clock(play_pos)} / {timefmt.clock(track_length)}"
        play_glyph = f"{C.ACCENT}▸{C.RESET} " if playing else "  "
        bar_width = max(10, avail - len(timer_plain) - 2)
        prog_row = len(out)
        bar = _progress_bar(bar_width, track_length, marks, play_pos)
        out.append(f"{indent}{play_glyph}{bar}{C.DIM}{timer_plain}{C.RESET}")
        out.append("")

        if edit is not None:
            start_str, end_str = _render_edit_fields(edit)
            out.append(f"{indent}{C.ACCENT}✎{C.RESET}  in  {start_str}    out  {end_str}")
            out.append("")

        cells: dict = {}
        _promptmod.append_chrome(out, _footer_pairs(), cells)
        return out, prog_row, bar_width, cells

    try:
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()
        need_redraw = True
        hint_cells: dict = {}
        prog_row = bar_width = 0

        while True:
            if playing:
                if mp and not mp.is_playing():
                    if aud_queue: _aud_next()
                    else: playing = False; aud_now = None
                    need_redraw = True
                elif time.time() > play_until:
                    if aud_queue: _aud_next()
                    else: do_stop(); aud_now = None
                    need_redraw = True
                elif mp:
                    pos = mp.get_time() / 1000.0
                    if abs(pos - play_pos) > 0.05:
                        play_pos = pos; need_redraw = True

            if ui.consume_resize():
                w.anchor_reset(); need_redraw = True

            if need_redraw:
                lines, prog_row, bar_width, hint_cells = _render()
                w.render(lines)
                need_redraw = False

            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            need_redraw = True

            if key == 'SCROLL_UP' or key == 'SCROLL_DOWN':
                continue
            if isinstance(key, str) and key.startswith(('MOUSE_CLICK:', 'MOUSE_RELEASE:')):
                chrome = _promptmod.consume_chrome(key, hint_cells)
                if chrome is _promptmod.CHROME_HANDLED:
                    continue
                if chrome is _promptmod.CHROME_REDRAW:
                    w.anchor_reset(); continue
                if chrome is not None:
                    key = chrome
                elif key.startswith('MOUSE_CLICK:') and w.row is not None:
                    parts = key.split(':')
                    r = int(parts[2]) if len(parts) > 2 else 0
                    col = int(parts[3]) if len(parts) > 3 else 1
                    line_idx = r - w.row - ui.MARGIN_V
                    # The bar sits after the indent and the 2-column play glyph.
                    bar_col = col - (ui.MARGIN_H + 2)
                    if line_idx == prog_row and 0 <= bar_col - 1 < bar_width and track_length > 0:
                        frac = max(0.0, min(1.0, (bar_col - 1) / max(1, bar_width - 1)))
                        do_preview(min(frac * track_length, max(0.0, track_length - 0.5)))
                    continue
                else:
                    continue
            else:
                chrome = _promptmod.consume_chrome(key, hint_cells)
                if chrome is _promptmod.CHROME_HANDLED:
                    continue
                if chrome is _promptmod.CHROME_REDRAW:
                    w.anchor_reset(); continue
                if chrome is not None:
                    key = chrome

            assert isinstance(key, str)

            if edit is not None:
                if key == 'ESC':
                    edit = None
                elif key == 'ENTER':
                    for rec in _edit_apply(marks, edit, edit_orig, frame_dur, track_length):
                        undo_stack.append(rec)
                    edit = None
                elif key in ('p', 'P'):
                    pm, ps, pms = _ts_parts(round(play_pos, 3))
                    bound = _EDIT_START if edit['fi'] < 3 else _EDIT_END
                    for fk, v in zip(bound, (pm, ps, pms)):
                        edit['fields'][fk] = list(v)
                else:
                    _edit_field_key(edit, key)
                continue

            if key in ('q', 'CTRL_C'):
                from backbone.nav import QuitToTerminal
                raise QuitToTerminal()
            if key == 'ESC':
                do_stop()
                return 'ESC'
            elif key == 'TAB':
                active = 'out' if active == 'in' else 'in'
            elif key == 'i':
                if mp: undo_stack.append(set_in(marks, play_pos, frame_dur))
            elif key == 'o':
                if mp: undo_stack.append(set_out(marks, play_pos, frame_dur, track_length))
            elif key == 'd':
                undo_stack.append(clear_in(marks) if active == 'in' else clear_out(marks))
            elif key == 'e':
                edit, edit_orig = _edit_seed(marks)
            elif key == 'LEFT':
                if active == 'in':
                    undo_stack.append(nudge_in(marks, -frame_dur, frame_dur))
                else:
                    undo_stack.append(nudge_out(marks, -frame_dur, frame_dur, track_length))
            elif key == 'RIGHT':
                if active == 'in':
                    undo_stack.append(nudge_in(marks, frame_dur, frame_dur))
                else:
                    undo_stack.append(nudge_out(marks, frame_dur, frame_dur, track_length))
            elif key == ',':
                if active == 'in':
                    undo_stack.append(nudge_in(marks, -_COARSE_STEP, frame_dur))
                else:
                    undo_stack.append(nudge_out(marks, -_COARSE_STEP, frame_dur, track_length))
            elif key == '.':
                if active == 'in':
                    undo_stack.append(nudge_in(marks, _COARSE_STEP, frame_dur))
                else:
                    undo_stack.append(nudge_out(marks, _COARSE_STEP, frame_dur, track_length))
            elif key == 'p':
                do_play_from_in()
            elif key == 'P':
                do_play_to_out()
            elif key == 'j':
                do_join()
            elif key == 'u':
                do_undo()
            elif key == '[' and mp is not None:
                do_jump_marker(-1)
            elif key == ']' and mp is not None:
                do_jump_marker(1)
            elif extra_key is not None and key == extra_key:
                do_stop()
                return extra_key
            elif key == finish_key:
                if marks.in_snapped is None or marks.out_snapped is None:
                    ui.show_status(f"Could not {finish_verb}: set both an in-point and an out-point first.")
                    continue
                if resulting_duration(marks) is None:
                    ui.show_status(f"Could not {finish_verb}: the out-point is not after the in-point.")
                    continue
                do_stop()
                return finish_key
    finally:
        if mp:
            try: mp.stop()
            except Exception: pass
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()
        _restore_term_attrs(fd, old)
        w.clear()


def _sting_suggestion(path: str, region: str, window_s: float, min_score: float) -> tuple[float, float] | None:
    """A learned-sting suggestion for `path`'s head or tail: the single-track
    version of the bulk conveyor's "learn from an earlier trim". An
    already-correctly-trimmed sibling elsewhere in this folder is
    ground truth for the same shared opening/closing, so a lone edit doesn't
    have to rediscover or mark it by hand every time. None if there's no
    usable history in this folder, or the match isn't confident enough."""
    learned = trim.learn_sting_from_history([path], region, window_s, min_score)
    if learned is None:
        return None
    backup_path, sting_start, sting_end, side = learned
    reference_pcm = trim.decode_mono_pcm(backup_path, sting_start, sting_end - sting_start)
    results = trim.seed_by_sting(reference_pcm, sting_end - sting_start, side, 0.0, [path],
                                 window_s=window_s, min_score=min_score, region=region)
    return results[path]


def trim_editor(path: str, library: list | None = None) -> None:
    """Run the interactive trimmer on `path` until the user backs out or
    commits. Marking, auditioning and undo are all in-session; nothing is
    written to disk until 's'."""
    if not drop_moved([path]):
        return
    if not trim.HAS_FFMPEG:
        ui.show_status("Could not open the trimmer: ffmpeg isn't installed. See README.md.")
        return
    try:
        frame_dur = trim.probe_frame_duration(path)
    except ValueError as e:
        ui.show_status(str(e))
        return

    from mutagen.mp3 import MP3
    track_length = MP3(path).info.length
    track_name, track_artist = _track_title_artist(path)
    # `library` itself is passed on as given: None means "not tracking one",
    # and a stand-in [] would be saved as the whole library cache on commit.
    siblings = sibling_durations(library or [], path)

    marks = Marks()
    undo_stack: list[tuple] = []

    cfg = load_config()
    window_s = float(setting(cfg, "trim_sting_window_s"))
    min_score = float(setting(cfg, "trim_sting_min_score"))
    head = _sting_suggestion(path, 'head', window_s, min_score)
    if head is not None and _confirm(
            f"Found a matching opening sting from an earlier trim in this folder (in-point {timefmt.clock(head[0])}). Use it?",
            default=True):
        set_in(marks, head[0], frame_dur)
    tail = _sting_suggestion(path, 'tail', window_s, min_score)
    if tail is not None and _confirm(
            f"Found a matching closing sting from an earlier trim in this folder (out-point {timefmt.clock(tail[0])}). Use it?",
            default=True):
        set_out(marks, tail[0], frame_dur, track_length)

    while True:
        outcome = _run_marking_screen(
            path, marks, undo_stack,
            track_length=track_length, frame_dur=frame_dur,
            track_name=track_name, track_artist=track_artist, siblings=siblings,
        )
        if outcome != 's':
            return

        # `_run_marking_screen` only returns 's' once both marks are valid.
        assert marks.in_snapped is not None and marks.out_snapped is not None
        chapters = resolve_chapters(path, marks.in_snapped, marks.out_snapped)

        dur = resulting_duration(marks)
        msg = (f"Trim to {timefmt.clock(marks.in_snapped)} → {timefmt.clock(marks.out_snapped)} ({dur:.1f}s)?"
               f" The original is backed up, so this can be undone.")
        if not _confirm(msg, default=False):
            continue   # back to the marking screen, marks intact

        result = trim.commit_trim(path, marks.in_snapped, marks.out_snapped,
                                  library=library, chapters=chapters)
        if result.ok:
            ui.show_status(f"Trimmed to {dur:.1f}s. Original backed up.")
            return
        ui.show_status(f"Could not trim: {result.error}")
        # loop back into the marking screen so a failed write can be retried
