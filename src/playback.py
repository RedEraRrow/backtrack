"""
Music playback engine with UI rendering.

Handles audio playback, lyric display, and interactive player controls.
Backend: python-vlc (replaces miniaudio for seeking, volume, and codec support).
"""
from __future__ import annotations
import sys
import os
import time
import re
import textwrap
import vlc
from mutagen.id3 import ID3

from src import ui_utils
from src.history import log_listening_history
from src.terminal_input import raw_mode, get_key_non_blocking, is_arrow_key, clear_escape_buffer
from src.album_art import get_ascii
from src.music_library import get_song_duration, TAG_MAP
from src.state import NAV_STACK

import bisect

# ── ASCII art cache ────────────────────────────────────────────────────────────
_art_cache: dict = {}   # (file_path, width) -> art_str


def _get_ascii_cached(file_path: str, width: int) -> str:
    key = (file_path, width)
    if key not in _art_cache:
        _art_cache[key] = get_ascii(file_path, width=width)
    return _art_cache[key]


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

# ============================================================================
# Progress Bar & Display
# ============================================================================


def update_progress_ui(row: int, elapsed: float, duration: float, width: int) -> None:
    """Update the progress bar display."""
    elapsed_str = ui_utils.format_time(elapsed)
    duration_str = ui_utils.format_time(duration)
    timer_text = f" {elapsed_str.rjust(5)} / {duration_str.ljust(5)} "
    bar_width = max(1, width - len(timer_text) - 2)
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0
    bar = ui_utils.get_progress_bar(percent, bar_width)

    sys.stdout.write(f"\033[{row};1H\033[K{bar}{timer_text}")
    sys.stdout.flush()


# ============================================================================
# Lyric Display
# ============================================================================


def normalise_lyric_newlines(text: str) -> str:
    """Ensure consistent newline format for lyric processing."""
    if not text:
        return ""
    return text.replace('\r\n', '\n').replace('\r', '\n')


# Abbreviations that should not be treated as sentence boundaries.
_ABBREV_RE = re.compile(
    r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|approx|govt|dept|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|'
    r'Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.',
    re.IGNORECASE,
)


def _sentence_split(text: str, wrap_w: int, max_lines: int) -> list[str]:
    """
    Split text into chunks that each wrap to at most max_lines rows at wrap_w.
    Prefers sentence boundaries; falls back to word-boundary bisection.
    Returns a list of plain-text chunk strings (not yet wrapped).
    """
    flat   = text.replace('\n', ' ').strip()
    masked = _ABBREV_RE.sub(lambda m: m.group().replace('.', '\x00'), flat)
    boundaries = [m.end() for m in re.finditer(r'[.!?]\s+', masked)]

    def _fits(chunk: str) -> bool:
        return len(textwrap.wrap(chunk, width=wrap_w)) <= max_lines

    if not boundaries:
        # No sentence boundaries — bisect on words until every chunk fits.
        chunks, result = [flat], []
        while chunks:
            chunk = chunks.pop(0)
            if _fits(chunk):
                result.append(chunk)
            else:
                words = chunk.split()
                mid   = len(words) // 2
                result.append(' '.join(words[:mid]))
                remainder = ' '.join(words[mid:])
                if remainder:
                    chunks.insert(0, remainder)
        return result

    # Collect individual sentences then greedily merge into max-fitting chunks.
    prev, sentences = 0, []
    for b in boundaries:
        sentences.append(flat[prev:b].strip())
        prev = b
    tail = flat[prev:].strip()
    if tail:
        sentences.append(tail)

    chunks, current = [], ''
    for sentence in sentences:
        candidate = (current + ' ' + sentence).strip() if current else sentence
        if _fits(candidate):
            current = candidate
        else:
            if current:
                chunks.append(current)
            if _fits(sentence):
                current = sentence
            else:
                sub = _sentence_split(sentence, wrap_w, max_lines)
                chunks.extend(sub[:-1])
                current = sub[-1]
    if current:
        chunks.append(current)
    return chunks


# Cache keyed on (wrap_w, max_lines_per_chunk, id(original_lines_list)).
# Cleared on terminal resize via expand_uslt_lines_for_width().
_expand_cache: dict = {}


