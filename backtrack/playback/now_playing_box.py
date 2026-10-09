"""The background-audio now-playing box drawn above the status bar on every
menu screen: transport glyphs, the track, and the clock over a progress border."""
from __future__ import annotations
from backtrack.music_library import chapter_at, chapter_label, format_tag_values, library_entry
from backbone import ui
from backbone.ui import Colors as C


def _clip_ansi_to_width(text: str, max_cols: int) -> str:
    """Truncate text to max_cols visible columns, preserving embedded ANSI escapes.

    The player composes a clipped line into a wider row (borders, padding), so
    it asks for no trailing reset: the caller closes its own styling.
    """
    return ui.clip_ansi(text, max_cols, reset=False)


def _fit_segments(segs: list, budget: int) -> tuple[str, int]:
    """Style-render (text, style) segments to fit ``budget`` plain **columns**,
    truncating the tail with an ellipsis. Returns (styled_string, plain_width).

    Widths are counted in columns, not codepoints: the transport glyphs carry a
    text-presentation selector that occupies no column of its own, so counting
    codepoints would over-measure them and shrink the title to compensate.
    Truncation walks the string a column at a time for the same reason: slicing
    by index could sever a glyph from the selector that decides its width.
    """
    out = ""
    used = 0
    for text, style in segs:
        if used >= budget:
            break
        remain = budget - used
        if ui.visual_len(text) > remain:
            text = _clip_to_cols(text, max(0, remain - 1)) + "…"
        out += (f"{style}{text}{C.RESET}" if style else text)
        used += ui.visual_len(text)
    return out, used


def _clip_to_cols(text: str, cols: int) -> str:
    """First `cols` display columns of `text`, keeping each glyph's zero-width
    presentation selector attached to it."""
    if cols <= 0:
        return ""
    out = []
    used = 0
    for ch in text:
        w = ui.char_cols(ch)
        if w == 0:                           # selector: rides with the previous glyph
            out.append(ch)
            continue
        if used + w > cols:                  # a 2-cell glyph needs 2 cells free
            break
        out.append(ch)
        used += w
    return "".join(out)


_chapters_of: dict = {}           # the playing file's chapters, looked up once per file


def _chapters(path: str) -> list:
    if path not in _chapters_of:
        _chapters_of.clear()
        _chapters_of[path] = library_entry(path).get('chapters') or []
    return _chapters_of[path]


