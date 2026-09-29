"""Playback UI rendering: album art, metadata, credits, volume bar, lyric panes."""
from __future__ import annotations
import os
import re
import sys

from src.music_library import format_value_list
from src.utils import prompt_core as pc
from src.utils import ui_utils
from src.playback.player_geom import geom
from src.utils import numbering
from src.utils.prompt_core import _hint
from src.utils.prompt_core import add_hint_click_cells
from src.utils.ui_utils import Colors as C
from src import tuning as tune
from src.utils.log import log
from src.playback.queue_pane import (  # noqa: F401 — re-exported
    _place_queue, _queue_click_rows, has_queue, queue_click_index, set_queue_context,
)
from src.playback.player_art import (  # noqa: F401 — re-exported
    ART_MAX_WIDTH, _art_width_for_height, _draw_inline_art, _inline_art, art_image_incomplete, inline_art_enabled, redraw_art_image, set_resizing,
)
from src.config import setting


# Absolute cursor positioning (\033[<row>;<col>H) — must require the trailing
# 'H' so it does NOT also match a 24-bit colour prefix like \033[38;2;r;g;bm,
# which every half-block art line starts with. Matching those made the renderer
# treat art lines as absolute (row-less), so metadata flowed to the top and drew
# ABOVE the art in standard/minimal layouts.
_ABS_ROW_RE = re.compile(r'^\033\[\d+;\d+H')

_WIDE_SPLIT_GUTTER = 3


_player_prev_rows = [0]          # flow-row count of the previous player frame
_player_overlay_rows = [set()]   # rows the previous frame overlaid


