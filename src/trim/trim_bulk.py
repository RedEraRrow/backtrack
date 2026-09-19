"""Bulk trimming: candidate detection (section 5.1), sting-based seeding with
auto-discovered candidates (section 4.4/5.2.1/5.2.2), and the conveyor that
walks a selection marking each track in turn before committing the whole
batch in one pass (5.2). The per-track marking screen is trim_editor's
`_run_marking_screen` — this module adds sting seeding, the group strip, the
walk, and the commit pass on top, never re-implementing marking/audition/undo.

Candidate discovery (`trim.find_candidate_stings`) compares the reference
track against one other track in the group to find segments they share —
there's usually more than one (a continuity announcement, the theme, a
trailer), so every candidate is listed with its score and is auditionable
before picking. "Mark manually" is always offered alongside, for when nothing
scores well enough or the group is a lone pair with no useful comparison.

Head and tail are seeded independently (section 5.2.2): an opening sting and
a closing one are offered as two separate questions, each with its own
keep/drop anchor, since keeping the theme while dropping a closing
announcement (or vice versa) is an ordinary combination, not a special case.

Declining the sting offer falls back to `_propagate_absolute_offset` (5.2.1):
mark one track normally, copy its absolute timestamps to the rest. Explicitly
the weaker tool — it assumes identical padding, which a single overrunning
episode breaks — so it never commits on its own; every track still goes
through the normal per-track review in the walk that follows.

Silence detection (4.3) is the fallback when there's no sting: it isn't a
separate bulk feature, it's inherited for free from `_run_marking_screen`'s
'[' / ']' jump-to-candidate keys, available on every track this conveyor
opens exactly as it is in single-track trim. Chapters (3.3) are handled the
same way, via trim_editor's `resolve_chapters`, asked per track as its turn
in the walk comes up rather than batched at commit time.

`apply_replaygain_op` (section 5.4) is a separate bulk operation over the
same material, not part of the trim commit — it measures loudness and
proposes a per-track gain as tags only, and only makes sense run after
trimming (a pre-trim measurement would include the material about to be cut).
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np

_vlc = None
try:
    import vlc as _vlc  # type: ignore[import-untyped]
    _HAS_VLC = True
except ImportError:
    _HAS_VLC = False

from mutagen.id3 import ID3, ID3NoHeaderError, TXXX  # type: ignore[reportPrivateImportUsage]

from src.config import load_config
from src.id3 import tag_writer as tw
from src.id3.id3_tag_handler import create_frame, save_id3
from src.music_library import refresh_library_entry
from src.trim import trim
from src.trim.trim_editor import (
    Marks, set_in, set_out, resulting_duration, sibling_durations,
    _run_marking_screen, _track_title_artist, _fmt, resolve_chapters,
)
from src.utils import prompt
from src.utils import ui_utils
from src.utils.ui_utils import Colors as C

# Preview columns for the commit pass: file · cut points · resulting duration.
_COMMIT_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim'),
    prompt.Column(style='dynamic-dim', align='right', pin=True),
]


# ---------------------------------------------------------------------------
# Candidate detection (section 5.1) — a pass over the cached library list,
# no file access. Groups are keyed by (artist, album), matching how the
# browse menus already group a series/season.
# ---------------------------------------------------------------------------

def _group_by_album(library: list) -> dict[tuple[str, str], list[dict]]:
    """MP3 tracks with a cached duration, grouped by (artist, album) — trimming
    is MP3-only (section 2.2), so non-MP3 tracks are excluded up front."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for t in library:
        path = t.get('path')
        dur = t.get('duration')
        if not path or not dur or tw.format_kind(path) != 'mp3':
            continue
        key = (t.get('artist') or '', t.get('album') or '')
        groups.setdefault(key, []).append(t)
    return groups


