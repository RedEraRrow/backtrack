"""The lyrics editor's keys: what each key does in each mode (the line and
word lists, TAP, AUDITION, the timestamp editor), plus the mouse, the review
walkthrough and the shared player transport. Mixed into the editor session,
whose state and actions these read and call."""
from __future__ import annotations
import sys, os, time
from src.music_library import format_value_list
from src.utils import ui_utils
from src.utils.prompt_core import _set_raw, _restore_term_attrs
from src.utils.prompt import text as _prompt_text
from src.utils.prompt_core import now_playing_click_action
from src.utils import prompt as _promptmod
from src.utils.prompt import chrome as _prompt_chrome
from src.lyrics.md_overlay import _SD_SCOPES, _sd_scope, build_md_overlay as _build_md_overlay
from src.lyrics.time_fields import _EDIT_END, _EDIT_START, _ts_parts
from src.lyrics.sync_doc import SOURCE_TRANSCRIPT, _make_stage_dir
from src.lyrics.verify import _make_dead_air
from src.lyrics.editor_view import AUDITION, EDIT, SEG, TAP, WORD, _AUD_COARSE, _AUD_STEP, _HAS_VLC
from src.state import QuitToTerminal
from src import tuning as tune

def _np_transport(action: str) -> None:
    """Drive the shared session behind the now-playing box (play/pause · next ·
    prev) from within the editor, then repaint the box."""
    from src.playback import session as sess
    a = sess.active_session()
    if   action == 'playpause': a.pause_toggle()
    elif action == 'next':      a.next()
    elif action == 'prev':      a.prev()
    ui_utils.pulse_now_playing()


_QUIT = object()     # a key handler's "leave the editor" (Esc; q quits the app)


