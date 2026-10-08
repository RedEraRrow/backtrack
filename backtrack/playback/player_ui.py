"""The player screen: layout, metadata and credits, transport controls, volume bar,
and the diffed frame painter."""
from __future__ import annotations
import re
import time
import sys

from backtrack.music_library import (chapter_at, chapter_label,
                                    format_value_list, track_title, first_text)
from backbone.prompt import core as pc
from backbone import nav
from backbone import keys, ui
from backtrack.playback.player_geom import geom
from backbone import numbering
from backbone.prompt.core import _hint
from backbone.prompt.core import add_hint_click_cells
from backbone.ui import Colors as C
from backtrack import tuning as tune
from backbone.log import log
from backtrack.playback import queue_pane
from backtrack.playback.queue_pane import (
    queue_title,  # noqa: F401 (re-exported)
    _place_queue, _queue_click_rows, has_queue, move_queue_cursor, queue_click_index, queue_cursor,
    set_chapter_context, set_queue_context, set_queue_cursor, use_list,
)
from backtrack.playback.player_art import (  # noqa: F401 (re-exported)
    ART_MAX_WIDTH, _art_width_for_height, _draw_inline_art, _inline_art, art_image_incomplete, inline_art_enabled, redraw_art_image, set_resizing,
    square_art,
)
from backtrack.config import setting
from backtrack.music_library import year_of


# The player's keys, shared by the host player and a joined window's view.
keys.define("player", "Player", [
    ("playpause", ("SPACE", "p", "P"), "play / pause"),
    ("back_5", ("LEFT",), "back 5s"),
    ("fwd_5", ("RIGHT",), "forward 5s"),
    ("back_1", ("j",), "back 1s"),
    ("fwd_1", ("l",), "forward 1s"),
    ("back_30", (",",), "back 30s"),
    ("fwd_30", (".",), "forward 30s"),
    ("near_end", ("E",), "jump to near the end (Diagnostics)"),
    ("edit", ("e",), "edit this track's tags"),
    ("album", ("a",), "this track's album, in Browse"),
    ("artist", ("A",), "this track's artist, in Browse"),
    ("prev", ("[",), "previous chapter or track"),
    ("next", ("]",), "next chapter or track"),
    ("slower", ("<",), "slower (audiobooks)"),
    ("faster", (">",), "faster (audiobooks)"),
    ("sleep", ("z", "Z"), "sleep timer"),
    ("chapter_time", ("t", "T"), "chapter or whole-file time"),
    ("vol_up", ("+", "="), "volume up"),
    ("vol_down", ("-", "_"), "volume down"),
    ("meta", ("m", "M"), "show or hide the details"),
    ("tabs", ("f", "F"), "show or hide the tab bar"),
    ("resume", ("r", "R"), "resume the last run's queue (nothing playing)"),
    ("panel", ("w", "W"), "choose the panels (and arrange them)"),
    ("back", ("b", "B", "ESC"), "back, keep playing"),
    ("stop", ("s", "S"), "stop"),
    ("quit", ("q", "Q"), "quit the app"),
])
# While the queue panel is showing (w): a cursor through the queue, and edits at it.
keys.define("player_queue", "Player: queue panel", [
    ("up", ("UP",), "cursor up the queue"),
    ("down", ("DOWN",), "cursor down the queue"),
    ("play", ("ENTER",), "play the track at the cursor"),
    ("move_up", ("J",), "move it up"),
    ("move_down", ("K",), "move it down"),
    ("remove", ("d", "DELETE", "BACKSPACE"), "remove it"),
    ("shuffle", ("x",), "shuffle what's coming"),
    ("clear", ("c",), "clear what's coming"),
    ("undo", ("u",), "undo the last queue change"),
], within=("player", "global"))


# Absolute cursor positioning (\033[<row>;<col>H): must require the trailing
# 'H' so it does NOT also match a 24-bit colour prefix like \033[38;2;r;g;bm,
# which every half-block art line starts with. Matching those made the renderer
# treat art lines as absolute (row-less), so metadata flowed to the top and drew
# ABOVE the art in the single-column layout.
_ABS_ROW_RE = re.compile(r'^\033\[\d+;\d+H')

_WIDE_SPLIT_GUTTER = 3
# Share of the width left blank before the lyrics in the standard layout, so
# they float under the controls rather than hug the edge.
_LYRICS_INSET_FRAC = 0.2
_PANEL_SIDE_MIN = 36            # the panels go beside the player when they'd get this many columns there
_PLAYER_SHARE = 0.5             # beside the panels, the player's box takes at most this share
_PLAYER_MIN_W = 28              # its inside with no art (no room for it)
_BOX_MIN_BODY = 3               # rows over the transport box a box needs (its borders and a line)
_BESIDE_TEXT = 24               # the details beside the art (a short, wide window) keep this many columns
_BESIDE_GAIN = 1.3              # the art goes beside the details only when that makes it this much bigger
_PANEL_MIN_ROWS = 8             # under the player, the lyrics or the queue keep at least this

# The boxed player's frame (_draw_boxed_ui): whether the last frame was boxed,
# its boxes (top, bottom, left, right; screen rows and columns) and their
# titles, the transport row (`prog_row`: the controls, the progress bar and
# the times, drawn by update_progress_ui and write_controls, not the frame),
# the column its controls start at, and the bar's (first column, width).
_frame: dict = {'boxed': False, 'boxes': [], 'titles': {}, 'prog_row': None,
                'icons_col': None, 'bar': None}


def _frame_borders(only: int | None = None) -> dict:
    """The boxed player's boxes as overlays per row: each box's top border
    (with its title) and bottom border, and its sides on the rows between.
    The transport row is left out unless asked for (`only`): update_progress_ui
    draws its own."""
    if not _frame['boxed']:
        return {}
    out: dict = {}
    for box in _frame['boxes']:
        r0, r1, c0, c1 = box
        title = _frame['titles'].get(box, "")
        edge = C.ACCENT2 if box == _frame.get('focus') else C.DIM       # the panel being arranged
        for r in range(r0, r1 + 1):
            if (only is not None and r != only) or (only is None and r == _frame['prog_row']):
                continue
            if r == r0:
                name = f" {ui.truncate_text(title, max(1, c1 - c0 - 6))} " if title else ""
                cell = (f"\033[{r};{c0}H{edge}╭─{C.RESET}{C.PRIMARY}{C.BOLD}{name}{C.RESET}"
                        f"{edge}{'─' * max(0, c1 - c0 - 2 - ui.visual_len(name))}╮{C.RESET}")
            elif r == r1:
                cell = f"\033[{r};{c0}H{edge}╰{'─' * (c1 - c0 - 1)}╯{C.RESET}"
            else:
                cell = f"\033[{r};{c0}H{edge}│{C.RESET}\033[{r};{c1}H{edge}│{C.RESET}"
            out.setdefault(r, []).append(cell)
    return out


def write_controls(row: int, status: str) -> None:
    """Redraw the transport controls in place: in the boxed player, at their
    own place on the transport row (the progress bar shares it); else the
    controls row, from its first column."""
    if _frame['boxed'] and _frame['bar']:
        text = status.strip()
        gap = max(0, _frame['bar'][0] - _frame['icons_col'] - ui.visual_len(text))
        # From the inset after the box's side, which no other paint covers.
        sys.stdout.write(pc.screen_span_paint(row, _frame['icons_col'] - 1, ' ' + text + ' ' * gap))
    else:
        width = ui.get_terminal_width()
        sys.stdout.write(pc.screen_span_paint(row, 1, ui.clip_ansi(status, width)
                                              + ' ' * max(0, width - ui.visual_len(status))))


_player_prev_rows = [0]          # flow-row count of the previous player frame
_player_overlay_rows = [set()]   # rows the previous frame overlaid


def _render_frame_buffer(buf: list, rows: int) -> None:
    """Flush the assembled frame, writing only the rows whose content changed.

    `buf[0]` is a clear sequence, deliberately **dropped**: erasing the whole
    screen every frame makes the player flicker on every progress tick, and with a
    diffed paint it isn't needed: rows the frame stops using are blanked
    explicitly, and `ui.clear_screen()` (entry, resize, exit) already drops
    the painter's model so the next frame repaints in full.

    `row` counts only flow (relative) lines; absolute-positioned items (the
    volume bar, controls, lyrics) pass through WITHOUT consuming a row slot;
    otherwise everything after them (e.g. the metadata) is pushed off-place.
    Their rows are forgotten, so a later flow paint of the same row still lands.
    """
    flow: dict[int, str] = {}
    overlay: dict[int, list[str]] = {}
    row = 0
    for item in buf[1:]:
        m = _ABS_ROW_RE.match(item)
        if m:
            # An overlay: it writes a few columns of a row the art (or another
            # flow line) also occupies: the volume bar sits to the right of the
            # art, on the art's own rows. It is layered *over* that row rather
            # than replacing it; dropping the row's flow content blanked the art.
            overlay.setdefault(int(m.group(0)[2:-1].split(';')[0]), []).append(item)
        else:
            row += 1
            if row <= rows:
                flow[row] = item

    # Every row this frame could need to touch: its own content, plus rows a
    # taller previous frame used, plus rows whose overlay has now gone away.
    # Blank counts as content: a row left out of this frame must be *erased*,
    # not left showing the previous screen.
    # The box's borders go over everything else on their rows, last.
    for r, cells in _frame_borders().items():
        overlay.setdefault(r, []).extend(cells)
    touched = set(flow) | set(overlay)
    touched |= {r for r in range(row + 1, _player_prev_rows[0] + 1) if r <= rows}
    touched |= {r for r in _player_overlay_rows[0] if r not in overlay and r <= rows}
    # The player owns the whole screen, so anything the painter still remembers
    # from the screen before it (the menu's now-playing box, a taller list) is
    # blanked here. There is no full clear per frame, so without this a first
    # frame shorter than the previous screen would leave its bottom rows behind.
    touched |= {r for r in pc.screen_rows() if r <= rows and r not in touched}
    touched.discard(_frame['prog_row'])     # update_progress_ui's (and the controls'), not the frame's
    touched |= set(range(1, ui.tab_rows() + 1))   # the tab bar's rows (the painter draws it there)
    _player_prev_rows[0] = row
    _player_overlay_rows[0] = set(overlay)

    painted = {r: pc.screen_row_paint(r, flow.get(r, ""), "".join(overlay.get(r, ()))) for r in sorted(touched)}
    out = "".join(painted.values())
    if out:
        sys.stdout.write(out)
        art = range(geom.art_top or 0, (geom.art_top or 0) + (geom.art_height or 0))
        first, last = (geom.art_left or 0) + 1, (geom.art_left or 0) + (geom.art_width or 0)
        if any(seg and r in art and pc.screen_painted(r, first, last) for r, seg in painted.items()):
            _draw_inline_art()  # cells under the image were rewritten: put it back