def expand_uslt_lines(
    lines: list[str],
    line_times: list[tuple],
    wrap_w: int,
    max_lines_per_chunk: int = 6,
) -> tuple[list[str], list[tuple]]:
    """
    Split any USLT line that wraps beyond max_lines_per_chunk into multiple
    sub-lines, each with a proportional share of the original line's time window
    (allocated by word count).  The result is a drop-in replacement for
    (uslt_lines, line_times) and is cached per wrap_w so resize invalidates it.
    """
    cache_key = (wrap_w, max_lines_per_chunk, id(lines))
    if cache_key in _expand_cache:
        return _expand_cache[cache_key]

    exp_lines: list[str]   = []
    exp_times: list[tuple] = []

    for text, (t_start, t_end) in zip(lines, line_times):
        if len(textwrap.wrap(text, width=wrap_w)) <= max_lines_per_chunk:
            exp_lines.append(text)
            exp_times.append((t_start, t_end))
            continue

        chunks      = _sentence_split(text, wrap_w, max_lines_per_chunk)
        word_counts = [max(1, len(c.split())) for c in chunks]
        total_words = sum(word_counts)
        duration    = t_end - t_start
        t = t_start
        for chunk, wc in zip(chunks, word_counts):
            chunk_dur = duration * (wc / total_words)
            exp_lines.append(chunk)
            exp_times.append((t, t + chunk_dur))
            t += chunk_dur

    _expand_cache[cache_key] = (exp_lines, exp_times)
    return exp_lines, exp_times


def build_uslt_line_times(lines: list, words_per_second: float = 2.2) -> list:
    """Pre-calculate (start, end) times for each USLT line by word count."""
    times = []
    t = 0.0

    for line in lines:
        text = line
        if ':' in text:
            text = text.split(':', 1)[1]
        text = re.sub(r'\([^)]*\)', '', text)
        text = re.sub(r'\[[^\]]*\]', '', text)
        n = len(text.split())
        duration = max(0.5, n / words_per_second)
        times.append((t, t + duration))
        t += duration

    return times


def find_current_uslt_line(line_times: list, elapsed: float) -> int:
    """Find which USLT line should currently be displayed using binary search."""
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def draw_lyric_window(row: int, sylt_data: list, current_idx: int,
                      width: int | None = None, max_row: int | None = None) -> None:
    """Display previous, current, and next lyrics for SYLT."""
    C      = ui_utils.Colours
    width  = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    budget  = max(4, max_row - row - 1)
    wrap_w  = max(20, width - 8)

    p_raw = sylt_data[current_idx - 1][0] if current_idx > 0 else ""
    c_raw = sylt_data[current_idx][0]     if 0 <= current_idx < len(sylt_data) else ""
    n_raw = sylt_data[current_idx + 1][0] if 0 <= current_idx < len(sylt_data) - 1 else ""

    def _ctx(raw: str) -> str:
        flat = normalise_lyric_newlines(raw).replace('\n', ' ')
        return flat[:wrap_w - 1] + '…' if len(flat) > wrap_w else flat

    p_line = _ctx(p_raw)
    n_line = _ctx(n_raw)

    c_flat = normalise_lyric_newlines(c_raw).replace('\n', ' ')
    c_wrap = textwrap.wrap(c_flat, width=wrap_w - 4)[:max(1, budget - 2)]

    sys.stdout.write(f"\033[{row};1H\033[J")
    sys.stdout.write(f"{C.DIM}  {p_line}{C.RESET}\n" if p_line else "\n")
    for i, seg in enumerate(c_wrap or [""]):
        pfx = "▶ " if i == 0 else "  "
        sys.stdout.write(f"  {C.BOLD}{pfx}{seg}{C.RESET}\n" if seg else "\n")
    sys.stdout.write(f"{C.DIM}  {n_line}{C.RESET}\n" if n_line else "\n")
    sys.stdout.flush()