def format_now_playing_bar(width: int) -> list[str] | None:
    """The background-audio now-playing box (#14), styling only (no colour): a
    rounded box whose bottom border doubles as the progress bar. Row 1 = top
    border; row 2 = ``⏸ ⏭  Title · Artist · Album … m:ss / m:ss`` (bold
    title, dim rest); row 3 = the progress border (heavy ``━`` elapsed / light
    ``─`` remaining). The transport keys are advertised in the hint bar with
    every other key, not on the border.

    Returns a list of styled rows, or None when nothing plays or the terminal is
    too narrow for a box."""
    from backtrack.playback.session import current_now_playing
    np = current_now_playing()
    ui.set_footer_time_cols(None)
    if np is None:
        ui.set_footer_signature(None)
        ui.set_footer_unboxed(False)
        return None
    # When the full player view is open in ANY window of the session, the player
    # itself is the now-playing display, so hide the ambient bar everywhere else so
    # it doesn't double up (#14). view_holder is the token of whichever window
    # holds the view (broadcast to joined windows), or None when no view is open.
    if np.get('view_holder'):
        ui.set_footer_signature(None)
        ui.set_footer_unboxed(False)
        return None
    # Identity of this track for the idle-tick redraw: a change here forces the
    # now-playing box to repaint even if the styled rows happen to match (#14).
    ui.set_footer_signature((
        np.get('file_path'), np.get('generation'), np.get('paused'),
        np.get('index'), np.get('count'), np.get('view_holder'),
    ))
    mh = 2
    box_w = width - 2 * mh
    inner = box_w - 4                       # content columns between "│ " and " │"

    # Play/pause then next. The glyph shows the action the key would take: ⏵
    # while paused, ⏸ while playing. Widths come from ui.char_cols, which
    # deliberately over-estimates these: no monospace font carries them, so the
    # terminal draws them from a fallback whose advance width this process
    # cannot know, and over-estimating is the direction that fails safely.
    pp_icon = '⏵' if np['paused'] else '⏸'
    icon = f"{pp_icon}  ⏭  "                  # the glyphs at ui.FOOTER_GLYPH_COLS
    # Just the elapsed/total time; volume + queue position live elsewhere (the
    # player view, and the queue pane inside it).
    # The player's own formatter (int: whole seconds), so an hour-long track
    # reads 1:00:00 here as it does in the player, not 60:00.
    # A file with chapters shows the chapter's title, and its time or the
    # file's (the player's setting; a click on the clock swaps it).
    from backtrack.playback.player_ui import shown_time
    chapters = _chapters(np.get('file_path') or '')
    at, length, span = shown_time(max(0, np['elapsed']), max(0, np['duration']), chapters)
    right = f"{ui.format_time(int(at))} / {ui.format_time(int(length))}"

    # How narrow the box may get, in terms of what it is actually being asked to
    # hold. A fixed threshold can't know: `right` grows with the track (an hour-
    # long file spends three more columns on the clock), and if the left side is
    # squeezed past its floor the gap below bottoms out at 1 and the content row
    # runs *wider than its own border*, a visibly ragged box. Two tiers:
    # glyphs + a readable stub of title, or no box at all.
    _MIN_TITLE = 6
    if inner < ui.visual_len(icon) + _MIN_TITLE + 2 + len(right):
        # No box: the hint bar advertises the transport keys instead, so they
        # are never both unadvertised and live.
        ui.set_footer_unboxed(True)
        return None

    ui.set_footer_unboxed(False)

    title = chapter_label(chapters, chapter_at(chapters, np['elapsed']))[0] if chapters else np['title']
    left_segs: list = [(icon, C.BOLD), (title or '?', C.BOLD)]
    if np['artist']:
        left_segs += [("  ·  ", C.DIM), (format_tag_values(np['artist']), C.DIM)]
    if np['album']:
        left_segs += [("  ·  ", C.DIM), (np['album'], C.DIM + C.ITALIC)]

    left_budget = max(6, inner - len(right) - 2)
    left_styled, _left_used = _fit_segments(left_segs, left_budget)

    pad = ' ' * mh
    top = f"{pad}{C.DIM}╭{'─' * (box_w - 2)}╮{C.RESET}"

    # The clock and the closing border are placed by absolute column (CSI G)
    # rather than by counting what precedes them. Everything left of the clock
    # contains transport glyphs whose real width is a fallback font's business,
    # not this process's, so a row built purely by counting puts its right-hand
    # border wherever that guess happened to land. Pinning both to the columns
    # they belong in makes the box square whatever the glyphs turn out to be;
    # the only thing that varies is the size of the gap before the clock.
    pipe_col = mh + box_w                    # 1-based column of the closing │
    time_col = pipe_col - 1 - len(right)
    if chapters:
        ui.set_footer_time_cols((time_col, time_col + len(right) - 1))
    mid = (f"{pad}{C.DIM}│{C.RESET} {left_styled}"
           f"\033[{time_col}G{C.DIM}{right}{C.RESET}"
           f"\033[{pipe_col}G{C.DIM}│{C.RESET}")

    # Progress along the bottom border: heavy ━ for the elapsed fraction, light ─
    # for the rest (the join to the rounded corners is intentionally light), and
    # the chapter playing picked out as in the player.
    pct = (np['elapsed'] / np['duration']) if np['duration'] else 0.0
    prog = ui.progress_cells(pct, box_w - 2, span)
    bot = f"{pad}{C.DIM}╰{C.RESET}{prog}{C.DIM}╯{C.RESET}"

    return [_clip_ansi_to_width(ln, width) for ln in (top, mid, bot)]
