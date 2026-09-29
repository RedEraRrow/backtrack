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

from src.music_library import drop_moved, format_value_list
from src.utils import ui_utils
from src.utils.ui_utils import Colors as C
from src.utils.prompt_core import _Widget, _read_key, _wait_for_keypress, _set_raw, _restore_term_attrs, _get_term_attrs, _cols
from src.utils.prompt import text as _prompt_text
from src.utils.prompt_core import add_hint_click_cells_auto, _visible_rows, now_playing_click_action
from src.utils import prompt as _promptmod
from src.utils.prompt import chrome as _prompt_chrome
from src.utils.files import write_text_atomic, backup_copy
from src.utils.log import log

_vlc = None
try:
    import vlc as _vlc  # type: ignore[import-untyped]
    _HAS_VLC = True
except ImportError:
    _HAS_VLC = False

from mutagen.id3 import ID3, ID3NoHeaderError  # type: ignore[reportPrivateImportUsage]
from src.lyrics.lyrics import _apply_markdown_formatting
# The MD↔JSON alignment is shared with the playback lyric display so the two
# always agree on speakers, stage directions and line text (see md_overlay).
from src.lyrics.md_overlay import (
    _SD_SCOPES, _sd_scope, _is_framed, _reading_time, build_md_overlay as _build_md_overlay,
)
from src.lyrics.time_fields import (
    _EDIT_END, _EDIT_LIM, _EDIT_MAXLEN, _EDIT_ORDER, _EDIT_START, _field_str, _field_value,
    _render_edit_fields, _ts_parts,
)
from src.lyrics.sync_doc import (
    SOURCE_SYLT, SOURCE_TRANSCRIPT, SOURCE_USLT, _REVIEW_PHASE_NAME, _REVIEW_PROGRAMS, _best_split_index, _clean_seg, _ensure_ids, _file_fp, _load, _make_stage_dir, _rebuild_srt, _review_phase_issues, _shift_seg, _shift_word,
)
from src.lyrics.verify import (
    _AIR_GAP_THRESHOLD, _make_dead_air, _split_candidates, _split_seg_at, _verify_matchup,
)
from src.utils import timefmt

SEG, WORD, EDIT, TAP, AUDITION = 'seg', 'word', 'edit', 'tap', 'audition'

_AUD_CLIP   = 1.0    # audition: seconds played at a line's start, and at its end
_AUD_STEP   = 0.05   # audition: fine whole-line move per arrow press
_AUD_COARSE = 0.25   # audition: coarser whole-line move per , / . press



_DUR_W = 6    # "123.4s" right-justified


def _dur(s_t, e_t) -> str:
    """Fixed-width duration column ('12.3s' r-justified, blank if untimed)."""
    if s_t is None or e_t is None:
        return " " * _DUR_W
    return f"{max(0.0, (e_t or 0) - (s_t or 0)):.1f}s".rjust(_DUR_W)


def _clip(s: str, width: int, ell: str = "…") -> str:
    """Truncate an ANSI-coloured string to `width` VISIBLE columns.

    Escape sequences are copied through without counting toward the width, so a
    truncation never lands mid-escape and never leaves colour bleeding.  This is
    the hard guarantee that no rendered line can exceed the terminal and wrap.
    """
    if width <= 0:
        return ""
    if ui_utils.visual_len(s) <= width:
        return s
    budget = max(0, width - ui_utils.visual_len(ell))
    return ui_utils.clip_ansi(s, budget, reset=False) + ell + C.RESET


def _fit_body_footer(out: list, footer: list, avail: int) -> list:
    """Assemble the scrolling body + the pinned hint footer into at most `avail`
    rows, footer flush to the bottom. The footer (hints) always survives — the
    body is trimmed to make room — so hints never overflow past the mini-player or
    off the bottom of the screen (that was them 'disappearing' on short screens)."""
    if avail <= 0:
        return []
    if len(footer) > avail:
        footer = footer[-avail:]                 # last resort: even hints won't fit
    body = out[:max(0, avail - len(footer))]
    pad  = max(0, avail - len(body) - len(footer))
    return body + [""] * pad + footer


def _footer_rows(footer: list, avail: int) -> int:
    """How many of `footer`'s rows survive `_fit_body_footer`'s trim.  The click
    mapper needs this to find the hint bar by counting up from the bottom, rather
    than sniffing for a divider glyph (which silently broke when the rules went
    from '─' to the dotted '·')."""
    return max(0, min(len(footer), avail))


def _prog_geo_if_visible(geo, footer: list, avail: int):
    """Pass through the progress bar's click geometry `(line_idx, col, width)` only
    while that row survives `_fit_body_footer` — the body is trimmed from the end,
    so surviving rows keep their index, but a very short terminal drops the bar
    entirely and clicks there must not seek."""
    if geo is None:
        return None
    return geo if geo[0] < max(0, avail - _footer_rows(footer, avail)) else None


def _np_transport(action: str) -> None:
    """Drive the shared session behind the mini-player box (play/pause · next ·
    prev) from within the editor, then repaint the box."""
    from src.playback import session as sess
    a = sess.active_session()
    if   action == 'playpause': a.pause_toggle()
    elif action == 'next':      a.next()
    elif action == 'prev':      a.prev()
    ui_utils.pulse_now_playing()