def _render_frame_buffer(buf: list, rows: int) -> None:
    """Flush the assembled frame, writing only the rows whose content changed.

    `buf[0]` is a clear sequence, deliberately **dropped**: erasing the whole
    screen every frame made the player flicker on every progress tick, and with a
    diffed paint it isn't needed — rows the frame stops using are blanked
    explicitly, and `ui_utils.clear_screen()` (entry, resize, exit) already drops
    the painter's model so the next frame repaints in full.

    `row` counts only flow (relative) lines; absolute-positioned items (the
    volume bar, controls, lyrics) pass through WITHOUT consuming a row slot —
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
            # flow line) also occupies — the volume bar sits to the right of the
            # art, on the art's own rows. It is layered *over* that row rather
            # than replacing it; dropping the row's flow content blanked the art.
            overlay.setdefault(int(m.group(0)[2:-1].split(';')[0]), []).append(item)
        else:
            row += 1
            if row <= rows:
                flow[row] = item

    # Every row this frame could need to touch: its own content, plus rows a
    # taller previous frame used, plus rows whose overlay has now gone away.
    # Blank counts as content — a row left out of this frame must be *erased*,
    # not left showing the previous screen.
    touched = set(flow) | set(overlay)
    touched |= {r for r in range(row + 1, _player_prev_rows[0] + 1) if r <= rows}
    touched |= {r for r in _player_overlay_rows[0] if r not in overlay and r <= rows}
    # The player owns the whole screen, so anything the painter still remembers
    # from the screen before it — the menu's miniplayer box, a taller list — is
    # blanked here. Entry used to rely on a full clear per frame; without one, a
    # first frame shorter than the previous screen left its bottom rows behind.
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
    'pane_mode': 'off',   # off → lyrics → queue → lyrics+credits (single-key cycle)
    # False in a joined window's player view: lyrics are only painted by the
    # window playing the audio, so there the panel offers credits and the queue.
    'lyrics_pane': True,
}
# Clickable-control geometry (set by _controls_line / the draw): transport-icon
# columns on the controls row, the active hint pairs, and the hint-glyph cell map.
_last_transport_cols: dict[str, int] = {}
_last_controls_hint_pairs: list = []
_last_hint_cells: dict[tuple[int, int], str] = {}


def _layout_mode(cols: int) -> str:
    """Classify terminal width into a layout mode: wide, standard, or minimal."""
    if cols >= 120:
        return 'wide'
    if cols >= 60:
        return 'standard'
    return 'minimal'



def update_progress_ui(row: int, elapsed: float, duration: float, width: int) -> None:
    """Update the default progress bar display."""
    elapsed_str = ui_utils.format_time(int(elapsed))
    duration_str = ui_utils.format_time(int(duration))
    timer_text = f" {elapsed_str.rjust(5)} / {duration_str.ljust(5)} "


    if geom.art_width and geom.art_width > 0:
        container_w = geom.art_width
        left_pad = geom.art_left if geom.art_left is not None else 0
    else:
        container_w = width
        left_pad = 0

    bar_width = max(1, container_w - len(timer_text) - 2)
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    bar = ui_utils.get_progress_bar(percent, bar_width)
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
    cols = ui_utils.get_terminal_width()
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
    # absolute cells aren't — so clamp the bar to the same limit instead of
    # letting it hang below a shortened art.
    rows = ui_utils.get_terminal_size()[1]
    visible = (rows - ui_utils.MARGIN_V) - top + 1
    if visible < height:
        height = visible
    if height < 3:
        return []

    pct = max(0.0, min(100.0, float(volume))) / 100.0
    # Whole-cell fill (rounded to the nearest row). A sub-cell partial block left
    # the top of the boundary cell as empty background — reading as a gap between
    # the fill and the tube — so every cell is now either solid fill or tube.
    filled = int(round(pct * height))

    cells: list[str] = []
    for d in range(height):  # d = distance from the bottom (0 = bottom row)
        row = top + (height - 1 - d)
        if d < filled:
            glyph = f"{C.DIM}█{C.RESET}"            # filled level — dim, not bright
        else:
            glyph = f"{C.DIM}░{C.RESET}"            # unused section — hollow "tube"
        cells.append(f"\033[{row};{bar_col}H{glyph}")

    # Speaker glyph at the top; percentage just below the bar in a fixed 3-wide
    # field so shorter values (100 → 90 → 0) fully overwrite the previous one.
    cells.append(f"\033[{top};{bar_col}H{C.DIM}♪{C.RESET}")
    label_col = max(1, bar_col - 1)
    # Label from the clamped percentage the bar itself was drawn from — printing
    # the raw value showed VLC's "-1" under an empty tube.
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
            _, top, height = geo         # record — let the next frame repaint them
            pc.screen_forget_rows(top, top + height)



def toggle_metadata() -> None:
    """Show or hide the track details line, and remember the choice."""
    _ui_state['show_metadata'] = not _ui_state['show_metadata']
    try:
        from src.config import update_config
        update_config({'player_show_metadata': _ui_state['show_metadata']})
    except Exception as exc:
        log.warning("couldn't save player_show_metadata: %s", exc)
    refresh_player_settings()
def toggle_help() -> None:
    """Show or hide the hint bar — the app-wide switch, so every screen follows."""
    pc.toggle_hints()
def _set_pane_mode(mode: str) -> None:
    """Set the right-pane mode and sync the show_lyrics/show_credits/show_queue flags to match it."""
    _ui_state['pane_mode'] = mode
    _ui_state['show_lyrics'] = mode in ('lyrics', 'lyrics+credits')
    _ui_state['show_credits'] = mode in ('credits', 'lyrics+credits')
    _ui_state['show_queue'] = mode == 'queue'


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
    """Re-read the player's settings (`debug`, `player_show_metadata`) into
    `_ui_state`.

    Called when a track loads and when the metadata panel is toggled — both rare,
    both moments where the answer could have changed — rather than on every draw,
    which would re-read the file for a value that almost never moves.
    """
    try:
        from src.config import load_config
        cfg = load_config()
        _ui_state['debug'] = bool(setting(cfg, 'debug'))
        _ui_state['show_metadata'] = bool(setting(cfg, 'player_show_metadata'))
    except Exception:
        _ui_state['debug'] = False


def set_lyric_sources(track: str, files: list[str], estimated: bool = False) -> None:
    """Register the files the lyric layer actually opened for this track.

    Set by the player from what the load RESOLVED, never from what it hoped to
    find, so the 'm' panel names the document being read rather than the one that
    ought to be there — the difference between the two is the whole bug class this
    exists to make visible.

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


