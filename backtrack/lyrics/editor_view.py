"""The lyrics editor's screen: its modes, and how the list, the word view, the
audition strip and the inline editors are drawn. Rendering only, with no input
and no state of its own (it also holds the optional vlc import the editor uses)."""
from __future__ import annotations
from backbone import ui
from backbone.ui import Colors as C
from backbone.prompt.core import _cols
from backbone.prompt.core import _visible_rows
from backbone import prompt as _promptmod
from backtrack.lyrics.formats import _apply_markdown_formatting
from backtrack.lyrics.md_overlay import _sd_scope, _is_framed
from backtrack.lyrics.time_fields import _render_edit_fields
from backtrack.lyrics.sync_doc import SOURCE_TRANSCRIPT
from backbone import timefmt


# Audio is optional: without python-vlc the editor still edits, it just can't play.
_vlc = None
try:
    import vlc as _vlc  # type: ignore[import-untyped]  # noqa: F401  (used by the editor)
    _HAS_VLC = True
except ImportError:
    _HAS_VLC = False

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
    truncation never lands mid-escape and never leaves colour bleeding.  So no
    rendered line can wrap.
    """
    if width <= 0:
        return ""
    if ui.visual_len(s) <= width:
        return s
    budget = max(0, width - ui.visual_len(ell))
    return ui.clip_ansi(s, budget, reset=False) + ell + C.RESET


def _fit_body_footer(out: list, footer: list, avail: int) -> list:
    """Assemble the scrolling body + the pinned hint footer into at most `avail`
    rows, footer flush to the bottom. The footer (hints) always survives (the
    body is trimmed to make room), so hints never overflow past the now-playing box
    or off the bottom of the screen."""
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
    while that row survives `_fit_body_footer`: the body is trimmed from the end,
    so surviving rows keep their index, but a very short terminal drops the bar
    entirely and clicks there must not seek."""
    if geo is None:
        return None
    return geo if geo[0] < max(0, avail - _footer_rows(footer, avail)) else None


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
    # helper the list menus use (it reserves the status bar, the now-playing box
    # and the vertical margins), so the editor never paints under the player.
    ui.footer_lines(ui.get_terminal_width())
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
    indent = " " * ui.MARGIN_H

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
    pos_str    = f"{review[1]} / {review[2]}" if review else (f"{cursor + 1} / {n}" if n else "-")
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
    track_txt    = ui.truncate_text(track_name + ctx_plain, track_budget)
    left_disp    = f"{C.BOLD}{label}{C.RESET}  {C.DIM}{track_txt}{C.RESET}"
    left_w       = len(label) + 2 + len(track_txt)
    gap          = max(1, B - left_w - right_w)

    # Header starts on the first line; _Widget.render adds the MARGIN_V top row,
    # matching the rest of the app (no extra leading blank here).
    term_w = ui.get_terminal_width()
    out: list[str] = [
        indent + left_disp + " " * gap + right_disp,
        f"{C.DIM}{_dot_rule(term_w)}{C.RESET}",
    ]

    sep = f"{C.DIM}{_dot_rule(term_w)}{C.RESET}"

    if mode == TAP:
        return _draw_tap(B, avail, cursor, hit_map, indent, mode, n, out, play_pos, playing, segs, sep, total_s, undo_depth, vp)

    if mode == AUDITION:
        return _draw_audition(B, aud_editing, aud_now, avail, cursor, edit, hit_map, indent, mode, n, out, play_pos, playing, segs, sep, total_s, undo_depth, vp)

    # Build footer first so its actual height governs how many items we show.
    footer = [sep]

    if mode == EDIT:
        _edit_footer(cursor, edit, footer, md_overlay, mode, review, segs, selected, show_hints, source, undo_depth)

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
            pairs += [('?', 'hide hints'), ('esc', 'back'), ('q', 'quit')]
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
            pairs += [('esc', 'back'), ('q', 'quit')]
            # (W = write to transcript.json, shown in full hints via ?)
        if selected: pairs.append(('', f'{len(selected)} marked'))
        footer.extend(_promptmod.chrome_hint_lines(pairs))

    HEADER    = 2   # title + rule (no leading blank; render adds the MARGIN_V top row)
    available = avail - HEADER - 2 - len(footer)
    vis       = max(1, available)

    # Compact grid (visible widths after the 2-col indent):
    #   ptr(1) sp(1) chk(1) sp(1)  start(9)  sp(2)  →  TEXT column (offset 15)
    # The words begin at ~col 15 so the transcript reads like a
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
        t      = ui.truncate_text(text_raw, budget)
        if fmt:
            # Emphasis reads as type style: *word* → italic, **word** → bold.
            # Every span restores `base` (the row's own colour), so the highlight
            # continues past the emphasis instead of the span's reset blanking the
            # rest of the line.
            t_disp = _apply_markdown_formatting(
                t, base=text_color, strong=C.BOLD, em=C.ITALIC)
            # A music note is accent on the current row (text_color set) and dim
            # elsewhere: accent is reserved for the current line.  Either way it
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
            # Pad off the VISIBLE width of what we actually print: markdown
            # formatting strips the * markers, so len(t) over-counts and the
            # gutter (and the duration pinned to its right edge) would drift left.
            vis = ui.visual_len(t_disp)
            pad = max(1, B - PREFIX_W - vis - rhs_w)
            return indent + prefix_disp + t_disp + " " * pad + rhs_disp
        return indent + prefix_disp + t_disp

    if show_words:
        vp = _draw_words(_compose, _rhs, cursor, hit_map, indent, out, seg_cursor, segs, vis, vp)

    else:  # SEG: build flat display_items interleaving segs with MD overlay
        vp = _draw_lines(B, HEADER, PREFIX_W, _compose, _rhs, avail, cursor, footer, hit_map, indent, md_overlay, md_quality, out, segs, selected, vp)

    _cap = ui.get_terminal_width()   # allow full-width rules; content self-limits to the margin
    return ([_clip(ln, _cap) for ln in _fit_body_footer(out, footer, avail)],
            vp, hit_map, _footer_rows(footer, avail), None)