def round_duration_candidates(library: list, *, tolerance_s: float = 1.0) -> list[dict]:
    """Signal 1: a cluster of >=2 tracks in a group sharing an identical
    duration that's a whole number of minutes. Near-conclusive on its own,
    but a group that is *entirely* one round duration only counts once a
    sibling group (same artist) proves that varying naturally is the norm —
    a series that genuinely always runs the same length isn't a false
    positive to flag (section 5.1's "beware false positives")."""
    groups = _group_by_album(library)
    results = []
    for (artist, album), tracks in groups.items():
        durations = [t['duration'] for t in tracks]
        if len(durations) < 2:
            continue

        # The largest same-value cluster (within tolerance).
        best_value, best_count = durations[0], 0
        for d in durations:
            n = sum(1 for x in durations if abs(x - d) <= tolerance_s)
            if n > best_count:
                best_value, best_count = d, n
        if best_count < 2:
            continue

        minutes = best_value / 60.0
        if abs(minutes - round(minutes)) * 60 > tolerance_s or round(minutes) <= 0:
            continue  # not a whole number of minutes

        clustered = [t for t in tracks if abs(t['duration'] - best_value) <= tolerance_s]
        others = [t['duration'] for t in tracks if t not in clustered]
        varies_within = any(abs(d - best_value) > tolerance_s * 3 for d in others)

        if best_count < len(tracks) and not varies_within:
            continue  # the "outliers" aren't actually different — no real cluster

        if best_count == len(tracks):
            # Whole group is one round value — only flag if a sibling group
            # (same artist, different album) shows real tracks vary naturally.
            varies_across = any(
                len(sib) >= 2 and (max(s['duration'] for s in sib) - min(s['duration'] for s in sib)) > tolerance_s * 3
                for (a2, alb2), sib in groups.items() if a2 == artist and alb2 != album
            )
            if not varies_across:
                continue

        results.append({
            'artist': artist, 'album': album,
            'paths': [t['path'] for t in clustered],
            'duration': best_value, 'count': best_count, 'total': len(tracks),
        })
    return results