def _controls_line(is_uslt: bool, is_paused: bool, volume: int, toast: str,
                   width: int | None = None,
                   has_lyrics: bool = True, has_credits: bool = True) -> tuple[str, str]:
    """Build the centered transport-controls line and the shortcuts/help hint line below it."""
    pp_icon = "⏵" if is_paused else "⏸"
    transport_icons = ["⏮ ", pp_icon, "⏭"]
    controls = "  ".join(transport_icons)

    if width:
        art_left = geom.art_left or 0
        left_pad = art_left + max(0, (width - ui_utils.visual_len(controls)) // 2)
    elif geom.art_width:
        art_left = geom.art_left or 0
        left_pad = art_left + max(0, (geom.art_width - ui_utils.visual_len(controls)) // 2)
    else:
        cols = ui_utils.get_terminal_size()[0]
        left_pad = max(0, (cols - ui_utils.visual_len(controls)) // 2)

    status = " " * left_pad + controls
    _record_transport_cols(status)
    if toast:
        # The feedback line for the last action (seek, volume, warnings): after
        # the controls, which keep their place, and cut at the window edge.
        status = ui_utils.clip_ansi(f"{status}   {C.DIM}{toast}{C.RESET}",
                                    ui_utils.get_terminal_size()[0] - ui_utils.MARGIN_H)

    if not pc.hints_visible():
        # Hints are off app-wide, but the player keeps its way back to them.
        pairs = [('i', 'help')]
        _set_controls_hint_pairs(pairs)
        return status, _hint(*pairs, always=True)

    hint_args = [
        ('space/p', 'play/pause'),
        ('←→', '±5s'),
        ('j/l', '±1s'),
        (',/.', '±30s'),
    ]
    if _ui_state['debug']:                       # Diagnostics: checking end credits
        hint_args.append(('e', f'last {tune.NEAR_END_JUMP_S}s'))
    hint_args += [('+/-', 'volume'), ('m', 'meta')]
    if has_lyrics or has_credits or has_queue():
        hint_args.append(('w', 'panel'))
    hint_args += [('i', 'hide help'), ('[/]', 'prev/next'), ('s', 'stop'), ('b', 'back'), ('q', 'quit')]

    _set_controls_hint_pairs(hint_args)
    return status, _hint(*hint_args)


def _record_transport_cols(status: str) -> None:
    """Store the 1-based columns of the ⏮ / ⏸⏵ / ⏭ glyphs on the controls row so
    clicks on them can be mapped back to prev / play-pause / next."""
    global _last_transport_cols
    pv = status.find('⏮')
    pp = status.find('⏸')
    if pp < 0:
        pp = status.find('⏵')
    nx = status.find('⏭')
    cols: dict[str, int] = {}
    if pv >= 0: cols['prev'] = pv + 1
    if pp >= 0: cols['playpause'] = pp + 1
    if nx >= 0: cols['next'] = nx + 1
    _last_transport_cols = cols


def _set_controls_hint_pairs(pairs: list) -> None:
    """Remember the hint pairs currently shown under the controls, for hit-testing."""
    global _last_controls_hint_pairs
    _last_controls_hint_pairs = list(pairs)


def _place_controls(log, ctrl_row: int, rows: int, is_uslt: bool, is_paused: bool,
                    volume: int, toast: str, has_lyrics: bool, has_credits: bool) -> tuple[int, list[str]]:
    """Draw the transport line at `ctrl_row` (pulled up so its hints still fit on
    screen) with the hint lines under it, and record their click cells. Returns
    the row it used and the hint lines."""
    status_ln, shortcuts_ln = _controls_line(is_uslt, is_paused, volume, toast,
                                             has_lyrics=has_lyrics, has_credits=has_credits)
    shortcut_lines = shortcuts_ln.splitlines() or [""]
    ctrl_row = min(ctrl_row, rows - len(shortcut_lines) - 1)
    log(f"\033[{ctrl_row};1H\033[K{status_ln}")
    for offset, line in enumerate(shortcut_lines, start=1):
        log(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui_utils.MARGIN_H}{line}")
    compute_controls_hint_cells(shortcut_lines, ctrl_row + 1)
    return ctrl_row, shortcut_lines


def compute_controls_hint_cells(shortcut_lines: list[str], first_row: int) -> None:
    """Populate ``_last_hint_cells`` for hint lines drawn at ``first_row`` onward
    (each rendered with a MARGIN_H left inset, like the draw does)."""
    _last_hint_cells.clear()
    if not _last_controls_hint_pairs:
        return
    for k, line in enumerate(shortcut_lines):
        add_hint_click_cells(_last_hint_cells, ' ' * ui_utils.MARGIN_H + line,
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
    """If (row, col) lands on the vertical volume bar, return the volume (0–100)
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
    the track that column represents (0.0–1.0); else None. The '[' and ']' caps
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
    """A movement number as a Roman numeral — the convention for classical
    movements. Non-numeric (or unnumbered) values pass through unchanged.
    """
    return numbering.roman(s) or s


def _meta_left_lines(audio, file_path: str, max_val_w: int) -> list[str]:
    """Build the left-column metadata lines (title, artist/album, and optional
    extras like year/genre/track/disc) shown beside the album art."""
    def _trim(text: str) -> str:
        """Truncate text to max_val_w with an ellipsis."""
        return ui_utils.truncate_text(text, max(1, max_val_w), placeholder='…')

    def _txt(frame_id: str) -> str:
        """Return a text frame's stripped value, or empty string if absent."""
        fr = audio.get(frame_id)
        return str(fr.text[0]).strip() if (fr is not None and getattr(fr, 'text', None)) else ""

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

    title = _txt('TIT2') or os.path.splitext(os.path.basename(file_path))[0]
    artist = _txts('TPE1') or _txts('TPE2')
    album = _txt('TALB')

    lines: list[str] = []
    if title:
        lines.append(f"{C.BOLD}{_trim(title)}{C.RESET}")

    # Album and artist ALWAYS show; the 'm' toggle only governs the extras below.
    if artist and album:
        second = f"{artist} — {album}"
    else:
        second = artist or album
    if second:
        lines.append(f"{C.DIM}{_trim(second)}{C.RESET}")

    if _ui_state['show_metadata']:
        details: list[str] = []

        # Year: v2.4 uses TDRC; v2.3 (which we save) uses TYER; fall back to
        # original-release frames so the year never silently disappears.
        year_src = _txt('TDRC') or _txt('TYER') or _txt('TDOR') or _txt('TORY')
        year_match = re.search(r'\b\d{4}\b', year_src)
        if year_match:
            details.append(year_match.group(0))
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
        # track number would only repeat it — show one or the other, never both.
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
        fp = ui_utils.truncate_text(file_path, max(1, max_val_w), placeholder='…', front=True)
        lines.append(f"{C.DIM}{fp}{C.RESET}")

    return lines


def _align_art_lines(art_lines: list[str], cols: int) -> list[str]:
    """Center art_lines horizontally within cols, padding every line to a uniform width."""
    if not art_lines:
        return []
    art_width = max(ui_utils.visual_len(line) for line in art_lines)
    left_pad = max(0, (cols - art_width) // 2)
    aligned = []
    for line in art_lines:
        extra_padding = max(0, art_width - ui_utils.visual_len(line))
        aligned.append(" " * left_pad + line + " " * extra_padding)
    return aligned


def _center_lines(lines: list[str], cols: int) -> list[str]:
    """Center each line horizontally within cols based on its visible length."""
    if not lines:
        return []
    centered: list[str] = []
    for line in lines:
        vis = ui_utils.visual_len(line)
        left = max(0, (cols - vis) // 2)
        centered.append(" " * left + line)
    return centered


def draw_full_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                 is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple[int, int, int, int, int]:
    """Hide the cursor and draw the default playback UI layout."""
    sys.stdout.write(f"{C.HIDE}")
    return _draw_default_ui(file_path, audio, pre_art, size, is_paused, volume, toast)


def _draw_default_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                     is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple[int, int, int, int, int]:
    """Render the full playback screen (art, metadata, controls, and any active pane)
    for the current layout mode, returning the progress/control/lyric row positions and art bottom row."""
    cols, rows = size
    mode = _layout_mode(cols)
    # Reset art geometry each frame; only branches that draw art repopulate it.
    # Likewise the queue's click rows: only a frame that draws the queue has any,
    # and the inline image: only a frame that lays out art has one.
    _queue_click_rows.clear()
    _inline_art['path'] = None
    geom.reset_frame()

    # 1. Clear terminal — home first (no scroll), erase saved lines, erase to end.
    frame_buffer = ["\033[H\033[3J\033[J"]

    def log(text):
        frame_buffer.append(text)

    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')
    has_cast = bool(cast_people or crew_people)
    has_lyrics = _ui_state['lyrics_pane'] and bool(audio.getall('SYLT') or audio.getall('USLT'))

    row_cursor = 0

    if mode == 'wide':
        show_pane = _ui_state['show_credits'] or _ui_state['show_lyrics'] or _ui_state['show_queue']
        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))

        if not show_pane:
            # No right pane: centred single-column layout with breathing room on sides.
            content_w_max = min(ART_MAX_WIDTH, max(54, cols // 2), cols - 2 * ui_utils.MARGIN_H)
            left_col = _meta_left_lines(audio, file_path, content_w_max - 4)
            # Size the controls/hints FIRST so the art height reserves room for
            # them. Otherwise toggling help (multi-line hints) draws over the
            # transport controls instead of shrinking the art and shifting the
            # controls up to make space.
            status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, toast, has_lyrics=has_lyrics, has_credits=has_cast)
            shortcut_lines = shortcuts_ln.splitlines() or [""]
            avail_h = max(3, rows - len(left_col) - len(shortcut_lines) - 5 - 2 * ui_utils.MARGIN_V)

            art_str, art_lines = _art_width_for_height(file_path, content_w_max, avail_h, pre_art)
            actual_art_w = max((ui_utils.visual_len(l) for l in art_lines), default=content_w_max) if art_lines else content_w_max
            left_margin = max(ui_utils.MARGIN_H, (cols - actual_art_w) // 2)

            geom.art_left = left_margin
            geom.art_width = actual_art_w
            geom.art_height = len(art_lines)
            geom.vol_bar_col = left_margin + actual_art_w + 2
            geom.right_left = None
            geom.right_width = None

            if art_lines:
                top_pad = max(ui_utils.MARGIN_V, (rows - len(art_lines) - 1 - len(left_col) - len(shortcut_lines) - 5) // 2)
                for _ in range(top_pad):
                    log("")
                row_cursor += top_pad
            geom.art_top = row_cursor + 1

            for line in art_lines:
                log(" " * left_margin + line)
            row_cursor += len(art_lines)
            log("")
            row_cursor += 1
            # Volume bar/label are absolute-positioned; log them AFTER the spacing
            # blank so the blank's erase-to-end can't wipe the label (which sits on
            # the art-bottom+1 row). Otherwise the number vanishes on a full redraw.
            for cell in _volume_bar_cells(volume):
                log(cell)

            for line in _center_lines(left_col, cols):
                log(line)
            row_cursor += len(left_col)

            log("")
            prog_row = row_cursor + 2
            ctrl_row = prog_row + 1

            # Recompute now that the art geometry is set, so the transport line
            # centers over the art. The hint-line count is unchanged from the
            # early call that sized the art above.
            ctrl_row, shortcut_lines = _place_controls(
                log, ctrl_row, rows, is_uslt_track, is_paused, volume, toast, has_lyrics, has_cast)

            lyric_row = ctrl_row + len(shortcut_lines) + 2
            art_bottom_row = max(row_cursor + 6, rows - ui_utils.MARGIN_V)

            _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
            sys.stdout.flush()
            return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

        # Split view: left half = art + meta, right pane = credits/lyrics.
        art_w = min(cols // 2, ART_MAX_WIDTH)
        right_w = cols - art_w - _WIDE_SPLIT_GUTTER
        meta_val_w = right_w - 10

        left_col = _meta_left_lines(audio, file_path, meta_val_w)
        # Reserve the controls/hints height before sizing the art (see above).
        status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, toast, has_lyrics=has_lyrics, has_credits=has_cast)
        shortcut_lines = shortcuts_ln.splitlines() or [""]
        avail_h = max(3, rows - len(left_col) - len(shortcut_lines) - 4 - 2 * ui_utils.MARGIN_V)
        # Art is inset from the panel edges so it floats with breathing room.
        art_inner_w = max(10, art_w * 3 // 4)
        art_str, art_lines = _art_width_for_height(file_path, art_inner_w, avail_h, pre_art)

        art_vis_w = max((ui_utils.visual_len(a) for a in art_lines), default=art_inner_w) if art_lines else art_inner_w
        left_margin = max(ui_utils.MARGIN_H, (art_w - art_vis_w) // 2)

        geom.art_width = art_vis_w
        geom.art_left = left_margin
        geom.art_height = len(art_lines)
        geom.right_left = art_w + _WIDE_SPLIT_GUTTER
        geom.right_width = right_w
        geom.vol_bar_col = left_margin + art_vis_w + 2

        if art_lines:
            # Counting the hint rows too, as the no-pane layout does: otherwise
            # showing help drew over the controls.
            top_pad = max(ui_utils.MARGIN_V,
                          (rows - len(art_lines) - 1 - len(left_col) - len(shortcut_lines) - 5) // 2)
            for _ in range(top_pad):
                log("")
            row_cursor += top_pad
        geom.art_top = row_cursor + 1

        for line in art_lines:
            log(" " * left_margin + line)
        row_cursor += len(art_lines)
        log("")
        row_cursor += 1
        # Volume cells after the spacing blank (see the no-pane branch note).
        for cell in _volume_bar_cells(volume):
            log(cell)

        for line in left_col:
            vis = ui_utils.visual_len(line)
            pad = ' ' * max(0, (art_w - vis) // 2)
            log(f"{pad}{line}")
        row_cursor += len(left_col)

        log("")
        prog_row = row_cursor + 2
        ctrl_row = prog_row + 1

        # Recompute now that the art geometry is set (see the no-pane branch).
        ctrl_row, shortcut_lines = _place_controls(
            log, ctrl_row, rows, is_uslt_track, is_paused, volume, toast, has_lyrics, has_cast)

        _pane_top = geom.art_top  # right pane aligns with art top after any vertical centering

        # Queue view takes over the whole right pane when toggled on.
        if _ui_state['show_queue']:
            _place_queue(log, _pane_top, geom.right_left, right_w, (ctrl_row - 1) - _pane_top)
            lyric_row = ctrl_row  # suppress the lyric area while the queue shows
            art_bottom_row = ctrl_row - 1
            _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
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
                pad_c = ' ' * max(0, cast_col_w - ui_utils.visual_len(lft))
                credits_lines.append(f"  {lft}{pad_c}{gap}{rgt}")

        for idx, line in enumerate(credits_lines):
            log(f"\033[{_pane_top + idx};{geom.right_left}H{line}")

        lyric_row = _pane_top + len(credits_lines) + (1 if credits_lines else 0)
        # Cap the right pane at the row above the transport controls so the
        # lyric-window clearing loop never touches the controls/hints rows.
        art_bottom_row = max(lyric_row + 2, ctrl_row - 1)

        _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

    elif mode == 'standard':
        geom.right_left = None
        geom.right_width = None

        meta_val_w = cols - 12
        left_col = _meta_left_lines(audio, file_path, meta_val_w)

        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))
        _, temp_shortcuts = _controls_line(is_uslt_track, is_paused, volume, toast, width=cols, has_lyrics=has_lyrics, has_credits=has_cast)
        control_rows = 2 + len(temp_shortcuts.splitlines() or [""])

        credits_est = tune.PANE_CREDITS_EST_ROWS if (has_cast and _ui_state['show_credits']) else 0
        lyrics_est = tune.PANE_LYRICS_EST_ROWS if _ui_state['show_lyrics'] else 0
        reserved_rows = len(left_col) + control_rows + credits_est + lyrics_est + 2
        max_art_h = max(3, rows - reserved_rows - 2 * ui_utils.MARGIN_V)

        art_str, art_lines = _art_width_for_height(file_path, cols, max_art_h, pre_art)
        actual_art_w = max((ui_utils.visual_len(l) for l in art_lines), default=cols) if art_lines else cols
        if actual_art_w < cols:
            # Art was narrowed to fit terminal height — centre it.
            art_lines = _align_art_lines(art_lines, cols)
            geom.art_left = max(0, (cols - actual_art_w) // 2)
            geom.art_width = actual_art_w
        else:
            geom.art_left = 0
            geom.art_width = cols if art_lines else 0

        geom.art_height = len(art_lines)
        geom.vol_bar_col = (geom.art_left or 0) + (geom.art_width or 0) + 2

        if art_lines and actual_art_w < cols:
            top_pad = max(0, (rows - len(art_lines) - 1 - len(left_col) - control_rows - 1) // 2)
            for _ in range(top_pad):
                log("")
            row_cursor += top_pad
        geom.art_top = row_cursor + 1

        for line in art_lines: log(line)
        row_cursor += len(art_lines)
        log("")
        row_cursor += 1
        # Volume cells after the spacing blank (see the no-pane branch note).
        for cell in _volume_bar_cells(volume):
            log(cell)

        left_col = _center_lines(left_col, cols)
        for line in left_col: log(line)
        row_cursor += len(left_col)

        # The progress row stays out of the frame: update_progress_ui owns it,
        # and a frame that painted it blank made the bar flicker on each redraw.
        row_cursor += 1
        prog_row = row_cursor
        ctrl_row = prog_row + 1

        ctrl_row, shortcut_lines = _place_controls(
            log, ctrl_row, rows, is_uslt_track, is_paused, volume, toast, has_lyrics, has_cast)

        ctrl_row_end = ctrl_row + len(shortcut_lines)

        if _ui_state['show_queue']:
            q_start = ctrl_row_end + 2
            _place_queue(log, q_start, 1 + ui_utils.MARGIN_H, cols - ui_utils.MARGIN_H,
                         rows - ui_utils.MARGIN_V - q_start)
            lyric_row = rows - ui_utils.MARGIN_V
            art_bottom_row = rows - ui_utils.MARGIN_V
            _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
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
                pad = ' ' * max(0, col_w - ui_utils.visual_len(lft))
                log(f"\033[{ctrl_row_end + 2 + i};1H\033[K{lft}{pad}{gap}{rgt}")

        lyric_row = ctrl_row_end + 2 + max(len(c_lines), len(cr_lines)) + 1
        art_bottom_row = rows - ui_utils.MARGIN_V
        _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

    else:
        meta_val_w = max(1, cols - 12)
        left_col = _meta_left_lines(audio, file_path, meta_val_w)
        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))
        _, temp_shortcuts = _controls_line(is_uslt_track, is_paused, volume, toast, width=cols, has_lyrics=has_lyrics, has_credits=has_cast)
        control_rows = 2 + len(temp_shortcuts.splitlines() or [""])
        reserved_rows = len(left_col) + control_rows + 8

        if cols >= 34:
            art_w = cols
            max_art_h = max(3, rows - reserved_rows - 2 * ui_utils.MARGIN_V)
            art_str, art_lines = _art_width_for_height(file_path, art_w, max_art_h, pre_art)
            actual_art_w = max((ui_utils.visual_len(l) for l in art_lines), default=art_w) if art_lines else art_w
            geom.art_left = max(0, (cols - actual_art_w) // 2)
            geom.art_width = actual_art_w
            geom.art_height = len(art_lines)
            if art_lines and actual_art_w < cols:
                top_pad = max(0, (rows - len(art_lines) - 1 - len(left_col) - control_rows - 1) // 2)
                for _ in range(top_pad):
                    log("")
                row_cursor += top_pad
            geom.art_top = row_cursor + 1
            # Printed where geom.art_left says it is (centred), which is where
            # the image, the volume bar and clicks all take it to be.
            for line in art_lines: log(" " * geom.art_left + line)
            row_cursor += len(art_lines)
            log("")
            row_cursor += 1
            left_col = _center_lines(left_col, cols)
            for line in left_col: log(line)
            row_cursor += len(left_col)
        else:
            available_rows = max(0, rows - row_cursor - 8)
            top_padding = max(0, (available_rows - len(left_col)) // 2)
            for _ in range(top_padding): log("")
            row_cursor += top_padding
            for line in left_col: log(line)
            row_cursor += len(left_col)

        # The progress row stays out of the frame: update_progress_ui owns it,
        # and a frame that painted it blank made the bar flicker on each redraw.
        row_cursor += 1
        prog_row = row_cursor
        ctrl_row = prog_row + 1
        ctrl_row, shortcut_lines = _place_controls(
            log, ctrl_row, rows, is_uslt_track, is_paused, volume, toast, has_lyrics, has_cast)
        lyric_row = ctrl_row + len(shortcut_lines) + 2
        art_bottom_row = rows - ui_utils.MARGIN_V
        _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row
