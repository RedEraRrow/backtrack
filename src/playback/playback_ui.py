"""Playback UI rendering: album art, metadata, credits, volume bar, lyric panes."""
from __future__ import annotations
import os
import re
import sys

from src.utils import ui_utils
from src.art.album_art import get_art
from src.utils.prompt import _hint
from src.state import NAV_STACK
from src.utils.ui_utils import Colors as C

ART_MAX_WIDTH = 200  # viu rendering degrades above this width on most terminals

# Absolute cursor positioning (\033[<row>;<col>H) — must require the trailing
# 'H' so it does NOT also match a 24-bit colour prefix like \033[38;2;r;g;bm,
# which every half-block art line starts with. Matching those made the renderer
# treat art lines as absolute (row-less), so metadata flowed to the top and drew
# ABOVE the art in standard/minimal layouts.
_ABS_ROW_RE = re.compile(r'^\033\[\d+;\d+H')

_ANSI_RE = re.compile(
    r'(\x1b\[[0-9;?]*[ -/]*[@-~])|'
    r'(\x1b_G[^\x1b]*\x1b\\)|'
    r'(\x1b\][^\x1b]*\x1b\\)|'
    r'(\x1b[PX^_].*?\x1b\\)|'
    r'(\x1b.)'
)

_WIDE_SPLIT_GUTTER = 3
_ART_INNER_MARGIN = 8


def _render_frame_buffer(buf: list, rows: int) -> None:
    """Flush the assembled frame in one write, positioning relative lines by row
    while letting absolute-positioned items (volume bar, controls, lyrics) pass through untouched."""
    parts = [buf[0]]  # clear sequence at row 1
    # `row` counts only flow (relative) lines; absolute-positioned items (the
    # volume bar, controls, lyrics) pass through WITHOUT consuming a row slot —
    # otherwise everything after them (e.g. the metadata) is pushed off-place.
    row = 0
    for item in buf[1:]:
        if _ABS_ROW_RE.match(item):
            parts.append(item)
        else:
            row += 1
            if row <= rows:
                parts.append(f"\033[{row};1H\033[K{item}")
    sys.stdout.write("".join(parts))

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

_art_cache: dict = {}
_ui_state = {
    'show_metadata': False,
    'show_credits': False,
    'show_lyrics': False,
    'show_help': False,
    'show_queue': False,
    'pane_mode': 'off',   # off → lyrics → queue → lyrics+credits (single-key cycle)
}
# Up-next context for the queue view: list of display titles + current index.
_queue_ctx: dict = {'titles': [], 'index': 0}
# Last rendered artwork width (visible characters) used to align progress/controls
_last_art_width: int | None = None
# Left pad (columns) where the artwork starts when printed
_last_art_left: int | None = None
# Artwork vertical geometry (1-based top row and rendered height in rows), used
# to draw the full-height volume bar to the right of the art.
_last_art_top: int | None = None
_last_art_height: int | None = None
# Explicit 1-based column where the volume bar is drawn (None = no room / hidden).
_last_vol_bar_col: int | None = None
# Right pane geometry when in wide mode (1-based column of start, and width)
_last_right_left: int | None = None
_last_right_width: int | None = None


def _layout_mode(cols: int) -> str:
    """Classify terminal width into a layout mode: wide, standard, or minimal."""
    if cols >= 120:
        return 'wide'
    if cols >= 60:
        return 'standard'
    return 'minimal'


def _get_art_cached(file_path: str, width: int) -> str:
    """Return the rendered album art for file_path at width, cached by (path, width, mtime)."""
    # Key on the file's mtime so editing the file (e.g. adding album art)
    # invalidates the cached render — otherwise a "No album art found." result
    # would stick until the program restarts.
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        mtime = 0.0
    key = (file_path, width, mtime)
    if key not in _art_cache:
        # Drop stale-mtime entries for this file+width so repeated edits don't
        # grow the cache unbounded.
        for k in [k for k in _art_cache if k[0] == file_path and k[1] == width and k[2] != mtime]:
            del _art_cache[k]
        _art_cache[key] = get_art(file_path, width=width)
    return _art_cache[key]


def _clip_ansi_to_width(text: str, max_cols: int) -> str:
    """Truncate text to max_cols visible columns, preserving embedded ANSI escapes intact."""
    visible = 0
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '\x1b':
            j = i + 1
            while j < len(text) and not (0x40 <= ord(text[j]) <= 0x7E):
                j += 1
            if j < len(text):
                j += 1
            result.append(text[i:j])
            i = j
        else:
            if visible >= max_cols:
                break
            result.append(text[i])
            visible += 1
            i += 1
    return ''.join(result)