def _draw_tap(B, avail, cursor, hit_map, indent, mode, n, out, play_pos, playing, segs, sep, total_s, undo_depth, vp):
    """TAP mode: the lines around the one being stamped, the clock and the tap keys."""
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
        txt  = ui.truncate_text(seg["text"].strip(), budget)
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
        bar = ui.get_progress_bar(pct, max(4, B - len(_clock) - 4))  # -4: bar's own [ ] + 2-space gap
        out.append(f"{indent}{C.DIM}{bar}  {_clock}{C.RESET}")
    else:
        out.append("")

    _cap = ui.get_terminal_width()   # allow full-width rules; content self-limits to the margin
    return ([_clip(ln, _cap) for ln in _fit_body_footer(out, footer, avail)],
            vp, hit_map, _footer_rows(footer, avail), None)


def _draw_audition(B, aud_editing, aud_now, avail, cursor, edit, hit_map, indent, mode, n, out, play_pos, playing, segs, sep, total_s, undo_depth, vp):
    """AUDITION: the line being auditioned, its boundaries and clip strip, the
    progress bar, and the inline timestamp editor when it's open."""
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
        txt = ui.truncate_text(seg.get("text", "").strip() or "(…)", max(4, B - 6))
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
        bar = ui.get_progress_bar(pct, _bar_w)
        out.append(f"{indent}{C.DIM}{bar}  {_clock}{C.RESET}")
        # Clickable: the bar's first cell sits one column past its '[' cap.
        prog_geo = (len(out) - 1, ui.MARGIN_H + 2, _bar_w)
    else:
        out.append("")

    _cap = ui.get_terminal_width()   # allow full-width rules; content self-limits to the margin
    return ([_clip(ln, _cap) for ln in _fit_body_footer(out, footer, avail)],
            vp, hit_map, _footer_rows(footer, avail),
            _prog_geo_if_visible(prog_geo, footer, avail))


def _edit_footer(cursor, edit, footer, md_overlay, mode, review, segs, selected, show_hints, source, undo_depth):
    """The timestamp editor and its hints, added to the footer while EDIT is open."""
    s_d, e_d = _render_edit_fields(edit)
    footer.append(f"  {C.ACCENT}✎{C.RESET}  start  {s_d}    end  {e_d}")
    footer.extend(_promptmod.chrome_hint_lines(
        [('tab/⇧tab', 'field'), ('←→', 'cursor'), ('↑↓', 'adjust'),
         ('p', 'playhead'), ('↵', 'apply'), ('esc', 'cancel')]))


def _draw_words(_compose, _rhs, cursor, hit_map, indent, out, seg_cursor, segs, vis, vp):
    """WORD mode body: the open line's words, each with its timing. Returns the viewport."""
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
    return vp


def _draw_lines(B, HEADER, PREFIX_W, _compose, _rhs, avail, cursor, footer, hit_map, indent, md_overlay, md_quality, out, segs, selected, vp):
    """SEG mode body: the lines, with any markdown overlay (speakers, stage
    directions, framed sections) woven in. Returns the viewport."""
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

    # Items aren't all one row: an external (framed) direction draws up to 3.
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
                # Speaker banner: plain white name (no colour), then a dim rule.
                name  = ov['text']
                stg   = ov.get('stage', '')
                head  = f"{C.RESET}{name}"
                hplain = name
                if stg:
                    head  += f" {C.DIM}({stg}){C.RESET}"
                    hplain += f" ({stg})"
                # Dotted rule runs to the right edge of the body (where the
                # duration column ends on the rows below), not the full window.
                rule = '┈' * max(1, B - len(hplain) - 1)
                out.append(f"{indent}{head} {C.DIM}{rule}{C.RESET}")
            else:
                # Isolated stage direction (uncommitted overlay): left-aligned
                # with a single ✦ and italic text, blank-fenced above and below
                # so it still reads as its own beat.  Its ✦ sits in the same
                # column as a committed cue's, so floating and committed cues line up.
                _sd = ui.truncate_text(f"({ov['text'].strip()})",
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
                    # A *timed* stage direction is a placed beat; the italic
                    # label sets it apart from plain silence (◌) and from an
                    # untimed cue still awaiting its moment.
                    lbl_c = f"{C.PRIMARY if is_cur else C.DIM}{C.ITALIC}"
                else:
                    lbl_c = C.PRIMARY if is_cur else C.DIM
                # inline / tone sit indented under the words; external is a
                # framed section (below).
                sd_pad  = '   ' if (is_sdir and scope in ('inline', 'tone')) else ''
                budget  = max(1, B - PREFIX_W - 2 - len(sd_pad) - (rhs_w + 2 if rhs_w else 0))
                disp    = ui.truncate_text(disp0, budget)
                body    = (f"{prefix}{sd_pad}{sym_c}{sym}{C.RESET} "
                           f"{lbl_c}{disp}{C.RESET}")
                if rhs_w:
                    pad  = max(1, B - PREFIX_W - 2 - len(sd_pad) - len(disp) - rhs_w)
                    body += " " * pad + rhs_disp
                if is_air or (is_sdir and scope == 'external'):
                    # Framed section (dead air / external direction, belongs to
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
    return vp