PLAYER_CREDITS_ROLES = [
    'performer',
    'various',
    'cast',
    'main cast',
    'guest',
    'starring',
    'featuring',
    'ensemble',
    'ensemble cast',
    'ensemble actor',
]

_CREW_ORDER = ['creator', 'writer', 'producer', 'director', 'script editor', 'composer']

_ui_state = {
    'show_metadata': True,      # from player_show_metadata; `m` flips and saves it
    'show_tabs': True,          # from player_show_tabs; `f` flips and saves it
    'debug': False,
    # The panels shown (see PANELS), and where: for each shape of window
    # ('wide': in columns beside the player; 'tall': under it), the panels in
    # order, each on a side ('left' / 'right'). From player_panels; `w` picks
    # them, its Arrange moves them. show_* follow it (set_panels).
    'panels': {'wide': [], 'tall': []},
    'arranging': None,          # the panel being moved (Arrange), or None
    'show_credits': False,
    'show_lyrics': False,
    'show_queue': False,
    'scrolling': False,         # a title line is too long and scrolls (ui.marquee): redraw it
    'chapter_title': '',        # the chapter playing: the title line shows it
    'chapter_pos': '',          # its "Chapter 3 of 20", for the details line
    'book': False,              # the track is an audiobook (speed keys apply)
    # False in a joined window's player view: lyrics are only painted by the
    # window playing the audio, so there the panel offers credits and the queue.
    'lyrics_pane': True,
    'nerd_icons': False,        # from player_nerd_font_icons
}
# (prev, play, pause, next) transport glyphs, keyed by the Nerd Font setting:
# Material Design icons when on, else the Unicode media symbols.
_TRANSPORT_ICONS = {
    False: ("⏮", "⏵", "⏸", "⏭"),
    True: ("\U000F04AE", "\U000F040A", "\U000F03E4", "\U000F04AD"),
}
# Clickable-control geometry (set by _controls_line / the draw): transport-icon
# columns on the controls row, the active hint pairs, and the hint-glyph cell map.
_last_transport_cols: dict[str, int] = {}
_last_controls_hint_pairs: list = []
_last_hint_cells: dict[tuple[int, int], str] = {}


# Below this width the single column shows no art: too small to be worth it.
_MIN_ART_COLS = 34


def _layout_mode(cols: int) -> str:
    """Classify terminal width into a layout mode: wide (art beside a pane) or
    standard (one column)."""
    return 'wide' if cols >= 120 else 'standard'



def timer_extra(rate: float = 1.0, sleep_left: float | None = None) -> str:
    """What follows the time on the progress row: the speed when it isn't 1×,
    and the sleep timer's countdown."""
    out = ''
    if rate and rate != 1.0:
        out += f"  ×{rate:g}"
    if sleep_left is not None:
        out += f"  ☾ {ui.format_time(int(sleep_left))}"
    return out


def set_chapter(chapters: list, index: int) -> None:
    """The chapter now playing, for the title and details lines and the chapters panel."""
    set_chapter_context(chapters, index)
    title, pos = chapter_label(chapters, index) if 0 <= index < len(chapters) else ('', '')
    _ui_state['chapter_title'], _ui_state['chapter_pos'] = title, pos


def chapter_time_on() -> bool:
    """Whether the time shown is the chapter's rather than the whole file's: one
    setting, so the player and the now-playing box always agree."""
    from backtrack.music_library import _live_config
    return bool(setting(_live_config(), 'player_chapter_time'))


def toggle_chapter_time() -> None:
    """Swap between the chapter's time and the whole file's, and remember it."""
    try:
        from backtrack.config import update_config
        update_config({'player_chapter_time': not chapter_time_on()})
    except Exception as exc:
        log.warning("couldn't save player_chapter_time: %s", exc)


def shown_time(elapsed: float, duration: float, chapters: list | None) -> tuple[float, float, tuple | None]:
    """The time to show, as (position, length), and the chapter playing as
    (start, end) fractions of the file for the progress bar, or None without chapters."""
    ci = chapter_at(chapters or [], elapsed)
    if ci < 0 or not duration:
        return elapsed, duration, None
    start = chapters[ci][0]
    end = chapters[ci + 1][0] if ci + 1 < len(chapters) else duration
    span = (start / duration, end / duration)
    if chapter_time_on():
        return elapsed - start, end - start, span
    return elapsed, duration, span


def update_progress_ui(row: int, elapsed: float, duration: float, width: int,
                       chapters: list | None = None, extra: str = '') -> None:
    """Update the default progress bar display. With chapters, the one playing
    is picked out on the bar, and `t` shows its time instead of the file's.
    `extra` follows the time (timer_extra)."""
    shown_at, shown_len, span = shown_time(elapsed, duration, chapters)
    elapsed_str = ui.format_time(int(shown_at))
    duration_str = ui.format_time(int(shown_len))
    # The elapsed time is padded to the duration's width so the bar holds its
    # length while the clock runs; swapping to a shorter time (`t`) widens it.
    full = f"{elapsed_str.rjust(len(duration_str))} / {duration_str}{extra}"

    if _frame['boxed'] and _frame['bar']:
        # The boxed player: the bar's own stretch of the transport row.
        left_pad, container_w = _frame['bar'][0] - 1 - ui.MARGIN_H, _frame['bar'][1] + 2 * ui.MARGIN_H
    elif geom.art_width and geom.art_width > 0:
        container_w = geom.art_width
        left_pad = geom.art_left if geom.art_left is not None else 0
    else:
        container_w = width
        left_pad = 0
    # Inset by the side margin at both ends, so the row never meets the edge.
    container_w -= 2 * ui.MARGIN_H
    left_pad += ui.MARGIN_H

    # What fits, giving way in turn and never cut short: the bar, then the
    # length (the position alone), then the time altogether.
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    bar_width = container_w - len(full) - 4
    if bar_width >= 3:
        bar, timer_text = ui.get_progress_bar(percent, bar_width, span), "  " + full
    else:
        bar, bar_width = "", 0
        timer_text = next((t for t in (full, elapsed_str) if ui.visual_len(t) <= container_w), "")

    # Remember where the bar landed so a click on it can be mapped back to a
    # position: get_progress_bar brackets the cells, so cell 0 sits one column
    # past the pad's '[' cap.
    geom.prog_row, geom.prog_col, geom.prog_w = row, left_pad + 2, bar_width

    # From the bar's own column, and blanked only to the container's end: the
    # box's border (and a panel beside) stay where they are.
    line = f"{bar}{timer_text}"
    # Boxed, it stops at the box's inside (its border is the frame's); else
    # it clears on to the margin.
    reach = container_w + (0 if _frame['boxed'] and _frame['bar'] else ui.MARGIN_H)
    out = pc.screen_span_paint(row, left_pad + 1, line + ' ' * max(0, reach - ui.visual_len(line)))
    for _r0, _r1, c0, c1 in (box for box in _frame['boxes'] if box[0] < row < box[1]) if _frame['boxed'] else ():
        out += pc.screen_span_paint(row, c0, f"{C.DIM}│{C.RESET}") + pc.screen_span_paint(row, c1, f"{C.DIM}│{C.RESET}")
    sys.stdout.write(out)               # cell by cell: nothing else on the row is disturbed
    sys.stdout.flush()


def _get_people(audio, tag_key: str) -> list[tuple[str, str]]:
    """Flatten an ID3 involved-people-list frame (e.g. TMCL/TIPL) into (role, name) pairs."""
    return [
        (role.strip().lower(), name.strip())
        for frame in audio.getall(tag_key)
        for role, name in frame.people
    ]


def _build_cast_lines(people: list[tuple[str, str]], max_w: int, limit: int = 4) -> list[str]:
    """Build display lines for cast/performer credits, truncating long labels and summarizing overflow past the limit."""
    total = len(people)
    cap = limit * 2
    lines = []

    for i, (role, name) in enumerate(people[:cap]):
        is_named = role not in PLAYER_CREDITS_ROLES
        label = f"{role.title()}: {name}" if is_named else name
        if len(label) > max_w - 3:
            label = label[:max_w - 4] + "…"
        prefix = f" • {label}"
        lines.append(prefix)

    if total > cap:
        lines.append(f"{C.DIM} • + {total - cap} more…{C.RESET}")

    return lines







