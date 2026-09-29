"""
Lyrics editor — unified sync and fine-tune tool.

Data sources (auto-detected in order):
  1. Transcript JSON  (Transcript/ .json)  — word-level timing; saves JSON + SRT
  2. SYLT ID3 tag                          — line-level timing; saves SYLT
  3. USLT ID3 tag                          — untimed lyrics; saves SYLT after tap

Modes:
  SEG    browse list; ↑↓ navigate, ←→/,./[] adjust timestamps
  WORD   per-word editing (transcript source only)
  TAP    real-time tap — audio plays, SPACE marks current line's start
  EDIT   type exact timestamp

Review walkthroughs (walk each item so it gets fixed without scrolling; Tab/⇧tab
step next/prev, recomputed live so fixed items drop out, Esc leaves, save (s) and
re-enter later to resume on whatever is outstanding):
  R  issues     — MD mismatches, then word-timing errors, then overlaps
  D  directions — uncategorised stage directions (x), then untimed ones (e / t)
  L  long lines — over-long spoken lines; / splits at the best semantic break

/ (in SEG): split the current spoken line at its strongest punctuation break
  nearest the middle (or the middle if none), redistributing words and timing.

Stage directions (on a stage-direction row in SEG) — press x to cycle the kind:
  inline    ✦ indented under the words (a mid-phrase beat)
  tone      ~ same indent — tonal / pronunciation note (e.g. drawn-out speech)
  external  a framed section (scene/sound, or another person's aside); who and
            when are left to be read from the text and context.
"""
from __future__ import annotations
import sys, os, json, time

from src.music_library import drop_moved, track_title
from src.utils import ui_utils
from src.utils.ui_utils import Colors as C
from src.utils.prompt_core import _Widget, _read_key, _wait_for_keypress, _set_raw, _restore_term_attrs, _get_term_attrs, _cols
from src.utils.prompt import text as _prompt_text
from src.utils.prompt_core import add_hint_click_cells_auto, _visible_rows
from src.utils import prompt as _promptmod
from src.utils.files import write_text_atomic, backup_copy
from src.utils.log import log, quietly

# The MD↔JSON alignment is shared with the playback lyric display so the two
# always agree on speakers, stage directions and line text (see md_overlay).
from src.lyrics.md_overlay import _sd_scope, _reading_time, build_md_overlay as _build_md_overlay
from src.lyrics.time_fields import (
    _EDIT_END, _EDIT_LIM, _EDIT_MAXLEN, _EDIT_ORDER, _EDIT_START, _field_str, _field_value,
    _ts_parts,
)
from src.lyrics.sync_doc import (
    SOURCE_SYLT, SOURCE_TRANSCRIPT, SOURCE_USLT, _REVIEW_PHASE_NAME, _REVIEW_PROGRAMS,
    _best_split_index, _clean_seg, _ensure_ids, _file_fp, _load, _rebuild_srt,
    _review_phase_issues, _shift_seg, _shift_word,
)
from src.lyrics.verify import _split_candidates, _split_seg_at, _verify_matchup
from src import tuning as tune
from src.lyrics.editor_view import EDIT, SEG, TAP, WORD, _AUD_CLIP, _clip, _draw, _vlc
from src.lyrics.editor_keys import _QUIT, _KeyHandlers