def draw_uslt_window(row: int, all_lines: list, line_times: list, elapsed: float,
                     width: int | None = None, manual_idx: int | None = None,
                     max_row: int | None = None) -> None:
    """Render prev / current / next from an already-expanded USLT line list."""
    C    = ui_utils.Colours
    width        = width or ui_utils.get_terminal_width()
    wrap_w       = max(20, width - 8)
    _, term_rows = ui_utils.get_terminal_size()
    max_row      = max_row or term_rows

    auto_idx    = find_current_uslt_line(line_times, elapsed)
    display_idx = manual_idx if manual_idx is not None else auto_idx

    prev_text = all_lines[display_idx - 1] if display_idx > 0                   else ""
    curr_text = all_lines[display_idx]     if 0 <= display_idx < len(all_lines) else ""
    next_text = all_lines[display_idx + 1] if display_idx < len(all_lines) - 1  else ""

    budget       = max(3, max_row - row - 1)
    curr_wrapped = textwrap.wrap(curr_text.replace('\n', ' '), width=wrap_w - 4) or ['']
    curr_wrapped = curr_wrapped[:max(1, budget - 2)]

    def _ctx(text: str) -> str:
        lines = textwrap.wrap(text.replace('\n', ' '), width=wrap_w)
        return lines[0] if lines else ""

    # Subtle manual scroll indicator instead of [MANUAL]/[AUTO] label
    scroll_hint = f" {C.DIM}↕ scroll{C.RESET}" if manual_idx is not None else ""
    hl  = C.SUCCESS if manual_idx is not None else C.ACCENT
    pfx = "● " if manual_idx is not None else "▶ "

    sys.stdout.write(f"\033[{row};1H\033[J")

    prev_line = _ctx(prev_text)
    sys.stdout.write(f"{C.DIM}  {prev_line}{C.RESET}\n" if prev_line else "\n")

    for i, seg in enumerate(curr_wrapped):
        if i == 0:
            sys.stdout.write(f"  {hl}{pfx}{seg}{C.RESET}{scroll_hint}\n")
        else:
            sys.stdout.write(f"    {hl}{seg}{C.RESET}\n")

    next_line = _ctx(next_text)
    sys.stdout.write(f"{C.DIM}  {next_line}{C.RESET}\n" if next_line else "\n")

    sys.stdout.flush()

# ============================================================================
# Full UI Drawing
# ============================================================================


def _visible_len(text: str) -> int:
    """Count string length without ANSI colour codes."""
    return len(re.sub(r'\033\[[0-9;]*m', '', text))


def _get_people(audio, tag_key: str) -> list:
    """Return raw (role, name) pairs from an ID3 frame."""
    return [
        (role.strip().lower(), name.strip())
        for frame in audio.getall(tag_key)
        for role, name in frame.people
    ]


_CREW_ORDER = ['creator', 'writer', 'producer', 'director', 'script editor', 'composer']


def _build_cast_lines(people: list, max_w: int, limit: int = 4) -> list:
    """First `limit` entries bold, rest normal, cap at limit*2 with overflow indicator."""
    BOLD, RESET, DIM = "\033[1m", "\033[0m", "\033[2m"
    total = len(people)
    cap = limit * 2
    lines = []

    for i, (role, name) in enumerate(people[:cap]):
        is_named = role not in PLAYER_CREDITS_ROLES
        label = f"{role.title()}: {name}" if is_named else name
        if len(label) > max_w - 3:
            label = label[:max_w - 5] + ".."
        lines.append(f"{BOLD} • {label}{RESET}" if i < limit else f" • {label}")

    if total > cap:
        lines.append(f"{DIM} • + {total - cap} more…{RESET}")

    return lines


def _build_crew_lines(people: list, max_w: int, cast_names: list | None = None, limit: int = 4) -> list:
    """Priority order: cast matches, then creator, producer, script editor, others."""
    DIM, RESET = "\033[2m", "\033[0m"
    cast_names = cast_names or []
    cast_name_lower = [n.lower() for n in cast_names]
    
    cast_matches = []
    other_crew = []
    seen_cast = set()
    
    for role, name in people:
        name_l = name.lower()
        if name_l in cast_name_lower and name_l not in seen_cast:
            cast_idx = cast_name_lower.index(name_l)
            cast_matches.append((cast_idx, role, name))
            seen_cast.add(name_l)
        else:
            other_crew.append((role, name))
    
    cast_matches.sort(key=lambda x: x[0])
    cast_matches = [(role, name) for _, role, name in cast_matches]
    
    priority_roles = ['creator', 'producer', 'script editor']
    if not cast_matches:
        priority_roles = ['creator', 'producer', 'writer', 'script editor']
    
    ordered, others = [], []

    for role, name in other_crew:
        if role in priority_roles:
            ordered.append((role, name))
        else:
            others.append((role, name))

    ordered.sort(key=lambda x: _CREW_ORDER.index(x[0]) if x[0] in _CREW_ORDER else 99)
    combined = cast_matches + ordered + others
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


def draw_full_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                 is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple:
    """Draw the player UI, selecting between config layout styles."""
    from src.config import load_config
    view = load_config().get("player_view", "default")
    if view == "ipod":
        return _draw_ipod_ui(file_path, audio, size, is_paused, volume, toast)
    return _draw_default_ui(file_path, audio, pre_art, size, is_paused, volume, toast)


