"""
Player UI rendering helpers for the playback engine.
"""
from __future__ import annotations
import os
import re
import sys
import textwrap

from src import ui_utils
from src.album_art import get_ascii
from src.prompt import _hint
from src.state import NAV_STACK

ART_MAX_WIDTH = 64

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
    'show_metadata': True,
    'show_credits': True,
    'show_help': False,
}
# Last rendered artwork width (visible characters) used to align progress/controls
_last_art_width: int | None = None
# Left pad (columns) where the artwork starts when printed
_last_art_left: int | None = None
# Right pane geometry when in wide mode (1-based column of start, and width)
_last_right_left: int | None = None
_last_right_width: int | None = None


def _layout_mode(cols: int) -> str:
    """Return layout mode based on terminal width.

    Modes:
    - 'wide' for large terminals
    - 'standard' for mid-size terminals
    - 'minimal' for narrow terminals
    """
    if cols >= 100:
        return 'wide'
    if cols >= 60:
        return 'standard'
    return 'minimal'


def _get_ascii_cached(file_path: str, width: int) -> str:
    key = (file_path, width)
    if key not in _art_cache:
        _art_cache[key] = get_ascii(file_path, width=width)
    return _art_cache[key]


def update_progress_ui(row: int, elapsed: float, duration: float, width: int) -> None:
    """Update the default progress bar display."""
    elapsed_str = ui_utils.format_time(elapsed)
    duration_str = ui_utils.format_time(duration)
    timer_text = f" {elapsed_str.rjust(5)} / {duration_str.ljust(5)} "
    # Prefer to draw the progress bar within the artwork width when available.
    global _last_art_width, _last_art_left
    if _last_art_width and _last_art_left is not None and _last_art_width < width:
        container_w = _last_art_width
        left_pad = _last_art_left
    else:
        container_w = width
        left_pad = max(0, (width - container_w) // 2)
    bar_width = max(1, container_w - len(timer_text) - 2)
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    bar = ui_utils.get_progress_bar(percent, bar_width)
    # centre the progress container under the art
    pad = ' ' * left_pad

    sys.stdout.write(f"\033[{row};1H\033[K{pad}{bar}{timer_text}")
    sys.stdout.flush()


def _visible_len(text: str) -> int:
    """Count visible characters excluding ANSI escape sequences and image control sequences."""
    if not text:
        return 0

    # Remove standard ANSI CSI sequences, OSC/DCS/ITERM image payloads, and raw escape terminators.
    ansi_re = re.compile(
        r'(\x1b\[[0-9;?]*[ -/]*[@-~])|'  # CSI sequences like \x1b[31m
        r'(\x1b_G[^\x1b]*\x1b\\)|'      # iTerm2 inline image blobs
        r'(\x1b\][^\x1b]*\x1b\\)|'     # OSC sequences
        r'(\x1b[PX^_].*?\x1b\\)|'        # DCS / SOS / PM / APC
        r'(\x1b.)'                          # Other single-char escapes
    )
    return len(ansi_re.sub('', text))


def _get_people(audio, tag_key: str) -> list[tuple[str, str]]:
    """Return raw (role, name) pairs from an ID3 frame."""
    return [
        (role.strip().lower(), name.strip())
        for frame in audio.getall(tag_key)
        for role, name in frame.people
    ]


def _build_cast_lines(people: list[tuple[str, str]], max_w: int, limit: int = 4) -> list[str]:
    """Build cast lines with the first block emphasised."""
    RESET, DIM = "\033[0m", "\033[2m"
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
        lines.append(f"{DIM} • + {total - cap} more…{RESET}")

    return lines


def _volume_slider(volume: int, width: int = 20) -> str:
    """Render volume as a horizontal line slider with a ball indicator."""
    percent = max(0.0, min(100.0, volume)) / 100.0
    pos = int(round(percent * (width - 1)))
    line = "━" * pos + "●" + "━" * (width - pos - 1)
    return line


def toggle_metadata() -> None:
    """Toggle metadata display visibility."""
    _ui_state['show_metadata'] = not _ui_state['show_metadata']


def toggle_credits() -> None:
    """Toggle cast/production credits display visibility."""
    _ui_state['show_credits'] = not _ui_state['show_credits']


def toggle_help() -> None:
    """Toggle display of the full hotkey help overlay."""
    _ui_state['show_help'] = not _ui_state['show_help']


def get_ui_state() -> dict:
    """Return current UI state for debugging/inspection."""
    return _ui_state.copy()


def _build_crew_lines(people: list[tuple[str, str]], max_w: int,
                     cast_names: list[str] | None = None,
                     limit: int = 4) -> list[str]:
    """Build a crew section prioritising cast matches and production roles."""
    DIM, RESET = "\033[2m", "\033[0m"
    cast_names = cast_names or []
    cast_name_lower = [n.lower() for n in cast_names]

    cast_matches = []
    others = []
    seen_cast = set()

    for role, name in people:
        name_l = name.lower()
        if name_l in cast_name_lower and name_l not in seen_cast:
            cast_matches.append((cast_name_lower.index(name_l), role, name))
            seen_cast.add(name_l)
        else:
            others.append((role, name))

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
        label = f"{role.capitalize()}: {name}"
        if len(label) > max_w - 3:
            label = label[:max_w - 5] + ".."
        lines.append(f" ⚙ {label}")

    if total > limit:
        lines.append(f"{DIM} ⚙ + {total - limit} more…{RESET}")

    return lines


def _controls_line(has_lyrics: bool, is_paused: bool, volume: int, toast: str, width: int | None = None) -> tuple[str, str]:
    """Return the current playback status and shortcut lines."""
    C = ui_utils.Colours
    pp_icon = "⏸" if is_paused else "⏵"
    vol_slider = _volume_slider(volume, width=20)
    # Build centered transport controls (back, play/pause, forward)
    transport_icons = ["⏮ ", pp_icon, "⏭"]
    controls = "  ".join(transport_icons)
    global _last_art_width, _last_art_left
    # If a target width is provided (e.g. artwork width), centre controls within that
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
    scroll_hint = f" [{C.DIM}↑/↓{C.RESET}] scroll" if has_lyrics else ""

    if _ui_state['show_help']:
        shortcuts = _hint(
            ('space', 'play/pause'),
            ('←/→', 'seek'),
            (',/.', 'seek ±30s'),
            ('+/-', 'volume'),
            ('m', 'meta'),
            ('c', 'cast'),
            ('i', 'help'),
            ('n', 'next'),
            ('q', 'quit'),
            extra=scroll_hint,
        )
    else:
        # When help overlay is hidden, show only the single info toggle using _hint.
        shortcuts = _hint(
            ('i', 'help'),
            extra=scroll_hint,
        )
    return status, shortcuts


def _meta_left_lines(audio, file_path: str, max_val_w: int) -> list[str]:
    """Build the left-side metadata display lines (compact)."""
    C = ui_utils.Colours

    def _trim(text: str) -> str:
        return ui_utils.truncate_text(text, max(1, max_val_w), placeholder='…')

    title = str(audio.get('TIT2') or os.path.splitext(os.path.basename(file_path))[0]).strip()
    artist = str(audio.get('TPE1') or audio.get('TPE2') or '').strip()
    album = str(audio.get('TALB') or '').strip()
    disc_subtitle = str(audio.get('TSST') or '').strip()

    lines: list[str] = []
    if title:
        lines.append(f"{C.BOLD}{_trim(title)}{C.RESET}")
    if artist:
        lines.append(_trim(artist))
    if album:
        album_line = album
        if disc_subtitle:
            album_line = f"{album} {disc_subtitle}"
        lines.append(f"{C.DIM}{_trim(album_line)}{C.RESET}")
    elif disc_subtitle:
        lines.append(f"{C.DIM}{_trim(disc_subtitle)}{C.RESET}")

    if _ui_state['show_metadata']:
        details = []
        year = str(audio.get('TDRC') or '').strip()
        genre = str(audio.get('TCON') or '').strip()
        disc = str(audio.get('TPOS') or '').strip()
        track = str(audio.get('TRCK') or '').strip()

        if year:
            details.append(year)
        if genre:
            details.append(genre)
        if disc:
            details.append(f"Disc {disc}")
        if track:
            details.append(f"Track {track}")

        if details:
            lines.append("")
            detail_line = f"{C.DIM}{_trim(' · '.join(details))}{C.RESET}"
            lines.append(detail_line)

        if not lines or len(lines) == 0:
            fp = ui_utils.truncate_text(file_path, max(1, max_val_w), placeholder='…', front=True)
            lines.append(f"{C.DIM}{fp}{C.RESET}")
    elif not lines:
        fp = ui_utils.truncate_text(file_path, max(1, max_val_w), placeholder='…', front=True)
        lines.append(f"{C.DIM}{fp}{C.RESET}")

    return lines


def _right_col_lines(cast_people: list[tuple[str, str]], crew_people: list[tuple[str, str]],
                     col_w: int, cast_limit: int, crew_limit: int) -> list[str]:
    """Build the right-side cast and crew display lines (compact)."""
    C = ui_utils.Colours
    lines: list[str] = []
    
    if not _ui_state['show_credits']:
        return lines
    
    if cast_people:
        lines.append(f"{C.DIM}CAST{C.RESET}")
        lines.extend(_build_cast_lines(cast_people, col_w, limit=min(cast_limit, 3)))
    if crew_people:
        if lines:
            lines.append("")
        lines.append(f"{C.DIM}PRODUCTION{C.RESET}")
        top_names = [n for _, n in cast_people[:cast_limit]]
        lines.extend(_build_crew_lines(crew_people, col_w, cast_names=top_names, limit=min(crew_limit, 2)))
    return lines


def _print_two_col(left: list[str], left_w: int,
                   right: list[str], sep: str = " │ ") -> int:
    """Print left and right columns with fixed left width and return row count."""
    n = max(len(left), len(right))
    for i in range(n):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        vis = _visible_len(l)
        pad = " " * max(0, left_w - vis)
        sys.stdout.write(f"{l}{pad}{sep}{r}\n")
    return n


def _align_art_lines(art_lines: list[str], cols: int) -> list[str]:
    """Center art in the available terminal width using visible line width."""
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
    """Horizontally centre a list of lines within `cols`, preserving ANSI escapes."""
    if not lines:
        return []
    centered: list[str] = []
    for line in lines:
        vis = _visible_len(line)
        left = max(0, (cols - vis) // 2)
        centered.append(" " * left + line)
    return centered

def _draw_ipod_ui(file_path: str, audio, size: tuple,
                  is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple[int, int, int, int]:
    cols, rows = size
    RESET = "\033[0m"
    INV = "\033[7m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    FRAME = "\033[2;37m"

    fw = min(max(36, cols - 4), 64)
    pad = " " * ((cols - fw) // 2)

    def _row(content: str, width: int = fw) -> str:
        vis = _visible_len(content)
        if vis > width:
            content = content[:width - 1] + "…"
            vis = width
        return content + " " * (width - vis)

    def _centre(text: str, width: int = fw) -> str:
        text = text[:width]
        total_pad = width - len(text)
        left = total_pad // 2
        return " " * left + text + " " * (total_pad - left)

    ui_utils.clear_screen()
    play_sym = "▶" if not is_paused else "⏸"
    battery = "▓▓▓"
    header_text = f" [{toast.upper()}]" if toast else " Now Playing"
    header_inner = _row(f" {play_sym} {header_text} {battery.rjust(fw - 4 - len(header_text))}", fw)

    print(f"{pad}{INV}{BOLD}{header_inner}{RESET}")
    print(f"{pad}{FRAME}{'─' * fw}{RESET}")

    try:
        track_num = str(audio['TRCK']).split('/')[0].strip() if 'TRCK' in audio else "?"
        track_tot = str(audio['TRCK']).split('/')[1].strip() if 'TRCK' in audio and '/' in str(audio['TRCK']) else "?"
        counter = f"{track_num} of {track_tot}"
    except Exception:
        counter = ""
    print(f"{pad}{DIM}{_row(f'  {counter}', fw)}{RESET}")
    print()

    title = str(audio['TIT2']) if 'TIT2' in audio else os.path.splitext(os.path.basename(file_path))[0]
    artist = str(audio.get('TPE2') or audio.get('TPE1') or '')
    album = str(audio['TALB']) if 'TALB' in audio else ''

    inner = fw - 4
    def _fit(s: str) -> str:
        return s if len(s) <= inner else s[:inner - 1] + '…'

    print(f"{pad}{BOLD}{_centre(_fit(title))}{RESET}")
    if artist:
        print(f"{pad}{_centre(_fit(artist))}")
    if album:
        print(f"{pad}{DIM}{_centre(_fit(album))}{RESET}")

    print()
    print(f"{pad}{FRAME}{'─' * fw}{RESET}")

    v_blocks = ["", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    v_idx = min(int((volume / 100) * 8), 8)
    vol_str = f"VOL {''.join(v_blocks[1:v_idx+1]):<8} {volume:3d}%"
    print(f"{pad}{DIM}{_row(f'  {vol_str}', fw)}{RESET}")
    hints = "  ◀◀  ▶▶   |  MENU"
    print(f"{pad}{DIM}{_row(hints, fw)}{RESET}")
    print(f"{pad}{FRAME}{'─' * fw}{RESET}")

    header_height = 12
    prog_row = header_height + 1
    ctrl_row = prog_row + 1
    lyric_row = ctrl_row + 3
    return prog_row, ctrl_row, lyric_row, cols


def update_progress_ipod(row: int, elapsed: float, duration: float,
                         width: int, cols: int) -> None:
    """Draw the iPod-style centred progress bar."""
    fw = min(max(36, cols - 4), 64)
    pad = " " * ((cols - fw) // 2)
    RESET = "\033[0m"
    DIM = "\033[2m"

    e_str = ui_utils.format_time(elapsed)
    d_str = ui_utils.format_time(duration)
    bar_w = fw - len(e_str) - len(d_str) - 4
    bar_w = max(4, bar_w)
    pct = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    filled = int(pct * bar_w)
    bar = "█" * filled + "░" * (bar_w - filled)

    line = f"{e_str}  {DIM}{bar}{RESET}  {d_str}"
    sys.stdout.write(f"\033[{row};1H\033[K{pad}{line}")
    sys.stdout.flush()


def draw_full_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                 is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple[int, int, int, int]:
    """Draw the selected player UI style and return key row positions."""
    from src.config import load_config
    view = load_config().get('player_view', 'default')
    if view == 'ipod':
        return _draw_ipod_ui(file_path, audio, size, is_paused, volume, toast)
    return _draw_default_ui(file_path, audio, pre_art, size, is_paused, volume, toast)


def _draw_default_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                     is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple[int, int, int, int]:
    cols, rows = size
    global _last_art_width, _last_art_left, _last_right_left, _last_right_width
    C = ui_utils.Colours
    mode = _layout_mode(cols)
    wide_lyric_row: int | None = None

    ui_utils.clear_screen()
    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')
    has_cast = bool(cast_people or crew_people)

    breadcrumb = ui_utils._get_breadcrumb_str(cols) if NAV_STACK else 'Music Player'
    sys.stdout.write(f"{C.DIM}{breadcrumb}{C.RESET}\n")
    sys.stdout.write(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}\n\n")
    row_cursor = 3

    if mode == 'wide':
        art_w = min(cols // 2, ART_MAX_WIDTH)
        right_w = cols - art_w - 3
        meta_val_w = right_w - 10
        art_str = pre_art if pre_art else _get_ascii_cached(file_path, width=art_w)
        art_lines = art_str.splitlines()
        # track artwork width and left pad (art sits at left)
        try:
            art_vis_w = max(_visible_len(a) for a in art_lines) if art_lines else art_w
        except ValueError:
            art_vis_w = art_w
        _last_art_width = art_vis_w
        _last_art_left = 0
        # right pane starts after artwork + two spaces (1-based column)
        _last_right_left = art_w + 3
        _last_right_width = right_w

        cast_limit = min(3, (rows - 10) // 3)
        crew_limit = max(1, (rows - 10) // 3)
        left_col = _meta_left_lines(audio, file_path, meta_val_w)

        gap = ' ' * 4
        cast_col_w = max(12, (right_w - len(gap)) // 2)
        crew_col_w = max(12, right_w - cast_col_w - len(gap))

        cast_heading = [f"{C.DIM}CAST{C.RESET}"] if cast_people and _ui_state['show_credits'] else []
        crew_heading = [f"{C.DIM}PRODUCTION{C.RESET}"] if crew_people and _ui_state['show_credits'] else []
        cast_body = _build_cast_lines(cast_people, cast_col_w, limit=cast_limit) if cast_people and _ui_state['show_credits'] else []
        crew_body = _build_crew_lines(crew_people, crew_col_w, cast_names=[n for _, n in cast_people[:cast_limit]], limit=crew_limit) if crew_people and _ui_state['show_credits'] else []

        cast_lines = cast_heading + cast_body
        crew_lines = crew_heading + crew_body
        right_line_count = max(len(cast_lines), len(crew_lines))
        reserved_lyric_rows = 6
        body_height = max(len(art_lines), right_line_count + reserved_lyric_rows)
        body_start = row_cursor
        if right_line_count:
            wide_lyric_row = body_start + right_line_count + 1
        else:
            wide_lyric_row = body_start + max(1, len(art_lines) // 2)

        for i in range(body_height):
            a = art_lines[i] if i < len(art_lines) else ''
            c = cast_lines[i] if i < len(cast_lines) else ''
            cr = crew_lines[i] if i < len(crew_lines) else ''
            pad_art = ' ' * max(0, art_w - _visible_len(a))
            pad_cast = ' ' * max(0, cast_col_w - _visible_len(c))
            right = f"{c}{pad_cast}{gap}{cr}"
            sys.stdout.write(f"{a}{pad_art}  {right}\n")

        row_cursor += body_height
        for line in left_col:
            sys.stdout.write(f"{line}\n")
        row_cursor += len(left_col)
        sys.stdout.write("\n")
        row_cursor += 1

    elif mode == 'standard':
        max_art_h = max(3, rows - 12)
        art_w = min(cols, ART_MAX_WIDTH)
        art_str = pre_art if pre_art else _get_ascii_cached(file_path, width=art_w)
        art_lines = _align_art_lines(art_str.splitlines()[:max_art_h], cols)

        meta_val_w = cols - 12
        left_col = _meta_left_lines(audio, file_path, meta_val_w)

        # compute artwork visible width and left pad (art is centred by _align_art_lines)
        if art_lines:
            leading = min(len(l) - len(l.lstrip(' ')) for l in art_lines)
            art_vis_w = max(_visible_len(l) - leading for l in art_lines)
            left_pad = leading
        else:
            art_vis_w = 0
            left_pad = 0
        _last_art_width = art_vis_w
        _last_art_left = left_pad
        for line in art_lines:
            sys.stdout.write(f"{line}\n")
        row_cursor += len(art_lines)

        left_col = _center_lines(left_col, cols)
        for line in left_col:
            sys.stdout.write(f"{line}\n")
        row_cursor += len(left_col)
        sys.stdout.write("\n")
        row_cursor += 1

        if has_cast and _ui_state['show_credits']:
            cast_limit = 2
            right_w = cols - 10
            right_col = _right_col_lines(cast_people, crew_people, right_w, cast_limit, cast_limit)
            for line in right_col:
                sys.stdout.write(f"{line}\n")
            row_cursor += len(right_col)
            sys.stdout.write("\n")
            row_cursor += 1

    else:
        if cols >= 34:
            art_w = min(36, cols - 4)
            art_str = pre_art if pre_art else _get_ascii_cached(file_path, width=art_w)
            art_lines = _align_art_lines(art_str.splitlines(), cols)
            max_art_h = max(3, rows - 12)
            if len(art_lines) > max_art_h:
                art_lines = art_lines[:max_art_h]

            meta_val_w = max(1, cols - 12)
            left_col = _meta_left_lines(audio, file_path, meta_val_w)
            # compute artwork visible width and left pad for minimal view
            if art_lines:
                leading = min(len(l) - len(l.lstrip(' ')) for l in art_lines)
                art_vis_w = max(_visible_len(l) - leading for l in art_lines)
                left_pad = leading
            else:
                art_vis_w = 0
                left_pad = 0
            _last_art_width = art_vis_w
            _last_art_left = left_pad

            for line in art_lines:
                sys.stdout.write(f"{line}\n")
            row_cursor += len(art_lines)

            left_col = _center_lines(left_col, cols)
            for line in left_col:
                sys.stdout.write(f"{line}\n")
            row_cursor += len(left_col)
        else:
            meta_val_w = max(1, cols - 12)
            left_col = _meta_left_lines(audio, file_path, meta_val_w)
            available_rows = max(0, rows - row_cursor - 8)
            top_padding = max(0, (available_rows - len(left_col)) // 2)
            for _ in range(top_padding):
                sys.stdout.write("\n")
            row_cursor += top_padding
            for line in left_col:
                sys.stdout.write(f"{line}\n")
            row_cursor += len(left_col)

    prog_row = row_cursor + 1
    ctrl_row = prog_row + 1
    if wide_lyric_row is None:
        lyric_row = ctrl_row + 3
    else:
        lyric_row = wide_lyric_row

    has_lyrics = bool(audio.getall('SYLT') or audio.getall('USLT'))
    status_ln, shortcuts_ln = _controls_line(has_lyrics, is_paused, volume, toast)
    shortcut_lines = shortcuts_ln.splitlines() or [""]

    sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}\n")
    for offset, line in enumerate(shortcut_lines, start=1):
        sys.stdout.write(f"\033[{ctrl_row + offset};1H\033[K{line}\n")
    sys.stdout.flush()

    if wide_lyric_row is None:
        lyric_row = ctrl_row + 1 + len(shortcut_lines) + 1
    return prog_row, ctrl_row, lyric_row, cols