def duration_outliers(library: list, *, outlier_margin_s: float = 60.0) -> list[dict]:
    """Signal 2: one track materially longer than the rest of its group, e.g.
    one episode at 29:36 in a series otherwise spanning 27:24-28:12."""
    groups = _group_by_album(library)
    results = []
    for (artist, album), tracks in groups.items():
        if len(tracks) < 3:
            continue
        durations = sorted(t['duration'] for t in tracks)
        median = durations[len(durations) // 2]
        for t in tracks:
            if t['duration'] - median >= outlier_margin_s:
                results.append({
                    'artist': artist, 'album': album, 'path': t['path'],
                    'duration': t['duration'], 'median_sibling': median,
                })
    return results


def group_duration_mismatch(library: list, *, mismatch_ratio: float = 1.10) -> list[dict]:
    """Signal 3 (advisory): one album/series averaging materially longer than
    its sibling albums by the same artist."""
    groups = _group_by_album(library)
    by_artist: dict[str, list[tuple[str, list[dict]]]] = {}
    for (artist, album), tracks in groups.items():
        by_artist.setdefault(artist, []).append((album, tracks))

    results = []
    for artist, albums in by_artist.items():
        if len(albums) < 2:
            continue
        means = {album: sum(t['duration'] for t in tracks) / len(tracks) for album, tracks in albums}
        overall = sum(means.values()) / len(means)
        if overall <= 0:
            continue
        for album, mean_d in means.items():
            if mean_d / overall >= mismatch_ratio:
                results.append({'artist': artist, 'album': album,
                                'mean_duration': mean_d, 'group_average': overall})
    return results


# ---------------------------------------------------------------------------
# Sting seeding (section 4.4 / 5.2.1 / 5.2.2). Pure aside from the ffmpeg
# decode calls it makes through trim.decode_mono_pcm/find_sting — no terminal
# output, no marks mutation; the conveyor decides what to do with the result.
# ---------------------------------------------------------------------------

# window_bounds and seed_by_sting are pure engine logic (no terminal/VLC
# touch) and are shared with trim_editor.py's single-track suggestion, so
# they live in trim.py; kept as local aliases since every call site here
# already refers to them by these names.
_window_bounds = trim.window_bounds
seed_by_sting = trim.seed_by_sting


# ---------------------------------------------------------------------------
# The conveyor (section 5.2).
# ---------------------------------------------------------------------------

@dataclass
class _TrackState:
    marks: Marks = field(default_factory=Marks)
    undo: list = field(default_factory=list)
    done: bool = False
    chapters: tuple[list[tuple], list[str], int | None] | None = None


def _build_strip(paths: list[str], state: dict[str, _TrackState], idx: int) -> list[str]:
    """One line of group state: position, and a glyph per track — the only
    group-level UI (section 5.2). ● current, ✔ marked, ◐ partly marked,
    ○ untouched."""
    indent = " " * ui_utils.MARGIN_H
    glyphs = []
    for i, p in enumerate(paths):
        s = state[p]
        has_in = s.marks.in_snapped is not None
        has_out = s.marks.out_snapped is not None
        if i == idx:
            glyphs.append(f"{C.ACCENT}{C.BOLD}●{C.RESET}")
        elif s.done and has_in and has_out:
            glyphs.append(f"{C.PRIMARY}✔{C.RESET}")
        elif has_in or has_out:
            glyphs.append(f"{C.ACCENT}◐{C.RESET}")
        else:
            glyphs.append(f"{C.DIM}○{C.RESET}")
    return [
        f"{indent}{C.BOLD}Trim group{C.RESET} · {idx + 1}/{len(paths)}  {' '.join(glyphs)}",
        "",
    ]


def _commit_group(paths: list[str], state: dict[str, _TrackState], library: list, header) -> None:
    """One write pass over every track marked done (section 5.2.3): a preview
    first, then synchronous foreground trims. A per-track failure is tallied
    and doesn't abort the group; Esc between tracks finishes the current file
    then stops and reports what was and wasn't written."""
    todo = [p for p in paths if state[p].done
            and state[p].marks.in_snapped is not None and state[p].marks.out_snapped is not None]
    if not todo:
        ui_utils.show_status("Nothing marked to trim.")
        return

    rows = []
    for p in todo:
        m = state[p].marks
        dur = resulting_duration(m)
        rows.append(prompt.Choice(
            title=os.path.basename(p), value=p, checked=True,
            cells=[os.path.basename(p),
                   f"{_fmt(m.in_snapped)} → {_fmt(m.out_snapped)}",
                   f"{dur:.1f}s" if dur is not None else "—"]))

    sub = ui_utils.plural(len(todo), "track") + " marked"
    sel = prompt.select("Preview — ↵ commits:", choices=rows,
                        columns=_COMMIT_COLUMNS, header=header(sub), multi=True)
    if not sel:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return

    from src.utils.terminal_input import raw_mode, get_key_non_blocking, clear_escape_buffer

    task_id = "trim_bulk"
    count = errors = 0
    interrupted = False
    with raw_mode(sys.stdin):
        for i, p in enumerate(todo):
            if p not in apply_set:
                continue
            ui_utils.set_status(task_id, f"Trimming {i + 1}/{len(todo)}: {os.path.basename(p)}")
            ui_utils.print_inline_progress(f"Trimming {i + 1}/{len(todo)}: {os.path.basename(p)}",
                                           i / len(todo))
            m = state[p].marks
            assert m.in_snapped is not None and m.out_snapped is not None  # `todo`'s filter guarantees this
            result = trim.commit_trim(p, m.in_snapped, m.out_snapped,
                                      library=library, chapters=state[p].chapters)
            if result.ok:
                count += 1
            else:
                errors += 1

            key = get_key_non_blocking()
            if key:
                clear_escape_buffer()
                if key in ('\x1b', 'ESC', 'q', 'Q'):
                    interrupted = True
                    break
    ui_utils.set_status(task_id, None)
    ui_utils.clear_inline_progress()

    msg = f"Trimmed {count} file(s)."
    if interrupted:
        msg += " Stopped early (Esc)."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def _mark_sting(reference_path: str, track_length: float, frame_dur: float) -> tuple[float, float] | None:
    """Mark the sting's own boundaries on the reference track, reusing the
    normal marking screen with relabeled hints — these marks are the sting's
    extent, not a cut point. None if the user backs out."""
    m = Marks()
    outcome = _run_marking_screen(
        reference_path, m, [],
        track_length=track_length, frame_dur=frame_dur,
        track_name="Mark the sting — its start and end", track_artist="",
        siblings=[],
        finish_key='s', finish_hint='use as sting', finish_verb='use this as the sting',
    )
    if outcome != 's' or m.in_snapped is None or m.out_snapped is None:
        return None
    return m.in_snapped, m.out_snapped


def _pick_sting_bounds(paths: list[str], *, region: str = 'head') -> tuple[float, float] | None:
    """Offer auto-discovered candidate stings (5.2.1) — segments the
    reference track (paths[0]) shares with another track in the group, each
    auditionable — or fall back to marking the sting by hand. `region`
    controls whether the head or tail of each track is scanned (opening vs.
    closing sting). None if the user backs out entirely."""
    reference_path = paths[0]

    def _mark_manually() -> tuple[float, float] | None:
        try:
            frame_dur = trim.probe_frame_duration(reference_path)
        except ValueError as e:
            ui_utils.show_status(str(e))
            return None
        from mutagen.mp3 import MP3
        track_length = MP3(reference_path).info.length
        return _mark_sting(reference_path, track_length, frame_dur)

    if len(paths) < 2:
        return _mark_manually()

    window_s = float(load_config().get("trim_sting_window_s", 90.0))
    ref_start_s, ref_dur_s = _window_bounds(reference_path, window_s, region)
    pcm_a = trim.decode_mono_pcm(reference_path, ref_start_s, ref_dur_s)
    cmp_start_s, cmp_dur_s = _window_bounds(paths[1], window_s, region)
    pcm_b = trim.decode_mono_pcm(paths[1], cmp_start_s, cmp_dur_s)
    candidates = trim.find_candidate_stings(pcm_a, pcm_b)
    if not candidates:
        return _mark_manually()
    # Candidates are relative to the scanned window — make them absolute
    # positions in the reference track before they're used as sting bounds.
    for c in candidates:
        c['start'] += ref_start_s
        c['end'] += ref_start_s

    mp = None
    if _vlc is not None:
        try:
            inst = _vlc.Instance('--no-video', '--quiet')
            mp_i = inst.media_player_new()          # type: ignore[union-attr]
            mp_i.set_media(inst.media_new(reference_path))  # type: ignore[union-attr]
            mp = mp_i
        except (AttributeError, OSError):
            mp = None

    def _audition(value) -> None:
        if mp is None or not isinstance(value, tuple):
            return
        start, end = value
        mp.set_time(int(start * 1000))
        if not mp.is_playing():
            mp.play()
        time.sleep(min(end - start, 6.0))
        mp.pause()

    choices = [
        prompt.Choice(title=f"{_fmt(c['start'])} → {_fmt(c['end'])}  (score {c['score']:.2f})",
                     value=(c['start'], c['end']))
        for c in candidates
    ]
    choices.append(prompt.separator())
    choices.append(prompt.Choice(title="Mark manually…", value='__manual__'))

    picked = prompt.select(
        "Candidate stings found (shared with another track in the group):",
        choices=choices,
        on_inspect=_audition if mp is not None else None, inspect_key='p',
        extra_hints={'p': 'audition'} if mp is not None else None,
    )
    if mp:
        try: mp.stop()
        except Exception: pass

    if picked is None:
        return None
    if picked == '__manual__':
        return _mark_manually()
    assert isinstance(picked, tuple)
    return picked


# Also pure engine logic (see the window_bounds/seed_by_sting note above) —
# shared with trim_editor.py's single-track suggestion.
_learn_sting_from_history = trim.learn_sting_from_history


def _seed_group_by_sting(paths: list[str], state: dict[str, _TrackState], *, bound: str = 'in') -> None:
    """Pick the sting once — learned from an earlier trim in this folder,
    auto-discovered against the reference track (paths[0]), or marked by hand
    — ask which side the cut falls on (skipped when learned: direction
    already answers it), then seed every other track's mark from the shape
    match (section 5.2.1). `bound` is 'in' (an opening sting) or 'out' (a
    closing one) — head and tail carry the keep/drop choice independently
    (section 5.2.2). Tracks that score too low are left unseeded."""
    region = 'head' if bound == 'in' else 'tail'
    which = "opening" if bound == 'in' else "closing"
    cfg = load_config()
    window_s = float(cfg.get("trim_sting_window_s", 90.0))
    min_score = float(cfg.get("trim_sting_min_score", 0.3))

    learned = _learn_sting_from_history(paths, region, window_s, min_score)
    used_history = learned is not None and prompt.confirm(
        f"Found a matching {which} sting from an earlier trim in this folder — use it?", default=True)

    def _apply(path: str, mark_s: float) -> bool:
        try:
            frame_dur = trim.probe_frame_duration(path)
        except ValueError:
            return False
        if bound == 'in':
            set_in(state[path].marks, mark_s, frame_dur)
        else:
            from mutagen.mp3 import MP3
            track_length = MP3(path).info.length
            set_out(state[path].marks, mark_s, frame_dur, track_length)
        return True

    if used_history:
        assert learned is not None
        reference_path, sting_start, sting_end, side = learned
        others = list(paths)
    else:
        bounds = _pick_sting_bounds(paths, region=region)
        if bounds is None:
            return
        sting_start, sting_end = bounds
        reference_path = paths[0]

        if bound == 'in':
            choices = [
                prompt.Choice(title="Keep it — the in-point is the sting's start", value='start'),
                prompt.Choice(title="Drop it — the in-point is the sting's end", value='end'),
            ]
        else:
            choices = [
                prompt.Choice(title="Keep it — the out-point is the sting's end", value='end'),
                prompt.Choice(title="Drop it — the out-point is the sting's start", value='start'),
            ]
        side = prompt.select("Which side of the sting does the cut fall on?", choices=choices)
        if side is None:
            return

        ref_edge = sting_start if side == 'start' else sting_end
        if not _apply(reference_path, ref_edge):
            ui_utils.show_status(f"Could not read {os.path.basename(reference_path)}'s frame info.")
            return
        others = [p for p in paths if p != reference_path]

    sting_dur = sting_end - sting_start
    reference_pcm = trim.decode_mono_pcm(reference_path, sting_start, sting_dur)
    results = seed_by_sting(reference_pcm, sting_dur, side, 0.0, others,
                            window_s=window_s, min_score=min_score, region=region)

    seeded, skipped = (0 if used_history else 1), 0
    for path, result in results.items():
        if result is None:
            skipped += 1
            continue
        mark_s, _score = result
        if _apply(path, mark_s):
            seeded += 1
        else:
            skipped += 1

    msg = f"Seeded {seeded} track(s) from the {which} sting" + (" (learned from an earlier trim)." if used_history else ".")
    if skipped:
        msg += f" {skipped} left unseeded (low match score)."
    ui_utils.show_status(msg)


def _propagate_absolute_offset(paths: list[str], state: dict[str, _TrackState]) -> None:
    """Fallback seeding for a group with no usable sting (section 5.2.1): mark
    the reference track normally, then copy its absolute in/out timestamps to
    every other track directly. The weaker tool — padding varies between
    episodes, and a news bulletin overrunning shifts everything after it — so
    it never commits on its own; each track still goes through the normal
    per-track review (and re-marking) in the walk that follows."""
    reference_path = paths[0]
    try:
        frame_dur = trim.probe_frame_duration(reference_path)
    except ValueError as e:
        ui_utils.show_status(str(e))
        return
    from mutagen.mp3 import MP3
    track_length = MP3(reference_path).info.length
    track_name, track_artist = _track_title_artist(reference_path)

    s = state[reference_path]
    outcome = _run_marking_screen(
        reference_path, s.marks, s.undo,
        track_length=track_length, frame_dur=frame_dur,
        track_name=track_name, track_artist=track_artist, siblings=[],
        finish_key='s', finish_hint='use these marks', finish_verb='use these marks',
    )
    if outcome != 's':
        return
    assert s.marks.in_requested is not None and s.marks.out_requested is not None
    in_s, out_s = s.marks.in_requested, s.marks.out_requested

    seeded = 0
    for path in paths:
        if path == reference_path:
            continue
        try:
            other_frame_dur = trim.probe_frame_duration(path)
        except ValueError:
            continue
        set_in(state[path].marks, in_s, other_frame_dur)
        set_out(state[path].marks, out_s, other_frame_dur, MP3(path).info.length)
        seeded += 1

    ui_utils.show_status(
        f"Propagated the same timestamps to {seeded} other track(s) — "
        f"review each one, padding may vary.")


def trim_conveyor(paths: list, library: list, header) -> None:
    """Walk `paths` one track at a time, marking each with the same screen as
    single-track trim, then commit the whole batch in one pass (section 5.2).
    Marking is separated from writing: nothing is trimmed until the preview
    at the end is confirmed."""
    mp3_paths = [p for p in paths if tw.format_kind(p) == 'mp3']
    if not trim.HAS_FFMPEG:
        ui_utils.show_status("Could not open the trimmer — ffmpeg isn't installed. See README.md.")
        return
    if not mp3_paths:
        ui_utils.show_status("No MP3 tracks to trim.")
        return

    state: dict[str, _TrackState] = {p: _TrackState() for p in mp3_paths}
    flags: dict = {}
    idx = 0

    # Sting seeding is the primary way a group gets seeded (section 4.4) — on
    # offer before the walk starts, not a hidden extra. Head and tail are
    # independent (section 5.2.2): a series can have either, both, or neither.
    if len(mp3_paths) > 1:
        if prompt.confirm("Seed this group from a shared opening (a sting)?", default=True):
            _seed_group_by_sting(mp3_paths, state, bound='in')
        elif prompt.confirm(
                "No sting — propagate one track's timestamp to the rest instead? "
                "(the weaker tool: assumes identical padding across episodes)", default=False):
            _propagate_absolute_offset(mp3_paths, state)
        if prompt.confirm("Seed the tail from a shared closing too?", default=False):
            _seed_group_by_sting(mp3_paths, state, bound='out')

    while 0 <= idx < len(mp3_paths):
        path = mp3_paths[idx]
        try:
            frame_dur = trim.probe_frame_duration(path)
        except ValueError as e:
            ui_utils.show_status(str(e))
            idx += 1
            continue

        from mutagen.mp3 import MP3
        track_length = MP3(path).info.length
        track_name, track_artist = _track_title_artist(path)
        siblings = sibling_durations(library, path)
        s = state[path]

        outcome = _run_marking_screen(
            path, s.marks, s.undo,
            track_length=track_length, frame_dur=frame_dur,
            track_name=track_name, track_artist=track_artist, siblings=siblings,
            finish_key='s', finish_hint='record & next', finish_verb='record',
            extra_key='n', extra_hint='skip & next',
            strip_lines=_build_strip(mp3_paths, state, idx), flags=flags,
        )

        if outcome == 's':
            s.done = True
            # A track's chapter questions come up when its turn comes, not
            # batched at commit time, and only if it has chapters (section 5.2).
            assert s.marks.in_snapped is not None and s.marks.out_snapped is not None
            s.chapters = resolve_chapters(path, s.marks.in_snapped, s.marks.out_snapped)
            idx += 1
        elif outcome == 'n':
            s.done = False
            idx += 1
        elif outcome == 'ESC':
            if idx == 0:
                marked = any(v.marks.in_snapped is not None or v.marks.out_snapped is not None
                            for v in state.values())
                if marked and not prompt.confirm(
                        "Leave without committing? Marks made so far are discarded.", default=False):
                    continue
                return
            idx -= 1

    _commit_group(mp3_paths, state, library, header)


# Loudness preview: file · measured level · proposed gain.
_REPLAYGAIN_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', align='right'),
    prompt.Column(style='dynamic-dim', align='right', pin=True),
]