VOLUME_W = 20                    # the volume as it changes: ♪, the bar, the percentage


def _volume_bar_w(width: int) -> int:
    return max(4, width - 7)                   # "♪ " before it, " 100%" after


def volume_text(width: int) -> str:
    """The volume: ♪, a bar, the level (the level alone when narrow), for
    whichever session this window plays or has joined."""
    from backtrack.playback.session import active_session
    try:
        v = max(0, min(100, int(active_session().get_volume())))
    except Exception:
        return f"{C.DIM}♪{C.RESET}"
    if width < 11:                             # too narrow for a bar: the level alone
        return f"{C.DIM}♪{C.RESET} {v}%"
    bw = _volume_bar_w(width)
    filled = round(bw * v / 100)
    return (f"{C.DIM}♪{C.RESET} {C.PRIMARY}{'━' * filled}{C.RESET}{C.DIM}{'─' * (bw - filled)}{C.RESET}"
            f" {v:>3}%")


def volume_changed() -> None:
    """The volume moved: show it for a moment in a small box over the middle
    of the screen (nothing shows it at rest)."""
    text = volume_text(VOLUME_W)
    w = ui.visual_len(text) + 4
    cols, rows = ui.get_terminal_size()
    if rows >= 3 and cols >= w:
        lines = [ln[ui.MARGIN_H:] for ln in pc.box_lines([text], w, 3)]
    else:                                              # no room for a box: the level alone
        lines = [volume_text(1)]
    pc.screen_float(lines, tune.VOLUME_SHOWN_S)


def toggle_metadata() -> None:
    """Show or hide the track details line, and remember the choice."""
    _ui_state['show_metadata'] = not _ui_state['show_metadata']
    try:
        from backtrack.config import update_config
        update_config({'player_show_metadata': _ui_state['show_metadata']})
    except Exception as exc:
        log.warning("couldn't save player_show_metadata: %s", exc)
    refresh_player_settings()

def toggle_tabs() -> None:
    """Show or hide the tab bar over the player, and remember the choice."""
    _ui_state['show_tabs'] = not _ui_state['show_tabs']
    try:
        from backtrack.config import update_config
        update_config({'player_show_tabs': _ui_state['show_tabs']})
    except Exception as exc:
        log.warning("couldn't save player_show_tabs: %s", exc)
    ui.set_tabs_hidden(not _ui_state['show_tabs'])


def view_chrome(open_: bool) -> None:
    """The app's chrome while a player view is open: no breadcrumb (it's its
    own place, not a menu level), and the tab bar as `f` last left it."""
    ui.hide_breadcrumb(open_)
    if not open_:
        pc.screen_release()                  # the lyrics' cells are every screen's again
        if _inline_art['path'] and geom.art_top and geom.art_width:
            # The cover image: the next screen writes over every cell of it,
            # whatever the painter thinks is there, so none of it is left.
            pc.screen_forget_cells(geom.art_top, geom.art_top + geom.art_height - 1,
                                   geom.art_left + 1, geom.art_left + geom.art_width)
    if open_:
        refresh_player_settings()
    ui.set_tabs_hidden(open_ and not _ui_state['show_tabs'])


def toggle_help() -> None:
    """Show or hide the hint bar: the app-wide switch, so every screen follows."""
    pc.toggle_hints()
# The panels the player can show beside (or under) itself, in the order `w` lists them.
PANELS = (('lyrics', "Lyrics"), ('people', "People"), ('queue', "Queue"), ('chapters', "Chapters"))


def shown_panels() -> list[str]:
    """The panels switched on, whatever this track has."""
    return [k for k, _side in _ui_state['panels']['wide']]


def set_panels(kinds: list[str]) -> None:
    """Show these panels: one switched on joins both shapes' arrangements at
    the end, on the right; one switched off leaves both. Saved."""
    for shape in ('wide', 'tall'):
        kept = [[k, side] for k, side in _ui_state['panels'][shape] if k in kinds]
        kept += [[k, 'right'] for k in kinds if k not in [x[0] for x in kept]]
        _ui_state['panels'][shape] = kept
    _panels_changed()


def _panels_changed() -> None:
    """Bring the flags the rest of the player reads in line, pick the list
    the queue keys work on, and remember the arrangement."""
    kinds = shown_panels()
    _ui_state['show_lyrics'] = 'lyrics' in kinds
    _ui_state['show_credits'] = 'people' in kinds
    _ui_state['show_queue'] = 'queue' in kinds or 'chapters' in kinds
    use_list('chapters' if chapters_listed() else 'queue')
    if not _ui_state['show_queue']:
        set_queue_cursor(None)                   # a reopened panel starts on the current item
    try:
        from backtrack.config import update_config
        update_config({'player_panels': _ui_state['panels']})
    except Exception as exc:
        log.warning("couldn't save player_panels: %s", exc)


def lyrics_laid_out() -> bool:
    """Whether the last frame gave the lyrics a box (they're switched on, the
    track has them, and there was room): the lyric pane paints there."""
    return _ui_state['show_lyrics'] and bool(geom.lyric_width)


def chapters_listed() -> bool:
    """Whether the queue keys and clicks work on the chapters (no queue panel)."""
    return 'chapters' in shown_panels() and 'queue' not in shown_panels()


def arrange_start() -> None:
    """Start moving the panels (Arrange): the next frame picks the first one
    on screen (none on screen: nothing to arrange)."""
    _ui_state['arranging'] = _ARRANGE_FIRST


_ARRANGE_FIRST = '__first__'       # Arrange asked for: the first panel the next frame draws


def arrange_key(key: str) -> bool:
    """A key while arranging: Tab / Shift-Tab to the next / previous panel,
    ↑ ↓ to move it up or down its column, ← → to the other side of the
    player (or the other column under it), Enter or Esc to finish (saved).
    The arrangement moved is this window shape's. Whether it was one."""
    kind = _ui_state['arranging']
    if kind is None:
        return False
    order = _frame.get('panel_order') or []
    arr = _ui_state['panels'][_frame.get('shape') or 'wide']
    at = next((i for i, (k, _s) in enumerate(arr) if k == kind), None)
    if key in ('ENTER', 'ESC', 'w', 'W') or at is None or kind not in order:
        _ui_state['arranging'] = None
        _panels_changed()
    elif key in ('TAB', 'BACKTAB'):
        _ui_state['arranging'] = order[(order.index(kind) + (1 if key == 'TAB' else -1)) % len(order)]
    elif key in ('UP', 'DOWN'):
        side = arr[at][1]
        same = [i for i, (_k, s) in enumerate(arr) if s == side]
        j = same.index(at) + (-1 if key == 'UP' else 1)
        if 0 <= j < len(same):
            arr[at], arr[same[j]] = arr[same[j]], arr[at]
    elif key in ('LEFT', 'RIGHT'):
        arr[at][1] = 'left' if key == 'LEFT' else 'right'
    else:
        return True                              # every other key waits while arranging
    return True


_lyric_src: dict = {'track': '', 'files': [], 'estimated': False}


def refresh_player_settings() -> None:
    """Re-read the player's settings (`debug`, `player_show_metadata`,
    `player_nerd_font_icons`) into `_ui_state`.

    Called when a track loads and when the metadata panel is toggled (both rare,
    both moments where the answer could have changed) rather than on every draw,
    which would re-read the file for a value that almost never moves.
    """
    try:
        from backtrack.config import load_config
        cfg = load_config()
        _ui_state['debug'] = bool(setting(cfg, 'debug'))
        _ui_state['show_metadata'] = bool(setting(cfg, 'player_show_metadata'))
        _ui_state['show_tabs'] = bool(setting(cfg, 'player_show_tabs'))
        _ui_state['nerd_icons'] = bool(setting(cfg, 'player_nerd_font_icons'))
        saved = cfg.get('player_panels') or {}
        _ui_state['panels'] = {shape: [[k, side] for k, side in (saved.get(shape) or [])
                                       if k in dict(PANELS) and side in ('left', 'right')]
                               for shape in ('wide', 'tall')}
        kinds = shown_panels()
        _ui_state['show_lyrics'] = 'lyrics' in kinds
        _ui_state['show_credits'] = 'people' in kinds
        _ui_state['show_queue'] = 'queue' in kinds or 'chapters' in kinds
        use_list('chapters' if chapters_listed() else 'queue')
    except Exception:
        _ui_state['debug'] = False


def set_lyric_sources(track: str, files: list[str], estimated: bool = False) -> None:
    """Register the files the lyric layer actually opened for this track.

    Set by the player from what the load RESOLVED, never from what it hoped to
    find, so the 'm' panel names the document being read rather than the one that
    ought to be there: the difference between the two is the bug class this
    exists to make visible. The panel only shows it with Diagnostics on.

    Stamped with the track it belongs to, and the panel only shows it for that
    track. Naming another episode's files would be worse than naming none, and a
    module-level record outlives the track that set it.
    """
    refresh_player_settings()
    _lyric_src['track'] = track or ''
    _lyric_src['files'] = [f for f in (files or []) if f]
    _lyric_src['estimated'] = estimated



