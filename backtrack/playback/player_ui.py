"""The player screen: layout, metadata and credits, transport controls, volume bar,
and the diffed frame painter."""
from __future__ import annotations
import re
import sys

from backtrack.music_library import format_value_list, track_title, first_text
from backbone.prompt import core as pc
from backbone import keys, ui
from backtrack.playback.player_geom import geom
from backbone import numbering
from backbone.prompt.core import _hint
from backbone.prompt.core import add_hint_click_cells
from backbone.ui import Colors as C
from backtrack import tuning as tune
from backbone.log import log
from backtrack.playback.queue_pane import (  # noqa: F401 (re-exported)
    _place_queue, _queue_click_rows, has_queue, move_queue_cursor, queue_click_index, queue_cursor,
    set_queue_context, set_queue_cursor,
)
from backtrack.playback.player_art import (  # noqa: F401 (re-exported)
    ART_MAX_WIDTH, _art_width_for_height, _draw_inline_art, _inline_art, art_image_incomplete, inline_art_enabled, redraw_art_image, set_resizing,
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
    ("near_end", ("e", "E"), "jump to near the end (Diagnostics)"),
    ("prev", ("[",), "previous track"),
    ("next", ("]",), "next track"),
    ("vol_up", ("+", "="), "volume up"),
    ("vol_down", ("-", "_"), "volume down"),
    ("meta", ("m", "M"), "show or hide the details"),
    ("panel", ("w", "W"), "cycle the side panel"),
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
    touched = set(flow) | set(overlay)
    touched |= {r for r in range(row + 1, _player_prev_rows[0] + 1) if r <= rows}
    touched |= {r for r in _player_overlay_rows[0] if r not in overlay and r <= rows}
    # The player owns the whole screen, so anything the painter still remembers
    # from the screen before it (the menu's now-playing box, a taller list) is
    # blanked here. There is no full clear per frame, so without this a first
    # frame shorter than the previous screen would leave its bottom rows behind.
    touched |= {r for r in pc.screen_rows() if r <= rows and r not in touched}
    _player_prev_rows[0] = row
    _player_overlay_rows[0] = set(overlay)

    out = "".join(
        pc.screen_row_paint(r, flow.get(r, ""), "".join(overlay.get(r, ())))
        for r in sorted(touched)
    )
    if out:
        sys.stdout.write(out)
        _draw_inline_art()      # rows were rewritten, which may have cut into it

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
    'debug': False,
    'show_credits': False,
    'show_lyrics': False,
    'show_queue': False,
    # Single-key cycle: off → lyrics → queue → lyrics+credits (credits alone when
    # the track has no lyrics).
    'pane_mode': 'off',
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



def update_progress_ui(row: int, elapsed: float, duration: float, width: int) -> None:
    """Update the default progress bar display."""
    elapsed_str = ui.format_time(int(elapsed))
    duration_str = ui.format_time(int(duration))
    # The elapsed time is padded to the duration's width so the bar holds its
    # length for the whole track, an hour-long one included.
    timer_text = f"  {elapsed_str.rjust(len(duration_str))} / {duration_str}"

    if geom.art_width and geom.art_width > 0:
        container_w = geom.art_width
        left_pad = geom.art_left if geom.art_left is not None else 0
    else:
        container_w = width
        left_pad = 0
    # Inset by the side margin at both ends, so the row never meets the edge.
    container_w -= 2 * ui.MARGIN_H
    left_pad += ui.MARGIN_H

    bar_width = max(1, container_w - len(timer_text) - 2)
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    bar = ui.get_progress_bar(percent, bar_width)
    pad = ' ' * left_pad

    # Remember where the bar landed so a click on it can be mapped back to a
    # position: get_progress_bar brackets the cells, so cell 0 sits one column
    # past the pad's '[' cap.
    geom.prog_row, geom.prog_col, geom.prog_w = row, left_pad + 2, bar_width

    sys.stdout.write(f"\033[{row};1H\033[K{pad}{bar}{timer_text}")
    pc.screen_forget_rows(row, row)     # painted outside the frame: model unknown
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
def _volume_bar_geometry() -> tuple[int, int, int] | None:
    """Return (column, top_row, height) for the volume bar, or None if there
    is no rendered artwork or no horizontal room to the right of it."""
    if not (geom.vol_bar_col and geom.art_top and geom.art_height):
        return None
    if geom.art_height < 3:
        return None
    cols = ui.get_terminal_width()
    if geom.vol_bar_col < 1 or geom.vol_bar_col + 1 > cols:
        return None
    return geom.vol_bar_col, geom.art_top, geom.art_height


def _volume_bar_cells(volume: int) -> list[str]:
    """Build absolute-positioned cells for a pretty full-height vertical volume
    bar sitting just right of the album art. Fills bottom-up; the boundary cell
    uses a fractional block. A small percentage label sits beneath it."""
    geo = _volume_bar_geometry()
    if geo is None:
        return []
    bar_col, top, height = geo

    # Keep the bar's bottom in line with the art's bottom even when the layout is
    # tight: the art's flow lines are clipped at `rows - MARGIN_V`, but these
    # absolute cells aren't, so clamp the bar to the same limit instead of
    # letting it hang below a shortened art.
    rows = ui.get_terminal_size()[1]
    visible = (rows - ui.MARGIN_V) - top + 1
    if visible < height:
        height = visible
    if not geom.art_gap:
        height -= 1             # no blank row under the art: the label takes its last
    if height < 3:
        return []

    pct = max(0.0, min(100.0, float(volume))) / 100.0
    # Whole-cell fill (rounded to the nearest row). A sub-cell partial block leaves
    # the top of the boundary cell as empty background, reading as a gap between
    # the fill and the tube, so every cell is either solid fill or tube.
    filled = int(round(pct * height))

    cells: list[str] = []
    for d in range(height):  # d = distance from the bottom (0 = bottom row)
        row = top + (height - 1 - d)
        if d < filled:
            glyph = f"{C.DIM}█{C.RESET}"            # filled level: dim, not bright
        else:
            glyph = f"{C.DIM}░{C.RESET}"            # unused section: hollow "tube"
        cells.append(f"\033[{row};{bar_col}H{glyph}")

    # Speaker glyph at the top; percentage just below the bar in a fixed 3-wide
    # field so shorter values (100 → 90 → 0) fully overwrite the previous one.
    cells.append(f"\033[{top};{bar_col}H{C.DIM}♪{C.RESET}")
    label_col = max(1, bar_col - 1)
    # Label from the clamped percentage the bar itself was drawn from: printing
    # the raw value would show VLC's "-1" under an empty tube.
    cells.append(f"\033[{top + height};{label_col}H{C.DIM}{int(round(pct * 100)):>3}{C.RESET}")
    return cells


def draw_volume_bar(volume: int) -> None:
    """Live-redraw just the vertical volume bar (called on +/- volume changes)."""
    cells = _volume_bar_cells(volume)
    if cells:
        sys.stdout.write("\0337" + "".join(cells) + "\0338")
        sys.stdout.flush()
        geo = _volume_bar_geometry()
        if geo:                          # those rows now carry glyphs we didn't
            _, top, height = geo         # record, so let the next frame repaint them
            pc.screen_forget_rows(top, top + height)



def toggle_metadata() -> None:
    """Show or hide the track details line, and remember the choice."""
    _ui_state['show_metadata'] = not _ui_state['show_metadata']
    try:
        from backtrack.config import update_config
        update_config({'player_show_metadata': _ui_state['show_metadata']})
    except Exception as exc:
        log.warning("couldn't save player_show_metadata: %s", exc)
    refresh_player_settings()
def toggle_help() -> None:
    """Show or hide the hint bar: the app-wide switch, so every screen follows."""
    pc.toggle_hints()
def _set_pane_mode(mode: str) -> None:
    """Set the right-pane mode and sync the show_lyrics/show_credits/show_queue flags to match it."""
    _ui_state['pane_mode'] = mode
    _ui_state['show_lyrics'] = mode in ('lyrics', 'lyrics+credits')
    _ui_state['show_credits'] = mode in ('credits', 'lyrics+credits')
    _ui_state['show_queue'] = mode == 'queue'
    if mode != 'queue':
        set_queue_cursor(None)                   # a reopened panel starts on the current track


def cycle_right_pane(has_lyrics: bool = True, has_credits: bool = True,
                     has_queue_flag: bool = True) -> bool:
    """Advance the right column through the available views with a single key:
    off → lyrics → queue → lyrics+credits → off (states with no content are skipped).
    False when there is nothing to switch to, so the caller need not redraw."""
    states = ['off']
    if has_lyrics:
        states.append('lyrics')
    if has_queue_flag:
        states.append('queue')
    if has_lyrics and has_credits:
        states.append('lyrics+credits')
    elif has_credits:
        states.append('credits')

    cur = _ui_state.get('pane_mode', 'off')
    if cur not in states:
        cur = 'off'
    new = states[(states.index(cur) + 1) % len(states)]
    if new == _ui_state.get('pane_mode', 'off'):
        return False
    _set_pane_mode(new)
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
        _ui_state['nerd_icons'] = bool(setting(cfg, 'player_nerd_font_icons'))
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


def _controls_line(is_uslt: bool, is_paused: bool, volume: int,
                   width: int | None = None,
                   has_lyrics: bool = True, has_credits: bool = True) -> tuple[str, str]:
    """Build the centred transport-controls line and the shortcuts/help hint line below it."""
    prev, play, pause, nxt = _TRANSPORT_ICONS[_ui_state['nerd_icons']]
    pp_icon = play if is_paused else pause
    icons = (prev, pp_icon, nxt)
    # Nerd Font icons sit centred in their cells, so even gaps. The emoji-font
    # glyphs draw left of their cells; the wider gap before play keeps them even.
    controls = f"{prev}   {pp_icon}   {nxt}" if _ui_state['nerd_icons'] else f"{prev}   {pp_icon}  {nxt}"

    if width:
        art_left = geom.art_left or 0
        left_pad = art_left + max(0, (width - ui.visual_len(controls)) // 2)
    elif geom.art_width:
        art_left = geom.art_left or 0
        left_pad = art_left + max(0, (geom.art_width - ui.visual_len(controls)) // 2)
    else:
        cols = ui.get_terminal_size()[0]
        left_pad = max(0, (cols - ui.visual_len(controls)) // 2)

    status = " " * left_pad + controls
    _record_transport_cols(status, icons)

    if not pc.hints_visible():
        # Hints are off app-wide, but the player keeps its way back to them.
        pairs = [(keys.label('global.help'), 'help')]
        _set_controls_hint_pairs(pairs)
        return status, _hint(*pairs, always=True)

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
    if has_lyrics or has_credits or has_queue():
        hint_args.append((L('player.panel'), 'panel'))
    if _ui_state.get('show_queue'):
        Q = lambda *n, **kw: L(*(f'player_queue.{x}' for x in n), **kw)  # noqa: E731
        hint_args += [(Q('up', 'down'), 'queue'), (Q('play'), 'play it'), (Q('move_up', 'move_down'), 'move'),
                      (Q('remove', most=1), 'remove'), (Q('shuffle'), 'shuffle'), (Q('clear'), 'clear'),
                      (Q('undo'), 'undo')]
    hint_args += [(L('global.help'), 'hide help'), (L('player.prev', 'player.next'), 'prev/next'),
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


def volume_from_click(row: int, col: int) -> int | None:
    """If (row, col) lands on the vertical volume bar, return the volume (0-100)
    for that height (top = 100, bottom = 0); else None."""
    geo = _volume_bar_geometry()
    if geo is None:
        return None
    bar_col, top, height = geo
    bottom = top + height - 1
    if not (top <= row <= bottom) or not (bar_col - 1 <= col <= bar_col + 1):
        return None
    if height <= 1:
        return 100
    frac = (bottom - row) / (height - 1)
    return int(round(max(0.0, min(1.0, frac)) * 100))


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

    title = _txt('TIT2') or track_title(file_path)
    artist = _txts('TPE1') or _txts('TPE2')
    album = _txt('TALB')

    lines: list[str] = []
    if title:
        lines.append(f"{C.BOLD}{_trim(title)}{C.RESET}")

    # Album and artist ALWAYS show; the 'm' toggle only governs the extras below.
    if artist and album:
        second = f"{artist} - {album}"
    else:
        second = artist or album
    if second:
        lines.append(f"{C.DIM}{_trim(second)}{C.RESET}")

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

    # buf[0] is a clear sequence that _render_frame_buffer drops (see there).
    frame_buffer = ["\033[H\033[3J\033[J"]

    def emit(text):
        frame_buffer.append(text)

    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')
    has_cast = bool(cast_people or crew_people)
    has_lyrics = _ui_state['lyrics_pane'] and bool(audio.getall('SYLT') or audio.getall('USLT'))

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
        avail_h = _art_room(rows - len(left_col) - len(shortcut_lines) - 4 - 2 * ui.MARGIN_V)
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
        geom.vol_bar_col = left_margin + art_vis_w + 2

        if art_lines:
            # The art sits at the top; a taller window only adds space at the
            # bottom (and to the pane beside it).
            for _ in range(ui.MARGIN_V):
                emit("")
            row_cursor += ui.MARGIN_V
        geom.art_top = row_cursor + 1

        for line in art_lines:
            emit(" " * left_margin + line)
        row_cursor += len(art_lines)
        emit("")
        row_cursor += 1
        # Volume cells after the spacing blank, so its erase can't wipe the label.
        for cell in _volume_bar_cells(volume):
            emit(cell)

        for line in left_col:
            vis = ui.visual_len(line)
            pad = ' ' * max(0, (art_w - vis) // 2)
            emit(f"{pad}{line}")
        row_cursor += len(left_col)

        emit("")
        prog_row = row_cursor + 2
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
        room = _single_column_room(rows, len(left_col), control_rows, credits_est + lyrics_est + queue_est)
        max_art_h = _art_room(room) if cols >= _MIN_ART_COLS else 0

        # Art the height holds back from full width gets the blank row above the
        # title too: a row taller, wider, and with room to make its sides even.
        art_lines = _art_width_for_height(file_path, cols, max_art_h, pre_art)[1]
        gap_used = int(bool(art_lines) and _art_w(art_lines) < cols)
        if gap_used:
            art_lines = _centred_art(file_path, cols, cols, _art_room(room + 1), pre_art)
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
        geom.vol_bar_col = (geom.art_left or 0) + (geom.art_width or 0) + 2

        # The art sits at the top: any rows it doesn't use (height rounding, a
        # window taller than full-width art needs, no art at all) are left at
        # the bottom, or go to a panel under the controls.
        geom.art_top = row_cursor + 1

        for line in art_lines: emit(line)
        row_cursor += len(art_lines)
        if geom.art_gap:
            emit("")
            row_cursor += 1
        # Volume cells after the spacing blank, so its erase can't wipe the label.
        for cell in _volume_bar_cells(volume):
            emit(cell)

        left_col = _center_lines(left_col, cols)
        for line in left_col: emit(line)
        row_cursor += len(left_col)

        # The progress row stays out of the frame: update_progress_ui owns it,
        # and a frame that painted it blank made the bar flicker on each redraw.
        row_cursor += 1
        prog_row = row_cursor
        ctrl_row = prog_row + 1

        ctrl_row, shortcut_lines = _place_controls(
            emit, ctrl_row, rows, is_uslt_track, is_paused, volume, has_lyrics, has_cast)

        ctrl_row_end = ctrl_row + len(shortcut_lines)

        if _ui_state['show_queue']:
            q_start = ctrl_row_end + 2
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
                emit(f"\033[{ctrl_row_end + 2 + i};1H\033[K{lft}{pad}{gap}{rgt}")

        lyric_row = ctrl_row_end + 3
        if c_lines or cr_lines:
            rule_row = ctrl_row_end + 3 + max(len(c_lines), len(cr_lines))
            emit(f"\033[{rule_row};1H\033[K{_credits_rule(cols)}")
            lyric_row = rule_row + 2
        inset = max(ui.MARGIN_H, int(cols * _LYRICS_INSET_FRAC))
        geom.lyric_left = inset + 1
        geom.lyric_width = cols - inset - ui.MARGIN_H
        art_bottom_row = rows - ui.MARGIN_V
        _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row