class _Session(_KeyHandlers):
    """One editing session over a track's timed lyrics: the document, the cursor
    and mode, the undo stack, the inline editors and the audition player."""

    def __init__(self, mp3_path: str, loaded: tuple) -> None:
        self.mp3_path = mp3_path
        self.segs, self.source, self.aux = loaded
        self.track_name = track_title(self.mp3_path, read_tags=True)
        if self.aux.get('drift'):
            ui_utils.show_status(
                "⚠ transcript.json changed since this working copy — W will overwrite it.", duration=tune.STATUS_WARNING_S)

        # Lead-in offset for tap sync (compensates for reaction time)
        try:
            from src.config import load_config
            cfg = load_config()
            self.tap_offset_s = -cfg["lyric_lead_in"]  # seconds; load_config fills in the default
        except Exception:
            self.tap_offset_s = 0.0

        self.mode       = SEG
        self.prev_mode  = SEG
        self.cursor     = 0
        self.seg_cursor = 0
        self.selected: set[int] = set()
        self.viewport   = 0
        self.dirty      = False
        self.show_hints = False
        self.review_phase: str | None = None          # current phase, or None when not reviewing
        self.review_program: str | None = None        # 'issues' (R) or 'dirs' (D)
        self.md_overlay: list | None = None
        self.md_quality: dict | None = None
        self.md_path:    str  | None = None
        self.undo_stack: list = []

        self.edit_fields: dict[str, list[str]] = {}   # fk → digit chars (MM:SS.mmm per bound)
        self.edit_orig:   dict[str, str]       = {}   # snapshot at open → detect what changed
        self.edit_fi    = 0                            # active field index into _EDIT_ORDER
        self.edit_pos   = 0                            # caret position within the active field
        self.edit_fresh = False                        # active field untouched → next digit clears it

        self.playing    = False
        self.play_until = 0.0
        self.play_pos   = 0.0

        self.aud_queue: list[tuple[float, float, str]] = []   # AUDITION: clips left to play
        self.aud_now:   str | None = None                      # 'start' / 'end' clip playing now
        self.aud_editing = False                               # AUDITION: inline timestamp editor open

        self.fd  = sys.stdin.fileno()
        self.old = _get_term_attrs(self.fd)

        self.mp = None
        if _vlc is not None:
            try:
                old_fd  = os.dup(2)
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2); os.close(devnull)
                inst = _vlc.Instance('--no-video', '--quiet')
                mp_i = inst.media_player_new()           # type: ignore[union-attr]
                mp_i.set_media(inst.media_new(self.mp3_path)) # type: ignore[union-attr]
                os.dup2(old_fd, 2); os.close(old_fd)
                self.mp = mp_i
            except (AttributeError, OSError):
                pass

        self.w = _Widget(self.fd)

    def _sources_label(self) -> str:
        """The documents this session is actually reading, named as they are on disk.

        `working copy` matters more than the rest: resuming from the `.sync.json`
        sidecar means edits are going there and not to the transcript until W, and
        a `diverged` copy means the transcript has changed underneath it since. Both
        were previously announced once at load and then invisible for the rest of
        the session.
        """
        parts: list[str] = []
        if self.aux.get('jpath'):
            parts.append(os.path.basename(self.aux['jpath']))
        if self.aux.get('from_sidecar'):
            parts.append('diverged working copy' if self.aux.get('drift') else 'working copy')
        if self.source == SOURCE_SYLT:
            parts.append('embedded SYLT')
        elif self.source == SOURCE_USLT:
            parts.append('embedded USLT')
        if self.md_path:
            parts.append(os.path.basename(self.md_path))
        return ' · '.join(parts)

    def cur_words(self) -> list:
        """Words of the segment currently open in WORD mode."""
        return self.segs[self.seg_cursor].get("words", []) if self.segs else []

    def _sync_seg_bounds(self, si: int) -> None:
        """Recompute segment si's start/end from its first/last word timing after a word edit."""
        words = self.segs[si].get("words", [])
        if not words: return
        first = words[0].get("start")
        last  = words[-1].get("end")
        if first is not None: self.segs[si]["start"] = round(first, 3)
        if last  is not None: self.segs[si]["end"]   = round(last,  3)

    def do_preview(self, start_s: float, dur: float | None = None) -> None:
        """Play from start_s.  With `dur`, stop automatically after that many
        seconds (the main loop honours play_until); without it, play open-ended."""
        if self.mp is None: return
        start_s = max(0.0, start_s)
        self.mp.set_time(int(start_s * 1000))
        if not self.mp.is_playing():
            self.mp.play(); time.sleep(0.15)
        self.playing    = True
        self.play_until = float('inf') if dur is None else time.time() + dur
        self.play_pos   = start_s

    def do_stop(self) -> None:
        """Pause playback and clear the playing flag."""
        if self.mp and self.mp.is_playing(): self.mp.pause()
        self.playing = False

    def _aud_next(self) -> None:
        """Play the next queued clip; stop when the queue drains."""
        if not self.aud_queue:
            self.aud_now = None; self.do_stop(); return
        lo, hi, label = self.aud_queue.pop(0)
        self.aud_now = label
        self.do_preview(lo, max(0.1, hi - lo))

    def _aud_clips(self, which: str = 'line') -> None:
        """Queue audition playback for the current line and start it:
          'line'  → the whole line, start → end (the default when you land on it);
          'start' / 'end' → a short (<=_AUD_CLIP) clip of just that boundary;
          'both'  → the start clip then the end clip, in sequence.
        Clips never bleed into the neighbouring lines."""
        self.aud_queue = []
        if self.segs and self.cursor < len(self.segs):
            s = self.segs[self.cursor].get("start")
            e = self.segs[self.cursor].get("end")
            if which == 'line' and s is not None:
                hi = e if e is not None else s + 3.0
                self.aud_queue.append((s, max(s + 0.1, hi), 'line'))
            if which in ('both', 'start') and s is not None:
                hi = s + _AUD_CLIP if e is None else min(e, s + _AUD_CLIP)
                self.aud_queue.append((s, max(s + 0.1, hi), 'start'))
            if which in ('both', 'end') and e is not None:
                floor = s if s is not None else 0.0     # never precede the line's start
                lo = max(0.0, floor, e - _AUD_CLIP)
                self.aud_queue.append((lo, e, 'end'))
        self._aud_next()

    def _aud_shift(self, delta: float) -> None:
        """Move the whole line — both timestamps and its words — by delta, keeping
        its duration, then play the start so the new position can be judged by ear.
        (The duration itself is changed by typing in the editor: press e.)"""
        if not self.segs or self.cursor >= len(self.segs):
            return
        if self.segs[self.cursor].get("start") is None:
            ui_utils.show_status("This line has no timestamp to move."); return
        self.apply_segs([self.cursor], delta)   # shifts start, end and words together (undoable)
        self._aud_clips('start')

    def refresh_overlay(self) -> None:
        """Re-derive the MD overlay from the current segs.  Call after ANY
        structural change to segs.  The overlay is a pure projection of
        (segs, MD): rebuilding is always safe and never duplicates, because
        committed stage_dir segs are reconciled inside _build_md_overlay."""
        if self.md_path:
            self.md_overlay, self.md_quality, _links = _build_md_overlay(self.segs, self.md_path)
            for _si, _lid in _links.items():   # record durable alignment on segs
                if _lid is not None:
                    self.segs[_si]['line_ref'] = _lid
        else:
            self.md_overlay = self.md_quality = None

    def _review_enter(self, program: str) -> None:
        """Start a walkthrough (program 'issues' or 'dirs'): jump to the first
        outstanding item, earliest phase first."""
        for ph in _REVIEW_PROGRAMS[program]:
            issues = _review_phase_issues(self.segs, ph, self.md_quality)
            if issues:
                self.review_program = program
                self.review_phase = ph
                self.cursor = issues[0]
                self.viewport = 0
                ui_utils.show_status(
                    f"Review · {_REVIEW_PHASE_NAME[ph]}: {len(issues)} to fix — "
                    f"Tab/⇧Tab next/prev, fix in place, Esc to leave.")
                return
        _none = ("stage directions need categorising or timing" if program == 'dirs'
                 else "MD mismatches, word or overlap errors")
        ui_utils.show_status(f"Nothing to review — no {_none}.")

    def _review_advance(self, direction: int) -> None:
        """Move to the next/prev outstanding item, recomputed live so fixed ones
        drop out; rolls through this program's phases and ends when none remain."""
        phases = _REVIEW_PROGRAMS.get(self.review_program, ())
        ph = self.review_phase
        while ph is not None and ph in phases:
            issues = _review_phase_issues(self.segs, ph, self.md_quality)
            if direction > 0:
                nxt = next((si for si in issues if si > self.cursor), None)
            else:
                nxt = next((si for si in reversed(issues) if si < self.cursor), None)
            if nxt is not None:
                self.review_phase = ph
                self.cursor = nxt
                self.viewport = 0
                return
            # exhausted this phase in this direction — step to the adjacent phase
            i = phases.index(ph) + (1 if direction > 0 else -1)
            if not (0 <= i < len(phases)):
                break
            ph = phases[i]
            issues = _review_phase_issues(self.segs, ph, self.md_quality)
            if issues:
                self.review_phase = ph
                self.cursor = issues[0] if direction > 0 else issues[-1]
                self.viewport = 0
                ui_utils.show_status(f"Review · {_REVIEW_PHASE_NAME[ph]}: {len(issues)} to fix.")
                return
        self.review_phase = None
        self.review_program = None
        ui_utils.show_status("Review complete — all resolved. (Save with s.)")

    def do_smart_split(self) -> None:
        """Split the current spoken line at its strongest semantic break nearest the
        middle (or the middle itself when there's no punctuation), redistributing
        words and timing like the per-word split. Repeat to split further."""
        if not (self.segs and 0 <= self.cursor < len(self.segs)):
            return
        seg = self.segs[self.cursor]
        if seg.get('kind') in ('dead_air', 'stage_dir'):
            ui_utils.show_status("Only spoken lines can be split."); return
        words = seg.get('words') or []
        if len(words) < 2:
            ui_utils.show_status("Line has too few timed words to split."); return
        text_toks = (seg.get('text') or '').split()
        i = _best_split_index(words, text_toks)
        w_a, w_b = words[:i], words[i:]
        # Keep each half's punctuation: rebuild text from the (punctuated) text
        # tokens when they line up 1:1 with the words, not from the bare word tokens.
        if len(text_toks) == len(words):
            a_txt, b_txt = ' '.join(text_toks[:i]), ' '.join(text_toks[i:])
        else:
            a_txt = ' '.join(ww.get('word', '').strip() for ww in w_a)
            b_txt = ' '.join(ww.get('word', '').strip() for ww in w_b)
        boundary = (w_b[0].get('start') or w_a[-1].get('end')
                    or round(((seg.get('start') or 0) + (seg.get('end') or 0)) / 2, 3))
        seg_a = {'start': seg.get('start'), 'end': round(boundary, 3),
                 'text': a_txt, 'words': w_a}
        seg_b = {'start': round(boundary, 3), 'end': seg.get('end'),
                 'text': b_txt, 'words': w_b}
        for k in ('kind', 'line_ref'):        # both halves stay the same kind / MD line
            if seg.get(k) is not None:
                seg_a[k] = seg_b[k] = seg[k]
        self.undo_stack.append(('split', self.cursor, seg))
        self.segs[self.cursor:self.cursor + 1] = [seg_a, seg_b]
        self.dirty = True
        self.refresh_overlay()
        _after = (text_toks[i - 1] if len(text_toks) == len(words)
                  else w_a[-1].get('word', '')).strip()
        ui_utils.show_status(f"Split after “{_after}”.")

    def apply_segs(self, idxs: list[int], delta: float) -> None:
        """Shift the given segments by delta seconds, marking dirty and recording an undo entry."""
        for i in idxs: _shift_seg(self.segs[i], delta)
        self.dirty = True; self.undo_stack.append(('seg', list(idxs), delta))

    def apply_word(self, si: int, wi: int, delta: float) -> None:
        """Shift one word by delta seconds, resync its segment's bounds, and record an undo entry."""
        _shift_word(self.segs[si]["words"][wi], delta)
        self._sync_seg_bounds(si)
        self.dirty = True; self.undo_stack.append(('word', si, wi, delta))

    def _may_quit(self) -> bool:
        """True to leave the editor: straight away with nothing unsaved,
        otherwise only if the user agrees to lose the changes."""
        if not self.dirty:
            return True
        _restore_term_attrs(self.fd, self.old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        _ans = _prompt_text("You have unsaved changes (s saves). Quit without saving? (y/N)")
        _set_raw(self.fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        self.w.anchor_reset()
        return (_ans or "").strip().lower().startswith("y")

    def do_undo(self) -> None:
        """Pop and reverse the most recent undo-stack entry, restoring segs/cursor/mode as needed."""
        if not self.undo_stack: return
        op = self.undo_stack.pop()
        if   op[0] == 'seg':
            [_shift_seg(self.segs[i], -op[2]) for i in op[1]]
        elif op[0] == 'seg_end':
            self.segs[op[1]]["end"] = op[2]
        elif op[0] == 'word':
            _shift_word(self.segs[op[1]]["words"][op[2]], -op[3])
            self._sync_seg_bounds(op[1])
        elif op[0] == 'word_end':
            self.segs[op[1]]["words"][op[2]]["end"] = op[3]
            self._sync_seg_bounds(op[1])
        elif op[0] == 'split':
            si, orig = op[1], op[2]
            self.segs[si:si + 2] = [orig]
            self.mode = SEG; self.cursor = si
        elif op[0] == 'join':
            ci, seg_a, seg_b = op[1], op[2], op[3]
            self.segs[ci:ci + 1] = [seg_a, seg_b]
            self.cursor = ci
        elif op[0] == 'delete':
            ci, seg = op[1], op[2]
            self.segs[ci:ci] = [seg]  # re-insert; do NOT overwrite the neighbour
            self.cursor = ci
        elif op[0] == 'tap':
            idx, old_start, old_prev_end = op[1], op[2], op[3]
            self.segs[idx]["start"] = old_start
            if idx > 0 and old_prev_end is None:
                self.segs[idx - 1]["end"] = None
            self.cursor = idx
        elif op[0] == 'snapshot':
            self.segs[:] = op[1]
        # Still unsaved: some edits (labels, dead air, fill gaps, credits) push
        # no undo entry, so an empty undo stack doesn't mean nothing changed.
        # Only a save clears this.
        self.dirty = True
        # segs may have changed structurally — keep the overlay in sync so
        # nothing is left pointing at stale indices.
        if op[0] in ('split', 'join', 'delete', 'snapshot'):
            self.refresh_overlay()

    def do_save(self) -> None:
        """Persist current edits: transcript source writes the working sidecar JSON;
        SYLT/USLT sources write the SYLT tag directly."""
        if self.source == SOURCE_TRANSCRIPT:
            # Save to the WORKING document (sidecar) — never touches the original
            # transcript.json until the user commits with 'W'.
            _ensure_ids(self.segs, self.aux['meta'])
            sdata = {'version': 1, 'source_json': os.path.basename(self.aux['jpath']),
                     'meta': self.aux['meta'], 'segments': self.segs}
            write_text_atomic(self.aux['sidecar'], json.dumps(sdata, indent=2, ensure_ascii=False))
            ui_utils.show_status(
                f"Saved to {os.path.basename(self.aux['sidecar'])} — press W to write transcript.json")
            self.dirty = False; self.undo_stack.clear()
            return
        else:
            from src.lyrics.lyrics import save_sylt_entries
            entries = []
            for s in self.segs:
                if s.get('start') is None:
                    continue
                _kind = s.get('kind')
                if _kind == 'dead_air' and not s.get('text', '').strip():
                    continue  # pure silence
                _txt = s.get('text', '').strip()
                # Wrap BOTH committed stage directions and labelled dead air in
                # *(...)* so _load reconstructs kind='stage_dir' on reload.  Without
                # this a labelled dead_air round-trips as a plain seg whose text can
                # collide with an MD stage direction and show twice.
                if _kind in ('stage_dir', 'dead_air'):
                    _txt = f"*({_txt})*" if _txt else ""
                    if not _txt: continue
                entries.append((_txt, max(0, int((s['start'] or 0) * 1000))))
            save_sylt_entries(self.aux['mp3'], entries, desc=self.aux.get('desc', ''), lang=self.aux.get('lang', 'eng'))
        changed: set[int] = set()
        for op in self.undo_stack:
            if   op[0] == 'seg':      changed.update(op[1])
            elif op[0] == 'seg_end':  changed.add(op[1])
            elif op[0] in ('word', 'word_end'): changed.add(op[1])
            elif op[0] == 'split':    changed.update([op[1], op[1] + 1])
            elif op[0] == 'join':     changed.add(op[1])
            elif op[0] == 'tap':      changed.add(op[1])
        ui_utils.show_status(f"Saved — {len(changed)} line{'s' if len(changed) != 1 else ''} changed.")
        self.dirty = False; self.undo_stack.clear()

    def do_commit(self) -> bool:
        """Write timings back to transcript.json (+ .srt) in the ORIGINAL Whisper
        schema: spoken segments only.  The editor's stage-direction / dead-air
        beats and all bookkeeping fields (ids, alignment, kind) are dropped — they
        live on in the sidecar and the .md — so transcript.json stays a plain
        Whisper transcript of just the spoken words."""
        jpath = self.aux['jpath']
        try:
            with open(jpath, encoding='utf-8') as f:
                container = json.load(f)
        except OSError:
            container = {}
        except json.JSONDecodeError:
            # Writing {} + segments would drop every other key it holds.
            ui_utils.show_status(f"{os.path.basename(jpath)} can't be read as JSON; not overwritten.", duration=tune.STATUS_WARNING_S)
            return False
        spoken = [s for s in self.segs if s.get('kind') not in ('stage_dir', 'dead_air', 'credit')]
        container['segments']      = [_clean_seg(s) for s in spoken]
        container['word_segments'] = [
            {'word': w.get('word', ''), 'start': w.get('start'),
             'end': w.get('end'), 'score': w.get('score')}
            for seg in spoken for w in seg.get('words', [])
        ]
        # The originals are kept as .bak (the last commit's), and each file is
        # written whole or not at all.
        backup_copy(jpath)
        backup_copy(jpath[:-5] + '.srt')
        write_text_atomic(jpath, json.dumps(container, indent=2, ensure_ascii=False))
        write_text_atomic(jpath[:-5] + '.srt', _rebuild_srt(self.segs))
        self.aux['meta']['source_fp'] = _file_fp(jpath)   # we now match the original
        # Also write an enriched sidecar (.sync.json) that preserves the
        # editor's stage-direction / dead-air beats so the player and editor
        # render the same segments when loading this track.
        try:
            _ensure_ids(self.segs, self.aux['meta'])
            sdata = {'version': 1, 'source_json': os.path.basename(jpath),
                     'meta': self.aux['meta'], 'segments': self.segs}
            write_text_atomic(jpath[:-5] + '.sync.json', json.dumps(sdata, indent=2, ensure_ascii=False))
        except Exception as exc:
            log.warning("couldn't write %s.sync.json: %s", jpath[:-5], exc)
            ui_utils.show_status(f"Committed, but the .sync.json couldn't be written: {exc}", duration=tune.STATUS_WARNING_S)
        return True

    def _pager(self, body: list, title: str) -> None:
        """Minimal scrollable full-screen viewer.  `body` lines are already
        coloured; the pager just windows and clips them."""
        vp = 0
        hint_cells: dict = {}
        while True:
            # Rebuilt each frame: the hints come and go with the corner toggle.
            foot = [f"{C.DIM}{ui_utils.divider()}{C.RESET}"] + \
                   _promptmod.chrome_hint_lines(
                       [('↑↓/j/k', 'scroll'), ('PgUp/PgDn', 'page'),
                        ('Home/End', 'ends'), ('q', 'back')])
            ui_utils.now_playing_lines(ui_utils.get_terminal_width())
            cols = _cols()
            avail = _visible_rows()
            vis = max(3, avail - len(foot) - 2)
            vp  = max(0, min(vp, max(0, len(body) - vis)))
            # The title (the summary / report path) heads the page, with the
            # help toggle beside it; it was passed in but never drawn.
            out = [_clip(f"  {C.BOLD}{title}{C.RESET}", cols), ""]
            for ln in body[vp:vp + vis]:
                out.append(_clip("  " + ln, cols + ui_utils.MARGIN_H))
            pad = max(0, (avail) - len(out) - len(foot))
            rendered = out + [""] * pad + foot
            hint_cells.clear()
            rendered[0] = _promptmod.add_help_corner(rendered[0], 0, hint_cells)
            self.w.render(rendered)
            for _i in range(len(rendered) - len(foot), len(rendered)):
                add_hint_click_cells_auto(hint_cells, rendered[_i], _i)
            if not _wait_for_keypress(0.2):
                continue
            k = _read_key(self.fd)
            if k.startswith('MOUSE_CLICK:') and self.w.row is not None:
                _p = k.split(':'); _r = int(_p[2]); _c = int(_p[3]) if len(_p) > 3 else 1
                k = hint_cells.get((_r - self.w.row - ui_utils.MARGIN_V, _c)) or ''
            _ch = _promptmod.consume_chrome(k, {})      # the transport keys it advertises
            if _ch is _promptmod.CHROME_REDRAW:
                self.w.anchor_reset(); continue
            if _ch is _promptmod.CHROME_HANDLED:
                continue
            if   k in ('q', 'ESC', 'CTRL_C'): break
            elif k in ('UP', 'k'):            vp -= 1
            elif k in ('DOWN', 'j', 'SPACE'): vp += 1
            elif k == 'PGUP':                 vp -= vis
            elif k == 'PGDN':                 vp += vis
            elif k == 'HOME':                 vp = 0
            elif k == 'END':                  vp = len(body)

    def do_verify(self) -> None:
        """Run the JSON<->MD verification report, write it alongside the transcript, and page through it."""
        from src.lyrics.lyrics import _find_markdown_for_audio
        _mdp = self.md_path or _find_markdown_for_audio(self.mp3_path)
        if not _mdp:
            ui_utils.show_status("No transcript.md found to verify against."); return
        rep = _verify_matchup(self.segs, _mdp)
        _rp = self.aux['jpath'][:-5] + '.verify.txt'
        try:
            with open(_rp, 'w', encoding='utf-8') as f:   # plain text (colour stripped)
                f.write("\n".join(ui_utils.strip_ansi(l) for l in rep['lines']) + "\n")
            _rp_note = f"  ·  report: {os.path.basename(_rp)}"
        except OSError:
            _rp_note = ""
        s = rep['summary']
        self._pager(rep['lines'],
               f"VERIFY  {s['match_pct']}% matched  ·  {s['discrepancies']} issues{_rp_note}")
        self.w.anchor_reset()

    def do_speaker_split(self) -> None:
        """Cut every segment the script says is more than one beat — a new speaker
        part way through, or an inline stage direction — pinning each piece to its
        MD line, then re-verify.  One beat per segment is the clean baseline to
        word-split from, and it is what gives each direction a boundary of its own:
        four *(Ding)*s on one line stop collapsing into one.

        Only cuts the script has punctuated its way into are made here. One that
        would land mid-sentence is reported by verify and left for you."""
        from src.lyrics.lyrics import _find_markdown_for_audio
        _mdp = self.md_path or _find_markdown_for_audio(self.mp3_path)
        if not _mdp:
            ui_utils.show_status("No transcript.md found to verify against."); return
        cands, _unplaced, _suggested = _split_candidates(self.segs, _mdp)
        if not cands:
            ui_utils.show_status("✔ Every segment is a single script beat — nothing to split.")
            return
        _restore_term_attrs(self.fd, self.old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        _lines = sum(1 for c in cands
                     if len({l for l in c['line_refs']}) > 1)
        _dirs  = len(cands) - _lines
        _what  = " and ".join(p for p in (
            f"{_lines} spanning multiple MD lines" if _lines else "",
            f"{_dirs} containing a stage direction" if _dirs else "") if p)
        _skip = (f"  {len(_suggested)} mid-sentence cut(s) left alone."
                 if _suggested else "")
        _ans = _prompt_text(
            f"Split {len(cands)} segment(s) — {_what}?{_skip} (y/N)")
        _set_raw(self.fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        self.w.anchor_reset()
        if not (_ans or "").strip().lower().startswith("y"):
            ui_utils.show_status("Split cancelled."); return
        self.undo_stack.append(('snapshot', list(self.segs)))
        n = len(cands)
        # split from the highest seg index down so earlier indices stay valid
        for c in sorted(cands, key=lambda c: c['seg'], reverse=True):
            pieces = _split_seg_at(self.segs[c['seg']], c['boundaries'], c['line_refs'])
            self.segs[c['seg']:c['seg'] + 1] = pieces
        self.cursor = min(self.cursor, len(self.segs) - 1)
        self.dirty = True
        self.refresh_overlay()
        remain = len(_split_candidates(self.segs, _mdp)[0])   # verification
        ui_utils.show_status(
            f"Split {n} segment(s) to one script beat each — {remain} remaining.")

    def do_recategorise(self, si: int) -> None:
        """Flip the beat at `si` between dead air (◌) and a stage direction (✦),
        preserving timings.  A direction needs descriptive text, so promoting
        blank silence prompts for it.  Shared by the 'k' key and the click path."""
        _seg = self.segs[si] if (self.segs and 0 <= si < len(self.segs)) else None
        if not (_seg and _seg.get("kind") in ("dead_air", "stage_dir")):
            ui_utils.show_status("Only dead air / stage direction segments can be recategorised.")
            return
        _old_kind = _seg["kind"]
        _new_kind = "stage_dir" if _old_kind == "dead_air" else "dead_air"
        _text     = _seg.get("text", "").strip()
        if _new_kind == "stage_dir" and not _text:
            _restore_term_attrs(self.fd, self.old)
            sys.stdout.write("\033[?1000l\033[?1006l")
            sys.stdout.flush()
            with quietly():  # drop a click's pending mouse-release bytes so
                import termios       # they don't leak into the text prompt
                termios.tcflush(self.fd, termios.TCIFLUSH)
            _lbl = _prompt_text("Stage direction text:")
            _set_raw(self.fd)
            sys.stdout.write("\033[?1000h\033[?1006h")
            self.w.anchor_reset()
            if _lbl is None or not _lbl.strip():
                ui_utils.show_status("Recategorise cancelled — a stage direction needs text.")
                return
            _text = _lbl.strip()
        # Replace with a fresh dict (never mutate in place) so the snapshot keeps
        # the original for undo.
        self.undo_stack.append(('snapshot', list(self.segs)))
        self.segs[si] = {**_seg, "kind": _new_kind, "text": _text}
        self.dirty = True
        self.refresh_overlay()
        ui_utils.show_status(
            "Recategorised as "
            f"{'stage direction' if _new_kind == 'stage_dir' else 'dead air'}.")

    def commit_field(self, field: str, val: float) -> None:
        """Apply an edited timestamp field to the current seg or word: 'start' shifts
        (undoable via apply_segs/apply_word), 'end' is set absolutely."""
        if self.prev_mode == SEG:
            old_val = self.segs[self.cursor].get(field)
            if old_val is None:
                # Setting a previously unset timestamp directly
                self.undo_stack.append(('seg_end', self.cursor, None))
                self.segs[self.cursor][field] = round(val, 3); self.dirty = True
                return
            d = round(val - old_val, 3)
            if abs(d) < 1e-6: return
            if field == 'start':
                self.apply_segs([self.cursor], d)
            else:
                self.undo_stack.append(('seg_end', self.cursor, self.segs[self.cursor].get("end")))
                self.segs[self.cursor]["end"] = round(val, 3); self.dirty = True
        else:
            words = self.cur_words()
            if self.cursor < len(words):
                ww      = words[self.cursor]
                old_val = ww.get(field) or 0.0
                d       = round(val - old_val, 3)
                if abs(d) < 1e-6: return
                if field == 'start':
                    self.apply_word(self.seg_cursor, self.cursor, d)
                else:
                    self.undo_stack.append(('word_end', self.seg_cursor, self.cursor, ww.get("end", 0.0)))
                    ww["end"] = round(val, 3)
                    self._sync_seg_bounds(self.seg_cursor)
                    self.dirty = True

    def _edit_target(self) -> dict | None:
        """The seg or word dict the EDIT-mode fields currently apply to."""
        if self.prev_mode == SEG:
            return self.segs[self.cursor] if (self.segs and self.cursor < len(self.segs)) else None
        ws = self.cur_words()
        return ws[self.cursor] if self.cursor < len(ws) else None

    def _edit_prefill(self) -> None:
        """Populate the segmented fields from the item being edited.  An *untimed*
        stage direction / dead air is seeded from the gap between its timed
        neighbours (or 0 at the top), so giving it a timing is usually a single
        Enter instead of typing MM:SS.mmm from scratch."""
        item = self._edit_target() or {}
        start_v, end_v = item.get('start'), item.get('end')
        seed = (self.prev_mode == SEG and start_v is None
                and item.get('kind') in ('stage_dir', 'dead_air')
                and self.segs and self.cursor < len(self.segs))
        if seed:
            prev_t = next((self.segs[j].get('end') for j in range(self.cursor - 1, -1, -1)
                           if self.segs[j].get('end') is not None), None)
            next_t = next((self.segs[j].get('start') for j in range(self.cursor + 1, len(self.segs))
                           if self.segs[j].get('start') is not None), None)
            start_v = prev_t if prev_t is not None else 0.0
            if end_v is None:
                if item.get('kind') == 'stage_dir' and _sd_scope(item) == 'tone':
                    # A tonal note starts at the end of the line above and runs a
                    # sensible reading time — free to overlap whatever comes next.
                    end_v = round(start_v + _reading_time(item.get('text', '')), 3)
                else:
                    end_v = next_t if next_t is not None else round(start_v + 1.0, 3)
        sm, ss, sms = _ts_parts(start_v)
        em, es, ems = _ts_parts(end_v)
        self.edit_fields = {'sm': list(sm), 'ss': list(ss), 'sms': list(sms),
                       'em': list(em), 'es': list(es), 'ems': list(ems)}
        self.edit_orig   = {k: "".join(v) for k, v in self.edit_fields.items()}
        if seed:
            # These bounds were unset; force Enter to commit the seeded values even
            # when they read as 0, so one Enter actually times the direction.
            for k in (*_EDIT_START, *_EDIT_END):
                self.edit_orig[k] = "\x00"
        self.edit_fi     = 0
        self.edit_pos    = 0
        self.edit_fresh  = True   # first digit fills the field from the left

    def _edit_field_key(self, key: str) -> None:
        """Handle one field-manipulation key for the segmented editor.  Digits fill
        from the left (a fresh field is replaced on the first digit); ↑↓ spin the
        value; ms treats its digits as a right-padded fraction (5 → 500)."""
        fk   = _EDIT_ORDER[self.edit_fi]
        buf  = self.edit_fields[fk]
        maxl = _EDIT_MAXLEN[fk]
        if key == 'TAB':
            self.edit_fi = (self.edit_fi + 1) % len(_EDIT_ORDER)
            self.edit_pos = 0; self.edit_fresh = True
        elif key == 'BACKTAB':
            self.edit_fi = (self.edit_fi - 1) % len(_EDIT_ORDER)
            self.edit_pos = 0; self.edit_fresh = True
        elif key == 'LEFT':
            self.edit_pos = max(0, self.edit_pos - 1); self.edit_fresh = False
        elif key == 'RIGHT':
            self.edit_pos = min(len(buf), self.edit_pos + 1); self.edit_fresh = False
        elif key in ('UP', 'DOWN'):
            v = _field_value(fk, "".join(buf)) + (1 if key == 'UP' else -1)
            v = max(0, min(_EDIT_LIM[fk], v))
            buf[:] = list(_field_str(fk, v)); self.edit_pos = len(buf); self.edit_fresh = False
        elif key == 'BACKSPACE':
            self.edit_fresh = False
            if self.edit_pos > 0: buf.pop(self.edit_pos - 1); self.edit_pos -= 1
        elif key == 'DELETE':
            self.edit_fresh = False
            if self.edit_pos < len(buf): buf.pop(self.edit_pos)
        elif key == 'HOME':
            self.edit_pos = 0; self.edit_fresh = False
        elif key == 'END':
            self.edit_pos = len(buf); self.edit_fresh = False
        elif len(key) == 1 and key.isdigit():
            if self.edit_fresh:
                buf[:] = [key]; self.edit_pos = 1; self.edit_fresh = False
            elif len(buf) < maxl:
                buf.insert(self.edit_pos, key); self.edit_pos += 1

    def _edit_apply(self) -> None:
        """Commit only the bound(s) whose digits changed, reusing commit_field so
        start keeps its shift semantics and end is set absolutely."""
        def _val(keys) -> float:
            """Combine a (minutes, seconds, ms) field triple into seconds."""
            m  = _field_value(keys[0], "".join(self.edit_fields[keys[0]]))
            s  = _field_value(keys[1], "".join(self.edit_fields[keys[1]]))
            ms = _field_value(keys[2], "".join(self.edit_fields[keys[2]]))
            return round(m * 60 + s + ms / 1000.0, 3)
        if any("".join(self.edit_fields[k]) != self.edit_orig[k] for k in _EDIT_START):
            self.commit_field('start', _val(_EDIT_START))
        if any("".join(self.edit_fields[k]) != self.edit_orig[k] for k in _EDIT_END):
            self.commit_field('end', _val(_EDIT_END))

    def _tick(self) -> None:
        """Once per loop: the track length, and the playback clock — ending a clip,
        moving to the next queued one, and keeping the cursor with the audio."""
        self.total_s: float = 0.0
        if self.mp and self.mp.get_length() > 0:
            self.total_s = self.mp.get_length() / 1000.0
        elif self.segs and self.segs[-1].get("end") is not None:
            self.total_s = float(self.segs[-1]["end"])

        if self.playing:
            if self.mp and not self.mp.is_playing():
                # clip ran to the track's natural end
                if self.aud_queue: self._aud_next()
                else: self.playing = False; self.aud_now = None
                self.need_redraw = True
            elif time.time() > self.play_until:
                # this clip's window elapsed — play the next queued one, or stop
                if self.aud_queue: self._aud_next()
                else: self.do_stop(); self.aud_now = None
                self.need_redraw = True
            elif self.mp:
                pos = self.mp.get_time() / 1000.0
                if abs(pos - self.play_pos) > 0.05:
                    self.play_pos = pos; self.need_redraw = True
                # auto-scroll cursor in SEG/WORD/TAP (never in AUDITION — the
                # boundary being auditioned must stay put while it plays)
                if self.mode in (SEG, WORD, TAP):
                    scroll_items = self.segs if self.mode in (SEG, TAP) else self.cur_words()
                    new_cur = self.cursor
                    for i in range(self.cursor + 1, len(scroll_items)):
                        if (scroll_items[i].get("start") or 0.0) <= self.play_pos:
                            new_cur = i
                        else:
                            break
                    if new_cur != self.cursor:
                        self.cursor = new_cur; self.need_redraw = True

    def _redraw(self) -> None:
        """Draw the editor and map its clickable hints."""
        review_info = None
        if self.review_phase:
            _riss = _review_phase_issues(self.segs, self.review_phase, self.md_quality)
            _ridx = (_riss.index(self.cursor) + 1) if self.cursor in _riss else '–'
            review_info = (_REVIEW_PHASE_NAME[self.review_phase], _ridx, len(_riss))
        lines, self.viewport, self.hit_map, footer_rows, self.prog_geo = _draw(
            self.segs, self.cursor, self.seg_cursor, self.mode, self.prev_mode, self.selected,
            self.viewport, self.dirty, len(self.undo_stack), self.track_name,
            self.playing, self.play_pos,
            {'fields': self.edit_fields, 'fi': self.edit_fi, 'pos': self.edit_pos},
            self.source, self.total_s,
            self.show_hints, self.md_overlay, self.md_quality, self.aud_now, self.aud_editing,
            review=review_info, sources=self._sources_label(),
        )
        self.hint_cells.clear()
        if lines:       # the app-wide hints toggle, on the top line
            lines[0] = _promptmod.add_help_corner(lines[0], 0, self.hint_cells, self.mode != EDIT)
        self.w.render(lines)
        self.need_redraw = False
        # Map the clickable footer-hint glyphs. `_draw` reports how many
        # trailing lines are the hint footer, so only those are scanned —
        # that keeps lyric text with brackets (e.g. "[Chorus]") from being
        # mistaken for a key, without depending on the divider glyph. Cells
        # are keyed by out-index row (col is absolute), matching the
        # r → line_idx inversion the mouse handler uses.
        for _i in range(max(0, len(lines) - footer_rows), len(lines)):
            add_hint_click_cells_auto(self.hint_cells, lines[_i], _i)

    def run(self) -> None:
        """The editor's key loop, until the user quits."""
        try:
            _set_raw(self.fd)
            sys.stdout.write("\033[?1000h\033[?1006h")  # enable mouse (click + scroll)
            sys.stdout.flush()
            self.need_redraw = True
            self.hit_map: dict[int, int] = {}   # row → item index, rebuilt on every _draw
            self.hint_cells: dict = {}          # (out-index, col) → synth key for footer hints
            self.prog_geo = None                # AUDITION progress bar: (out-index, col, width)

            while True:
                self._tick()

                if ui_utils.consume_resize():
                    self.w.anchor_reset(); self.need_redraw = True

                if self.need_redraw:
                    self._redraw()

                if not _wait_for_keypress(0.05):
                    continue
                key = _read_key(self.fd)
                self.need_redraw = True
                if self._on_key(key) is _QUIT:
                    break

        finally:
            if self.mp:
                try: self.mp.stop()
                except Exception: pass
            sys.stdout.write("\033[?1000l\033[?1006l")  # disable mouse
            sys.stdout.flush()
            _restore_term_attrs(self.fd, self.old)
            self.w.clear()


def lyrics_editor(mp3_path: str) -> None:
    """Run the interactive lyrics/transcript sync editor for mp3_path until the user quits."""
    if not drop_moved([mp3_path]):
        return
    result = _load(mp3_path)
    if result is None:
        ui_utils.show_status("No lyrics or transcript found for this track.")
        return
    _Session(mp3_path, result).run()