def _art_width_for_height(file_path: str, max_w: int, avail_h: int,
                          pre_art: str | None) -> tuple[str, list[str]]:
    """Fetch art at max_w; if the rendered output exceeds avail_h rows,
    compute a narrower width from the actual aspect ratio and re-fetch."""
    art_str = pre_art if pre_art else _get_art_cached(file_path, width=max_w)
    lines = art_str.splitlines()
    if not lines or len(lines) <= avail_h:
        return art_str, lines

    actual_h = len(lines)
    actual_w = max((_visible_len(l) for l in lines), default=max_w)
    ratio = actual_w / actual_h if actual_h > 0 else 2.0
    fit_w = max(10, min(max_w - 1, int(avail_h * ratio)))

    art_str2 = _get_art_cached(file_path, width=fit_w)
    lines2 = art_str2.splitlines()
    return art_str2, lines2[:avail_h]  # safety cap in case ratio was off


def update_progress_ui(row: int, elapsed: float, duration: float, width: int) -> None:
    """Update the default progress bar display."""
    elapsed_str = ui_utils.format_time(int(elapsed))
    duration_str = ui_utils.format_time(int(duration))
    timer_text = f" {elapsed_str.rjust(5)} / {duration_str.ljust(5)} "

    global _last_art_width, _last_art_left

    if _last_art_width and _last_art_width > 0:
        container_w = _last_art_width
        left_pad = _last_art_left if _last_art_left is not None else 0
    else:
        container_w = width
        left_pad = 0

    bar_width = max(1, container_w - len(timer_text) - 2)
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    bar = ui_utils.get_progress_bar(percent, bar_width)
    pad = ' ' * left_pad

    sys.stdout.write(f"\033[{row};1H\033[K{pad}{bar}{timer_text}")
    sys.stdout.flush()


def _visible_len(text: str) -> int:
    """Return the length of text ignoring ANSI escape sequences."""
    if not text:
        return 0
    return len(_ANSI_RE.sub('', text))


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
            label = label[:max_w - 5] + ".."
        prefix = f" • {label}"
        lines.append(prefix)

    if total > cap:
        lines.append(f"{C.DIM} • + {total - cap} more…{C.RESET}")

    return lines


def _volume_slider(volume: int, width: int = 20) -> str:
    """Render volume as a horizontal bar with a position marker."""
    percent = max(0.0, min(100.0, volume)) / 100.0
    pos = int(round(percent * (width - 1)))
    line = "━" * pos + "●" + "━" * (width - pos - 1)
    return line




def _volume_bar_geometry() -> tuple[int, int, int] | None:
    """Return (column, top_row, height) for the volume bar, or None if there
    is no rendered artwork or no horizontal room to the right of it."""
    if not (_last_vol_bar_col and _last_art_top and _last_art_height):
        return None
    if _last_art_height < 3:
        return None
    cols = ui_utils.get_terminal_width()
    if _last_vol_bar_col < 1 or _last_vol_bar_col + 1 > cols:
        return None
    return _last_vol_bar_col, _last_art_top, _last_art_height


def _volume_bar_cells(volume: int) -> list[str]:
    """Build absolute-positioned cells for a pretty full-height vertical volume
    bar sitting just right of the album art. Fills bottom-up; the boundary cell
    uses a fractional block. A small percentage label sits beneath it."""
    geo = _volume_bar_geometry()
    if geo is None:
        return []
    bar_col, top, height = geo

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
    cells.append(f"\033[{top + height};{label_col}H{C.DIM}{int(round(volume)):>3}{C.RESET}")
    return cells


def draw_volume_bar(volume: int) -> None:
    """Live-redraw just the vertical volume bar (called on +/- volume changes)."""
    cells = _volume_bar_cells(volume)
    if cells:
        sys.stdout.write("\0337" + "".join(cells) + "\0338")
        sys.stdout.flush()


def toggle_metadata() -> None:
    """Toggle display of the extended metadata details line."""
    _ui_state['show_metadata'] = not _ui_state['show_metadata']


def toggle_credits() -> None:
    """Toggle display of the cast/crew credits pane."""
    _ui_state['show_credits'] = not _ui_state['show_credits']