# ── iPod 2G view ──────────────────────────────────────────────────────────────

def _draw_ipod_ui(file_path: str, audio, size: tuple,
                  is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple:
    cols, rows = size

    RESET  = "\033[0m"
    INV    = "\033[7m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    FRAME  = "\033[2;37m"

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
        l = total_pad // 2
        return " " * l + text + " " * (total_pad - l)

    ui_utils.clear_screen()

    play_sym = "▶" if not is_paused else "⏸"
    battery  = f"▓▓▓"
    # Show running toast inside the top banner if available
    header_text = f" [{toast.upper()}]" if toast else " Now Playing"
    header_inner = _row(f" {play_sym} {header_text} {battery.rjust(fw - 4 - len(header_text))}", fw)
    print(f"{pad}{INV}{BOLD}{header_inner}{RESET}")
    print(f"{pad}{FRAME}{'─' * fw}{RESET}")

    try:
        track_num = str(audio['TRCK']).split('/')[0].strip() if 'TRCK' in audio else "?"
        track_tot = str(audio['TRCK']).split('/')[1].strip() if 'TRCK' in audio and '/' in str(audio['TRCK']) else "?"
        counter   = f"{track_num} of {track_tot}"
    except Exception:
        counter = ""
    print(f"{pad}{DIM}{_row(f'  {counter}', fw)}{RESET}")

    print()

    title  = str(audio['TIT2']) if 'TIT2' in audio else os.path.splitext(os.path.basename(file_path))[0]
    artist = str(audio.get('TPE2') or audio.get('TPE1') or '')
    album  = str(audio['TALB']) if 'TALB' in audio else ''

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
    v_idx    = min(int((volume / 100) * 8), 8)
    vol_str  = f"VOL {''.join(v_blocks[1:v_idx+1]):<8} {volume:3d}%"
    print(f"{pad}{DIM}{_row(f'  {vol_str}', fw)}{RESET}")

    hints = "  ◀◀  ▶▶   |  MENU"
    print(f"{pad}{DIM}{_row(hints, fw)}{RESET}")
    print(f"{pad}{FRAME}{'─' * fw}{RESET}")

    header_height = 12
    prog_row      = header_height + 1
    ctrl_row      = prog_row + 1
    lyric_row     = ctrl_row + 3

    return prog_row, ctrl_row, lyric_row, cols


# ── Progress bar override for iPod view ──────────────────────────────────────

def update_progress_ipod(row: int, elapsed: float, duration: float,
                         width: int, cols: int) -> None:
    """iPod-style progress: elapsed  [████░░░░░]  total — centred."""
    fw      = min(max(36, cols - 4), 64)
    pad     = " " * ((cols - fw) // 2)
    RESET   = "\033[0m"
    DIM     = "\033[2m"

    e_str   = ui_utils.format_time(elapsed)
    d_str   = ui_utils.format_time(duration)
    bar_w   = fw - len(e_str) - len(d_str) - 4
    bar_w   = max(4, bar_w)
    pct     = max(0.0, min(elapsed / duration, 1.0)) if duration else 0
    filled  = int(pct * bar_w)
    bar     = "█" * filled + "░" * (bar_w - filled)

    line = f"{e_str}  {DIM}{bar}{RESET}  {d_str}"
    sys.stdout.write(f"\033[{row};1H\033[K{pad}{line}")
    sys.stdout.flush()


# ── Default view — responsive layout ─────────────────────────────────────────

_LAYOUT_WIDE     = 100  # art-left | meta+cast-right
_LAYOUT_STANDARD = 60   # art-above, meta | cast below
# < 60: minimal — no art, stacked meta only


def _layout_mode(cols: int) -> str:
    if cols >= _LAYOUT_WIDE:
        return "wide"
    if cols >= _LAYOUT_STANDARD:
        return "standard"
    return "minimal"


def _controls_line(has_lyrics: bool, is_paused: bool, volume: int, toast: str) -> tuple[str, str]:
    """Return (status_line, shortcuts_line) strings."""
    C = ui_utils.Colours
    pp_icon = f"{C.BOLD}⏸  PAUSED{C.RESET}" if is_paused else f"{C.BOLD}⏵  PLAYING{C.RESET}"

    v_blocks = [" ", "▂", "▃", "▅", "▆", "▇"]
    v_idx    = min(int((volume / 100) * 5), len(v_blocks) - 1)
    vol_bar  = ''.join(v_blocks[:v_idx + 1])
    vol_str  = f"{C.DIM}VOL{C.RESET} {vol_bar} {volume}%"
    toast_str = f"   \033[1;33m{toast}\033[0m" if toast else ""

    status = f"  {pp_icon}   {vol_str}{toast_str}"

    scroll_hint = "  [↑/↓] Scroll" if has_lyrics else ""
    shortcuts   = (
        f"{C.DIM}  [Space] Pause  [←/→] ±5s  [,/.] ±30s"
        f"  [+/-] Vol{scroll_hint}  [N] Next  [Q] Quit{C.RESET}"
    )
    return status, shortcuts


def _meta_left_lines(audio, file_path: str, max_val_w: int) -> list[str]:
    """Build metadata display lines. max_val_w is the value column width."""
    C = ui_utils.Colours
    TAG_DISPLAY = [
        ('TIT2', None),
        ('TIT3', None),
        ('TPE2', 'Album Artist'),
        ('TPE1', 'Artist'),
        ('TALB', 'Album'),
        ('TSST', 'Disc'),
        ('TRCK', 'Track'),
        ('TPOS', 'Disc No.'),
        ('TDRC', 'Year'),
        ('TYER', 'Year'),
        ('TCON', 'Genre'),
        ('TCOM', 'Composer'),
        ('TBPM', 'BPM'),
    ]
    LABEL_W = 13
    lines = []
    for tag, label in TAG_DISPLAY:
        if tag not in audio:
            continue
        val = str(audio[tag]).strip()
        if not val:
            continue
        val = ui_utils.truncate_text(val, max(1, max_val_w), placeholder="…")
        if label is None:
            lines.append(f"{C.BOLD}{val}{C.RESET}")
        else:
            lines.append(f"{C.DIM}{label:<{LABEL_W}}{C.RESET} {val}")
    if not lines:
        fp = ui_utils.truncate_text(file_path, max(1, max_val_w + LABEL_W), placeholder="…", front=True)
        lines.append(f"{C.DIM}{fp}{C.RESET}")
    return lines


def _right_col_lines(cast_people: list, crew_people: list,
                     col_w: int, cast_limit: int, crew_limit: int) -> list[str]:
    """Build cast/crew right-column lines, truncated to col_w."""
    C = ui_utils.Colours
    lines: list[str] = []
    if cast_people:
        lines.append(f"{ui_utils.Colours.YELLOW}CAST{C.RESET}")
        lines.extend(_build_cast_lines(cast_people, col_w, limit=cast_limit))
    if crew_people:
        if lines:
            lines.append("")
        lines.append(f"{C.CYAN}PRODUCTION{C.RESET}")
        top_names = [n for _, n in cast_people[:cast_limit]]
        lines.extend(_build_crew_lines(crew_people, col_w, cast_names=top_names, limit=crew_limit))
    return lines


def _print_two_col(left: list[str], left_w: int,
                   right: list[str], sep: str = " │ ") -> int:
    """
    Print left and right columns with a fixed left_w gutter.
    Every left cell is padded to exactly left_w visible chars.
    Returns number of rows printed.
    """
    n = max(len(left), len(right))
    for i in range(n):
        l = left[i]  if i < len(left)  else ""
        r = right[i] if i < len(right) else ""
        vis = _visible_len(l)
        pad = " " * max(0, left_w - vis)
        sys.stdout.write(f"{l}{pad}{sep}{r}\n")
    return n


def _draw_default_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                     is_paused: bool = False, volume: int = 100, toast: str = "") -> tuple:
    cols, rows = size
    C    = ui_utils.Colours
    mode = _layout_mode(cols)

    ui_utils.clear_screen()

    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')
    has_cast    = bool(cast_people or crew_people)

    # ── Header (breadcrumb + divider) — always 2 lines ───────────────────────
    breadcrumb = ui_utils._get_breadcrumb_str(cols) if NAV_STACK else "Music Player"
    sys.stdout.write(f"{C.DIM}{breadcrumb}{C.RESET}\n")
    sys.stdout.write(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}\n")
    row_cursor = 2  # lines written so far

    # ── WIDE (≥ 100): art left, metadata+cast right ───────────────────────────
    if mode == "wide":
        art_w   = min(cols // 2, 56)          # cap art column
        right_w = cols - art_w - 3            # " │ " separator = 3 chars
        meta_val_w = right_w - 15             # label col = 14 + space

        art_str   = pre_art if pre_art else _get_ascii_cached(file_path, width=art_w)
        art_lines = art_str.splitlines()

        cast_limit = min(4, (rows - 10) // 3)
        crew_limit = max(1, (rows - 10) // 3)

        left_col  = _meta_left_lines(audio, file_path, meta_val_w)
        right_col = _right_col_lines(cast_people, crew_people, right_w, cast_limit, crew_limit)

        # Merge meta + cast into one right column: meta on top, cast below
        meta_lines = left_col + ([""] if right_col else []) + right_col
        num_body   = max(len(art_lines), len(meta_lines))
        for i in range(num_body):
            a   = art_lines[i]  if i < len(art_lines)  else ""
            m   = meta_lines[i] if i < len(meta_lines) else ""
            vis = _visible_len(a)
            pad = " " * max(0, art_w - vis)
            sys.stdout.write(f"{a}{pad} │ {m}\n")

        row_cursor += num_body
        sys.stdout.write(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}\n")
        row_cursor += 1

    # ── STANDARD (60–99): art above full-width, meta|cast below ──────────────
    elif mode == "standard":
        # Reserve: 2 header + divider-after-art + meta rows + divider + prog + ctrl×2 + lyric×3
        reserved  = 2 + 1 + 1 + 3 + 2 + 3
        max_art_h = max(2, rows - reserved - 8)

        art_str   = pre_art if pre_art else _get_ascii_cached(file_path, width=cols)
        art_lines = art_str.splitlines()[:max_art_h]
        sys.stdout.write("\n".join(art_lines) + "\n")
        sys.stdout.write(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}\n")
        row_cursor += len(art_lines) + 1

        # Meta left, cast right — only split if there's cast to show
        if has_cast:
            # Fixed split: metadata gets 60 % of cols, cast gets the rest
            left_w     = max(20, int(cols * 0.58))
            right_w    = cols - left_w - 3
            meta_val_w = left_w - 15
            cast_limit = max(1, (rows - row_cursor - 8) // 2)
            crew_limit = max(1, cast_limit)

            left_col  = _meta_left_lines(audio, file_path, meta_val_w)
            right_col = _right_col_lines(cast_people, crew_people, right_w, cast_limit, crew_limit)
            num_body  = _print_two_col(left_col, left_w, right_col)
        else:
            meta_val_w = cols - 16
            left_col   = _meta_left_lines(audio, file_path, meta_val_w)
            for line in left_col:
                sys.stdout.write(f"{line}\n")
            num_body = len(left_col)

        row_cursor += num_body
        sys.stdout.write(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}\n")
        row_cursor += 1

    # ── MINIMAL (< 60): no art, stacked metadata only ────────────────────────
    else:
        meta_val_w = max(1, cols - 16)
        left_col   = _meta_left_lines(audio, file_path, meta_val_w)
        for line in left_col:
            sys.stdout.write(f"{line}\n")
        sys.stdout.write(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}\n")
        row_cursor += len(left_col) + 1

    # ── Progress + controls (absolute positioning below body) ─────────────────
    prog_row  = row_cursor + 1    # +1 blank gap after last divider
    ctrl_row  = prog_row + 1
    lyric_row = ctrl_row + 3

    has_lyrics = bool(audio.getall('SYLT') or audio.getall('USLT'))
    status_ln, shortcuts_ln = _controls_line(has_lyrics, is_paused, volume, toast)
    sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}\n")
    sys.stdout.write(f"\033[{ctrl_row + 1};1H\033[K{shortcuts_ln}\n")
    sys.stdout.flush()

    return prog_row, ctrl_row, lyric_row, cols


# ============================================================================
# VLC factory — stderr suppressed to hide codec noise (e.g. libmpg123)
# ============================================================================


def _make_player(file_path: str) -> vlc.MediaPlayer:
    """Create a VLC MediaPlayer. Caller is responsible for stderr suppression."""
    instance = vlc.Instance('--no-video', '--quiet')
    media = instance.media_new(file_path)
    mp = instance.media_player_new()
    mp.set_media(media)
    return mp


# ============================================================================
# Seek
# ============================================================================


def _handle_seek(mp, elapsed: float, duration: float, seek_amount: int) -> None:
    """Seek by seek_amount seconds (positive = forward, negative = backward)."""
    target = max(0.0, min(elapsed + seek_amount, duration - 0.5))
    mp.set_time(int(target * 1000))


# ============================================================================
# Playback Engine
# ============================================================================


def _parse_sylt(audio) -> list:
    """Parse SYLT (synced lyrics) from ID3 tags."""
    sylt_data = []
    for tag in audio.getall('SYLT'):
        sylt_data.extend(tag.text)
    sylt_data.sort(key=lambda x: x[1])

    return sylt_data


def _parse_uslt(audio) -> list:
    """Parse USLT (unsynced lyrics) from ID3 tags."""
    tags = audio.getall('USLT')
    if not tags:
        return []

    text = tags[0].text
    if isinstance(text, list):
        text = '\n'.join(text)

    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return [(line.strip(), 0) for line in text.split('\n') if line.strip()]


def musicplayer(file_path: str, preloaded_data: dict | None = None) -> dict:
    """Main music player engine (VLC backend)."""
    manual_line_index = None
    arrow_key_time = None
    uslt_time_offset = 0.0
    
    toast_text = ""
    toast_expiry = 0.0

    # Graceful Error Handling Intercept
    if preloaded_data:
        audio = preloaded_data['audio']
        duration = preloaded_data['duration']
        pre_art = preloaded_data['art']
    else:
        try:
            audio = ID3(file_path)
            duration = get_song_duration(file_path)
            pre_art = None
        except Exception as e:
            ui_utils.clear_screen()
            sys.stdout.write(f"\033[1;31mPlayback Error:\033[0m Could not load structure for:\n")
            sys.stdout.write(f" → {file_path}\n")
            sys.stdout.write(f"\033[2mDetail: {str(e)}\033[0m\n\n")
            sys.stdout.write("Press any key to return...")
            sys.stdout.flush()
            with raw_mode(sys.stdin):
                get_key_non_blocking()
                while True:
                    if get_key_non_blocking():
                        break
                    time.sleep(0.05)
            return {"status": "ERROR"}

    sylt_data = _parse_sylt(audio)
    is_uslt = False
    uslt_lines, line_times = [], []

    if not sylt_data:
        uslt_lines_raw = _parse_uslt(audio)
        if uslt_lines_raw:
            is_uslt = True
            sylt_data = uslt_lines_raw
            uslt_lines = [t for t, _ in uslt_lines_raw]
            line_times = build_uslt_line_times(uslt_lines)

    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    mp = _make_player(file_path)

    last_size = ui_utils.get_terminal_size()
    last_lyric_idx = -1
    
    resize_pending = False
    resize_timer = 0.0
    pending_size = last_size

    with raw_mode(sys.stdin):
        mp.play()
        time.sleep(0.3)

        if not duration or duration <= 0:
            vlc_len = mp.get_length()
            duration = vlc_len / 1000.0 if vlc_len > 0 else 999.0

        volume = mp.audio_get_volume()

        prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
            file_path, audio, pre_art, last_size, is_paused=False, volume=volume, toast=toast_text
        )

        if is_uslt:
            _wrap_w = max(20, current_width - 8)
            exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)
        else:
            exp_lines, exp_times = uslt_lines, line_times

        track_start = time.time()

        # Dynamic control row inline update utility
        def update_ctrl_ui():
            is_paused = (mp.get_state() == vlc.State(4))
            vol = mp.audio_get_volume()
            curr_toast = toast_text if time.time() < toast_expiry else ""
            from src.config import load_config as _lc
            if _lc().get("player_view") != "ipod":
                status_ln, shortcuts_ln = _controls_line(bool(sylt_data), is_paused, vol, curr_toast)
                sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}\n")
                sys.stdout.write(f"\033[{ctrl_row + 1};1H\033[K{shortcuts_ln}\n")
                sys.stdout.flush()
            else:
                draw_full_ui(file_path, audio, pre_art, last_size, is_paused, vol, curr_toast)

        while True:
            current_size = ui_utils.get_terminal_size()

            # Window Resize Debounce Logic
            if current_size != last_size:
                if not resize_pending:
                    resize_pending = True
                    pending_size = current_size
                    resize_timer = time.time()
                elif current_size != pending_size:
                    pending_size = current_size
                    resize_timer = time.time()

            if resize_pending and (time.time() - resize_timer > 0.15):
                last_size = pending_size
                resize_pending = False
                volume = mp.audio_get_volume()
                prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
                    file_path, audio, pre_art, last_size,
                    is_paused=(mp.get_state() == vlc.State(4)),
                    volume=volume,
                    toast=toast_text if time.time() < toast_expiry else ""
                )
                last_lyric_idx = -1
                if is_uslt:
                    _wrap_w = max(20, current_width - 8)
                    exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)

            # Clear transient toast feedback messages
            if toast_text and time.time() >= toast_expiry:
                toast_text = ""
                update_ctrl_ui()

            elapsed_ms = mp.get_time()
            elapsed = elapsed_ms / 1000.0 if elapsed_ms >= 0 else 0.0

            state = mp.get_state()
            if state in (vlc.State(6), vlc.State(5), vlc.State(7)):
                break
            if duration and elapsed >= duration:
                break

            key = get_key_non_blocking()
            if key:
                clear_escape_buffer()
                arrow = is_arrow_key(key)
                is_paused = (state == vlc.State(4))

                if key in (' ', 'p', 'P'):
                    mp.pause()
                    time.sleep(0.05)
                    update_ctrl_ui()
                    last_lyric_idx = -1

                elif arrow == 'C':
                    _handle_seek(mp, elapsed, duration, 5)
                    toast_text = "Seek Forward +5s"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif arrow == 'D':
                    _handle_seek(mp, elapsed, duration, -5)
                    toast_text = "Seek Backward -5s"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif arrow in ('A', 'B') and is_uslt:
                    current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)
                    target_idx = max(0, current_idx - 1) if arrow == 'A' else min(len(exp_times) - 1, current_idx + 1)
                    uslt_time_offset = exp_times[target_idx][0] - elapsed
                    manual_line_index = target_idx
                    arrow_key_time = time.time()

                elif key == ',':
                    _handle_seek(mp, elapsed, duration, -30)
                    toast_text = "Seek Backward -30s"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif key == '.':
                    _handle_seek(mp, elapsed, duration, 30)
                    toast_text = "Seek Forward +30s"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif key.lower() == 'j':
                    _handle_seek(mp, elapsed, duration, -1)
                    toast_text = "Seek Backward -1s"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif key.lower() == 'l':
                    _handle_seek(mp, elapsed, duration, 1)
                    toast_text = "Seek Forward +1s"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif key.lower() == 'n':
                    mp.stop()
                    os.dup2(old_stderr, 2)
                    os.close(old_stderr)
                    ui_utils.clear_screen()
                    return {"status": "BACK"}

                elif key.lower() == 'q':
                    mp.stop()
                    os.dup2(old_stderr, 2)
                    os.close(old_stderr)
                    return {"status": "QUIT_ALL"}

                elif key in ('=', '+'):
                    new_vol = min(100, mp.audio_get_volume() + 10)
                    mp.audio_set_volume(new_vol)
                    toast_text = f"Volume: {new_vol}%"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

                elif key in ('-', '_'):
                    new_vol = max(0, mp.audio_get_volume() - 10)
                    mp.audio_set_volume(new_vol)
                    toast_text = f"Volume: {new_vol}%"
                    toast_expiry = time.time() + 1.0
                    update_ctrl_ui()

            from src.config import load_config as _lc
            if _lc().get("player_view") == "ipod":
                update_progress_ipod(prog_row, elapsed, duration, current_width, current_width)
            else:
                update_progress_ui(prog_row, elapsed, duration, current_width)

            if sylt_data:
                if is_uslt:
                    # Extended to 4.0 seconds for clean reading visibility
                    if manual_line_index is not None and arrow_key_time and time.time() - arrow_key_time > 4.0:
                        manual_line_index = None

                    current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)

                    if current_idx != last_lyric_idx or manual_line_index is not None:
                        draw_uslt_window(
                            lyric_row, exp_lines, exp_times, elapsed + uslt_time_offset,
                            width=current_width, manual_idx=manual_line_index,
                            max_row=last_size[1],
                        )
                        last_lyric_idx = current_idx
                else:
                    current_idx = max(
                        (i for i, (_, ts) in enumerate(sylt_data) if ts <= elapsed_ms),
                        default=-1
                    )
                    if current_idx >= 0 and current_idx != last_lyric_idx:
                        draw_lyric_window(lyric_row, sylt_data, current_idx,
                                          width=current_width, max_row=last_size[1])
                        last_lyric_idx = current_idx

            time.sleep(0.05)

    mp.stop()
    os.dup2(old_stderr, 2)
    os.close(old_stderr)
    log_listening_history(file_path, track_start, time.time())
    ui_utils.clear_screen()
    return {"status": "FINISHED"}