def _draw(segs, cursor, seg_cursor, mode, prev_mode, selected, viewport,
          dirty, undo_depth, track_name, playing, play_pos,
          edit, source, total_s, show_hints=False, md_overlay=None,
          md_quality=None, aud_now=None, aud_editing=False,
          review=None, sources="") -> tuple[list[str], int, dict, int, tuple | None]:
    """Render the full editor screen for the current mode (TAP/AUDITION/SEG/WORD),
    returning the display lines, the updated viewport, a row→item hit-map for
    mouse clicks, how many trailing lines are the pinned hint footer, and the
    progress bar's `(line_idx, first_col, width)` click geometry (None when the
    mode draws no bar)."""
    cols = _cols()
    # Refresh the now-playing box height, then budget the body with the same
    # helper the list menus use — it reserves the status bar, the mini-player box,
    # and the vertical margins — so the editor never paints under the player.
    ui_utils.now_playing_lines(ui_utils.get_terminal_width())
    avail = _visible_rows()
    n    = len(segs)
    vp   = viewport

    # Row → item-index map for mouse clicks.  Keyed by the index of a line within
    # the returned list (== its offset below the widget anchor + MARGIN_V), so the
    # click handler never has to reverse-engineer the layout: only rows that carry
    # a selectable item (a seg in SEG, a word in WORD) get an entry.  Speaker
    # banners, ✦ stage-direction overlays and blank spacers are absent, so clicks
    # on them are correctly ignored.
    hit_map: dict[int, int] = {}

    show_words = mode == WORD or (mode == EDIT and prev_mode == WORD)

    B      = cols                 # body width (inside the 2-col indent)
    indent = " " * ui_utils.MARGIN_H

    def _dot_rule(width: int | None = None) -> str:
        """Return an equal-spaced round dotted rule filling `width` columns.

        Uses a fine middle-dot repeated to produce a visually light, evenly
        spaced full-width dotted divider.
        """
        w = width if width is not None else B
        return "·" * max(0, w)

    # In review, the header names the phase; otherwise the mode.
    if review:
        label = f"REVIEW · {review[0].upper()}"
    else:
        label = ("TAP SYNC"    if mode == TAP
                 else "AUDITION"   if mode == AUDITION
                 else "TRANSCRIPT" if source == SOURCE_TRANSCRIPT
                 else "LYRICS")

    # Right-aligned status cluster: dirty dot · playhead · position.
    # In review, show the issue counter (i / N) instead of the seg position.
    pos_str    = f"{review[1]} / {review[2]}" if review else (f"{cursor + 1} / {n}" if n else "–")
    right_plain, right_disp = "", ""
    if dirty:
        right_disp += f"{C.ACCENT}●{C.RESET}   "; right_plain += "●   "
    if playing:
        _pp = f"▶ {timefmt.clock(play_pos)}"
        right_disp += f"{C.ACCENT}{_pp}{C.RESET}   "; right_plain += _pp + "   "
    right_disp += f"{C.DIM}{pos_str}{C.RESET}"; right_plain += pos_str
    right_w = len(right_plain)

    # Left cluster: title + track (+ current-seg preview in word view), truncated
    # so the whole bar fits the body width and never wraps.
    ctx_plain = ""
    if show_words and segs and seg_cursor < len(segs):
        _raw = segs[seg_cursor].get("text", "").strip()
        ctx_plain = f"  ›  {_raw}"
    elif sources:
        # Which documents are being edited, in the one place already reserved for
        # "what am I looking at". An editor that does not name its files lets you
        # spend an hour retiming a working copy you meant to throw away, or the
        # wrong episode's transcript entirely. Word view gives the space back to
        # the line under the cursor, which is what matters there.
        ctx_plain = f"  ·  {sources}"
    track_budget = max(0, B - len(label) - 2 - right_w - 2)
    track_txt    = ui_utils.truncate_text(track_name + ctx_plain, track_budget)
    left_disp    = f"{C.BOLD}{label}{C.RESET}  {C.DIM}{track_txt}{C.RESET}"
    left_w       = len(label) + 2 + len(track_txt)
    gap          = max(1, B - left_w - right_w)

    # Header starts on the first line; _Widget.render adds the MARGIN_V top row,
    # matching the rest of the app (no extra leading blank here).
    term_w = ui_utils.get_terminal_width()
    out: list[str] = [
        indent + left_disp + " " * gap + right_disp,
        f"{C.DIM}{_dot_rule(term_w)}{C.RESET}",
    ]

    sep = f"{C.DIM}{_dot_rule(term_w)}{C.RESET}"

    if mode == TAP:
        prev_s = segs[cursor - 1] if cursor > 0     else None
        curr_s = segs[cursor]     if cursor < n      else None
        next_s = segs[cursor + 1] if cursor < n - 1  else None

        def _tap_line(seg, bold: bool = False, arrow: bool = False) -> str:
            """Render one prev/current/next line of the TAP-mode readout."""
            ptr = f"{indent}{C.ACCENT}▶{C.RESET} " if arrow else indent + "  "
            if seg is None:
                return f"{ptr} {C.DIM}──{C.RESET}"
            ts_v = timefmt.clock(seg['start']) if seg.get("start") is not None else " " * 9
            ts   = f"{C.DIM}{ts_v}{C.RESET}"
            col  = C.BOLD + C.PRIMARY if bold else C.DIM
            budget = max(4, B - 3 - 9 - 3)   # ptr(3) + ts(9) + gaps(3)
            txt  = ui_utils.truncate_text(seg["text"].strip(), budget)
            return f"{ptr} {ts}   {col}{txt}{C.RESET}"

        # Build footer first so its actual height governs padding. The hints go
        # through the shared chrome so the transport keys the editor already
        # handles (^P/^N/^B/^O) are actually advertised while audio is playing.
        footer: list[str] = [sep]
        pairs: list[tuple[str, str]] = [('spc/↵', 'mark')]
        if cursor > 0: pairs += [('←→', '±0.25s'), (',/.', '±0.1s')]
        pairs.append(('p', 'pause' if playing else 'play'))
        pairs.append(('s', 'save'))
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        pairs += [('esc', 'done'), ('q', 'quit')]
        footer.extend(_promptmod.chrome_hint_lines(pairs))

        TAP_ROWS = 9  # 3 content lines + 4 blank spacers + progress + blank
        n_body   = max(0, avail - 2 - len(footer))   # 2 header rows (title + rule)
        pad_top  = max(0, (n_body - TAP_ROWS) // 2)

        out += [""] * pad_top
        out.append(_tap_line(prev_s))
        out += ["", ""]
        out.append(_tap_line(curr_s, bold=True, arrow=True))
        out += ["", ""]
        out.append(_tap_line(next_s))
        out.append("")

        if total_s > 0:
            pct = min(play_pos / total_s, 1.0)
            _clock = f"{timefmt.clock(play_pos)} / {timefmt.clock(total_s)}"
            bar = ui_utils.get_progress_bar(pct, max(4, B - len(_clock) - 4))  # -4: bar's own [ ] + 2-space gap
            out.append(f"{indent}{C.DIM}{bar}  {_clock}{C.RESET}")
        else:
            out.append("")

        _cap = ui_utils.get_terminal_width()   # allow full-width rules; content self-limits to the margin
        return ([_clip(ln, _cap) for ln in _fit_body_footer(out, footer, avail)],
                vp, hit_map, _footer_rows(footer, avail), None)

    if mode == AUDITION:
        prev_s = segs[cursor - 1] if cursor > 0    else None
        curr_s = segs[cursor]     if cursor < n     else None
        next_s = segs[cursor + 1] if cursor < n - 1 else None
        s_t = curr_s.get("start") if curr_s else None
        e_t = curr_s.get("end")   if curr_s else None

        def _line(seg, bold=False) -> str:
            """Render one prev/current/next line of the AUDITION-mode readout."""
            if seg is None:
                return f"{indent}"
            col = C.BOLD + C.PRIMARY if bold else C.DIM
            txt = ui_utils.truncate_text(seg.get("text", "").strip() or "(…)", max(4, B - 6))
            return f"{indent}   {col}{txt}{C.RESET}"

        # Bottom line: either the static start/end readout (playing one accented)
        # or, when editing, the inline segmented MM:SS.mmm editor for this line.
        if aud_editing:
            s_d, e_d = _render_edit_fields(edit)
            marks = f"{C.ACCENT}✎{C.RESET}  start {s_d}    end {e_d}"
        else:
            def _mark(lbl, val, on) -> str:
                """Render one 'label value' marker, accented when `on` (the clip currently playing)."""
                v = timefmt.clock(val)
                c = C.ACCENT + C.BOLD if on else C.DIM
                return f"{c}{lbl} {v}{C.RESET}"
            marks = (f"{_mark('start', s_t, aud_now == 'start')}"
                     f"    {C.DIM}·{C.RESET}    {_mark('end', e_t, aud_now == 'end')}")

        footer: list[str] = [sep]
        if aud_editing:
            pairs = [('tab/⇧tab', 'field'), ('←→', 'cursor'), ('↑↓', 'adjust'),
                     ('↵', 'apply'), ('esc', 'cancel')]
        else:
            pairs = [('↑↓', 'line'), ('spc', 'whole line'),
                     ('←→', 'move 50ms'), (',/.', 'move ¼s'),
                     ('[/]', 'hear s/e'), ('e', 'edit dur')]
            if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
            pairs += [('p', 'pause' if playing else 'play'), ('esc', 'back'), ('q', 'quit')]
        footer.extend(_promptmod.chrome_hint_lines(pairs))

        AUD_ROWS = 8
        n_body   = max(0, avail - 2 - len(footer))   # 2 header rows (title + rule)
        pad_top  = max(0, (n_body - AUD_ROWS) // 2)

        out += [""] * pad_top
        out.append(_line(prev_s))
        out += [""]
        out.append(_line(curr_s, bold=True))
        out += ["", ""]
        out.append(f"{indent}   {marks}")
        out += [""]
        out.append(_line(next_s))
        out.append("")

        prog_geo = None
        if total_s > 0:
            pct = min(play_pos / total_s, 1.0)
            _clock = f"{timefmt.clock(play_pos)} / {timefmt.clock(total_s)}"
            _bar_w = max(4, B - len(_clock) - 4)   # -4: bar's own [ ] + 2-space gap
            bar = ui_utils.get_progress_bar(pct, _bar_w)
            out.append(f"{indent}{C.DIM}{bar}  {_clock}{C.RESET}")
            # Clickable: the bar's first cell sits one column past its '[' cap.
            prog_geo = (len(out) - 1, ui_utils.MARGIN_H + 2, _bar_w)
        else:
            out.append("")

        _cap = ui_utils.get_terminal_width()   # allow full-width rules; content self-limits to the margin
        return ([_clip(ln, _cap) for ln in _fit_body_footer(out, footer, avail)],
                vp, hit_map, _footer_rows(footer, avail),
                _prog_geo_if_visible(prog_geo, footer, avail))

    # Build footer first so its actual height governs how many items we show.
    footer = [sep]

    if mode == EDIT:
        s_d, e_d = _render_edit_fields(edit)
        footer.append(f"  {C.ACCENT}✎{C.RESET}  start  {s_d}    end  {e_d}")
        footer.extend(_promptmod.chrome_hint_lines(
            [('tab/⇧tab', 'field'), ('←→', 'cursor'), ('↑↓', 'adjust'),
             ('p', 'playhead'), ('↵', 'apply'), ('esc', 'cancel')]))

    elif mode == WORD:
        pairs: list[tuple[str, str]] = [('↑↓', 'navigate'), ('←→', '±0.25s'), (',/.', '±0.1s'), ('[/]', '±1s')]
        pairs += [('x', 'split'), ('e', 'edit')]
        if _HAS_VLC: pairs.append(('p', 'preview'))
        pairs.append(('s', 'save'))
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        pairs += [('esc', 'back'), ('q', 'quit')]
        footer.extend(_promptmod.chrome_hint_lines(pairs))

    else:  # SEG
        cur_item  = segs[cursor] if (segs and cursor < len(segs)) else None
        cur_kind  = cur_item.get("kind") if cur_item else None
        is_air    = cur_kind == "dead_air"
        is_sdir   = cur_kind == "stage_dir"
        _md_label = 'remove md' if md_overlay is not None else 'import md'
        _has_sdir = bool(md_overlay) and any(ov['kind'] == 'stage_dir' for ov in md_overlay)
        if review:
            pairs = [('tab/⇧tab', 'next/prev')]
            if is_sdir:
                pairs += [('x', 'kind'), ('e', 'timing'), ('l', 'label')]
                if _HAS_VLC: pairs.append(('t', 'tap'))
            elif is_air:
                pairs += [('e', 'timing'), ('l', 'label'), ('k', 'make dir')]
            else:
                pairs += [('/', 'split'), ('e', 'edit')]
                if source == SOURCE_TRANSCRIPT: pairs.append(('w', 'words'))
                pairs.append(('←→', '±0.25s'))
            pairs += [('s', 'save'), ('esc', 'leave review'), ('q', 'quit')]
        elif show_hints:
            pairs = [('↑↓', 'navigate'), ('←→', '±0.25s'), (',/.', '±0.1s'), ('[/]', '±1s')]
            if source == SOURCE_TRANSCRIPT: pairs.append(('w', 'words'))
            if _HAS_VLC:                    pairs.append(('t', 'tap sync'))
            if _HAS_VLC:                    pairs.append(('b', 'audition'))
            pairs += [('e', 'edit'), ('j', 'join↓'), ('J/K', 'move↑/↓'),
                      ('a', 'dead air'), ('d', 'del'), ('l', 'label'),
                      ('k', 'air↔dir'), ('r', 'fill gaps'), ('m', _md_label)]
            if is_sdir:
                pairs.append(('x', 'kind (inline/tone/external)'))
            if _has_sdir: pairs.append(('M', 'commit stage dirs'))
            pairs += [('c', 'credits'), ('spc', 'mark')]
            if _HAS_VLC: pairs.append(('p', 'preview'))
            pairs.append(('s', 'save working'))
            if source == SOURCE_TRANSCRIPT:
                pairs += [('V', 'verify md'), ('S', 'split by line'), ('W', 'write file')]
            pairs += [('/', 'split line'),
                      ('R', 'review issues'), ('D', 'review directions'), ('L', 'long lines')]
            if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
            pairs += [('?', 'hide hints'), ('q', 'quit')]
        else:
            pairs = [('↑↓', 'navigate'), ('←→', '±0.25s')]
            if is_air or is_sdir:
                pairs += [('d', 'del'), ('l', 'label'),
                          ('k', 'make dir' if is_air else 'make air'),
                          ('e', 'timing')]
                if is_sdir:
                    pairs.append(('x', 'kind'))
                pairs.append(('J/K', 'move'))
            else:
                pairs += [('e', 'edit'), ('j', 'join'), ('J/K', 'move')]
                if source == SOURCE_TRANSCRIPT: pairs.append(('w', 'words'))
            if _has_sdir: pairs.append(('M', 'commit stage dirs'))
            pairs += [('/', 'split'), ('R', 'review'), ('D', 'dirs'), ('L', 'long'), ('s', 'save'), ('?', 'more')]
            if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
            pairs.append(('q', 'quit'))
            # (W = write to transcript.json — shown in full hints via ?)
        if selected: pairs.append(('', f'{len(selected)} marked'))
        footer.extend(_promptmod.chrome_hint_lines(pairs))

    HEADER    = 2   # title + rule (no leading blank; render adds the MARGIN_V top row)
    available = avail - HEADER - 2 - len(footer)
    vis       = max(1, available)

    # Compact grid (visible widths after the 2-col indent):
    #   ptr(1) sp(1) chk(1) sp(1)  start(9)  sp(2)  →  TEXT column (offset 15)
    # The words now begin at ~col 15 (was 37) so the transcript reads like a
    # script; duration / end-time / flags live in a dim right gutter.
    PREFIX_W = 1 + 1 + 1 + 1 + 9 + 2   # == 15

    def _rhs(s_t, e_t, is_cur, flags):
        """Right gutter, pinned to the body's right edge: end-time (current row
        only), then any warnings, then the DURATION last so it sits flush right in
        its own column.  Returns (display_string, visible_width)."""
        parts: list[tuple[str, str]] = []
        if is_cur and s_t is not None and e_t is not None:
            parts.append((C.DIM, f"→ {timefmt.clock(e_t)}"))
        parts.extend(flags)                              # warnings BEFORE duration
        if s_t is not None:
            parts.append((C.DIM, _dur(s_t, e_t).strip() or "·"))   # duration pinned rightmost
        disp = "  ".join(f"{c}{t}{C.RESET}" for c, t in parts)
        wid  = sum(len(t) for _, t in parts) + 2 * (len(parts) - 1) if parts else 0
        return disp, wid

    def _compose(prefix_disp, text_raw, text_color, rhs_disp, rhs_w, fmt=False):
        """Fixed prefix, flexible prominent text, right-aligned dim gutter.  The
        visible line never exceeds the body width, so it cannot wrap."""
        budget = max(1, B - PREFIX_W - (rhs_w + 2 if rhs_w else 0))
        t      = ui_utils.truncate_text(text_raw, budget)
        if fmt:
            # Emphasised words are underlined (strong ones also bold) so they stand
            # out even on the bold current row, and every span restores `base` —
            # the row's own colour — so the highlight continues past the emphasis
            # instead of the span's reset blanking the rest of the line.
            # Emphasis reads as type style, not underlines: *word* → italic,
            # **word** → bold. Cleaner than the old underline on already-bright rows.
            t_disp = _apply_markdown_formatting(
                t, base=text_color, strong=C.BOLD, em=C.ITALIC)
            # A music note is accent on the current row (text_color set) and dim
            # elsewhere — accent is reserved for the current line.  Either way it
            # renders in a fixed style rather than inheriting the row's bold, so
            # its glyph stays consistent; the span restores `base` afterwards.
            if '♪' in t_disp:
                _note_c = C.ACCENT if text_color else C.DIM
                t_disp = t_disp.replace('♪', f"{C.RESET}{_note_c}♪{C.RESET}{text_color}")
        else:
            t_disp = t
        if text_color:
            t_disp = f"{text_color}{t_disp}{C.RESET}"
        if rhs_w:
            # Pad off the VISIBLE width of what we actually print — markdown
            # formatting strips the * markers, so len(t) over-counts and the
            # gutter (and the duration pinned to its right edge) would drift left.
            vis = ui_utils.visual_len(t_disp)
            pad = max(1, B - PREFIX_W - vis - rhs_w)
            return indent + prefix_disp + t_disp + " " * pad + rhs_disp
        return indent + prefix_disp + t_disp

    if show_words:
        items   = segs[seg_cursor].get("words", []) if segs else []
        n_items = len(items)
        if cursor < vp:         vp = cursor
        if cursor >= vp + vis:  vp = cursor - vis + 1
        vp = max(0, min(vp, max(0, n_items - vis)))
        out.append(f"{indent}{C.DIM}↑  {vp} above{C.RESET}" if vp > 0 else "")
        for slot in range(vis):
            i = vp + slot
            if i >= n_items: break
            item  = items[i]
            is_cur = (i == cursor)
            s_t   = item.get("start"); e_t = item.get("end")
            prev  = items[i - 1] if i > 0 else None
            overlap = (prev is not None and s_t is not None
                       and prev.get("end") is not None and s_t < prev["end"])
            ptr     = f"{C.ACCENT}›{C.RESET}" if is_cur else " "
            start_d = f"{C.DIM}{timefmt.clock(s_t)}{C.RESET}" if s_t is not None else " " * 9
            prefix  = f"{ptr}   {start_d}  "   # ptr + 3sp fills the chk slot → offset 15
            flags   = [(C.YELLOW, "⚠ overlap")] if overlap else []
            rhs_d, rhs_w = _rhs(s_t, e_t, is_cur, flags)
            hit_map[len(out)] = i
            out.append(_compose(prefix, item.get('word', '').strip(),
                                 C.PRIMARY if is_cur else "", rhs_d, rhs_w))
        below = n_items - (vp + vis)
        out.append(f"{indent}{C.DIM}↓  {below} below{C.RESET}" if below > 0 else "")

    else:  # SEG — build flat display_items interleaving segs with MD overlay
        display_items: list[dict] = []
        ov_map: dict[int, list] = {}
        for ov in (md_overlay or []):
            ov_map.setdefault(ov['before_si'], []).append(ov)
        for si, seg in enumerate(segs):
            for ov in ov_map.get(si, []):
                display_items.append({'type': 'overlay', 'data': ov})
            display_items.append({'type': 'seg', 'si': si, 'seg': seg})
        for ov in ov_map.get(len(segs), []):
            display_items.append({'type': 'overlay', 'data': ov})

        # Isolated stage directions (floating ✦ overlays) get a blank line before
        # and after so they hover as a separate beat, not glued to the dialogue.
        def _is_float_sd(it: dict) -> bool:
            """True if display item is a floating (uncommitted) stage-direction overlay."""
            return it['type'] == 'overlay' and it['data']['kind'] == 'stage_dir'
        spaced: list[dict] = []
        _n = len(display_items); _i = 0
        def _is_banner(it: dict | None) -> bool:
            return bool(it) and it['type'] == 'overlay' and it['data'].get('kind') == 'speaker'
        while _i < _n:
            if _is_float_sd(display_items[_i]):
                # Hug the speaker banner when one sits directly above (a wordless
                # noise line, e.g. MARTIN then ✦ (exasperated noise)); otherwise a
                # blank fences the direction off as its own beat.
                if not _is_banner(spaced[-1] if spaced else None):
                    spaced.append({'type': 'blank'})
                while _i < _n and _is_float_sd(display_items[_i]):
                    spaced.append(display_items[_i]); _i += 1
                spaced.append({'type': 'blank'})
            else:
                spaced.append(display_items[_i]); _i += 1
        display_items = spaced

        n_disp    = len(display_items)
        cursor_di = next((i for i, it in enumerate(display_items)
                          if it['type'] == 'seg' and it['si'] == cursor), 0)

        # Items aren't all one row — an external (framed) direction draws up to 3.
        # Window by ROWS, not slots, so a run of tall items can't push the last
        # line under the footer.
        def _ov_spk(x):
            return bool(x) and x['type'] == 'overlay' and x['data'].get('kind') == 'speaker'
        def _ov_framed(x):
            return bool(x) and x['type'] == 'seg' and _is_framed(x['seg'])
        def _disp_rows(di: int) -> int:
            it = display_items[di]
            if not (it['type'] == 'seg' and _is_framed(it['seg'])):
                return 1
            prev = display_items[di - 1] if di > 0 else None
            nxt  = display_items[di + 1] if di + 1 < n_disp else None
            return 1 + (0 if (_ov_spk(prev) or _ov_framed(prev)) else 1) \
                     + (0 if _ov_spk(nxt) else 1)

        # Rows for the item list: reserve the two ↑/↓ indicators AND one blank so
        # the last/selected item always clears the footer by +1 row.
        item_rows = max(1, avail - HEADER - 2 - len(footer) - 1)

        if cursor_di < vp:
            vp = cursor_di
        while vp < cursor_di:                       # grow vp until the cursor fits
            _used = 0; _last = vp - 1
            for _di in range(vp, n_disp):
                _used += _disp_rows(_di)
                if _used > item_rows:
                    break
                _last = _di
            if cursor_di <= _last:
                break
            vp += 1
        # Keep leading overlays (e.g. a stage direction before the first seg)
        # visible when the cursor is that first seg and they still fit.
        if not any(it['type'] == 'seg' for it in display_items[:cursor_di]):
            if sum(_disp_rows(d) for d in range(cursor_di + 1)) <= item_rows:
                vp = 0
        vp = max(0, vp)

        segs_above = sum(1 for it in display_items[:vp] if it['type'] == 'seg')
        out.append(f"{indent}{C.DIM}↑  {segs_above} above{C.RESET}" if vp > 0 else "")

        _used_rows = 0
        _last_shown = vp - 1
        for di in range(vp, n_disp):
            if _used_rows + _disp_rows(di) > item_rows:
                break
            _used_rows += _disp_rows(di)
            _last_shown = di
            it = display_items[di]

            if it['type'] == 'blank':
                out.append("")
            elif it['type'] == 'overlay':
                ov = it['data']
                if ov['kind'] == 'speaker':
                    # Speaker banner — plain white name (no colour), then a dim rule.
                    name  = ov['text']
                    stg   = ov.get('stage', '')
                    head  = f"{C.RESET}{name}"
                    hplain = name
                    if stg:
                        head  += f" {C.DIM}({stg}){C.RESET}"
                        hplain += f" ({stg})"
                    # Dotted rule runs to the right edge of the body — i.e. where
                    # the duration column ends on the rows below — not full window.
                    rule = '┈' * max(1, B - len(hplain) - 1)
                    out.append(f"{indent}{head} {C.DIM}{rule}{C.RESET}")
                else:
                    # Isolated stage direction (uncommitted overlay) — left-aligned
                    # with a single ✦ and italic text, blank-fenced above and below
                    # so it still reads as its own beat.  Its ✦ sits in the same
                    # column as a committed cue's, so floating and committed cues line up.
                    _sd = ui_utils.truncate_text(f"({ov['text'].strip()})",
                                                 max(4, B - PREFIX_W - 2))
                    # A floating overlay is never the current line, so its ✦ is dim
                    # (accent stays reserved for the current row).
                    out.append(f"{indent}{' ' * PREFIX_W}{C.DIM}✦{C.RESET} "
                               f"{C.DIM}{C.ITALIC}{_sd}{C.RESET}")
            else:
                si  = it['si']
                seg = it['seg']
                is_cur = si == cursor
                is_sel = si in selected
                s_t    = seg.get('start')
                e_t    = seg.get('end')
                is_air  = seg.get('kind') == 'dead_air'
                is_sdir = seg.get('kind') == 'stage_dir'

                prev_seg = segs[si - 1] if si > 0 else None
                overlap  = (prev_seg is not None and s_t is not None
                            and prev_seg.get('end') is not None
                            and s_t < prev_seg['end'])
                word_overlap = False
                if not is_air and not is_sdir:
                    ws = seg.get('words', [])
                    for wi in range(len(ws) - 1):
                        if (ws[wi].get('end') is not None
                                and ws[wi + 1].get('start') is not None
                                and ws[wi]['end'] > ws[wi + 1]['start']):
                            word_overlap = True; break

                ptr  = f"{C.ACCENT}›{C.RESET}" if is_cur else " "
                chk  = f"{C.ACCENT}✔{C.RESET}" if is_sel else " "

                flags: list[tuple[str, str]] = []
                if overlap:      flags.append((C.YELLOW, "⚠ overlap"))
                if word_overlap: flags.append((C.YELLOW, "⚠ words"))
                if md_quality is not None and not is_air and not is_sdir:
                    _mq = md_quality.get(si)
                    if _mq is not None:
                        if _mq['score'] == 0.0:    flags.append((C.DIM,    "? md"))
                        elif _mq['score'] < 0.75:  flags.append((C.YELLOW, "≈ md"))

                start_d = f"{C.DIM}{timefmt.clock(s_t)}{C.RESET}" if s_t is not None else " " * 9
                prefix  = f"{ptr} {chk} {start_d}  "
                rhs_disp, rhs_w = _rhs(s_t, e_t, is_cur, flags)

                if is_air or is_sdir:
                    scope = _sd_scope(seg) if is_sdir else ''
                    sym   = ('~' if scope == 'tone' else '✦') if is_sdir else '◌'
                    _lbl  = seg.get('text', '').strip()
                    disp0 = f"({_lbl})" if _lbl else ("(…)" if is_sdir else "silence")
                    sym_c = C.ACCENT if is_cur else C.DIM
                    if is_sdir and s_t is not None:
                        # A *timed* stage direction is a placed beat — the italic
                        # label sets it apart from plain silence (◌) and from an
                        # untimed cue still awaiting its moment.
                        lbl_c = f"{C.PRIMARY if is_cur else C.DIM}{C.ITALIC}"
                    else:
                        lbl_c = C.PRIMARY if is_cur else C.DIM
                    # inline / tone sit indented under the words; external is a
                    # framed section (below).
                    sd_pad  = '   ' if (is_sdir and scope in ('inline', 'tone')) else ''
                    budget  = max(1, B - PREFIX_W - 2 - len(sd_pad) - (rhs_w + 2 if rhs_w else 0))
                    disp    = ui_utils.truncate_text(disp0, budget)
                    body    = (f"{prefix}{sd_pad}{sym_c}{sym}{C.RESET} "
                               f"{lbl_c}{disp}{C.RESET}")
                    if rhs_w:
                        pad  = max(1, B - PREFIX_W - 2 - len(sd_pad) - len(disp) - rhs_w)
                        body += " " * pad + rhs_disp
                    if is_air or (is_sdir and scope == 'external'):
                        # Framed section (dead air / external direction — belongs to
                        # nobody). Never double up dotted lines: a neighbouring
                        # speaker banner (NAME ┈┈) already draws a rule, so let it
                        # serve as the border; a rule between two stacked framed
                        # beats is drawn once.
                        _prev = display_items[di - 1] if di > 0 else None
                        _next = display_items[di + 1] if di + 1 < n_disp else None
                        def _is_speaker(it):
                            return bool(it) and it.get('type') == 'overlay' \
                                and it['data'].get('kind') == 'speaker'
                        def _is_framed_it(it):
                            return bool(it) and it.get('type') == 'seg' and _is_framed(it['seg'])
                        _erule = f"{C.DIM}{indent}{'┈' * B}{C.RESET}"
                        if not (_is_speaker(_prev) or _is_framed_it(_prev)):
                            out.append(_erule)                 # top rule
                        hit_map[len(out)] = si
                        out.append(indent + body)
                        if not _is_speaker(_next):
                            out.append(_erule)                 # bottom rule (else the banner below is it)
                    else:
                        hit_map[len(out)] = si   # this display row selects seg `si`
                        out.append(indent + body)
                else:
                    hit_map[len(out)] = si   # this display row selects seg `si`
                    _mq     = (md_quality or {}).get(si)
                    _use_md = bool(_mq and _mq.get('md_text'))
                    raw     = (_mq['md_text'] if (_use_md and _mq) else seg.get('text', '')).strip()
                    # Words are the anchor: bright/bold on the current row, normal
                    # elsewhere.  Markdown emphasis renders as styling, not literal *.
                    out.append(_compose(prefix, raw, C.PRIMARY if is_cur else "",
                                        rhs_disp, rhs_w, fmt=True))

        segs_below = sum(1 for it in display_items[_last_shown + 1:] if it['type'] == 'seg')
        out.append(f"{indent}{C.DIM}↓  {segs_below} below{C.RESET}" if segs_below > 0 else "")

    _cap = ui_utils.get_terminal_width()   # allow full-width rules; content self-limits to the margin
    return ([_clip(ln, _cap) for ln in _fit_body_footer(out, footer, avail)],
            vp, hit_map, _footer_rows(footer, avail), None)



def lyrics_editor(mp3_path: str) -> None:
    """Run the interactive lyrics/transcript sync editor for mp3_path until the user quits."""
    if not drop_moved([mp3_path]):
        return
    result = _load(mp3_path)
    if result is None:
        ui_utils.show_status("No lyrics or transcript found for this track.")
        return

    segs, source, aux = result
    # Prefer ID3 `TIT2` title when available, otherwise fall back to filename stem
    try:
        try:
            id3 = ID3(mp3_path)
            tit = id3.get('TIT2')
            if tit and getattr(tit, 'text', None):
                track_name = str(tit.text[0])
            else:
                track_name = os.path.splitext(os.path.basename(mp3_path))[0]
        except ID3NoHeaderError:
            track_name = os.path.splitext(os.path.basename(mp3_path))[0]
    except Exception:
        track_name = os.path.splitext(os.path.basename(mp3_path))[0]
    if aux.get('drift'):
        ui_utils.show_status(
            "⚠ transcript.json changed since this working copy — W will overwrite it.", 6.0)

    # Lead-in offset for tap sync (compensates for reaction time)
    try:
        from src.config import load_config
        cfg = load_config()
        tap_offset_s = -cfg["lyric_lead_in"]  # seconds; load_config fills in the default
    except Exception:
        tap_offset_s = 0.0

    mode       = SEG
    prev_mode  = SEG
    cursor     = 0
    seg_cursor = 0
    selected: set[int] = set()
    viewport   = 0
    dirty      = False
    show_hints = False
    review_phase: str | None = None          # current phase, or None when not reviewing
    review_program: str | None = None        # 'issues' (R) or 'dirs' (D)
    md_overlay: list | None = None
    md_quality: dict | None = None
    md_path:    str  | None = None
    undo_stack: list = []

    edit_fields: dict[str, list[str]] = {}   # fk → digit chars (MM:SS.mmm per bound)
    edit_orig:   dict[str, str]       = {}   # snapshot at open → detect what changed
    edit_fi    = 0                            # active field index into _EDIT_ORDER
    edit_pos   = 0                            # caret position within the active field
    edit_fresh = False                        # active field untouched → next digit clears it

    playing    = False
    play_until = 0.0
    play_pos   = 0.0

    aud_queue: list[tuple[float, float, str]] = []   # AUDITION: clips left to play
    aud_now:   str | None = None                      # 'start' / 'end' clip playing now
    aud_editing = False                               # AUDITION: inline timestamp editor open

    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)

    mp = None
    if _vlc is not None:
        try:
            old_fd  = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2); os.close(devnull)
            inst = _vlc.Instance('--no-video', '--quiet')
            mp_i = inst.media_player_new()           # type: ignore[union-attr]
            mp_i.set_media(inst.media_new(mp3_path)) # type: ignore[union-attr]
            os.dup2(old_fd, 2); os.close(old_fd)
            mp = mp_i
        except (AttributeError, OSError):
            pass

    w = _Widget(fd)

    def _sources_label() -> str:
        """The documents this session is actually reading, named as they are on disk.

        `working copy` matters more than the rest: resuming from the `.sync.json`
        sidecar means edits are going there and not to the transcript until W, and
        a `diverged` copy means the transcript has changed underneath it since. Both
        were previously announced once at load and then invisible for the rest of
        the session.
        """
        parts: list[str] = []
        if aux.get('jpath'):
            parts.append(os.path.basename(aux['jpath']))
        if aux.get('from_sidecar'):
            parts.append('diverged working copy' if aux.get('drift') else 'working copy')
        if source == SOURCE_SYLT:
            parts.append('embedded SYLT')
        elif source == SOURCE_USLT:
            parts.append('embedded USLT')
        if md_path:
            parts.append(os.path.basename(md_path))
        return ' · '.join(parts)

    def cur_words() -> list:
        """Words of the segment currently open in WORD mode."""
        return segs[seg_cursor].get("words", []) if segs else []

    def _sync_seg_bounds(si: int) -> None:
        """Recompute segment si's start/end from its first/last word timing after a word edit."""
        words = segs[si].get("words", [])
        if not words: return
        first = words[0].get("start")
        last  = words[-1].get("end")
        if first is not None: segs[si]["start"] = round(first, 3)
        if last  is not None: segs[si]["end"]   = round(last,  3)

    def do_preview(start_s: float, dur: float | None = None) -> None:
        """Play from start_s.  With `dur`, stop automatically after that many
        seconds (the main loop honours play_until); without it, play open-ended."""
        nonlocal playing, play_until, play_pos
        if mp is None: return
        start_s = max(0.0, start_s)
        mp.set_time(int(start_s * 1000))
        if not mp.is_playing():
            mp.play(); time.sleep(0.15)
        playing    = True
        play_until = float('inf') if dur is None else time.time() + dur
        play_pos   = start_s

    def do_stop() -> None:
        """Pause playback and clear the playing flag."""
        nonlocal playing
        if mp and mp.is_playing(): mp.pause()
        playing = False

    def _aud_next() -> None:
        """Play the next queued clip; stop when the queue drains."""
        nonlocal aud_now
        if not aud_queue:
            aud_now = None; do_stop(); return
        lo, hi, label = aud_queue.pop(0)
        aud_now = label
        do_preview(lo, max(0.1, hi - lo))

    def _aud_clips(which: str = 'line') -> None:
        """Queue audition playback for the current line and start it:
          'line'  → the whole line, start → end (the default when you land on it);
          'start' / 'end' → a short (<=_AUD_CLIP) clip of just that boundary;
          'both'  → the start clip then the end clip, in sequence.
        Clips never bleed into the neighbouring lines."""
        nonlocal aud_queue
        aud_queue = []
        if segs and cursor < len(segs):
            s = segs[cursor].get("start")
            e = segs[cursor].get("end")
            if which == 'line' and s is not None:
                hi = e if e is not None else s + 3.0
                aud_queue.append((s, max(s + 0.1, hi), 'line'))
            if which in ('both', 'start') and s is not None:
                hi = s + _AUD_CLIP if e is None else min(e, s + _AUD_CLIP)
                aud_queue.append((s, max(s + 0.1, hi), 'start'))
            if which in ('both', 'end') and e is not None:
                floor = s if s is not None else 0.0     # never precede the line's start
                lo = max(0.0, floor, e - _AUD_CLIP)
                aud_queue.append((lo, e, 'end'))
        _aud_next()

    def _aud_shift(delta: float) -> None:
        """Move the whole line — both timestamps and its words — by delta, keeping
        its duration, then play the start so the new position can be judged by ear.
        (The duration itself is changed by typing in the editor: press e.)"""
        if not segs or cursor >= len(segs):
            return
        if segs[cursor].get("start") is None:
            ui_utils.show_status("This line has no timestamp to move."); return
        apply_segs([cursor], delta)   # shifts start, end and words together (undoable)
        _aud_clips('start')

    def refresh_overlay() -> None:
        """Re-derive the MD overlay from the current segs.  Call after ANY
        structural change to segs.  The overlay is a pure projection of
        (segs, MD): rebuilding is always safe and never duplicates, because
        committed stage_dir segs are reconciled inside _build_md_overlay."""
        nonlocal md_overlay, md_quality
        if md_path:
            md_overlay, md_quality, _links = _build_md_overlay(segs, md_path)
            for _si, _lid in _links.items():   # record durable alignment on segs
                if _lid is not None:
                    segs[_si]['line_ref'] = _lid
        else:
            md_overlay = md_quality = None

    def _review_enter(program: str) -> None:
        """Start a walkthrough (program 'issues' or 'dirs'): jump to the first
        outstanding item, earliest phase first."""
        nonlocal review_phase, review_program, cursor, viewport
        for ph in _REVIEW_PROGRAMS[program]:
            issues = _review_phase_issues(segs, ph, md_quality)
            if issues:
                review_program = program
                review_phase = ph
                cursor = issues[0]
                viewport = 0
                ui_utils.show_status(
                    f"Review · {_REVIEW_PHASE_NAME[ph]}: {len(issues)} to fix — "
                    f"Tab/⇧Tab next/prev, fix in place, Esc to leave.")
                return
        _none = ("stage directions need categorising or timing" if program == 'dirs'
                 else "MD mismatches, word or overlap errors")
        ui_utils.show_status(f"Nothing to review — no {_none}.")

    def _review_advance(direction: int) -> None:
        """Move to the next/prev outstanding item, recomputed live so fixed ones
        drop out; rolls through this program's phases and ends when none remain."""
        nonlocal review_phase, review_program, cursor, viewport
        phases = _REVIEW_PROGRAMS.get(review_program, ())
        ph = review_phase
        while ph is not None and ph in phases:
            issues = _review_phase_issues(segs, ph, md_quality)
            if direction > 0:
                nxt = next((si for si in issues if si > cursor), None)
            else:
                nxt = next((si for si in reversed(issues) if si < cursor), None)
            if nxt is not None:
                review_phase = ph
                cursor = nxt
                viewport = 0
                return
            # exhausted this phase in this direction — step to the adjacent phase
            i = phases.index(ph) + (1 if direction > 0 else -1)
            if not (0 <= i < len(phases)):
                break
            ph = phases[i]
            issues = _review_phase_issues(segs, ph, md_quality)
            if issues:
                review_phase = ph
                cursor = issues[0] if direction > 0 else issues[-1]
                viewport = 0
                ui_utils.show_status(f"Review · {_REVIEW_PHASE_NAME[ph]}: {len(issues)} to fix.")
                return
        review_phase = None
        review_program = None
        ui_utils.show_status("Review complete — all resolved. (Save with s.)")

    def do_smart_split() -> None:
        """Split the current spoken line at its strongest semantic break nearest the
        middle (or the middle itself when there's no punctuation), redistributing
        words and timing like the per-word split. Repeat to split further."""
        nonlocal dirty
        if not (segs and 0 <= cursor < len(segs)):
            return
        seg = segs[cursor]
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
        undo_stack.append(('split', cursor, seg))
        segs[cursor:cursor + 1] = [seg_a, seg_b]
        dirty = True
        refresh_overlay()
        _after = (text_toks[i - 1] if len(text_toks) == len(words)
                  else w_a[-1].get('word', '')).strip()
        ui_utils.show_status(f"Split after “{_after}”.")

    def apply_segs(idxs: list[int], delta: float) -> None:
        """Shift the given segments by delta seconds, marking dirty and recording an undo entry."""
        nonlocal dirty
        for i in idxs: _shift_seg(segs[i], delta)
        dirty = True; undo_stack.append(('seg', list(idxs), delta))

    def apply_word(si: int, wi: int, delta: float) -> None:
        """Shift one word by delta seconds, resync its segment's bounds, and record an undo entry."""
        nonlocal dirty
        _shift_word(segs[si]["words"][wi], delta)
        _sync_seg_bounds(si)
        dirty = True; undo_stack.append(('word', si, wi, delta))

    def _may_quit() -> bool:
        """True to leave the editor: straight away with nothing unsaved,
        otherwise only if the user agrees to lose the changes."""
        if not dirty:
            return True
        _restore_term_attrs(fd, old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        _ans = _prompt_text("You have unsaved changes (s saves). Quit without saving? (y/N)")
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        w.anchor_reset()
        return (_ans or "").strip().lower().startswith("y")

    def do_undo() -> None:
        """Pop and reverse the most recent undo-stack entry, restoring segs/cursor/mode as needed."""
        nonlocal dirty, cursor, mode
        if not undo_stack: return
        op = undo_stack.pop()
        if   op[0] == 'seg':
            [_shift_seg(segs[i], -op[2]) for i in op[1]]
        elif op[0] == 'seg_end':
            segs[op[1]]["end"] = op[2]
        elif op[0] == 'word':
            _shift_word(segs[op[1]]["words"][op[2]], -op[3])
            _sync_seg_bounds(op[1])
        elif op[0] == 'word_end':
            segs[op[1]]["words"][op[2]]["end"] = op[3]
            _sync_seg_bounds(op[1])
        elif op[0] == 'split':
            si, orig = op[1], op[2]
            segs[si:si + 2] = [orig]
            mode = SEG; cursor = si
        elif op[0] == 'join':
            ci, seg_a, seg_b = op[1], op[2], op[3]
            segs[ci:ci + 1] = [seg_a, seg_b]
            cursor = ci
        elif op[0] == 'delete':
            ci, seg = op[1], op[2]
            segs[ci:ci] = [seg]  # re-insert; do NOT overwrite the neighbour
            cursor = ci
        elif op[0] == 'tap':
            idx, old_start, old_prev_end = op[1], op[2], op[3]
            segs[idx]["start"] = old_start
            if idx > 0 and old_prev_end is None:
                segs[idx - 1]["end"] = None
            cursor = idx
        elif op[0] == 'snapshot':
            segs[:] = op[1]
        # Still unsaved: some edits (labels, dead air, fill gaps, credits) push
        # no undo entry, so an empty undo stack doesn't mean nothing changed.
        # Only a save clears this.
        dirty = True
        # segs may have changed structurally — keep the overlay in sync so
        # nothing is left pointing at stale indices.
        if op[0] in ('split', 'join', 'delete', 'snapshot'):
            refresh_overlay()

    def do_save() -> None:
        """Persist current edits: transcript source writes the working sidecar JSON;
        SYLT/USLT sources write the SYLT tag directly."""
        nonlocal dirty
        if source == SOURCE_TRANSCRIPT:
            # Save to the WORKING document (sidecar) — never touches the original
            # transcript.json until the user commits with 'W'.
            _ensure_ids(segs, aux['meta'])
            sdata = {'version': 1, 'source_json': os.path.basename(aux['jpath']),
                     'meta': aux['meta'], 'segments': segs}
            write_text_atomic(aux['sidecar'], json.dumps(sdata, indent=2, ensure_ascii=False))
            ui_utils.show_status(
                f"Saved to {os.path.basename(aux['sidecar'])} — press W to write transcript.json")
            dirty = False; undo_stack.clear()
            return
        else:
            from src.lyrics.lyrics import save_sylt_entries
            entries = []
            for s in segs:
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
            save_sylt_entries(aux['mp3'], entries, desc=aux.get('desc', ''), lang=aux.get('lang', 'eng'))
        changed: set[int] = set()
        for op in undo_stack:
            if   op[0] == 'seg':      changed.update(op[1])
            elif op[0] == 'seg_end':  changed.add(op[1])
            elif op[0] in ('word', 'word_end'): changed.add(op[1])
            elif op[0] == 'split':    changed.update([op[1], op[1] + 1])
            elif op[0] == 'join':     changed.add(op[1])
            elif op[0] == 'tap':      changed.add(op[1])
        ui_utils.show_status(f"Saved — {len(changed)} line{'s' if len(changed) != 1 else ''} changed.")
        dirty = False; undo_stack.clear()

    def do_commit() -> bool:
        """Write timings back to transcript.json (+ .srt) in the ORIGINAL Whisper
        schema: spoken segments only.  The editor's stage-direction / dead-air
        beats and all bookkeeping fields (ids, alignment, kind) are dropped — they
        live on in the sidecar and the .md — so transcript.json stays a plain
        Whisper transcript of just the spoken words."""
        jpath = aux['jpath']
        try:
            with open(jpath, encoding='utf-8') as f:
                container = json.load(f)
        except OSError:
            container = {}
        except json.JSONDecodeError:
            # Writing {} + segments would drop every other key it holds.
            ui_utils.show_status(f"{os.path.basename(jpath)} can't be read as JSON; not overwritten.",
                                 duration=5.0)
            return False
        spoken = [s for s in segs if s.get('kind') not in ('stage_dir', 'dead_air', 'credit')]
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
        write_text_atomic(jpath[:-5] + '.srt', _rebuild_srt(segs))
        aux['meta']['source_fp'] = _file_fp(jpath)   # we now match the original
        # Also write an enriched sidecar (.sync.json) that preserves the
        # editor's stage-direction / dead-air beats so the player and editor
        # render the same segments when loading this track.
        try:
            _ensure_ids(segs, aux['meta'])
            sdata = {'version': 1, 'source_json': os.path.basename(jpath),
                     'meta': aux['meta'], 'segments': segs}
            write_text_atomic(jpath[:-5] + '.sync.json', json.dumps(sdata, indent=2, ensure_ascii=False))
        except Exception as exc:
            log.warning("couldn't write %s.sync.json: %s", jpath[:-5], exc)
            ui_utils.show_status(f"Committed, but the .sync.json couldn't be written: {exc}", duration=5.0)
        return True

    def _pager(body: list, title: str) -> None:
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
            w.render(rendered)
            for _i in range(len(rendered) - len(foot), len(rendered)):
                add_hint_click_cells_auto(hint_cells, rendered[_i], _i)
            if not _wait_for_keypress(0.2):
                continue
            k = _read_key(fd)
            if k.startswith('MOUSE_CLICK:') and w.row is not None:
                _p = k.split(':'); _r = int(_p[2]); _c = int(_p[3]) if len(_p) > 3 else 1
                k = hint_cells.get((_r - w.row - ui_utils.MARGIN_V, _c)) or ''
            _ch = _promptmod.consume_chrome(k, {})      # the transport keys it advertises
            if _ch is _promptmod.CHROME_REDRAW:
                w.anchor_reset(); continue
            if _ch is _promptmod.CHROME_HANDLED:
                continue
            if   k in ('q', 'ESC', 'CTRL_C'): break
            elif k in ('UP', 'k'):            vp -= 1
            elif k in ('DOWN', 'j', 'SPACE'): vp += 1
            elif k == 'PGUP':                 vp -= vis
            elif k == 'PGDN':                 vp += vis
            elif k == 'HOME':                 vp = 0
            elif k == 'END':                  vp = len(body)

    def do_verify() -> None:
        """Run the JSON<->MD verification report, write it alongside the transcript, and page through it."""
        from src.lyrics.lyrics import _find_markdown_for_audio
        _mdp = md_path or _find_markdown_for_audio(mp3_path)
        if not _mdp:
            ui_utils.show_status("No transcript.md found to verify against."); return
        rep = _verify_matchup(segs, _mdp)
        _rp = aux['jpath'][:-5] + '.verify.txt'
        try:
            with open(_rp, 'w', encoding='utf-8') as f:   # plain text (colour stripped)
                f.write("\n".join(ui_utils.strip_ansi(l) for l in rep['lines']) + "\n")
            _rp_note = f"  ·  report: {os.path.basename(_rp)}"
        except OSError:
            _rp_note = ""
        s = rep['summary']
        _pager(rep['lines'],
               f"VERIFY  {s['match_pct']}% matched  ·  {s['discrepancies']} issues{_rp_note}")
        w.anchor_reset()

    def do_speaker_split() -> None:
        """Cut every segment the script says is more than one beat — a new speaker
        part way through, or an inline stage direction — pinning each piece to its
        MD line, then re-verify.  One beat per segment is the clean baseline to
        word-split from, and it is what gives each direction a boundary of its own:
        four *(Ding)*s on one line stop collapsing into one.

        Only cuts the script has punctuated its way into are made here. One that
        would land mid-sentence is reported by verify and left for you."""
        nonlocal dirty, cursor
        from src.lyrics.lyrics import _find_markdown_for_audio
        _mdp = md_path or _find_markdown_for_audio(mp3_path)
        if not _mdp:
            ui_utils.show_status("No transcript.md found to verify against."); return
        cands, _unplaced, _suggested = _split_candidates(segs, _mdp)
        if not cands:
            ui_utils.show_status("✔ Every segment is a single script beat — nothing to split.")
            return
        _restore_term_attrs(fd, old)
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
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        w.anchor_reset()
        if not (_ans or "").strip().lower().startswith("y"):
            ui_utils.show_status("Split cancelled."); return
        undo_stack.append(('snapshot', list(segs)))
        n = len(cands)
        # split from the highest seg index down so earlier indices stay valid
        for c in sorted(cands, key=lambda c: c['seg'], reverse=True):
            pieces = _split_seg_at(segs[c['seg']], c['boundaries'], c['line_refs'])
            segs[c['seg']:c['seg'] + 1] = pieces
        cursor = min(cursor, len(segs) - 1)
        dirty = True
        refresh_overlay()
        remain = len(_split_candidates(segs, _mdp)[0])   # verification
        ui_utils.show_status(
            f"Split {n} segment(s) to one script beat each — {remain} remaining.")

    def do_recategorise(si: int) -> None:
        """Flip the beat at `si` between dead air (◌) and a stage direction (✦),
        preserving timings.  A direction needs descriptive text, so promoting
        blank silence prompts for it.  Shared by the 'k' key and the click path."""
        nonlocal dirty
        _seg = segs[si] if (segs and 0 <= si < len(segs)) else None
        if not (_seg and _seg.get("kind") in ("dead_air", "stage_dir")):
            ui_utils.show_status("Only dead air / stage direction segments can be recategorised.")
            return
        _old_kind = _seg["kind"]
        _new_kind = "stage_dir" if _old_kind == "dead_air" else "dead_air"
        _text     = _seg.get("text", "").strip()
        if _new_kind == "stage_dir" and not _text:
            _restore_term_attrs(fd, old)
            sys.stdout.write("\033[?1000l\033[?1006l")
            sys.stdout.flush()
            try:                     # drop a click's pending mouse-release bytes so
                import termios       # they don't leak into the text prompt
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                pass
            _lbl = _prompt_text("Stage direction text:")
            _set_raw(fd)
            sys.stdout.write("\033[?1000h\033[?1006h")
            w.anchor_reset()
            if _lbl is None or not _lbl.strip():
                ui_utils.show_status("Recategorise cancelled — a stage direction needs text.")
                return
            _text = _lbl.strip()
        # Replace with a fresh dict (never mutate in place) so the snapshot keeps
        # the original for undo.
        undo_stack.append(('snapshot', list(segs)))
        segs[si] = {**_seg, "kind": _new_kind, "text": _text}
        dirty = True
        refresh_overlay()
        ui_utils.show_status(
            "Recategorised as "
            f"{'stage direction' if _new_kind == 'stage_dir' else 'dead air'}.")

    def commit_field(field: str, val: float) -> None:
        """Apply an edited timestamp field to the current seg or word: 'start' shifts
        (undoable via apply_segs/apply_word), 'end' is set absolutely."""
        nonlocal dirty
        if prev_mode == SEG:
            old_val = segs[cursor].get(field)
            if old_val is None:
                # Setting a previously unset timestamp directly
                undo_stack.append(('seg_end', cursor, None))
                segs[cursor][field] = round(val, 3); dirty = True
                return
            d = round(val - old_val, 3)
            if abs(d) < 1e-6: return
            if field == 'start':
                apply_segs([cursor], d)
            else:
                undo_stack.append(('seg_end', cursor, segs[cursor].get("end")))
                segs[cursor]["end"] = round(val, 3); dirty = True
        else:
            words = cur_words()
            if cursor < len(words):
                ww      = words[cursor]
                old_val = ww.get(field) or 0.0
                d       = round(val - old_val, 3)
                if abs(d) < 1e-6: return
                if field == 'start':
                    apply_word(seg_cursor, cursor, d)
                else:
                    undo_stack.append(('word_end', seg_cursor, cursor, ww.get("end", 0.0)))
                    ww["end"] = round(val, 3)
                    _sync_seg_bounds(seg_cursor)
                    dirty = True

    def _edit_target() -> dict | None:
        """The seg or word dict the EDIT-mode fields currently apply to."""
        if prev_mode == SEG:
            return segs[cursor] if (segs and cursor < len(segs)) else None
        ws = cur_words()
        return ws[cursor] if cursor < len(ws) else None

    def _edit_prefill() -> None:
        """Populate the segmented fields from the item being edited.  An *untimed*
        stage direction / dead air is seeded from the gap between its timed
        neighbours (or 0 at the top), so giving it a timing is usually a single
        Enter instead of typing MM:SS.mmm from scratch."""
        nonlocal edit_fields, edit_orig, edit_fi, edit_pos, edit_fresh
        item = _edit_target() or {}
        start_v, end_v = item.get('start'), item.get('end')
        seed = (prev_mode == SEG and start_v is None
                and item.get('kind') in ('stage_dir', 'dead_air')
                and segs and cursor < len(segs))
        if seed:
            prev_t = next((segs[j].get('end') for j in range(cursor - 1, -1, -1)
                           if segs[j].get('end') is not None), None)
            next_t = next((segs[j].get('start') for j in range(cursor + 1, len(segs))
                           if segs[j].get('start') is not None), None)
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
        edit_fields = {'sm': list(sm), 'ss': list(ss), 'sms': list(sms),
                       'em': list(em), 'es': list(es), 'ems': list(ems)}
        edit_orig   = {k: "".join(v) for k, v in edit_fields.items()}
        if seed:
            # These bounds were unset; force Enter to commit the seeded values even
            # when they read as 0, so one Enter actually times the direction.
            for k in (*_EDIT_START, *_EDIT_END):
                edit_orig[k] = "\x00"
        edit_fi     = 0
        edit_pos    = 0
        edit_fresh  = True   # first digit fills the field from the left

    def _edit_field_key(key: str) -> None:
        """Handle one field-manipulation key for the segmented editor.  Digits fill
        from the left (a fresh field is replaced on the first digit); ↑↓ spin the
        value; ms treats its digits as a right-padded fraction (5 → 500)."""
        nonlocal edit_fi, edit_pos, edit_fresh
        fk   = _EDIT_ORDER[edit_fi]
        buf  = edit_fields[fk]
        maxl = _EDIT_MAXLEN[fk]
        if key == 'TAB':
            edit_fi = (edit_fi + 1) % len(_EDIT_ORDER)
            edit_pos = 0; edit_fresh = True
        elif key == 'BACKTAB':
            edit_fi = (edit_fi - 1) % len(_EDIT_ORDER)
            edit_pos = 0; edit_fresh = True
        elif key == 'LEFT':
            edit_pos = max(0, edit_pos - 1); edit_fresh = False
        elif key == 'RIGHT':
            edit_pos = min(len(buf), edit_pos + 1); edit_fresh = False
        elif key in ('UP', 'DOWN'):
            v = _field_value(fk, "".join(buf)) + (1 if key == 'UP' else -1)
            v = max(0, min(_EDIT_LIM[fk], v))
            buf[:] = list(_field_str(fk, v)); edit_pos = len(buf); edit_fresh = False
        elif key == 'BACKSPACE':
            edit_fresh = False
            if edit_pos > 0: buf.pop(edit_pos - 1); edit_pos -= 1
        elif key == 'DELETE':
            edit_fresh = False
            if edit_pos < len(buf): buf.pop(edit_pos)
        elif key == 'HOME':
            edit_pos = 0; edit_fresh = False
        elif key == 'END':
            edit_pos = len(buf); edit_fresh = False
        elif len(key) == 1 and key.isdigit():
            if edit_fresh:
                buf[:] = [key]; edit_pos = 1; edit_fresh = False
            elif len(buf) < maxl:
                buf.insert(edit_pos, key); edit_pos += 1

    def _edit_apply() -> None:
        """Commit only the bound(s) whose digits changed, reusing commit_field so
        start keeps its shift semantics and end is set absolutely."""
        def _val(keys) -> float:
            """Combine a (minutes, seconds, ms) field triple into seconds."""
            m  = _field_value(keys[0], "".join(edit_fields[keys[0]]))
            s  = _field_value(keys[1], "".join(edit_fields[keys[1]]))
            ms = _field_value(keys[2], "".join(edit_fields[keys[2]]))
            return round(m * 60 + s + ms / 1000.0, 3)
        if any("".join(edit_fields[k]) != edit_orig[k] for k in _EDIT_START):
            commit_field('start', _val(_EDIT_START))
        if any("".join(edit_fields[k]) != edit_orig[k] for k in _EDIT_END):
            commit_field('end', _val(_EDIT_END))

    try:
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")  # enable mouse (click + scroll)
        sys.stdout.flush()
        need_redraw = True
        hit_map: dict[int, int] = {}   # row → item index, rebuilt on every _draw
        hint_cells: dict = {}          # (out-index, col) → synth key for footer hints
        prog_geo = None                # AUDITION progress bar: (out-index, col, width)

        while True:
            n = len(segs)

            total_s: float = 0.0
            if mp and mp.get_length() > 0:
                total_s = mp.get_length() / 1000.0
            elif segs and segs[-1].get("end") is not None:
                total_s = float(segs[-1]["end"])

            if playing:
                if mp and not mp.is_playing():
                    # clip ran to the track's natural end
                    if aud_queue: _aud_next()
                    else: playing = False; aud_now = None
                    need_redraw = True
                elif time.time() > play_until:
                    # this clip's window elapsed — play the next queued one, or stop
                    if aud_queue: _aud_next()
                    else: do_stop(); aud_now = None
                    need_redraw = True
                elif mp:
                    pos = mp.get_time() / 1000.0
                    if abs(pos - play_pos) > 0.05:
                        play_pos = pos; need_redraw = True
                    # auto-scroll cursor in SEG/WORD/TAP (never in AUDITION — the
                    # boundary being auditioned must stay put while it plays)
                    if mode in (SEG, WORD, TAP):
                        scroll_items = segs if mode in (SEG, TAP) else cur_words()
                        new_cur = cursor
                        for i in range(cursor + 1, len(scroll_items)):
                            if (scroll_items[i].get("start") or 0.0) <= play_pos:
                                new_cur = i
                            else:
                                break
                        if new_cur != cursor:
                            cursor = new_cur; need_redraw = True

            if ui_utils.consume_resize():
                w.anchor_reset(); need_redraw = True

            if need_redraw:
                review_info = None
                if review_phase:
                    _riss = _review_phase_issues(segs, review_phase, md_quality)
                    _ridx = (_riss.index(cursor) + 1) if cursor in _riss else '–'
                    review_info = (_REVIEW_PHASE_NAME[review_phase], _ridx, len(_riss))
                lines, viewport, hit_map, footer_rows, prog_geo = _draw(
                    segs, cursor, seg_cursor, mode, prev_mode, selected,
                    viewport, dirty, len(undo_stack), track_name,
                    playing, play_pos,
                    {'fields': edit_fields, 'fi': edit_fi, 'pos': edit_pos},
                    source, total_s,
                    show_hints, md_overlay, md_quality, aud_now, aud_editing,
                    review=review_info, sources=_sources_label(),
                )
                hint_cells.clear()
                if lines:       # the app-wide hints toggle, on the top line
                    lines[0] = _promptmod.add_help_corner(lines[0], 0, hint_cells, mode != EDIT)
                w.render(lines)
                need_redraw = False
                # Map the clickable footer-hint glyphs. `_draw` reports how many
                # trailing lines are the hint footer, so only those are scanned —
                # that keeps lyric text with brackets (e.g. "[Chorus]") from being
                # mistaken for a key, without depending on the divider glyph. Cells
                # are keyed by out-index row (col is absolute), matching the
                # r → line_idx inversion the mouse handler uses.
                for _i in range(max(0, len(lines) - footer_rows), len(lines)):
                    add_hint_click_cells_auto(hint_cells, lines[_i], _i)

            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            need_redraw = True

            # Mini-player (shared session) transport from the editor: Ctrl-P/N/B,
            # and Ctrl-O to open the full player over the editor — typed, or
            # clicked in the hint bar (replayed below).
            def _transport(key) -> bool:
                if key in ('\x10', '\x0e', '\x02'):
                    _np_transport({'\x10': 'playpause', '\x0e': 'next', '\x02': 'prev'}[key])
                    return True
                if key == '\x0f' and _prompt_chrome._player_opener is not None:
                    _prompt_chrome._player_opener()
                    sys.stdout.write("\033[?1000h\033[?1006h"); sys.stdout.flush()
                    w.anchor_reset()
                    return True
                return False

            if _transport(key):
                continue
            if key == _promptmod.HINTS_CLICK or (key == 'i' and mode != EDIT):
                _promptmod.toggle_hints()
                w.anchor_reset()
                continue

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
                        _np_transport(_act); continue
                    if _act == 'open':
                        if _prompt_chrome._player_opener is not None:
                            _prompt_chrome._player_opener()
                            sys.stdout.write("\033[?1000h\033[?1006h"); sys.stdout.flush()
                            w.anchor_reset()
                        continue
                # Only the press acts; the paired release is swallowed so one
                # physical click is one logical action.  A click on a footer-hint
                # glyph replays that key through the switch below; otherwise
                # `hit_map` (from the last _draw) maps a rendered line index to its
                # item — the widget draws line[i] at row w.row + MARGIN_V + i, so
                # invert that. Clicking the ALREADY-current line opens its word
                # view; a not-yet-current line is made current first (double-click).
                _hk = None
                if key.startswith('MOUSE_CLICK:') and w.row is not None:
                    parts = key.split(':')
                    r = int(parts[2]) if len(parts) > 2 else 0
                    col = int(parts[3]) if len(parts) > 3 else 1
                    line_idx = r - w.row - ui_utils.MARGIN_V
                    _hk = hint_cells.get((line_idx, col))
                    # A click on AUDITION's progress bar moves the playhead there:
                    # the clip queue is abandoned (the clicked point is nobody's
                    # boundary) and playback runs on open-ended from that spot.
                    if _hk is None and prog_geo is not None and total_s > 0:
                        _pl, _pc, _pw = prog_geo
                        # the '[' / ']' caps count as the two ends
                        if line_idx == _pl and _pc - 1 <= col <= _pc + _pw:
                            _frac = (col - _pc) / (_pw - 1) if _pw > 1 else 0.0
                            aud_queue = []
                            aud_now = None
                            # stop just shy of the end, like SESSION.seek does, so
                            # clicking the far right doesn't run the track out
                            do_preview(min(max(0.0, min(1.0, _frac)) * total_s,
                                           max(0.0, total_s - 0.5)))
                            continue
                    if _hk is None and mode in (SEG, WORD):
                        target = hit_map.get(line_idx)
                        if target is not None:
                            _t = segs[target] if target < len(segs) else {}
                            if mode == SEG and target == cursor:
                                if source == SOURCE_TRANSCRIPT and _t.get("words"):
                                    seg_cursor = target
                                    mode = WORD; cursor = 0; viewport = 0
                                elif _t.get("kind") == "dead_air" and _t.get("start") is not None:
                                    do_recategorise(target)
                            else:
                                cursor = target
                if _hk is None:
                    continue
                key = _hk           # replay the clicked hint's key through the switch
                if _transport(key):
                    continue
                if key == _promptmod.HINTS_CLICK:
                    _promptmod.toggle_hints()
                    w.anchor_reset()
                    continue

            # Review walkthrough (layered over SEG): Tab/⇧Tab step between flagged
            # lines, Esc leaves review; every other key falls through so you fix
            # the current line in place with the normal editing keys.
            if review_phase and mode == SEG:
                if key == 'TAB':
                    _review_advance(1); continue
                if key == 'BACKTAB':
                    _review_advance(-1); continue
                if key == 'ESC':
                    review_phase = None
                    review_program = None
                    ui_utils.show_status("Left review.")
                    continue

            if mode == EDIT:
                if key == 'ESC':
                    mode = prev_mode
                elif key == 'ENTER':
                    _edit_apply()
                    mode = prev_mode
                elif key in ('p', 'P'):
                    # Grab the live audio playhead into the focused start/end bound.
                    _pm, _ps, _pms = _ts_parts(round(play_pos, 3))
                    _bound = _EDIT_START if edit_fi < 3 else _EDIT_END
                    for _fk, _v in zip(_bound, (_pm, _ps, _pms)):
                        edit_fields[_fk] = list(_v)
                    edit_fresh = False
                else:
                    _edit_field_key(key)
                continue

            if mode == TAP:
                if key in ('q', 'CTRL_C'):
                    if not _may_quit(): continue
                    do_stop(); break
                elif key == 's':
                    do_save()
                elif key == 'u':
                    do_undo()
                elif key == 'ESC':
                    mode = SEG; do_stop()
                elif key == 'p':
                    if playing:
                        do_stop()
                    else:
                        do_preview(play_pos)  # resume from where we paused
                elif key in ('SPACE', 'ENTER') and cursor < n:
                    old_start    = segs[cursor].get("start")
                    old_prev_end = segs[cursor - 1].get("end") if cursor > 0 else None
                    corrected    = round(play_pos + tap_offset_s, 3)
                    segs[cursor]["start"] = max(0.0, corrected)
                    if cursor > 0 and segs[cursor - 1].get("end") is None:
                        segs[cursor - 1]["end"] = segs[cursor]["start"]
                    undo_stack.append(('tap', cursor, old_start, old_prev_end))
                    dirty = True
                    if cursor < n - 1:
                        cursor += 1
                    else:
                        mode = SEG; do_stop()  # reached end — done
                elif key == 'LEFT' and cursor > 0:
                    apply_segs([cursor - 1], -0.25)
                elif key == 'RIGHT' and cursor > 0:
                    apply_segs([cursor - 1],  0.25)
                elif key == ',':
                    if cursor > 0: apply_segs([cursor - 1], -0.1)
                elif key == '.':
                    if cursor > 0: apply_segs([cursor - 1],  0.1)
                continue

            if mode == AUDITION:
                if aud_editing:
                    # inline timestamp editor at the bottom of the audition view
                    if key in ('q', 'CTRL_C'):
                        if not _may_quit(): continue
                        aud_editing = False; do_stop(); break
                    elif key == 'ESC':
                        aud_editing = False                 # cancel, keep listening
                    elif key == 'ENTER':
                        _edit_apply(); aud_editing = False
                        _aud_clips('line')                  # hear the line in context
                    else:
                        _edit_field_key(key)
                    continue
                if key in ('q', 'CTRL_C'):
                    if not _may_quit(): continue
                    do_stop(); break
                elif key == 'ESC':
                    mode = SEG; do_stop(); aud_now = None
                elif key == 'UP':
                    cursor = max(0, cursor - 1); _aud_clips('line')
                elif key == 'DOWN':
                    cursor = min(n - 1, cursor + 1); _aud_clips('line')
                elif key in ('SPACE', 'ENTER'):
                    _aud_clips('line')          # play the whole line
                # move the whole line by ear (both timestamps together):
                elif key == 'LEFT':
                    _aud_shift(-_AUD_STEP)
                elif key == 'RIGHT':
                    _aud_shift( _AUD_STEP)
                elif key == ',':
                    _aud_shift(-_AUD_COARSE)
                elif key == '.':
                    _aud_shift( _AUD_COARSE)
                elif key == '[':
                    _aud_clips('start')         # hear the start boundary (no change)
                elif key == ']':
                    _aud_clips('end')           # hear the end boundary (no change)
                elif key == 'u':
                    do_undo(); _aud_clips('line')
                elif key == 'e':
                    # open the inline timestamp editor for this line
                    if segs and cursor < len(segs):
                        do_stop(); aud_now = None
                        prev_mode   = SEG       # audition edits the line (a seg)
                        aud_editing = True
                        _edit_prefill()
                elif key == 'p':
                    if playing: do_stop(); aud_now = None
                    else:       _aud_clips('line')
                continue

            items = segs if mode == SEG else cur_words()
            n_i   = len(items)

            if key in ('q', 'CTRL_C'):
                if not _may_quit(): continue
                if playing: do_stop()
                break
            elif key == '?' and mode == SEG:
                show_hints = not show_hints
            elif key == 's':
                do_save()
            elif key == 'W' and source == SOURCE_TRANSCRIPT:
                _restore_term_attrs(fd, old)
                sys.stdout.write("\033[?1000l\033[?1006l")
                _ans = _prompt_text(
                    f"Write spoken words (Whisper format) to "
                    f"{os.path.basename(aux['jpath'])}? (y/N)")
                _set_raw(fd)
                sys.stdout.write("\033[?1000h\033[?1006h")
                w.anchor_reset()
                if not (_ans or "").strip().lower().startswith("y"):
                    ui_utils.show_status("Commit cancelled — working file untouched.")
                elif do_commit():  # writes transcript.json + refreshes fingerprint;
                    do_save()      # False (with its reason shown) if it couldn't
                    ui_utils.show_status(f"Written to {os.path.basename(aux['jpath'])} + .srt.")
            elif key == 'V' and source == SOURCE_TRANSCRIPT:
                do_verify()
            elif key == 'S' and mode == SEG and source == SOURCE_TRANSCRIPT:
                do_speaker_split()
            elif key == 'u':
                do_undo()
            elif key == 'UP':
                cursor = max(0, cursor - 1)
            elif key == 'DOWN':
                cursor = min(n_i - 1, max(0, cursor + 1))
            elif key == 'SPACE' and mode == SEG:
                selected.symmetric_difference_update({cursor})
            elif key == 'c' and mode == SEG:
                try:
                    from mutagen.id3 import ID3
                    _aud = ID3(mp3_path)
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
                            segs.append({"text": _cl, "kind": "credit"})
                        cursor = len(segs) - 1
                        dirty = True
                        refresh_overlay()  # seg count changed — re-derive before_si
                    else:
                        ui_utils.show_status("No composer or lyricist tags found.")
                except Exception as _ce:
                    ui_utils.show_status(f"Could not read tags: {_ce}")
            elif key == 'a' and mode == SEG:
                _restore_term_attrs(fd, old)
                sys.stdout.write("\033[?1000l\033[?1006l")
                # At position 0 offer inserting before the first item (track intro)
                # whenever it starts after 0:00 — including before an initial stage
                # direction, so you can place silence ahead of it.
                _insert_before = (cursor == 0 and segs
                                  and (segs[0].get("start") or 0) > 0)
                if _insert_before:
                    _where = _prompt_text("Insert before first segment (b) or after cursor (a)?")
                    _insert_before = (_where or "").strip().lower().startswith("b")
                _dur_s = _prompt_text("Dead air duration (seconds):")
                _label = _prompt_text("Stage direction (leave blank for silence):")
                _set_raw(fd)
                sys.stdout.write("\033[?1000h\033[?1006h")
                w.anchor_reset()
                if _dur_s is not None:
                    try:
                        _dur = float(_dur_s.strip())
                        if _dur > 0:
                            if _insert_before:
                                _end   = round(float(segs[0].get("start") or 0), 3)
                                _start = max(0.0, _end - _dur)
                                _air   = _make_dead_air(_start, _end, (_label or "").strip())
                                segs.insert(0, _air)
                                # cursor stays at 0, which is now the new dead air
                            else:
                                _ref   = segs[cursor].get("end") if segs and cursor < len(segs) else None
                                _start = round(float(_ref or 0), 3)
                                _air   = _make_dead_air(_start, _start + _dur, (_label or "").strip())
                                segs.insert(cursor + 1, _air)
                                cursor += 1
                            dirty = True
                            refresh_overlay()
                    except ValueError:
                        ui_utils.show_status("Enter a number of seconds, e.g. 2 or 1.5.")
            elif key == 'd' and mode == SEG:
                if segs and cursor < len(segs) and segs[cursor].get("kind") in ("dead_air", "stage_dir"):
                    undo_stack.append(('delete', cursor, segs[cursor]))
                    segs.pop(cursor)
                    cursor = min(cursor, len(segs) - 1)
                    dirty = True
                    refresh_overlay()
                else:
                    ui_utils.show_status("Only dead air / stage direction segments can be deleted here.")
            elif key == 'l' and mode == SEG:
                if segs and cursor < len(segs) and segs[cursor].get("kind") in ("dead_air", "stage_dir"):
                    _cur_lbl = segs[cursor].get("text", "")
                    _restore_term_attrs(fd, old)
                    sys.stdout.write("\033[?1000l\033[?1006l")
                    _prompt = ("Stage direction text:" if segs[cursor].get("kind") == "stage_dir"
                               else "Stage direction (blank = silence):")
                    _new_lbl = _prompt_text(_prompt, default=_cur_lbl)
                    _set_raw(fd)
                    sys.stdout.write("\033[?1000h\033[?1006h")
                    w.anchor_reset()
                    if _new_lbl is not None:
                        segs[cursor]["text"] = _new_lbl.strip()
                        dirty = True
                        refresh_overlay()  # text changed — re-reconcile the overlay
                else:
                    ui_utils.show_status("Cursor is not on a dead air or stage direction segment.")
            elif key == 'k' and mode == SEG:
                do_recategorise(cursor)
            elif key == 'R' and mode == SEG:
                _review_enter('issues')
            elif key == 'D' and mode == SEG:
                _review_enter('dirs')
            elif key == 'L' and mode == SEG:
                _review_enter('long')
            elif key == '/' and mode == SEG:
                do_smart_split()
            elif key == 'x' and mode == SEG:
                # Cycle the stage-direction kind: inline → tone → external.
                _sd = segs[cursor] if (segs and cursor < len(segs)) else None
                if not (_sd and _sd.get('kind') == 'stage_dir'):
                    ui_utils.show_status("Cursor is not on a stage direction (✦).")
                else:
                    _next = _SD_SCOPES[(_SD_SCOPES.index(_sd_scope(_sd)) + 1) % len(_SD_SCOPES)]
                    undo_stack.append(('snapshot', list(segs)))
                    segs[cursor] = {**_sd, 'scope': _next}
                    dirty = True
                    refresh_overlay()
                    ui_utils.show_status(f"Direction: {_next}")
            elif key == 'r' and mode == SEG:
                # Scan gaps between timed segments; also time any untimed stage_dir segs.
                _restore_term_attrs(fd, old)
                sys.stdout.write("\033[?1000l\033[?1006l")
                _inserted = 0; _timed = 0
                _i = 0
                _new_segs: list[dict] = []
                while _i < len(segs):
                    _cur = segs[_i]
                    # Auto-time an untimed stage_dir that sits between two timed segs
                    if (_cur.get("kind") == "stage_dir" and _cur.get("start") is None):
                        _prev_t = _new_segs[-1].get("end") if _new_segs else None
                        _next_t = next((segs[j].get("start") for j in range(_i + 1, len(segs))
                                        if segs[j].get("start") is not None), None)
                        if _prev_t is not None and _next_t is not None:
                            _cur = dict(_cur)
                            _cur["start"] = round(float(_prev_t), 3)
                            _cur["end"]   = round(float(_next_t), 3)
                            _timed += 1
                    _new_segs.append(_cur)
                    _nxt = segs[_i + 1] if _i + 1 < len(segs) else None
                    _skip_kinds = ("dead_air", "stage_dir")
                    if (_nxt is not None
                            and _cur.get("end") is not None
                            and _nxt.get("start") is not None
                            and _nxt["start"] - _cur["end"] >= _AIR_GAP_THRESHOLD
                            and _nxt.get("kind") not in _skip_kinds
                            and _cur.get("kind") not in _skip_kinds):
                        _gap_start = float(_cur["end"])
                        _gap_end   = float(_nxt["start"])
                        _gap       = round(_gap_end - _gap_start, 3)
                        if mp is not None:
                            _play_from = max(0.0, _gap_start - 0.3)
                            mp.set_time(int(_play_from * 1000))
                            if not mp.is_playing():
                                mp.play(); time.sleep(0.15)
                        _lbl = _prompt_text(
                            f"Gap of {_gap}s — stage direction? (blank = silence, skip = ignore):")
                        if mp is not None and mp.is_playing():
                            mp.pause()
                        if _lbl is not None and _lbl.strip().lower() != 'skip':
                            _new_segs.append(_make_dead_air(_gap_start, _gap_end, _lbl.strip()))
                            _inserted += 1
                    _i += 1
                segs[:] = _new_segs
                if _inserted:
                    refresh_overlay()
                _set_raw(fd)
                sys.stdout.write("\033[?1000h\033[?1006h")
                w.anchor_reset()
                if _inserted or _timed:
                    dirty = True
                    _msg = []
                    if _inserted: _msg.append(f"{_inserted} dead air added")
                    if _timed:    _msg.append(f"{_timed} stage dir timed")
                    ui_utils.show_status(", ".join(_msg))
                else:
                    ui_utils.show_status("No gaps above threshold found.")
            elif key == 'm' and mode == SEG:
                if md_overlay is not None:
                    md_overlay = md_quality = md_path = None
                    viewport   = 0
                    ui_utils.show_status("Transcript overlay removed.")
                else:
                    from src.lyrics.lyrics import _find_markdown_for_audio
                    _md_path = _find_markdown_for_audio(mp3_path)
                    if _md_path is None:
                        ui_utils.show_status("No transcript.md found next to this file.")
                    else:
                        try:
                            md_overlay, md_quality, _links = _build_md_overlay(segs, _md_path)
                            for _si, _lid in _links.items():   # record durable alignment
                                if _lid is not None:
                                    segs[_si]['line_ref'] = _lid
                            md_path    = _md_path
                            viewport   = 0
                            ui_utils.show_status(
                                f"Overlay: {len(md_overlay)} annotations from {os.path.basename(_md_path)}"
                            )
                        except Exception as _exc:
                            ui_utils.show_status(f"MD overlay failed: {_exc}")
            elif key == 'M' and mode == SEG:
                # Materialize the overlay's stage directions as real (untimed)
                # segs so they can be timed and saved.  Speakers are display-only
                # and are never committed — they stay derived from the overlay, so
                # there is only ever one source for a speaker header and nothing
                # can duplicate.  After inserting, refresh_overlay() re-derives:
                # the new stage_dir segs are now "materialized" and drop out of the
                # overlay, making a second M a no-op (idempotent).
                if not md_overlay:
                    ui_utils.show_status("No stage directions to commit — press m first.")
                else:
                    _sdir_items = [ov for ov in md_overlay if ov['kind'] == 'stage_dir']
                    if not _sdir_items:
                        ui_utils.show_status("No stage directions in the overlay to commit.")
                    else:
                        undo_stack.append(('snapshot', list(segs)))
                        # md_overlay is in ascending (before_si, order); inserting in
                        # reverse keeps earlier indices valid and preserves the order
                        # of directions that share a before_si.
                        for _ov in reversed(_sdir_items):
                            segs.insert(_ov['before_si'], _make_stage_dir(_ov['text']))
                        _committed = len(_sdir_items)
                        refresh_overlay()
                        viewport = 0
                        dirty    = True
                        ui_utils.show_status(
                            f"Committed {_committed} stage direction{'s' if _committed != 1 else ''}.")
            elif key == 't' and mode == SEG:
                if _HAS_VLC:
                    mode = TAP
                    start_s = segs[cursor].get("start") or play_pos
                    do_preview(start_s)
            elif key == 'b' and mode == SEG:
                if _HAS_VLC and mp is not None and segs:
                    mode = AUDITION
                    _aud_clips('line')   # landing on a line plays it whole
                elif not _HAS_VLC:
                    ui_utils.show_status("Audition needs VLC (not available).")
            elif key == 'w' and mode == SEG and source == SOURCE_TRANSCRIPT:
                if segs and cursor < len(segs) and segs[cursor].get("words"):
                    seg_cursor = cursor
                    mode = WORD; cursor = 0; viewport = 0
            elif key == 'ESC' and mode == WORD:
                mode = SEG; cursor = seg_cursor; viewport = max(0, seg_cursor - 2)
            elif key == 'e':
                if mode == SEG:
                    item = segs[cursor] if (segs and cursor < len(segs)) else None
                else:
                    words = cur_words()
                    item  = words[cursor] if cursor < len(words) else None
                if item is not None:
                    prev_mode = mode
                    mode      = EDIT
                    _edit_prefill()
            elif key == 'p':
                if playing:
                    do_stop()
                elif mode == WORD:
                    words = cur_words()
                    if cursor < len(words):
                        ww = words[cursor]
                        do_preview(ww.get("start") or 0.0)
                elif segs and cursor < len(segs):
                    do_preview(segs[cursor].get("start") or 0.0)
            elif key == 'J' and mode == SEG:
                if segs and cursor > 0:
                    undo_stack.append(('snapshot', list(segs)))
                    segs[cursor], segs[cursor - 1] = segs[cursor - 1], segs[cursor]
                    cursor -= 1
                    dirty = True
                    refresh_overlay()
            elif key == 'K' and mode == SEG:
                if segs and cursor < len(segs) - 1:
                    undo_stack.append(('snapshot', list(segs)))
                    segs[cursor], segs[cursor + 1] = segs[cursor + 1], segs[cursor]
                    cursor += 1
                    dirty = True
                    refresh_overlay()
            elif key == 'j' and mode == SEG:
                if segs and cursor < len(segs) - 1:
                    seg_a  = segs[cursor]
                    seg_b  = segs[cursor + 1]
                    if seg_a.get("kind") in ("dead_air", "stage_dir") or \
                       seg_b.get("kind") in ("dead_air", "stage_dir"):
                        ui_utils.show_status("Cannot join dead air or stage direction segments.")
                        continue
                    merged = {
                        "start": seg_a.get("start"),
                        "end":   seg_b.get("end"),
                        "text":  (seg_a.get("text", "").strip() + " " +
                                  seg_b.get("text", "").strip()).strip(),
                        "words": seg_a.get("words", []) + seg_b.get("words", []),
                    }
                    if seg_a.get("line_ref"):   # keep the pinned MD line through the join
                        merged["line_ref"] = seg_a["line_ref"]
                    undo_stack.append(('join', cursor, seg_a, seg_b))
                    segs[cursor:cursor + 2] = [merged]
                    dirty = True
                    refresh_overlay()
            elif key == 'x' and mode == WORD:
                words = cur_words()
                if 0 < cursor < len(words):
                    seg = segs[seg_cursor]
                    w_a, w_b = words[:cursor], words[cursor:]
                    boundary = (w_b[0].get("start") or w_a[-1].get("end")
                                or round((seg.get("start", 0) + seg.get("end", 0)) / 2, 3))
                    seg_a = {"start": seg.get("start"), "end": round(boundary, 3),
                             "text": " ".join(ww["word"] for ww in w_a), "words": w_a}
                    seg_b = {"start": round(boundary, 3), "end": seg.get("end"),
                             "text": " ".join(ww["word"] for ww in w_b), "words": w_b}
                    if seg.get("line_ref"):    # both halves stay on the split line's MD line
                        seg_a["line_ref"] = seg_b["line_ref"] = seg["line_ref"]
                    undo_stack.append(('split', seg_cursor, seg))
                    segs[seg_cursor:seg_cursor + 1] = [seg_a, seg_b]
                    dirty = True; mode = SEG; cursor = seg_cursor + 1
                    viewport = max(0, cursor - 2)
                    refresh_overlay()
            else:
                if mode == SEG:
                    tgts = sorted(selected) if selected else [cursor]
                    key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                    if key in key_deltas:
                        if segs[cursor].get("start") is None:
                            ui_utils.show_status("No timestamp set — press e to enter one.")
                        else:
                            apply_segs(tgts, key_deltas[key])
                else:
                    key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                    if key in key_deltas:
                        apply_word(seg_cursor, cursor, key_deltas[key])

    finally:
        if mp:
            try: mp.stop()
            except Exception: pass
        sys.stdout.write("\033[?1000l\033[?1006l")  # disable mouse
        sys.stdout.flush()
        _restore_term_attrs(fd, old)
        w.clear()