def toggle_lyrics() -> None:
    """Toggle display of the lyrics pane."""
    _ui_state['show_lyrics'] = not _ui_state['show_lyrics']


def toggle_help() -> None:
    """Toggle display of the full keyboard-shortcut help line."""
    _ui_state['show_help'] = not _ui_state['show_help']


def toggle_queue() -> None:
    """Toggle display of the up-next queue pane."""
    _ui_state['show_queue'] = not _ui_state['show_queue']


def _set_pane_mode(mode: str) -> None:
    """Set the right-pane mode and sync the show_lyrics/show_credits/show_queue flags to match it."""
    _ui_state['pane_mode'] = mode
    _ui_state['show_lyrics'] = mode in ('lyrics', 'lyrics+credits')
    _ui_state['show_credits'] = mode in ('credits', 'lyrics+credits')
    _ui_state['show_queue'] = mode == 'queue'


def cycle_right_pane(has_lyrics: bool = True, has_credits: bool = True,
                     has_queue_flag: bool = True) -> None:
    """Advance the right column through the available views with a single key:
    off → lyrics → queue → lyrics+credits → off (states with no content are skipped)."""
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
    _set_pane_mode(states[(states.index(cur) + 1) % len(states)])


def set_queue_context(titles: list[str], index: int) -> None:
    """Register the current play queue so the queue view can render it."""
    _queue_ctx['titles'] = list(titles or [])
    _queue_ctx['index'] = index


def has_queue() -> bool:
    """Return whether there's more than one track in the queue worth showing."""
    return len(_queue_ctx['titles']) > 1


def get_ui_state() -> dict:
    """Return a copy of the current UI state (pane visibility/mode flags)."""
    return _ui_state.copy()