def _build_crew_lines(people: list[tuple[str, str]], max_w: int,
                     cast_names: list[str] | None = None,
                     limit: int = 4) -> list[str]:
    """Build production-team credit lines: names matching cast_names surface first
    (in cast order), then priority roles, then the rest, truncated to limit with an overflow summary."""
    cast_names = cast_names or []
    cast_name_lower = [n.lower() for n in cast_names]

    cast_matches = []
    others = []
    seen_cast = set()

    for role, name in people:
        name_l = name.lower()
        if name_l in cast_name_lower and name_l not in seen_cast:
            cast_matches.append((cast_name_lower.index(name_l), role.title(), name))
            seen_cast.add(name_l)
        else:
            others.append((role.title(), name))

    cast_matches.sort(key=lambda x: x[0])
    cast_matches = [(role, name) for _, role, name in cast_matches]

    priority_roles = ['creator', 'producer', 'script editor']
    if not cast_matches:
        priority_roles = ['creator', 'producer', 'writer', 'script editor']

    ordered = []
    extra = []
    for role, name in others:
        if role in priority_roles:
            ordered.append((role, name))
        else:
            extra.append((role, name))

    ordered.sort(key=lambda x: _CREW_ORDER.index(x[0]) if x[0] in _CREW_ORDER else 99)
    combined = cast_matches + ordered + extra
    total = len(combined)
    lines = []

    for role, name in combined[:limit]:
        label = f"{role}: {name}"
        if len(label) > max_w - 3:
            label = label[:max_w - 4] + "…"
        lines.append(f" ⚙ {label}")

    if total > limit:
        lines.append(f"{C.DIM} ⚙ + {total - limit} more…{C.RESET}")

    return lines


def _controls_text(is_paused: bool, n: int = 3) -> tuple[tuple, str]:
    """The transport's glyphs and their line: all three, or in a window too
    narrow for them play/pause and next (n=2), or play/pause alone (n=1)."""
    prev, play, pause, nxt = _TRANSPORT_ICONS[_ui_state['nerd_icons']]
    pp_icon = play if is_paused else pause
    # Nerd Font icons sit centred in their cells, so even gaps. The emoji-font
    # glyphs draw left of their cells; the wider gap before play keeps them even.
    gap = "   " if _ui_state['nerd_icons'] else "  "
    if n >= 3:
        return (prev, pp_icon, nxt), f"{prev}   {pp_icon}{gap}{nxt}"
    if n == 2:
        return (pp_icon, nxt), f"{pp_icon}{gap}{nxt}"
    return (pp_icon,), pp_icon


def _icons_fitting(inner: int) -> int:
    """How many transport glyphs a row `inner` columns wide keeps: they're the
    last thing to go, next before play/pause."""
    for n in (3, 2):
        if ui.visual_len(_controls_text(False, n)[1]) <= inner:
            return n
    return 1


_TINY_ROWS = 5                  # a window this short (or shorter) shows only the transport
_TINY_COLS = 10                 # likewise one this narrow, unboxed