def apply_replaygain_op(paths: list, library: list, header) -> None:
    """Measure loudness and propose a per-track gain (section 5.4): tags
    only, no audio bytes change. A separate operation from the trim, and one
    that only makes sense run after it — a pre-trim measurement would
    include the continuity announcement or trailer about to be cut, which is
    exactly the loud material that would skew it. A track already at target
    gets no frame written (section 5.4.5)."""
    mp3_paths = [p for p in paths if tw.format_kind(p) == 'mp3']
    if not trim.HAS_FFMPEG:
        ui_utils.show_status("Could not measure loudness — ffmpeg isn't installed. See README.md.")
        return
    if not mp3_paths:
        ui_utils.show_status("No MP3 tracks to measure.")
        return

    target_lufs = float(load_config().get("trim_target_lufs", -18.0))
    task_id = "trim_loudness"
    measurements: dict[str, dict] = {}
    for i, path in enumerate(mp3_paths):
        label = f"Measuring {i + 1}/{len(mp3_paths)}: {os.path.basename(path)}"
        ui_utils.set_status(task_id, label)
        ui_utils.print_inline_progress(label, i / len(mp3_paths))
        result = trim.measure_track_gain(path, target_lufs=target_lufs)
        if result is not None:
            measurements[path] = result
    ui_utils.set_status(task_id, None)
    ui_utils.clear_inline_progress()

    if not measurements:
        ui_utils.show_status("Could not measure any of these tracks.")
        return

    # Write nothing when there's nothing to correct — the same rule as the
    # sort-tag convention in docs/tag-etiquette.md.
    candidates = {p for p, m in measurements.items() if abs(m['gain_db']) >= 0.1}
    if not candidates:
        ui_utils.show_status("Every measured track is already at target — nothing to write.")
        return

    rows = []
    for path in mp3_paths:
        m = measurements.get(path)
        if m is None:
            continue
        clip_note = "  ⚠ may clip" if m['clips'] else ""
        rows.append(prompt.Choice(
            title=os.path.basename(path), value=path, checked=path in candidates,
            cells=[os.path.basename(path), f"{m['integrated_lufs']:.1f} LUFS",
                   f"{m['gain_db']:+.2f} dB{clip_note}"]))

    sub = f"target {target_lufs:.0f} LUFS · " + ui_utils.plural(len(candidates), "track") + " to change"
    sel = prompt.select("Preview — ↵ writes gain tags:", choices=rows,
                        columns=_REPLAYGAIN_COLUMNS, header=header(sub), multi=True)
    if not sel:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return

    count = errors = 0
    for path in mp3_paths:
        if path not in apply_set:
            continue
        m = measurements.get(path)
        if m is None:
            continue
        try:
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()
            audio.delall('TXXX:REPLAYGAIN_TRACK_GAIN')
            audio.delall('TXXX:REPLAYGAIN_TRACK_PEAK')
            audio.add(TXXX(encoding=3, desc='REPLAYGAIN_TRACK_GAIN', text=[f"{m['gain_db']:+.2f} dB"]))
            audio.add(TXXX(encoding=3, desc='REPLAYGAIN_TRACK_PEAK', text=[f"{m['peak_linear']:.6f}"]))
            rva2 = create_frame('RVA2', {'gain': m['gain_db']})
            if rva2 is not None:
                audio.delall('RVA2')
                audio.add(rva2)
            save_id3(audio, path)
        except Exception:
            errors += 1
            continue
        count += 1
        try:
            refresh_library_entry(library, path)
        except Exception:
            pass

    msg = f"Wrote gain tags for {count} file(s)."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)