def _build_queue_lines(max_w: int, max_rows: int) -> list[str]:
    """Render the play queue as a scrolling list centred on the current track."""
    titles = _queue_ctx['titles']
    idx = _queue_ctx['index']
    if not titles:
        return [f"{C.DIM}(queue empty){C.RESET}"]

    out = [f"{C.DIM}UP NEXT  ({idx + 1}/{len(titles)}){C.RESET}", ""]
    body_rows = max(1, max_rows - len(out))

    # Window the list so the current track stays visible.
    if len(titles) <= body_rows:
        start = 0
    else:
        start = max(0, min(idx - body_rows // 2, len(titles) - body_rows))
    end = min(len(titles), start + body_rows)

    for i in range(start, end):
        num = f"{i + 1:>2}. "
        avail = max(4, max_w - len(num) - 2)
        title = titles[i]
        if len(title) > avail:
            title = title[:avail - 1] + "…"
        if i == idx:
            out.append(f"{C.ACCENT}▶ {C.RESET}{C.BOLD}{num}{title}{C.RESET}")
        else:
            out.append(f"  {C.DIM}{num}{title}{C.RESET}")

    # Centre the block horizontally within the pane so it sits balanced (#57).
    content_w = max((ui_utils.visual_len(l) for l in out), default=0)
    pad = max(0, (max_w - content_w) // 2)
    if pad:
        out = [(" " * pad) + l for l in out]
    return out


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
        name_l = name.title()
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
            label = label[:max_w - 5] + ".."
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
    global _last_art_width, _last_art_left

    if width:
        art_left = _last_art_left or 0
        left_pad = art_left + max(0, (width - _visible_len(controls)) // 2)
    elif _last_art_width:
        art_left = _last_art_left or 0
        left_pad = art_left + max(0, (_last_art_width - _visible_len(controls)) // 2)
    else:
        cols = ui_utils.get_terminal_size()[0]
        left_pad = max(0, (cols - _visible_len(controls)) // 2)

    status = " " * left_pad + controls

    if not _ui_state['show_help']:
        shortcuts = _hint(('i', 'help'))
        return status, shortcuts

    hint_args = [
        ('space', 'play/pause'),
        ('←→', '±5s'),
        ('j/l', '±1s'),
        (',/.', '±30s'),
        ('e', 'last 35s'),          # TEMP shortcut
        ('+/-', 'volume'),
        ('m', 'meta'),
    ]
    if has_lyrics or has_credits or has_queue():
        hint_args.append(('w', 'panel'))
    if is_uslt:
        hint_args.append(('↑↓', 'scroll'))
    hint_args += [('i', 'hide help'), ('n', 'next'), ('b', 'back'), ('q', 'quit')]

    shortcuts = _hint(*hint_args)
    return status, shortcuts


def _movement_roman(s: str) -> str:
    """Convert an integer string to Roman numerals; pass non-numeric strings through."""
    try:
        n = int(s)
        return ui_utils.roman(n) if n > 0 else s
    except (ValueError, TypeError):
        return s


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

    def _frac(frame_id: str) -> tuple[str, str]:
        """Return (current, total) from a fractional frame like '3/12'."""
        clean = re.sub(r'\s+', ' ', _txt(frame_id))
        parts = [p.strip() for p in re.split(r'[/|∕⁄]|\bof\b', clean, flags=re.IGNORECASE) if p.strip()]
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")

    title = _txt('TIT2') or os.path.splitext(os.path.basename(file_path))[0]
    artist = _txt('TPE1') or _txt('TPE2')
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
        genre = _txt('TCON')
        if genre:
            details.append(genre)

        work = _txt('TIT1')
        movement, _ = _frac('MVIN')
        disc, disc_total = _frac('TPOS')
        disc_subtitle = _txt('TSST')
        track, track_total = _frac('TRCK')

        def _track_str(t: str, total: str) -> str:
            """Format a track number, appending the total when known."""
            return f"Track {t} of {total}" if (t and total) else f"Track {t}"

        def _disc_str(d: str, total: str) -> str:
            """Format a disc number, appending the total when known."""
            return f"Disc {d} of {total}" if (d and total) else f"Disc {d}"

        if work or movement:
            # Classical: work + movement (Roman numerals are conventional here).
            if work:
                details.append(work)
            if movement:
                details.append(f"Movement {_movement_roman(movement)}")
        elif disc_subtitle or (disc_total.isdigit() and int(disc_total) > 1):
            # Multi-disc / boxed set: disc (or its subtitle) + track.
            details.append(disc_subtitle or _disc_str(disc, disc_total))
            if track:
                details.append(_track_str(track, track_total))
        elif track:
            # Plain single-disc track.
            details.append(_track_str(track, track_total))

        if details:
            lines.append(f"{C.DIM}{_trim(' · '.join(details))}{C.RESET}")

    if not lines:
        fp = ui_utils.truncate_text(file_path, max(1, max_val_w), placeholder='…', front=True)
        lines.append(f"{C.DIM}{fp}{C.RESET}")

    return lines


def _align_art_lines(art_lines: list[str], cols: int) -> list[str]:
    """Center art_lines horizontally within cols, padding every line to a uniform width."""
    if not art_lines:
        return []
    art_width = max(_visible_len(line) for line in art_lines)
    left_pad = max(0, (cols - art_width) // 2)
    aligned = []
    for line in art_lines:
        extra_padding = max(0, art_width - _visible_len(line))
        aligned.append(" " * left_pad + line + " " * extra_padding)
    return aligned


def _center_lines(lines: list[str], cols: int) -> list[str]:
    """Center each line horizontally within cols based on its visible length."""
    if not lines:
        return []
    centered: list[str] = []
    for line in lines:
        vis = _visible_len(line)
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
    global _last_art_width, _last_art_left, _last_right_left, _last_right_width
    global _last_art_top, _last_art_height, _last_vol_bar_col
    mode = _layout_mode(cols)
    # Reset art geometry each frame; only branches that draw art repopulate it.
    _last_art_top = None
    _last_art_height = None
    _last_vol_bar_col = None

    # 1. Clear terminal — home first (no scroll), erase saved lines, erase to end.
    frame_buffer = ["\033[H\033[3J\033[J"]

    def log(text):
        frame_buffer.append(text)

    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')
    has_cast = bool(cast_people or crew_people)
    has_lyrics = bool(audio.getall('SYLT') or audio.getall('USLT'))

    row_cursor = 0

    if mode == 'wide':
        show_pane = _ui_state['show_credits'] or _ui_state['show_lyrics'] or _ui_state['show_queue']
        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))

        if not show_pane:
            # No right pane: centred single-column layout with breathing room on sides.
            content_w_max = min(ART_MAX_WIDTH, max(54, cols // 2), cols - 2 * ui_utils.MARGIN_H)
            left_col = _meta_left_lines(audio, file_path, content_w_max - 4)
            avail_h = max(3, rows - len(left_col) - 6 - 2 * ui_utils.MARGIN_V)

            art_str, art_lines = _art_width_for_height(file_path, content_w_max, avail_h, pre_art)
            actual_art_w = max((_visible_len(l) for l in art_lines), default=content_w_max) if art_lines else content_w_max
            left_margin = max(ui_utils.MARGIN_H, (cols - actual_art_w) // 2)

            _last_art_left = left_margin
            _last_art_width = actual_art_w
            _last_art_height = len(art_lines)
            _last_vol_bar_col = left_margin + actual_art_w + 2
            _last_right_left = None
            _last_right_width = None

            if art_lines:
                top_pad = max(ui_utils.MARGIN_V, (rows - len(art_lines) - 1 - len(left_col) - 6) // 2)
                for _ in range(top_pad):
                    log("")
                row_cursor += top_pad
            _last_art_top = row_cursor + 1

            for line in art_lines:
                log(" " * left_margin + line)
            row_cursor += len(art_lines)
            for cell in _volume_bar_cells(volume):
                log(cell)
            log("")
            row_cursor += 1

            for line in _center_lines(left_col, cols):
                log(line)
            row_cursor += len(left_col)

            log("")
            prog_row = row_cursor + 2
            ctrl_row = prog_row + 1

            status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, toast, has_lyrics=has_lyrics, has_credits=has_cast)
            shortcut_lines = shortcuts_ln.splitlines() or [""]
            ctrl_row = min(ctrl_row, rows - len(shortcut_lines) - 1)

            log(f"\033[{ctrl_row};1H\033[K{status_ln}")
            for offset, line in enumerate(shortcut_lines, start=1):
                log(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui_utils.MARGIN_H}{line}")

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
        avail_h = max(3, rows - len(left_col) - 5 - 2 * ui_utils.MARGIN_V)
        # Art is inset from the panel edges so it floats with breathing room.
        art_inner_w = max(10, art_w * 3 // 4)
        art_str, art_lines = _art_width_for_height(file_path, art_inner_w, avail_h, pre_art)

        art_vis_w = max((_visible_len(a) for a in art_lines), default=art_inner_w) if art_lines else art_inner_w
        left_margin = max(ui_utils.MARGIN_H, (art_w - art_vis_w) // 2)

        _last_art_width = art_vis_w
        _last_art_left = left_margin
        _last_art_height = len(art_lines)
        _last_right_left = art_w + _WIDE_SPLIT_GUTTER
        _last_right_width = right_w
        _last_vol_bar_col = left_margin + art_vis_w + 2

        if art_lines:
            top_pad = max(ui_utils.MARGIN_V, (rows - len(art_lines) - 1 - len(left_col) - 6) // 2)
            for _ in range(top_pad):
                log("")
            row_cursor += top_pad
        _last_art_top = row_cursor + 1

        for line in art_lines:
            log(" " * left_margin + line)
        row_cursor += len(art_lines)
        for cell in _volume_bar_cells(volume):
            log(cell)
        log("")
        row_cursor += 1

        for line in left_col:
            vis = _visible_len(line)
            pad = ' ' * max(0, (art_w - vis) // 2)
            log(f"{pad}{line}")
        row_cursor += len(left_col)

        log("")
        prog_row = row_cursor + 2
        ctrl_row = prog_row + 1

        status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, toast)
        shortcut_lines = shortcuts_ln.splitlines() or [""]
        ctrl_row = min(ctrl_row, rows - len(shortcut_lines) - 1)

        log(f"\033[{ctrl_row};1H\033[K{status_ln}")
        for offset, line in enumerate(shortcut_lines, start=1):
            log(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui_utils.MARGIN_H}{line}")

        _pane_top = _last_art_top  # right pane aligns with art top after any vertical centering

        # Queue view takes over the whole right pane when toggled on.
        if _ui_state['show_queue']:
            pane_rows = max(3, (ctrl_row - 1) - _pane_top)
            for qi, line in enumerate(_build_queue_lines(right_w, pane_rows)):
                log(f"\033[{_pane_top + qi};{_last_right_left}H{line}")
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
                pad_c = ' ' * max(0, cast_col_w - _visible_len(lft))
                credits_lines.append(f"  {lft}{pad_c}{gap}{rgt}")

        for idx, line in enumerate(credits_lines):
            log(f"\033[{_pane_top + idx};{_last_right_left}H{line}")

        lyric_row = _pane_top + len(credits_lines) + (1 if credits_lines else 0)
        # Cap the right pane at the row above the transport controls so the
        # lyric-window clearing loop never touches the controls/hints rows.
        art_bottom_row = max(lyric_row + 2, ctrl_row - 1)

        _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

    elif mode == 'standard':
        _last_right_left = None
        _last_right_width = None

        meta_val_w = cols - 12
        left_col = _meta_left_lines(audio, file_path, meta_val_w)

        is_uslt_track = bool(audio.getall('USLT')) and not bool(audio.getall('SYLT'))
        _, temp_shortcuts = _controls_line(is_uslt_track, is_paused, volume, toast, width=cols, has_lyrics=has_lyrics, has_credits=has_cast)
        control_rows = 2 + len(temp_shortcuts.splitlines() or [""])

        credits_est = 5 if (has_cast and _ui_state['show_credits']) else 0
        lyrics_est = 6 if _ui_state['show_lyrics'] else 0
        reserved_rows = len(left_col) + control_rows + credits_est + lyrics_est + 2
        max_art_h = max(3, rows - reserved_rows - 2 * ui_utils.MARGIN_V)

        art_str, art_lines = _art_width_for_height(file_path, cols, max_art_h, pre_art)
        actual_art_w = max((_visible_len(l) for l in art_lines), default=cols) if art_lines else cols
        if actual_art_w < cols:
            # Art was narrowed to fit terminal height — centre it.
            art_lines = _align_art_lines(art_lines, cols)
            _last_art_left = max(0, (cols - actual_art_w) // 2)
            _last_art_width = actual_art_w
        else:
            _last_art_left = 0
            _last_art_width = cols if art_lines else 0

        _last_art_height = len(art_lines)
        _last_vol_bar_col = (_last_art_left or 0) + (_last_art_width or 0) + 2

        if art_lines and actual_art_w < cols:
            top_pad = max(0, (rows - len(art_lines) - 1 - len(left_col) - control_rows - 1) // 2)
            for _ in range(top_pad):
                log("")
            row_cursor += top_pad
        _last_art_top = row_cursor + 1

        for line in art_lines: log(line)
        row_cursor += len(art_lines)
        for cell in _volume_bar_cells(volume):
            log(cell)
        log("")
        row_cursor += 1

        left_col = _center_lines(left_col, cols)
        for line in left_col: log(line)
        row_cursor += len(left_col)

        log("")
        row_cursor += 1
        prog_row = row_cursor
        ctrl_row = prog_row + 1

        status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, toast)
        shortcut_lines = shortcuts_ln.splitlines() or [""]
        ctrl_row = min(ctrl_row, rows - len(shortcut_lines) - 1)

        log(f"\033[{ctrl_row};1H\033[K{status_ln}")
        for offset, line in enumerate(shortcut_lines, start=1):
            log(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui_utils.MARGIN_H}{line}")

        ctrl_row_end = ctrl_row + len(shortcut_lines)

        if _ui_state['show_queue']:
            q_start = ctrl_row_end + 2
            q_rows = max(3, rows - ui_utils.MARGIN_V - q_start)
            for qi, line in enumerate(_build_queue_lines(cols - 2, q_rows)):
                log(f"\033[{q_start + qi};1H\033[K{line}")
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
                pad = ' ' * max(0, col_w - _visible_len(lft))
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
            actual_art_w = max((_visible_len(l) for l in art_lines), default=art_w) if art_lines else art_w
            _last_art_left = max(0, (cols - actual_art_w) // 2)
            _last_art_width = actual_art_w
            if art_lines and actual_art_w < cols:
                top_pad = max(0, (rows - len(art_lines) - 1 - len(left_col) - control_rows - 1) // 2)
                for _ in range(top_pad):
                    log("")
                row_cursor += top_pad
            _last_art_top = row_cursor + 1
            for line in art_lines: log(line)
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

        log("")
        row_cursor += 1
        prog_row = row_cursor
        ctrl_row = prog_row + 1
        status_ln, shortcuts_ln = _controls_line(is_uslt_track, is_paused, volume, toast)
        shortcut_lines = shortcuts_ln.splitlines() or [""]
        ctrl_row = min(ctrl_row, rows - len(shortcut_lines) - 1)
        log(f"\033[{ctrl_row};1H\033[K{status_ln}")
        for offset, line in enumerate(shortcut_lines, start=1):
            log(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui_utils.MARGIN_H}{line}")
        lyric_row = ctrl_row + len(shortcut_lines) + 2
        art_bottom_row = rows - ui_utils.MARGIN_V
        _render_frame_buffer(frame_buffer, rows - ui_utils.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row