def _draw_tiny(size: tuple, is_paused: bool) -> tuple:
    """The player in a tiny window: the transport and nothing else, boxed
    from 3 rows (and room for a glyph inside), centred; the progress
    update fits what it can beside the glyphs (update_progress_ui)."""
    cols, rows = size
    mh = ui.MARGIN_H
    boxed = rows >= 3 and cols - 2 * mh - 4 >= 2
    if boxed:
        top = max(1, (rows - 3) // 2 + 1)
        left, right = mh + 1, cols - mh
        ctrl, icons_col, inner = top + 1, left + 2, right - left - 3
        boxes = [(top, top + 2, left, right)]
    else:
        ctrl, icons_col, inner = max(1, (rows + 1) // 2), 2, cols - 1     # the glyphs from column 2
        boxes = []
    # Narrower than a box holding play/pause: that alone, unboxed (what a
    # narrower window already gave up stays given up).
    if rows >= 3 and not boxed:
        inner = 2
    n = _icons_fitting(inner)
    _frame.update(boxed=True, icons_n=n, icons_col=icons_col)
    status = _controls_line(False, is_paused, 100)[0]
    bar_l = icons_col + ui.visual_len(status.strip()) + 3
    last = icons_col + inner - 1
    _frame.update(boxed=True, boxes=boxes, titles={}, prog_row=ctrl, bar=(bar_l, max(0, last - bar_l + 1)),
                  side=False, beside=False)
    _render_frame_buffer(["\033[H\033[3J\033[J"], rows)
    write_controls(ctrl, status)
    sys.stdout.flush()
    return ctrl, ctrl, rows + 1, cols, rows


def _controls_line(is_uslt: bool, is_paused: bool, volume: int,
                   width: int | None = None,
                   has_lyrics: bool = True, has_credits: bool = True) -> tuple[str, str]:
    """Build the centred transport-controls line and the shortcuts/help hint line below it."""
    icons, controls = _controls_text(is_paused, _frame.get('icons_n') or 3)

    art_left = geom.art_left or 0
    if _frame['boxed'] and _frame['icons_col']:
        left_pad = _frame['icons_col'] - 1         # the boxed player's transport row
    elif width:
        left_pad = art_left + max(0, (width - ui.visual_len(controls)) // 2)
    elif geom.art_width:
        left_pad = art_left + max(0, (geom.art_width - ui.visual_len(controls)) // 2)
    else:
        cols = ui.get_terminal_size()[0]
        left_pad = max(0, (cols - ui.visual_len(controls)) // 2)

    status = " " * left_pad + controls
    _record_transport_cols(status, icons)

    if not pc.hints_visible():
        # Hints are off app-wide, but the player keeps its way back to them,
        # unless the help toggle is switched off (then you know to press ?).
        pairs = [(keys.label('global.help'), 'help')] if pc.help_toggle_shown() else []
        _set_controls_hint_pairs(pairs)
        return status, _hint(*pairs, always=True) if pairs else ""

    L = keys.label
    hint_args = [
        (L('player.playpause'), 'play/pause'),
        (L('player.back_5', 'player.fwd_5'), '±5s'),
        (L('player.back_1', 'player.fwd_1'), '±1s'),
        (L('player.back_30', 'player.fwd_30'), '±30s'),
    ]
    if _ui_state['debug']:                       # Diagnostics: checking end credits
        hint_args.append((L('player.near_end'), f'last {tune.NEAR_END_JUMP_S}s'))
    hint_args += [(L('player.vol_up', 'player.vol_down', first=True), 'volume'), (L('player.meta'), 'meta')]
    if nav.TABS:
        hint_args.append((L('player.tabs'), 'tabs'))
    if _ui_state.get('book'):
        hint_args.append((L('player.slower', 'player.faster'), 'speed'))
    hint_args.append((L('player.sleep'), 'sleep'))
    hint_args.append((L('player.edit'), 'edit'))
    if nav.TABS:
        hint_args.append((L('player.album', 'player.artist'), 'album/artist'))
    if _ui_state.get('chapter_title'):
        hint_args.append((L('player.chapter_time'), 'file time' if chapter_time_on() else 'chapter time'))
    if _ui_state['arranging'] is not None:       # Arrange: its keys, and nothing else to do
        hint_args = [("tab", 'next panel'), ("↑↓", 'up/down'), ("←→", 'side'), ("↵", 'done')]
        _set_controls_hint_pairs(hint_args)
        return status, _hint(*hint_args, always=True)
    if has_lyrics or has_credits or has_queue() or _ui_state.get('chapter_title'):
        hint_args.append((L('player.panel'), 'panels'))
    if chapters_listed():
        Q = lambda *n, **kw: L(*(f'player_queue.{x}' for x in n), **kw)  # noqa: E731
        hint_args += [(Q('up', 'down'), 'chapters'), (Q('play'), 'go to it')]
    elif _ui_state.get('show_queue'):
        Q = lambda *n, **kw: L(*(f'player_queue.{x}' for x in n), **kw)  # noqa: E731
        hint_args += [(Q('up', 'down'), 'queue'), (Q('play'), 'play it'), (Q('move_up', 'move_down'), 'move'),
                      (Q('remove', most=1), 'remove'), (Q('shuffle'), 'shuffle'), (Q('clear'), 'clear'),
                      (Q('undo'), 'undo')]
    if pc.help_toggle_shown():
        hint_args.append((L('global.help'), 'hide help'))
    hint_args += [(L('player.prev', 'player.next'), 'prev/next'),
                  (L('player.stop'), 'stop'), (L('player.back', first=True), 'back'), (L('player.quit'), 'quit')]

    _set_controls_hint_pairs(hint_args)
    return status, _hint(*hint_args)


def _record_transport_cols(status: str, icons: tuple[str, str, str]) -> None:
    """Store the 1-based columns of the prev / play-pause / next glyphs on the
    controls row so clicks on them can be mapped back to those actions. Columns,
    not string positions: the emoji-font glyphs are two cells wide."""
    global _last_transport_cols
    _last_transport_cols = {action: ui.visual_len(status[:i]) + 1
                            for action, icon in zip(('prev', 'playpause', 'next'), icons)
                            if (i := status.find(icon)) >= 0}


def _set_controls_hint_pairs(pairs: list) -> None:
    """Remember the hint pairs currently shown under the controls, for hit-testing."""
    global _last_controls_hint_pairs
    _last_controls_hint_pairs = list(pairs)


def _place_controls(emit, ctrl_row: int, rows: int, is_uslt: bool, is_paused: bool,
                    volume: int, has_lyrics: bool, has_credits: bool) -> tuple[int, list[str]]:
    """Draw the transport line at `ctrl_row` (pulled up so its hints still fit on
    screen) with the hint lines under it, and record their click cells. Returns
    the row it used and the hint lines."""
    status_ln, shortcuts_ln = _controls_line(is_uslt, is_paused, volume,
                                             has_lyrics=has_lyrics, has_credits=has_credits)
    shortcut_lines = shortcuts_ln.splitlines() or [""]
    # A window too short for all the help shows the lines that fit under the
    # controls; only with no room for even one is the controls row pulled up.
    shortcut_lines = shortcut_lines[:max(1, rows - ui.MARGIN_V - ctrl_row)]
    ctrl_row = max(1, min(ctrl_row, rows - len(shortcut_lines) - ui.MARGIN_V))
    emit(f"\033[{ctrl_row};1H\033[K{status_ln}")
    for offset, line in enumerate(shortcut_lines, start=1):
        emit(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui.MARGIN_H}{line}")
    compute_controls_hint_cells(shortcut_lines, ctrl_row + 1)
    return ctrl_row, shortcut_lines


def compute_controls_hint_cells(shortcut_lines: list[str], first_row: int) -> None:
    """Populate ``_last_hint_cells`` for hint lines drawn at ``first_row`` onward
    (each rendered with a MARGIN_H left inset, like the draw does)."""
    _last_hint_cells.clear()
    if not _last_controls_hint_pairs:
        return
    for k, line in enumerate(shortcut_lines):
        add_hint_click_cells(_last_hint_cells, ' ' * ui.MARGIN_H + line,
                             first_row + k, _last_controls_hint_pairs)


def transport_click_action(row: int, col: int, ctrl_row: int) -> str | None:
    """Return 'prev' / 'playpause' / 'next' if (row, col) hits a transport glyph
    on the controls row, else None."""
    if row != ctrl_row or not _last_transport_cols:
        return None
    for action, c in _last_transport_cols.items():
        if abs(col - c) <= 1:
            return action
    return None




def progress_from_click(row: int, col: int) -> float | None:
    """If (row, col) lands on the horizontal progress bar, return the fraction of
    the track that column represents (0.0-1.0); else None. The '[' and ']' caps
    count as the two ends, so clicking either edge seeks to the start / end."""
    if geom.prog_row is None or geom.prog_col is None or geom.prog_w <= 0:
        return None
    if row != geom.prog_row:
        return None
    lo, hi = geom.prog_col - 1, geom.prog_col + geom.prog_w   # include the caps
    if not (lo <= col <= hi):
        return None
    if geom.prog_w <= 1:
        return 0.0
    frac = (col - geom.prog_col) / (geom.prog_w - 1)
    return max(0.0, min(1.0, frac))


def time_clicked(row: int, col: int) -> bool:
    """Whether (row, col) is on the time after the progress bar (it swaps it)."""
    return (geom.prog_row is not None and geom.prog_col is not None
            and row == geom.prog_row and col > geom.prog_col + geom.prog_w)


def hint_click_key(row: int, col: int) -> str | None:
    """The synthesised key for a click on a controls hint glyph, or None."""
    return _last_hint_cells.get((row, col))


def _movement_roman(s: str) -> str:
    """A movement number as a Roman numeral, the convention for classical
    movements. Non-numeric (or unnumbered) values pass through unchanged.
    """
    return numbering.roman(s) or s


def _credits_rule(width: int) -> str:
    """The divider between the credits and the lyrics under them."""
    return f"{C.DIM}{'─' * max(0, width)}{C.RESET}"


def _meta_left_lines(audio, file_path: str, max_val_w: int) -> list[str]:
    """Build the left-column metadata lines (title, artist/album, and optional
    extras like year/genre/track/disc) shown beside the album art."""
    def _trim(text: str) -> str:
        """Truncate text to max_val_w with an ellipsis."""
        return ui.truncate_text(text, max(1, max_val_w), placeholder='…')

    def _scroll(text: str) -> str:
        """Fit text to max_val_w by scrolling through it (ui.marquee), and say
        the view needs redrawing while it does."""
        if ui.visual_len(text) > max(1, max_val_w):
            _ui_state['scrolling'] = True
        return ui.marquee(text, max(1, max_val_w), time.monotonic())

    _ui_state['scrolling'] = False

    def _txt(frame_id: str) -> str:
        """Return a text frame's stripped value, or empty string if absent."""
        return first_text(audio.get(frame_id))

    def _txts(frame_id: str) -> str:
        """A list-like frame's values for display, comma-separated.

        `_txt` returns only `text[0]`, which silently hid every value after the
        first on a duet credit or a two-genre track.
        """
        fr = audio.get(frame_id)
        if fr is None or not getattr(fr, 'text', None):
            return ""
        return format_value_list(list(fr.text))

    def _frac(frame_id: str) -> tuple[str, str]:
        """Return (current, total) from a fractional frame like '3/12'."""
        clean = re.sub(r'\s+', ' ', _txt(frame_id))
        parts = [p.strip() for p in re.split(r'[/|∕⁄]|\bof\b', clean, flags=re.IGNORECASE) if p.strip()]
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")

    title = _ui_state['chapter_title'] or _txt('TIT2') or track_title(file_path)
    artist = _txts('TPE1') or _txts('TPE2')
    album = _txt('TALB')

    lines: list[str] = []
    if title:
        lines.append(f"{C.BOLD}{_scroll(title)}{C.RESET}")

    # Album and artist ALWAYS show; the 'm' toggle only governs the extras below.
    if artist and album:
        second = f"{artist} - {album}"
    else:
        second = artist or album
    if second:
        lines.append(f"{C.DIM}{_scroll(second)}{C.RESET}")

    if _ui_state['show_metadata']:
        details: list[str] = []

        # Year from TDRC (v2.4) or TYER (v2.3), else the original-release frames.
        year = year_of(_txt('TDRC') or _txt('TYER') or _txt('TDOR') or _txt('TORY'))
        if year:
            details.append(str(year))
        genre = _txts('TCON')
        if genre:
            details.append(genre)

        work = _txt('TIT1')
        movement, _ = _frac('MVIN')
        disc, disc_total = _frac('TPOS')
        disc_subtitle = _txt('TSST')
        track, track_total = _frac('TRCK')

        def _track_str(t: str, total: str) -> str:
            """TRCK in display form: 'Track 3 of 12', or 'Track 3' with no total."""
            return f"Track {t} of {total}" if (t and total) else f"Track {t}"

        def _disc_str(d: str, total: str) -> str:
            """TPOS in display form: 'Disc 1 of 2', or 'Disc 1' with no total."""
            return f"Disc {d} of {total}" if (d and total) else f"Disc {d}"

        # Classical: the work, then its movement in Roman numerals. These sit
        # alongside the disc/track fields rather than replacing them.
        if work:
            details.append(work)
        if movement:
            details.append(f"Movement {_movement_roman(movement)}")

        # Which part of the set this is: the disc subtitle names it better than a
        # number ever does, so TSST wins and TPOS is the fallback.
        if disc_subtitle:
            details.append(disc_subtitle)
        elif disc:
            details.append(_disc_str(disc, disc_total))

        # A movement number already says where the track sits in the work, so the
        # track number would only repeat it: show one or the other, never both.
        if track and not movement:
            details.append(_track_str(track, track_total))
        if _ui_state['chapter_pos']:
            details.append(_ui_state['chapter_pos'])

        if details:
            lines.append(f"{C.DIM}{_trim(' · '.join(details))}{C.RESET}")

        # Which documents the words on screen are coming from. A track can have a
        # script, a timed transcript, both or neither, and until this was shown the
        # only way to tell an accurate timeline from a guessed one was to watch it
        # drift for ten minutes.
        if _ui_state.get('debug') and _lyric_src['track'] == file_path:
            src = ' · '.join(_lyric_src['files']) or 'none found'
            est = _lyric_src['estimated']
            txt = _trim(f"lyrics: {src}" + (" · estimated, will drift" if est else ""))
            lines.append(f"{C.ACCENT if est else C.DIM}{txt}{C.RESET}")

    if not lines:
        fp = ui.truncate_text(file_path, max(1, max_val_w), placeholder='…', front=True)
        lines.append(f"{C.DIM}{fp}{C.RESET}")

    return lines


def _align_art_lines(art_lines: list[str], cols: int) -> list[str]:
    """Center art_lines horizontally within cols, padding every line to a uniform width."""
    if not art_lines:
        return []
    art_width = max(ui.visual_len(line) for line in art_lines)
    left_pad = max(0, (cols - art_width) // 2)
    aligned = []
    for line in art_lines:
        extra_padding = max(0, art_width - ui.visual_len(line))
        aligned.append(" " * left_pad + line + " " * extra_padding)
    return aligned


def _center_lines(lines: list[str], cols: int) -> list[str]:
    """Center each line horizontally within cols based on its visible length."""
    if not lines:
        return []
    centered: list[str] = []
    for line in lines:
        vis = ui.visual_len(line)
        left = max(0, (cols - vis) // 2)
        centered.append(" " * left + line)
    return centered


def draw_full_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                 is_paused: bool = False, volume: int = 100) -> tuple[int, int, int, int, int]:
    """Hide the cursor and draw the default playback UI layout."""
    sys.stdout.write(f"{C.HIDE}")
    return _draw_default_ui(file_path, audio, pre_art, size, is_paused, volume)


_MIN_ART_ROWS = 3


def _art_room(avail: int) -> int:
    """The rows the art gets out of `avail`: all of them, or none when there's
    too little for art worth showing, rather than art forced in over the controls."""
    return avail if avail >= _MIN_ART_ROWS else 0


def _single_column_room(rows: int, meta_rows: int, control_rows: int, pane_rows: int) -> int:
    """Rows left for the art in the single-column layout. Counts exactly what is
    drawn under it (a blank, the metadata, progress, controls and hints, any pane
    below) plus the bottom margin, so at the window height that fits full-width
    art the hints sit on the last row with nothing spare under them."""
    return rows - (1 + meta_rows + control_rows + pane_rows) - ui.MARGIN_V


def _art_w(lines: list[str]) -> int:
    return max((ui.visual_len(l) for l in lines), default=0)


def _centred_art(file_path: str, box_w: int, max_w: int, avail_h: int,
                 pre_art: str | None) -> list[str]:
    """Art for a box `box_w` wide with equal gaps either side: when the best fit
    would leave an odd gap, one column wider if that still fits, else one narrower."""
    lines = _art_width_for_height(file_path, max_w, avail_h, pre_art)[1]
    w = _art_w(lines)
    if not lines or w >= box_w or (box_w - w) % 2 == 0:
        return lines
    wider = _art_width_for_height(file_path, w + 1, avail_h, pre_art)[1]
    if _art_w(wider) == w + 1:
        return wider
    return _art_width_for_height(file_path, w - 1, avail_h, pre_art)[1]


_PEOPLE_COL_MIN = 24             # a column of people narrower than this doesn't split in two


def _people_lines(cast: list, crew: list, width: int, rows: int) -> list[str]:
    """The People box's lines in `width` × `rows`: everyone credited, name
    then a dim role, the performers and then the production team. One column
    when they fit, else two side by side when the width allows, else cut with
    a count of the rest."""
    entries = ([(name, role.title()) for role, name in cast] + ([("", "")] if cast and crew else [])
               + [(name, role.title()) for role, name in crew])
    two = len(entries) > rows and width >= 2 * _PEOPLE_COL_MIN + 3
    col_w = (width - 3) // 2 if two else width
    per = -(-len(entries) // 2) if two else len(entries)            # rows each column needs
    if per > rows > 0:                                               # still too many: the rest counted
        keep = rows * (2 if two else 1) - 1
        entries = entries[:keep] + [(f"{C.DIM}… {len(entries) - keep} more{C.RESET}", "")]
        per = -(-len(entries) // 2) if two else len(entries)
    name_w = min(max((ui.visual_len(n) for n, _r in entries), default=0), max(8, col_w * 3 // 5))

    def cell(name: str, role: str) -> str:
        name = ui.truncate_text(name, name_w)
        line = f"{name}{' ' * (name_w - ui.visual_len(name))}  {C.DIM}{role}{C.RESET}" if role else name
        line = ui.clip_ansi(line, col_w) if ui.visual_len(line) > col_w else line
        return line + " " * max(0, col_w - ui.visual_len(line))
    columns = [entries[:per], entries[per:]] if two else [entries]
    return [f" {C.DIM}│{C.RESET} ".join(cell(*col[k]) if k < len(col) else " " * col_w for col in columns).rstrip()
            for k in range(per)]


def _chapter_titles() -> list:
    from backtrack.playback.queue_pane import _chapter_ctx
    return _chapter_ctx['titles']


def _stack(kinds: list, y0: int, y1: int, people_rows: int) -> list:
    """A column of panels stacked down rows y0..y1, no gap left: (kind, top,
    bottom) each. The people get the rows they need; the others share the
    rest; a column of only the people stretches them. Panels that would get
    fewer than 3 rows (a box and a line) wait, from the last."""
    kinds = list(kinds)
    while kinds:
        h = y1 - y0 + 1
        flex = [k for k in kinds if k != 'people']
        size = {}
        if 'people' in kinds:
            size['people'] = min(people_rows, h - 3 * len(flex)) if flex else h
        rest = h - sum(size.values())
        for i, k in enumerate(flex):
            size[k] = rest // len(flex) + (1 if i < rest % len(flex) else 0)
        if all(v >= 3 for v in size.values()):
            break
        kinds.pop()
    out, y = [], y0
    for k in kinds:
        out.append((k, y, y + size[k] - 1))
        y += size[k]
    return out


def _draw_boxed_ui(file_path: str, audio, pre_art: str | None, size: tuple, is_paused: bool, volume: int,
                   cast_people: list, crew_people: list, has_cast: bool, has_lyrics: bool):
    """The player in boxes, under the tab bar or with a panel showing: the art
    and the details in a box only as wide as the art; the panel's boxes (the
    people over the lyrics, or the queue) beside it in a wide window, else
    under it; along the bottom, a box with the controls, the progress bar and
    the times; the hints free under that. Returns what _draw_default_ui does."""
    cols, rows = size
    mh, mv = ui.MARGIN_H, ui.MARGIN_V
    frame_buffer = ["\033[H\033[3J\033[J"]
    emit = frame_buffer.append
    # The panels this track has something for, as each shape arranges them.
    has = {'lyrics': has_lyrics, 'people': has_cast, 'queue': has_queue(),
           'chapters': bool(_chapter_titles())}
    arrange = {shape: [(k, s) for k, s in _ui_state['panels'][shape] if has.get(k)]
               for shape in ('wide', 'tall')}
    panel = bool(arrange['wide'])
    is_uslt = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))

    # Bottom up: the hints, under them nothing; over them the transport box.
    _frame['icons_col'] = mh + 3
    _frame['icons_n'] = _icons_fitting(cols - 2 * mh - 4)
    status, hints = _controls_line(is_uslt, is_paused, volume, has_lyrics=has_lyrics, has_credits=has_cast)
    hint_lines = hints.splitlines() if hints else []
    # The hints keep what's left once the transport box has its rows.
    hint_lines = hint_lines[:max(0, rows - mv - ui.top_margin() - 3)]
    hint_top = rows - mv - len(hint_lines) + 1
    t_bot = hint_top - 1
    t_top, ctrl = t_bot - 2, t_bot - 1
    top, body_bot = ui.top_margin() + 1, t_bot - 3
    body_h = body_bot - top + 1
    full_l, full_r = mh + 1, cols - mh
    aspect = ui.cell_aspect()
    boxes: list = []
    titles: dict = {}
    lyric_row, lyric_bot = body_bot, body_bot - 1
    side = beside = False
    # Too short for any box over the transport's (a tiny window, all the
    # hints): that one alone, still boxed.
    if body_h >= _BOX_MIN_BODY:

        # The boxes tile the body with no gap: the player's box as wide as its art
        # with the panel's beside it, when that leaves the panel room enough; else
        # each box the full width, the panel's under the player's.
        meta = _meta_left_lines(audio, file_path, 40)
        meta_n, meta_w = len(meta), max((ui.visual_len(m) for m in meta), default=0)
        # Beside the panel, the player's box at most its share: the art over the
        # details, or beside them when that makes it bigger; never narrower than
        # the details need.
        side_inner = int((full_r - full_l + 1) * _PLAYER_SHARE) - 4
        s_rows = min(_art_room(body_h - 3 - meta_n), int(side_inner / aspect))
        b_text = min(meta_w, side_inner - 2 - round(_MIN_ART_ROWS * aspect))
        b_rows = min(_art_room(body_h - 2), int((side_inner - 2 - b_text) / aspect)) if b_text >= _BESIDE_TEXT else 0
        side_beside = b_rows >= max(_MIN_ART_ROWS, s_rows * _BESIDE_GAIN)
        side_in = (round(b_rows * aspect) + 2 + b_text if side_beside
                   else max(round(s_rows * aspect), min(_PLAYER_MIN_W, side_inner)))
        # Wide: a column of panels beside the player on each side that has
        # any, each column at least _PANEL_SIDE_MIN. Else tall: under it.
        cols_w = [[k for k, s in arrange['wide'] if s == side_] for side_ in ('left', 'right')]
        n_cols = sum(bool(c) for c in cols_w)
        spare = (full_r - full_l + 1) - (side_in + 4) - n_cols * mh
        side = panel and spare >= n_cols * _PANEL_SIDE_MIN
        cols_t = [[k for k, s in arrange['tall'] if s == side_] for side_ in ('left', 'right')]
        two_under = all(cols_t) and (full_r - full_l + 1) - mh >= 2 * _PANEL_SIDE_MIN
        people_rows = len(_people_lines(cast_people, crew_people, full_r - full_l - 3, body_h // 3)) + 2

        def need(kinds: list) -> int:
            """Rows a column of panels under the player keeps: the people what
            they need, the others a few each."""
            return sum(people_rows if k == 'people' else _PANEL_MIN_ROWS for k in kinds)

        # The player's box: the art as big as the room allows, the details under it.
        if side:
            max_inner = side_inner
            avail = body_h - 3 - meta_n
        else:
            max_inner = full_r - full_l + 1 - 4
            # Under it, the panels keep what they need: the people's rows, and a
            # few for the lyrics or the queue.
            reserve = (max(need(cols_t[0]), need(cols_t[1])) if two_under
                       else need(cols_t[0] + cols_t[1])) if panel else 0
            avail = body_h - 3 - meta_n - reserve
        # The art is a square, as big as the room allows, filled with no gap (the
        # cover cropped to it); the box is the art and its inset, no wider.
        art_rows = min(_art_room(avail), int(max_inner / aspect))
        # A short, wide box: the art beside the details rather than over them,
        # when that makes it bigger.
        beside = False
        if side:
            beside = side_beside
            art_rows = b_rows if beside else s_rows
        else:
            b_rows = min(_art_room(avail + meta_n + 1), int((max_inner - 2 - _BESIDE_TEXT) / aspect))
            beside = b_rows >= max(_MIN_ART_ROWS, art_rows * _BESIDE_GAIN)
            art_rows = b_rows if beside else art_rows
        art_cols = min(max_inner, round(art_rows * aspect))
        art = square_art(file_path, art_cols, art_rows, pre_art) if art_rows >= _MIN_ART_ROWS else []
        beside = beside and bool(art)
        art_w = _art_w(art) if art else 0
        content_h = max(len(art), meta_n) if beside else len(art) + (1 if art else 0) + meta_n
        if panel and not side and body_bot - (top + 1 + content_h) < 3:
            panel = False                     # no room under the player for the panel's box: it waits
        lw = (spare // n_cols if cols_w[0] else 0) if side else 0      # the left column's width
        p_l = full_l + (lw + mh if lw else 0)
        p_r = p_l + side_in + 3 if side else full_r
        box_in = p_r - p_l - 1                                    # between the borders
        # Alone, the box reaches down to the transport box, what's in it centred.
        p_bot = body_bot if side or not panel else top + 1 + content_h
        c_top = top + 1 + max(0, (p_bot - top - 1 - content_h) // 2)
        if beside:                                                # the pair centred across the box
            meta = _meta_left_lines(audio, file_path, min(box_in - 4 - art_cols, 60))
            pair_w = art_cols + 2 + max((ui.visual_len(m) for m in meta), default=0)
            left = p_l + 2 + max(0, (box_in - 2 - pair_w) // 2)
        else:
            left = p_l + 2 + (box_in - 2 - art_w) // 2           # centred across the box
        for k, line in enumerate(art):
            emit(f"\033[{c_top + k};{left}H{line}")
        geom.art_top, geom.art_left, geom.art_width, geom.art_height = c_top, left - 1, art_w, len(art)
        geom.art_gap = bool(art)
        # The details, as many as the box holds (a tiny window: the title first).
        if beside:
            r = c_top + max(0, (len(art) - len(meta)) // 2)
            for line in meta[:max(0, p_bot - r)]:
                emit(f"\033[{r};{left + art_cols + 2}H{line}")
                r += 1
        else:
            r = c_top + len(art) + (1 if art else 0)
            text_w = box_in - 2                                   # the details: inside "│ " … " │"
            for line in _meta_left_lines(audio, file_path, text_w)[:max(0, p_bot - r)]:
                emit(f"\033[{r};{p_l + 2 + max(0, (text_w - ui.visual_len(line)) // 2)}H{line}")
                r += 1
        boxes.append((top, p_bot, p_l, p_r))

        # The panels: in their columns beside the player, or under it (in two
        # columns when both sides have some and there's room), each column
        # stacked in its order. The arrangement is this shape's.
        if panel:
            shape = 'wide' if side else 'tall'
            if side:
                regions = [(c, top, body_bot, l_, r_) for c, l_, r_ in (
                    (cols_w[0], full_l, full_l + lw - 1), (cols_w[1], p_r + 1 + mh, full_r)) if c]
            elif two_under:
                half = (full_r - full_l + 1 - mh) // 2
                regions = [(cols_t[0], p_bot + 1, body_bot, full_l, full_l + half - 1),
                           (cols_t[1], p_bot + 1, body_bot, full_l + half + mh, full_r)]
            else:
                regions = [(cols_t[0] + cols_t[1], p_bot + 1, body_bot, full_l, full_r)]
            order = []
            for kinds, y0, y1, l_, r_ in regions:
                for kind, b0, b1 in _stack(kinds, y0, y1, people_rows):
                    box = (b0, b1, l_, r_)
                    boxes.append(box)
                    order.append(kind)
                    if kind == _ui_state['arranging']:
                        _frame['focus'] = box
                    a_in = r_ - l_ - 3                                # inside "│ " … " │"
                    if kind == 'people':
                        titles[box] = "People"
                        for k, line in enumerate(_people_lines(cast_people, crew_people, a_in, b1 - b0 - 1)):
                            emit(f"\033[{b0 + 1 + k};{l_ + 2}H{line}")
                    elif kind == 'lyrics':
                        titles[box] = "Lyrics"
                        lyric_row, lyric_bot = b0 + 1, b1 - 1
                        geom.lyric_left, geom.lyric_width = l_ + 2, a_in
                        geom.lyric_centre = (lyric_row + lyric_bot) // 2
                        pc.screen_reserve(lyric_row, lyric_bot, l_ + 2, l_ + 1 + a_in)   # the lyrics' own
                    else:                                            # the queue, or the chapters
                        use_list(kind)
                        titles[box] = queue_title()
                        _place_queue(emit, b0 + 1, l_ + 2, a_in, b1 - b0 - 1, heading=False, kind=kind)
            use_list('chapters' if chapters_listed() else 'queue')
            _frame.update(panel_order=order, shape=shape)
            if queue_pane.scrolled():            # a queue title scrolling: keep redrawing as it moves
                _ui_state['scrolling'] = True
            if _ui_state['arranging'] == _ARRANGE_FIRST and order:
                _ui_state['arranging'] = order[0]
                _frame['focus'] = boxes[-len(order)]
        if _ui_state['arranging'] is not None and _ui_state['arranging'] not in (_frame.get('panel_order') or []):
            _ui_state['arranging'] = None                    # nothing of it on screen: arranging ends

    # The transport box: the controls, then the bar and the times (update_progress_ui).
    # Over it a body too short for a box of its own: the transport's box
    # takes those rows too, the title in them.
    if not boxes and body_h > 0:
        t_top = top
        for k, line in enumerate(_meta_left_lines(audio, file_path, full_r - full_l - 3)[:ctrl - top - 1]):
            emit(f"\033[{top + 1 + k};{full_l + 2}H{line}")
    controls = status.strip()
    bar_l = _frame['icons_col'] + ui.visual_len(controls) + 3
    boxes.append((t_top, t_bot, full_l, full_r))
    _frame.update(boxes=boxes, titles=titles, prog_row=ctrl, bar=(bar_l, max(0, full_r - 2 - bar_l + 1)),
                  side=side, beside=beside)

    for k, line in enumerate(hint_lines):
        emit(f"\033[{hint_top + k};1H\033[K{' ' * mh}{line}")
    compute_controls_hint_cells(hint_lines, hint_top)
    _render_frame_buffer(frame_buffer, rows)
    # The transport row is the controls' and the bar's, not the frame's (see _frame_borders).
    write_controls(ctrl, status)
    sys.stdout.flush()
    return ctrl, ctrl, lyric_row, cols, lyric_bot


def _draw_default_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                     is_paused: bool = False, volume: int = 100) -> tuple[int, int, int, int, int]:
    """Render the full playback screen (art, metadata, controls, and any active pane)
    for the current layout mode, returning the progress/control/lyric row positions and art bottom row."""
    cols, rows = size
    mode = _layout_mode(cols)
    # Wide is the split view; with nothing to put beside the art, a wide window
    # is laid out like a standard one, so the art doesn't halve at 120 columns.
    if mode == 'wide' and not (_ui_state['show_credits'] or _ui_state['show_lyrics'] or _ui_state['show_queue']):
        mode = 'standard'
    # Reset art geometry each frame; only branches that draw art repopulate it.
    # Likewise the queue's click rows: only a frame that draws the queue has any,
    # and the inline image: only a frame that lays out art has one.
    _queue_click_rows.clear()
    _inline_art['path'] = None
    geom.reset_frame()
    pc.screen_release()            # this frame reserves the lyrics' cells afresh
    pc.screen_forget_pictures()    # a picture the screen before left: written over in full
    cols_, rows_ = size
    _frame.update(icons_n=3)
    if rows_ <= _TINY_ROWS or cols_ < _TINY_COLS:
        return _draw_tiny(size, is_paused)

    # buf[0] is a clear sequence that _render_frame_buffer drops (see there).
    frame_buffer = ["\033[H\033[3J\033[J"]

    def emit(text):
        frame_buffer.append(text)

    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')
    has_cast = bool(cast_people or crew_people)
    has_lyrics = _ui_state['lyrics_pane'] and bool(audio.getall('SYLT') or audio.getall('USLT'))

    # Boxed under a tab bar or with a panel showing; clean, edge to edge, with neither.
    pane_on = (_ui_state['show_queue'] or (_ui_state['show_lyrics'] and has_lyrics)
               or (_ui_state['show_credits'] and has_cast))
    # Boxed whenever the tab bar shows or a panel does, at any size: only with
    # the bar hidden (f) and no panel is the player clean, edge to edge.
    boxed = bool(ui.tab_rows()) or bool(pane_on)
    _frame.update(boxed=boxed, boxes=[], titles={}, prog_row=None, icons_col=None, bar=None,
                  focus=None, panel_order=[], shape=None)
    if boxed:
        drawn = _draw_boxed_ui(file_path, audio, pre_art, size, is_paused, volume,
                               cast_people, crew_people, has_cast, has_lyrics)
        if drawn is not None:
            return drawn                                     # else no room for boxes: clean
    lyrics_on = has_lyrics and _ui_state['show_lyrics'] and not _ui_state['show_queue']
    above = ui.tab_rows()                                    # rows over the content

    row_cursor = 0

    if mode == 'wide':
        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))

        # Split view: left half = art + meta, right pane = credits/lyrics.
        art_w = min(cols // 2, ART_MAX_WIDTH)
        right_w = cols - art_w - _WIDE_SPLIT_GUTTER
        meta_val_w = right_w - 10

        left_col = _meta_left_lines(audio, file_path, meta_val_w)
        # Size the controls/hints first so the art leaves room for them: showing
        # help (several hint lines) shrinks the art instead of drawing over the controls.
        status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, has_lyrics=has_lyrics, has_credits=has_cast)
        shortcut_lines = shortcuts_ln.splitlines() or [""]
        avail_h = _art_room(rows - len(left_col) - len(shortcut_lines) - 4 - ui.MARGIN_V
                            - max(ui.MARGIN_V, above))
        # Art is inset from the panel edges so it floats with breathing room.
        art_inner_w = max(10, art_w * 3 // 4)
        art_lines = _centred_art(file_path, art_w, art_inner_w, avail_h, pre_art)

        art_vis_w = _art_w(art_lines)
        left_margin = max(ui.MARGIN_H, (art_w - art_vis_w) // 2)

        geom.art_width = art_vis_w
        geom.art_left = left_margin
        geom.art_height = len(art_lines)
        geom.right_left = art_w + _WIDE_SPLIT_GUTTER
        geom.right_width = right_w

        # The art sits at the top, under the top margin (the tab bar's row);
        # a taller window only adds space at the bottom (and to the pane beside it).
        top = max(ui.MARGIN_V if art_lines else 0, above)
        for _ in range(top):
            emit("")
        row_cursor += top
        geom.art_top = row_cursor + 1

        for line in art_lines:
            emit(" " * left_margin + line)
        row_cursor += len(art_lines)
        emit("")
        row_cursor += 1

        for line in left_col:
            vis = ui.visual_len(line)
            pad = ' ' * max(0, (art_w - vis) // 2)
            emit(f"{pad}{line}")
        row_cursor += len(left_col)

        emit("")
        prog_row = row_cursor + 2
        _frame['prog_row'] = prog_row                 # update_progress_ui's, not the frame's
        ctrl_row = prog_row + 1

        # Recompute now that the art geometry is set, so the transport line centres over the art.
        ctrl_row, shortcut_lines = _place_controls(
            emit, ctrl_row, rows, is_uslt_track, is_paused, volume, has_lyrics, has_cast)

        _pane_top = geom.art_top  # right pane aligns with art top after any vertical centring

        # Queue view takes over the whole right pane when toggled on.
        if _ui_state['show_queue']:
            _place_queue(emit, _pane_top, geom.right_left, right_w, (ctrl_row - 1) - _pane_top)
            lyric_row = ctrl_row  # suppress the lyric area while the queue shows
            art_bottom_row = ctrl_row - 1
            _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
            sys.stdout.flush()
            return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

        credits_lines = []
        if has_cast and _ui_state['show_credits']:
            cast_limit, crew_limit = 3, 3
            gap = ' ' * 4
            cast_col_w = max(12, (right_w - len(gap)) // 2)
            crew_col_w = max(12, right_w - cast_col_w - len(gap))

            cast_heading = [f"{C.DIM}PERFORMERS{C.RESET}"] if cast_people else []
            crew_heading = [f"{C.DIM}PRODUCTION TEAM{C.RESET}"] if crew_people else []
            cast_body = _build_cast_lines(cast_people, cast_col_w, limit=cast_limit) if cast_people else []
            crew_body = _build_crew_lines(crew_people, crew_col_w, cast_names=[n for _, n in cast_people[:cast_limit]], limit=crew_limit) if crew_people else []

            c_lines = cast_heading + cast_body
            cr_lines = crew_heading + crew_body
            for i in range(max(len(c_lines), len(cr_lines))):
                lft = c_lines[i] if i < len(c_lines) else ''
                rgt = cr_lines[i] if i < len(cr_lines) else ''
                pad_c = ' ' * max(0, cast_col_w - ui.visual_len(lft))
                credits_lines.append(f"  {lft}{pad_c}{gap}{rgt}")

        for idx, line in enumerate(credits_lines):
            emit(f"\033[{_pane_top + idx};{geom.right_left}H{line}")

        lyric_row = _pane_top
        if credits_lines:
            rule_row = _pane_top + len(credits_lines) + 1
            emit(f"\033[{rule_row};{geom.right_left}H{_credits_rule(right_w)}")
            lyric_row = rule_row + 2
        if art_lines:
            geom.lyric_centre = geom.art_top + (len(art_lines) - 1) // 2
        # Cap the right pane at the row above the transport controls so the
        # lyric-window clearing loop never touches the controls/hints rows.
        art_bottom_row = max(lyric_row + 2, ctrl_row - 1)
        if lyrics_on:                 # the lyrics' own cells: the frame leaves them be
            pc.screen_reserve(lyric_row, art_bottom_row, geom.right_left, geom.right_left + right_w - 1)

        _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

    else:
        geom.right_left = None
        geom.right_width = None

        meta_val_w = cols - 12
        left_col = _meta_left_lines(audio, file_path, meta_val_w)

        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))
        _, temp_shortcuts = _controls_line(is_uslt_track, is_paused, volume, width=cols, has_lyrics=has_lyrics, has_credits=has_cast)
        control_rows = 2 + len(temp_shortcuts.splitlines() or [""])

        credits_est = tune.PANE_CREDITS_EST_ROWS if (has_cast and _ui_state['show_credits']) else 0
        lyrics_est = tune.PANE_LYRICS_EST_ROWS if _ui_state['show_lyrics'] else 0
        queue_est = tune.PANE_QUEUE_MIN_ROWS if _ui_state['show_queue'] else 0
        tab = above
        room = _single_column_room(rows, len(left_col), control_rows,
                                   credits_est + lyrics_est + queue_est) - tab
        max_art_h = _art_room(room) if cols >= _MIN_ART_COLS else 0

        # Art the height holds back from full width gets the blank row above the
        # title too: a row taller, wider, and with room to make its sides even.
        art_lines = _art_width_for_height(file_path, cols, max_art_h, pre_art)[1]
        gap_used = int(bool(art_lines) and _art_w(art_lines) < cols)
        # Narrowed art leaves room beside it for the volume bar: both then keep
        # the wide layout's top margin rather than touching the window's edge.
        # The tab bar, when there is one, is that margin.
        top_margin = max(ui.MARGIN_V if gap_used else 0, tab)
        if gap_used:
            art_lines = _centred_art(file_path, cols, cols, _art_room(room + tab + 1 - top_margin), pre_art)
        geom.art_gap = not gap_used
        actual_art_w = _art_w(art_lines) or cols
        if actual_art_w < cols:
            # Art was narrowed to fit terminal height: centre it.
            art_lines = _align_art_lines(art_lines, cols)
            geom.art_left = max(0, (cols - actual_art_w) // 2)
            geom.art_width = actual_art_w
        else:
            geom.art_left = 0
            geom.art_width = cols if art_lines else 0

        geom.art_height = len(art_lines)

        # The art sits at the top: any rows it doesn't use (height rounding, a
        # window taller than full-width art needs, no art at all) are left at
        # the bottom, or go to a panel under the controls.
        for _ in range(top_margin):
            emit("")
        row_cursor += top_margin
        geom.art_top = row_cursor + 1

        for line in art_lines: emit(line)
        row_cursor += len(art_lines)
        if geom.art_gap:
            emit("")
            row_cursor += 1

        left_col = _center_lines(left_col, cols)
        for line in left_col: emit(line)
        row_cursor += len(left_col)

        # The progress row stays out of the frame: update_progress_ui owns it,
        # and a frame that painted it blank made the bar flicker on each redraw.
        row_cursor += 1
        prog_row = row_cursor
        _frame['prog_row'] = prog_row
        ctrl_row = prog_row + 1

        ctrl_row, shortcut_lines = _place_controls(
            emit, ctrl_row, rows, is_uslt_track, is_paused, volume, has_lyrics, has_cast)

        ctrl_row_end = ctrl_row + len(shortcut_lines)
        p0 = ctrl_row_end + 2                                  # the panel's first row

        if _ui_state['show_queue']:
            q_start = p0
            _place_queue(emit, q_start, 1 + ui.MARGIN_H, cols - ui.MARGIN_H,
                         rows - ui.MARGIN_V - q_start + 1)     # down to the last row, inclusive
            lyric_row = rows - ui.MARGIN_V
            art_bottom_row = rows - ui.MARGIN_V
            _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
            sys.stdout.flush()
            return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

        c_lines = []
        cr_lines = []

        if has_cast and _ui_state['show_credits']:
            cast_limit, crew_limit = 3, 3
            gap = '  '
            col_w = max(12, (cols - len(gap)) // 2)
            c_lines = [f"{C.DIM}PERFORMERS{C.RESET}"] + _build_cast_lines(cast_people, col_w, limit=cast_limit)
            cr_lines = [f"{C.DIM}PRODUCTION TEAM{C.RESET}"] + _build_crew_lines(crew_people, col_w, cast_names=[n for _, n in cast_people[:cast_limit]], limit=crew_limit)

            for i in range(max(len(c_lines), len(cr_lines))):
                lft = c_lines[i] if i < len(c_lines) else ''
                rgt = cr_lines[i] if i < len(cr_lines) else ''
                pad = ' ' * max(0, col_w - ui.visual_len(lft))
                emit(f"\033[{p0 + i};1H\033[K{lft}{pad}{gap}{rgt}")

        lyric_row = ctrl_row_end + 3
        n_credits = max(len(c_lines), len(cr_lines))
        if c_lines or cr_lines:
            rule_row = ctrl_row_end + 3 + n_credits
            emit(f"\033[{rule_row};1H\033[K{_credits_rule(cols)}")
            lyric_row = rule_row + 2
        inset = max(ui.MARGIN_H, int(cols * _LYRICS_INSET_FRAC))
        geom.lyric_left = inset + 1
        geom.lyric_width = cols - inset - ui.MARGIN_H
        if lyrics_on:                 # the lyrics' own cells: the frame leaves them be
            pc.screen_reserve(lyric_row, rows - ui.MARGIN_V, geom.lyric_left,
                              geom.lyric_left + geom.lyric_width - 1)
        art_bottom_row = rows - ui.MARGIN_V
        _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row