class _KeyHandlers:
    """Key handling for `lyrics_editor._Session`."""

    # Now-playing box (shared session) transport from the editor: Ctrl-P/N/B,
    # and Ctrl-O to open the full player over the editor, typed or
    # clicked in the hint bar (replayed below).
    def _transport(self, key) -> bool:
        if key in ('\x10', '\x0e', '\x02'):
            _np_transport({'\x10': 'playpause', '\x0e': 'next', '\x02': 'prev'}[key])
            return True
        if key == '\x0f' and _prompt_chrome._player_opener is not None:
            _prompt_chrome._player_opener()
            sys.stdout.write("\033[?1000h\033[?1006h"); sys.stdout.flush()
            self.w.anchor_reset()
            return True
        return False

    def _on_key(self, key: str) -> object:
        """One key: the shared transport and hint keys, the mouse, the review
        walkthrough, then whatever the current mode does with it. Returns _QUIT
        to leave the editor."""
        if self._transport(key):
            return
        if key == _promptmod.HINTS_CLICK or (key == 'i' and self.mode != EDIT):
            _promptmod.toggle_hints()
            self.w.anchor_reset()
            return

        # Mouse: scroll navigates; a click positions the cursor on a row.
        if key == 'SCROLL_UP':
            key = 'UP'
        elif key == 'SCROLL_DOWN':
            key = 'DOWN'
        elif key.startswith(('MOUSE_CLICK:', 'MOUSE_RELEASE:')):
            # A click on the now-playing box drives the shared session: the
            # ⏯/⏭ icons play-pause/skip, elsewhere opens the full player.
            if key.startswith('MOUSE_CLICK:'):
                _mp = key.split(':')
                _mr = int(_mp[2]) if len(_mp) > 2 else 0
                _mc = int(_mp[3]) if len(_mp) > 3 else 1
                _act = now_playing_click_action(_mr, _mc)
                if _act in ('playpause', 'next', 'prev'):
                    _np_transport(_act); return
                if _act == 'open':
                    if _prompt_chrome._player_opener is not None:
                        _prompt_chrome._player_opener()
                        sys.stdout.write("\033[?1000h\033[?1006h"); sys.stdout.flush()
                        self.w.anchor_reset()
                    return
            # Only the press acts; the paired release is swallowed so one
            # physical click is one logical action.  A click on a footer-hint
            # glyph replays that key through the switch below; otherwise
            # `hit_map` (from the last _draw) maps a rendered line index to its
            # item (the widget draws line[i] at row w.row + MARGIN_V + i, so
            # invert that). Clicking the ALREADY-current line opens its word
            # view; a not-yet-current line is made current first (double-click).
            _hk = None
            if key.startswith('MOUSE_CLICK:') and self.w.row is not None:
                parts = key.split(':')
                r = int(parts[2]) if len(parts) > 2 else 0
                col = int(parts[3]) if len(parts) > 3 else 1
                line_idx = r - self.w.row - ui_utils.MARGIN_V
                _hk = self.hint_cells.get((line_idx, col))
                # A click on AUDITION's progress bar moves the playhead there:
                # the clip queue is abandoned (the clicked point is nobody's
                # boundary) and playback runs on open-ended from that spot.
                if _hk is None and self.prog_geo is not None and self.total_s > 0:
                    _pl, _pc, _pw = self.prog_geo
                    # the '[' / ']' caps count as the two ends
                    if line_idx == _pl and _pc - 1 <= col <= _pc + _pw:
                        _frac = (col - _pc) / (_pw - 1) if _pw > 1 else 0.0
                        self.aud_queue = []
                        self.aud_now = None
                        # stop just shy of the end, like SESSION.seek does, so
                        # clicking the far right doesn't run the track out
                        self.do_preview(min(max(0.0, min(1.0, _frac)) * self.total_s,
                                       max(0.0, self.total_s - tune.SEEK_END_MARGIN_S)))
                        return
                if _hk is None and self.mode in (SEG, WORD):
                    target = self.hit_map.get(line_idx)
                    if target is not None:
                        _t = self.segs[target] if target < len(self.segs) else {}
                        if self.mode == SEG and target == self.cursor:
                            if self.source == SOURCE_TRANSCRIPT and _t.get("words"):
                                self.seg_cursor = target
                                self.mode = WORD; self.cursor = 0; self.viewport = 0
                            elif _t.get("kind") == "dead_air" and _t.get("start") is not None:
                                self.do_recategorise(target)
                        else:
                            self.cursor = target
            if _hk is None:
                return
            key = _hk           # replay the clicked hint's key through the switch
            if self._transport(key):
                return
            if key == _promptmod.HINTS_CLICK:
                _promptmod.toggle_hints()
                self.w.anchor_reset()
                return

        # Review walkthrough (layered over SEG): Tab/⇧Tab step between flagged
        # lines, Esc leaves review; every other key falls through so you fix
        # the current line in place with the normal editing keys.
        if self.review_phase and self.mode == SEG:
            if key == 'TAB':
                self._review_advance(1); return
            if key == 'BACKTAB':
                self._review_advance(-1); return
            if key == 'ESC':
                self.review_phase = None
                self.review_program = None
                ui_utils.show_status("Left review.")
                return

        if self.mode == EDIT:
            return self._keys_edit(key)
        if self.mode == TAP:
            return self._keys_tap(key)
        if self.mode == AUDITION:
            return self._keys_audition(key)
        return self._keys_list(key)

    def _keys_edit(self, key: str) -> object:
        """Keys in the timestamp editor."""
        if key == 'ESC':
            self.mode = self.prev_mode
        elif key == 'ENTER':
            self._edit_apply()
            self.mode = self.prev_mode
        elif key in ('p', 'P'):
            # Grab the live audio playhead into the focused start/end bound.
            _pm, _ps, _pms = _ts_parts(round(self.play_pos, 3))
            _bound = _EDIT_START if self.edit['fi'] < 3 else _EDIT_END
            for _fk, _v in zip(_bound, (_pm, _ps, _pms)):
                self.edit['fields'][_fk] = list(_v)
            self.edit['fresh'] = False
        else:
            self._edit_field_key(key)
        return

    def _keys_tap(self, key: str) -> object:
        """Keys in TAP mode: space/↵ stamps the current line as the audio reaches it."""
        n = len(self.segs)
        if key in ('q', 'CTRL_C'):
            if not self._may_quit(): return
            self.do_stop(); raise QuitToTerminal()
        elif key == 's':
            self.do_save()
        elif key == 'u':
            self.do_undo()
        elif key == 'ESC':
            self.mode = SEG; self.do_stop()
        elif key == 'p':
            if self.playing:
                self.do_stop()
            else:
                self.do_preview(self.play_pos)  # resume from where we paused
        elif key in ('SPACE', 'ENTER') and self.cursor < n:
            old_start    = self.segs[self.cursor].get("start")
            old_prev_end = self.segs[self.cursor - 1].get("end") if self.cursor > 0 else None
            corrected    = round(self.play_pos + self.tap_offset_s, 3)
            self.segs[self.cursor]["start"] = max(0.0, corrected)
            if self.cursor > 0 and self.segs[self.cursor - 1].get("end") is None:
                self.segs[self.cursor - 1]["end"] = self.segs[self.cursor]["start"]
            self.undo_stack.append(('tap', self.cursor, old_start, old_prev_end))
            self.dirty = True
            if self.cursor < n - 1:
                self.cursor += 1
            else:
                self.mode = SEG; self.do_stop()  # reached end: done
        elif key == 'LEFT' and self.cursor > 0:
            self.apply_segs([self.cursor - 1], -0.25)
        elif key == 'RIGHT' and self.cursor > 0:
            self.apply_segs([self.cursor - 1],  0.25)
        elif key == ',':
            if self.cursor > 0: self.apply_segs([self.cursor - 1], -0.1)
        elif key == '.':
            if self.cursor > 0: self.apply_segs([self.cursor - 1],  0.1)
        return

    def _keys_audition(self, key: str) -> object:
        """Keys in AUDITION: hear a line's start and end, and move it by ear."""
        n = len(self.segs)
        if self.aud_editing:
            # inline timestamp editor at the bottom of the audition view
            if key in ('q', 'CTRL_C'):
                if not self._may_quit(): return
                self.aud_editing = False; self.do_stop(); raise QuitToTerminal()
            elif key == 'ESC':
                self.aud_editing = False                 # cancel, keep listening
            elif key == 'ENTER':
                self._edit_apply(); self.aud_editing = False
                self._aud_clips('line')                  # hear the line in context
            else:
                self._edit_field_key(key)
            return
        if key in ('q', 'CTRL_C'):
            if not self._may_quit(): return
            self.do_stop(); raise QuitToTerminal()
        elif key == 'ESC':
            self.mode = SEG; self.do_stop(); self.aud_now = None
        elif key == 'UP':
            self.cursor = max(0, self.cursor - 1); self._aud_clips('line')
        elif key == 'DOWN':
            self.cursor = min(n - 1, self.cursor + 1); self._aud_clips('line')
        elif key in ('SPACE', 'ENTER'):
            self._aud_clips('line')          # play the whole line
        # move the whole line by ear (both timestamps together):
        elif key == 'LEFT':
            self._aud_shift(-_AUD_STEP)
        elif key == 'RIGHT':
            self._aud_shift( _AUD_STEP)
        elif key == ',':
            self._aud_shift(-_AUD_COARSE)
        elif key == '.':
            self._aud_shift( _AUD_COARSE)
        elif key == '[':
            self._aud_clips('start')         # hear the start boundary (no change)
        elif key == ']':
            self._aud_clips('end')           # hear the end boundary (no change)
        elif key == 'u':
            self.do_undo(); self._aud_clips('line')
        elif key == 'e':
            # open the inline timestamp editor for this line
            if self.segs and self.cursor < len(self.segs):
                self.do_stop(); self.aud_now = None
                self.prev_mode   = SEG       # audition edits the line (a seg)
                self.aud_editing = True
                self._edit_prefill()
        elif key == 'p':
            if self.playing: self.do_stop(); self.aud_now = None
            else:       self._aud_clips('line')
        return

    def _keys_list(self, key: str) -> object:
        """Keys in the line list (SEG) and the word list (WORD)."""
        items = self.segs if self.mode == SEG else self.cur_words()
        n_i   = len(items)

        if key in ('q', 'CTRL_C'):
            if not self._may_quit(): return
            if self.playing: self.do_stop()
            raise QuitToTerminal()
        elif key == 'ESC' and self.mode == SEG:      # back to the tag editor
            if not self._may_quit(): return
            if self.playing: self.do_stop()
            return _QUIT
        elif key == '?' and self.mode == SEG:
            self.show_hints = not self.show_hints
        elif key == 's':
            self.do_save()
        elif key == 'W' and self.source == SOURCE_TRANSCRIPT:
            return self._commit_to_transcript()
        elif key == 'V' and self.source == SOURCE_TRANSCRIPT:
            self.do_verify()
        elif key == 'S' and self.mode == SEG and self.source == SOURCE_TRANSCRIPT:
            self.do_speaker_split()
        elif key == 'u':
            self.do_undo()
        elif key == 'UP':
            self.cursor = max(0, self.cursor - 1)
        elif key == 'DOWN':
            self.cursor = min(n_i - 1, max(0, self.cursor + 1))
        elif key == 'SPACE' and self.mode == SEG:
            self.selected.symmetric_difference_update({self.cursor})
        elif key == 'c' and self.mode == SEG:
            return self._add_credits()
        elif key == 'a' and self.mode == SEG:
            return self._add_gap()
        elif key == 'd' and self.mode == SEG:
            return self._delete_inserted()
        elif key == 'l' and self.mode == SEG:
            return self._relabel()
        elif key == 'k' and self.mode == SEG:
            self.do_recategorise(self.cursor)
        elif key == 'R' and self.mode == SEG:
            self._review_enter('issues')
        elif key == 'D' and self.mode == SEG:
            self._review_enter('dirs')
        elif key == 'L' and self.mode == SEG:
            self._review_enter('long')
        elif key == '/' and self.mode == SEG:
            self.do_smart_split()
        elif key == 'x' and self.mode == SEG:
            return self._cycle_direction()
        elif key == 'r' and self.mode == SEG:
            return self._fill_gaps()
        elif key == 'm' and self.mode == SEG:
            return self._toggle_overlay()
        elif key == 'M' and self.mode == SEG:
            return self._commit_directions()
        elif key == 't' and self.mode == SEG:
            if _HAS_VLC:
                self.mode = TAP
                start_s = self.segs[self.cursor].get("start") or self.play_pos
                self.do_preview(start_s)
        elif key == 'b' and self.mode == SEG:
            if _HAS_VLC and self.mp is not None and self.segs:
                self.mode = AUDITION
                self._aud_clips('line')   # landing on a line plays it whole
            elif not _HAS_VLC:
                ui_utils.show_status("Audition needs VLC (not available).")
        elif key == 'w' and self.mode == SEG and self.source == SOURCE_TRANSCRIPT:
            if self.segs and self.cursor < len(self.segs) and self.segs[self.cursor].get("words"):
                self.seg_cursor = self.cursor
                self.mode = WORD; self.cursor = 0; self.viewport = 0
        elif key == 'ESC' and self.mode == WORD:
            self.mode = SEG; self.cursor = self.seg_cursor; self.viewport = max(0, self.seg_cursor - 2)
        elif key == 'e':
            return self._open_editor()
        elif key == 'p':
            return self._play_toggle()
        elif key in ('J', 'K') and self.mode == SEG:
            self._move_line(-1 if key == 'J' else 1)
        elif key == 'j' and self.mode == SEG:
            return self._join_next()
        elif key == 'x' and self.mode == WORD:
            return self._split_at_word()
        else:
            if self.mode == SEG:
                tgts = sorted(self.selected) if self.selected else [self.cursor]
                key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                if key in key_deltas:
                    if self.segs[self.cursor].get("start") is None:
                        ui_utils.show_status("No timestamp set: press e to enter one.")
                    else:
                        self.apply_segs(tgts, key_deltas[key])
            else:
                key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                if key in key_deltas:
                    self.apply_word(self.seg_cursor, self.cursor, key_deltas[key])

    def _move_line(self, delta: int) -> None:
        """J/K: swap the line under the cursor with the one above (-1) or below (+1)."""
        j = self.cursor + delta
        if self.segs and 0 <= j < len(self.segs):
            self.undo_stack.append(('snapshot', list(self.segs)))
            self.segs[self.cursor], self.segs[j] = self.segs[j], self.segs[self.cursor]
            self.cursor = j
            self.dirty = True
            self.refresh_overlay()

    def _commit_to_transcript(self) -> object:
        """W: after a y/N, write the spoken words back to the transcript JSON (and .srt)."""
        _restore_term_attrs(self.fd, self.old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        _ans = _prompt_text(
            f"Write spoken words (Whisper format) to "
            f"{os.path.basename(self.aux['jpath'])}? (y/N)")
        _set_raw(self.fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        self.w.anchor_reset()
        if not (_ans or "").strip().lower().startswith("y"):
            ui_utils.show_status("Commit cancelled, working copy untouched.")
        elif self.do_commit():  # writes transcript.json + refreshes fingerprint;
            self.do_save()      # False (with its reason shown) if it couldn't
            ui_utils.show_status(f"Written to {os.path.basename(self.aux['jpath'])} + .srt.")

    def _add_credits(self) -> object:
        """c: append the composer and lyricist tags as credit lines (shown, never exported)."""
        try:
            from mutagen.id3 import ID3
            _aud = ID3(self.mp3_path)
            _credits: list[str] = []
            _tcom = _aud.getall('TCOM')
            if _tcom:
                _credits.append("Music by: " + format_value_list(list(_tcom)))
            _text = _aud.getall('TEXT')
            if _text:
                _credits.append("Words by: " + format_value_list(list(_text)))
            if _credits:
                for _cl in _credits:
                    # Mark as 'credit' so it's shown but excluded from the
                    # spoken-word transcript.json export (do_commit).
                    self.segs.append({"text": _cl, "kind": "credit"})
                self.cursor = len(self.segs) - 1
                self.dirty = True
                self.refresh_overlay()  # seg count changed: re-derive before_si
            else:
                ui_utils.show_status("No composer or lyricist tags found.")
        except Exception as _ce:
            ui_utils.show_status(f"Could not read tags: {_ce}")

    def _add_gap(self) -> object:
        """a: insert dead air, or a stage direction, at the cursor or before the first line."""
        _restore_term_attrs(self.fd, self.old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        # At position 0 offer inserting before the first item (track intro)
        # whenever it starts after 0:00, including before an initial stage
        # direction, so you can place silence ahead of it.
        _insert_before = (self.cursor == 0 and self.segs
                          and (self.segs[0].get("start") or 0) > 0)
        if _insert_before:
            _where = _prompt_text("Insert before first segment (b) or after cursor (a)?")
            _insert_before = (_where or "").strip().lower().startswith("b")
        _dur_s = _prompt_text("Dead air duration (seconds):")
        _label = _prompt_text("Stage direction (leave blank for silence):")
        _set_raw(self.fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        self.w.anchor_reset()
        if _dur_s is not None:
            try:
                _dur = float(_dur_s.strip())
                if _dur > 0:
                    if _insert_before:
                        _end   = round(float(self.segs[0].get("start") or 0), 3)
                        _start = max(0.0, _end - _dur)
                        _air   = _make_dead_air(_start, _end, (_label or "").strip())
                        self.segs.insert(0, _air)
                        # cursor stays at 0, which is now the new dead air
                    else:
                        _ref   = self.segs[self.cursor].get("end") if self.segs and self.cursor < len(self.segs) else None
                        _start = round(float(_ref or 0), 3)
                        _air   = _make_dead_air(_start, _start + _dur, (_label or "").strip())
                        self.segs.insert(self.cursor + 1, _air)
                        self.cursor += 1
                    self.dirty = True
                    self.refresh_overlay()
            except ValueError:
                ui_utils.show_status("Enter a number of seconds, e.g. 2 or 1.5.")

    def _delete_inserted(self) -> object:
        """d: delete the dead air or stage direction under the cursor."""
        if self.segs and self.cursor < len(self.segs) and self.segs[self.cursor].get("kind") in ("dead_air", "stage_dir"):
            self.undo_stack.append(('delete', self.cursor, self.segs[self.cursor]))
            self.segs.pop(self.cursor)
            self.cursor = min(self.cursor, len(self.segs) - 1)
            self.dirty = True
            self.refresh_overlay()
        else:
            ui_utils.show_status("Only dead air / stage direction segments can be deleted here.")

    def _relabel(self) -> object:
        """l: retype the text of the dead air or stage direction under the cursor."""
        if self.segs and self.cursor < len(self.segs) and self.segs[self.cursor].get("kind") in ("dead_air", "stage_dir"):
            _cur_lbl = self.segs[self.cursor].get("text", "")
            _restore_term_attrs(self.fd, self.old)
            sys.stdout.write("\033[?1000l\033[?1006l")
            _prompt = ("Stage direction text:" if self.segs[self.cursor].get("kind") == "stage_dir"
                       else "Stage direction (blank = silence):")
            _new_lbl = _prompt_text(_prompt, default=_cur_lbl)
            _set_raw(self.fd)
            sys.stdout.write("\033[?1000h\033[?1006h")
            self.w.anchor_reset()
            if _new_lbl is not None:
                self.segs[self.cursor]["text"] = _new_lbl.strip()
                self.dirty = True
                self.refresh_overlay()  # text changed: re-reconcile the overlay
        else:
            ui_utils.show_status("Cursor is not on a dead air or stage direction segment.")

    def _cycle_direction(self) -> object:
        """x: cycle the stage direction under the cursor through inline / tone / external."""
        _sd = self.segs[self.cursor] if (self.segs and self.cursor < len(self.segs)) else None
        if not (_sd and _sd.get('kind') == 'stage_dir'):
            ui_utils.show_status("Cursor is not on a stage direction (✦).")
        else:
            _next = _SD_SCOPES[(_SD_SCOPES.index(_sd_scope(_sd)) + 1) % len(_SD_SCOPES)]
            self.undo_stack.append(('snapshot', list(self.segs)))
            self.segs[self.cursor] = {**_sd, 'scope': _next}
            self.dirty = True
            self.refresh_overlay()
            ui_utils.show_status(f"Direction: {_next}")

    def _fill_gaps(self) -> object:
        """r: fill the gaps between timed lines with dead air, and time any untimed
        stage direction that sits between two timed lines."""
        _restore_term_attrs(self.fd, self.old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        _inserted = 0; _timed = 0
        _i = 0
        _new_segs: list[dict] = []
        while _i < len(self.segs):
            _cur = self.segs[_i]
            # Auto-time an untimed stage_dir that sits between two timed segs
            if (_cur.get("kind") == "stage_dir" and _cur.get("start") is None):
                _prev_t = _new_segs[-1].get("end") if _new_segs else None
                _next_t = next((self.segs[j].get("start") for j in range(_i + 1, len(self.segs))
                                if self.segs[j].get("start") is not None), None)
                if _prev_t is not None and _next_t is not None:
                    _cur = dict(_cur)
                    _cur["start"] = round(float(_prev_t), 3)
                    _cur["end"]   = round(float(_next_t), 3)
                    _timed += 1
            _new_segs.append(_cur)
            _nxt = self.segs[_i + 1] if _i + 1 < len(self.segs) else None
            _skip_kinds = ("dead_air", "stage_dir")
            if (_nxt is not None
                    and _cur.get("end") is not None
                    and _nxt.get("start") is not None
                    and _nxt["start"] - _cur["end"] >= tune.LYRIC_AIR_THRESHOLD_S
                    and _nxt.get("kind") not in _skip_kinds
                    and _cur.get("kind") not in _skip_kinds):
                _gap_start = float(_cur["end"])
                _gap_end   = float(_nxt["start"])
                _gap       = round(_gap_end - _gap_start, 3)
                if self.mp is not None:
                    _play_from = max(0.0, _gap_start - 0.3)
                    self.mp.set_time(int(_play_from * 1000))
                    if not self.mp.is_playing():
                        self.mp.play(); time.sleep(0.15)
                _lbl = _prompt_text(
                    f"Gap of {_gap}s: stage direction? (blank = silence, skip = ignore):")
                if self.mp is not None and self.mp.is_playing():
                    self.mp.pause()
                if _lbl is not None and _lbl.strip().lower() != 'skip':
                    _new_segs.append(_make_dead_air(_gap_start, _gap_end, _lbl.strip()))
                    _inserted += 1
            _i += 1
        self.segs[:] = _new_segs
        if _inserted:
            self.refresh_overlay()
        _set_raw(self.fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        self.w.anchor_reset()
        if _inserted or _timed:
            self.dirty = True
            _msg = []
            if _inserted: _msg.append(f"{_inserted} dead air added")
            if _timed:    _msg.append(f"{_timed} stage dir timed")
            ui_utils.show_status(", ".join(_msg))
        else:
            ui_utils.show_status("No gaps above threshold found.")

    def _toggle_overlay(self) -> object:
        """m: lay the markdown transcript over the list, or take it off again."""
        if self.md_overlay is not None:
            self.md_overlay = self.md_quality = self.md_path = None
            self.viewport   = 0
            ui_utils.show_status("Transcript overlay removed.")
        else:
            from src.lyrics.lyrics import _find_markdown_for_audio
            _md_path = _find_markdown_for_audio(self.mp3_path)
            if _md_path is None:
                ui_utils.show_status("No transcript.md found next to this file.")
            else:
                try:
                    self.md_overlay, self.md_quality, _links = _build_md_overlay(self.segs, _md_path)
                    for _si, _lid in _links.items():   # record durable alignment
                        if _lid is not None:
                            self.segs[_si]['line_ref'] = _lid
                    self.md_path    = _md_path
                    self.viewport   = 0
                    ui_utils.show_status(
                        f"Overlay: {len(self.md_overlay)} annotations from {os.path.basename(_md_path)}"
                    )
                except Exception as _exc:
                    ui_utils.show_status(f"MD overlay failed: {_exc}")

    def _commit_directions(self) -> object:
        """M: make the overlay's stage directions real lines of the document."""
        # Materialise the overlay's stage directions as real (untimed)
        # segs so they can be timed and saved.  Speakers are display-only
        # and are never committed: they stay derived from the overlay, so
        # there is only ever one source for a speaker header and nothing
        # can duplicate.  After inserting, refresh_overlay() re-derives:
        # the new stage_dir segs are now "materialised" and drop out of the
        # overlay, making a second M a no-op (idempotent).
        if not self.md_overlay:
            ui_utils.show_status("No stage directions to commit: press m first.")
        else:
            _sdir_items = [ov for ov in self.md_overlay if ov['kind'] == 'stage_dir']
            if not _sdir_items:
                ui_utils.show_status("No stage directions in the overlay to commit.")
            else:
                self.undo_stack.append(('snapshot', list(self.segs)))
                # md_overlay is in ascending (before_si, order); inserting in
                # reverse keeps earlier indices valid and preserves the order
                # of directions that share a before_si.
                for _ov in reversed(_sdir_items):
                    self.segs.insert(_ov['before_si'], _make_stage_dir(_ov['text']))
                _committed = len(_sdir_items)
                self.refresh_overlay()
                self.viewport = 0
                self.dirty    = True
                ui_utils.show_status(
                    f"Committed {_committed} stage direction{'s' if _committed != 1 else ''}.")

    def _open_editor(self) -> object:
        """e: open the timestamp editor on the line or word under the cursor."""
        if self.mode == SEG:
            item = self.segs[self.cursor] if (self.segs and self.cursor < len(self.segs)) else None
        else:
            words = self.cur_words()
            item  = words[self.cursor] if self.cursor < len(words) else None
        if item is not None:
            self.prev_mode = self.mode
            self.mode      = EDIT
            self._edit_prefill()

    def _play_toggle(self) -> object:
        """p: stop, or play from the word or line under the cursor."""
        if self.playing:
            self.do_stop()
        elif self.mode == WORD:
            words = self.cur_words()
            if self.cursor < len(words):
                ww = words[self.cursor]
                self.do_preview(ww.get("start") or 0.0)
        elif self.segs and self.cursor < len(self.segs):
            self.do_preview(self.segs[self.cursor].get("start") or 0.0)

    def _join_next(self) -> object:
        """j: join the line under the cursor with the next one."""
        if self.segs and self.cursor < len(self.segs) - 1:
            seg_a  = self.segs[self.cursor]
            seg_b  = self.segs[self.cursor + 1]
            if seg_a.get("kind") in ("dead_air", "stage_dir") or \
               seg_b.get("kind") in ("dead_air", "stage_dir"):
                ui_utils.show_status("Cannot join dead air or stage direction segments.")
                return
            merged = {
                "start": seg_a.get("start"),
                "end":   seg_b.get("end"),
                "text":  (seg_a.get("text", "").strip() + " " +
                          seg_b.get("text", "").strip()).strip(),
                "words": seg_a.get("words", []) + seg_b.get("words", []),
            }
            if seg_a.get("line_ref"):   # keep the pinned MD line through the join
                merged["line_ref"] = seg_a["line_ref"]
            self.undo_stack.append(('join', self.cursor, seg_a, seg_b))
            self.segs[self.cursor:self.cursor + 2] = [merged]
            self.dirty = True
            self.refresh_overlay()

    def _split_at_word(self) -> object:
        """x (words): split the open line into two at the word under the cursor."""
        words = self.cur_words()
        if 0 < self.cursor < len(words):
            seg = self.segs[self.seg_cursor]
            w_a, w_b = words[:self.cursor], words[self.cursor:]
            boundary = (w_b[0].get("start") or w_a[-1].get("end")
                        or round((seg.get("start", 0) + seg.get("end", 0)) / 2, 3))
            seg_a = {"start": seg.get("start"), "end": round(boundary, 3),
                     "text": " ".join(ww["word"] for ww in w_a), "words": w_a}
            seg_b = {"start": round(boundary, 3), "end": seg.get("end"),
                     "text": " ".join(ww["word"] for ww in w_b), "words": w_b}
            if seg.get("line_ref"):    # both halves stay on the split line's MD line
                seg_a["line_ref"] = seg_b["line_ref"] = seg["line_ref"]
            self.undo_stack.append(('split', self.seg_cursor, seg))
            self.segs[self.seg_cursor:self.seg_cursor + 1] = [seg_a, seg_b]
            self.dirty = True; self.mode = SEG; self.cursor = self.seg_cursor + 1
            self.viewport = max(0, self.cursor - 2)
            self.refresh_overlay()
