"""
Music playback engine with UI rendering.

Handles audio playback, lyric display, and interactive player controls.
Backend: python-vlc (replaces miniaudio for seeking, volume, and codec support).
"""

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
from src.ascii_art import get_ascii
from src.music_library import get_song_duration, TAG_MAP
from src.state import NAV_STACK

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
    """Find which USLT line should currently be displayed."""
    for i, (start, end) in enumerate(line_times):
        if elapsed < end:
            return i
    return max(0, len(line_times) - 1)


def draw_lyric_window(row: int, sylt_data: list, current_idx: int, width: int | None = None) -> None:
    """Display previous, current, and next lyrics for SYLT."""
    width = width or ui_utils.get_terminal_width()
    wrap_w = max(20, width - 12)

    # Get raw text for surrounding lines, handling start/end boundaries gracefully
    p_raw = sylt_data[current_idx - 1][0] if current_idx > 0 else ""
    c_raw = sylt_data[current_idx][0] if 0 <= current_idx < len(sylt_data) else ""
    n_raw = sylt_data[current_idx + 1][0] if 0 <= current_idx < len(sylt_data) - 1 else ""

    p_wrap = textwrap.wrap(normalise_lyric_newlines(p_raw).replace('\n', ' '), width=wrap_w)[:1]
    c_wrap = textwrap.wrap(normalise_lyric_newlines(c_raw).replace('\n', ' '), width=wrap_w)
    n_wrap = textwrap.wrap(normalise_lyric_newlines(n_raw).replace('\n', ' '), width=wrap_w)[:1]

    sys.stdout.write(f"\033[{row};1H\033[J")

    # Draw previous line (DIM)
    if p_wrap:
        sys.stdout.write(f"{ui_utils.Colours.DIM}   {p_wrap[0]}{ui_utils.Colours.RESET}\n")
    else:
        sys.stdout.write("\n")

    # Draw current line (BOLD)
    if c_wrap:
        for i, line in enumerate(c_wrap):
            prefix = ">> " if i == 0 else "   "
            sys.stdout.write(f"{ui_utils.Colours.BOLD}{prefix}{line}{ui_utils.Colours.RESET}\n")
    else:
        sys.stdout.write("\n")

    # Draw next line (DIM)
    if n_wrap:
        sys.stdout.write(f"{ui_utils.Colours.DIM}   {n_wrap[0]}{ui_utils.Colours.RESET}\n")
    else:
        sys.stdout.write("\n")

    sys.stdout.flush()


def draw_uslt_window(row: int, all_lines: list, line_times: list, elapsed: float,
                     width: int | None = None, manual_idx: int | None = None) -> None:
    """Display unsynced lyrics with manual navigation support."""
    BOLD, RESET = "\033[1m", "\033[0m"
    HIGHLIGHT, YELLOW, CYAN = "\033[1;31m", "\033[1;33m", "\033[1;36m"

    auto_idx = find_current_uslt_line(line_times, elapsed)
    display_idx = manual_idx if manual_idx is not None else auto_idx
    display_start = max(0, display_idx - 3)

    sys.stdout.write(f"\033[{row};1H\033[J")

    status = f"{YELLOW}[MANUAL]{RESET}" if manual_idx is not None else f"{CYAN}[AUTO]{RESET}"
    sys.stdout.write(status + "\n")

    for i in range(display_start, min(display_start + 8, len(all_lines))):
        line = all_lines[i]
        if manual_idx is not None and i == manual_idx:
            sys.stdout.write(f"{YELLOW}[•] {line}{RESET}\n")
        elif i == auto_idx:
            sys.stdout.write(f"{HIGHLIGHT}>>> {line} <<<{RESET}\n")
        else:
            sys.stdout.write(f"{BOLD}{line}{RESET}\n")

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
    """Priority order: cast matches (in billing order), then creator, producer, script editor, others. Ignores writers. Cap at limit."""
    DIM, RESET = "\033[2m", "\033[0m"
    cast_names = cast_names or []
    cast_name_lower = [n.lower() for n in cast_names]
    
    # Separate crew into matching and non-matching
    cast_matches = []
    other_crew = []
    seen_cast = set() # Track cast members we've already given priority
    
    for role, name in people:
        name_l = name.lower()
        if name_l in cast_name_lower and name_l not in seen_cast:
            # Find original cast position to maintain billing order
            cast_idx = cast_name_lower.index(name_l)
            cast_matches.append((cast_idx, role, name))
            seen_cast.add(name_l) # Ensure they are only given priority once
        else:
            # Subsequent roles for the same cast member (or non-cast) go here
            other_crew.append((role, name))
    
    # Sort cast matches by billing order (original cast index)
    cast_matches.sort(key=lambda x: x[0])
    cast_matches = [(role, name) for _, role, name in cast_matches]
    
    # Priority order for non-matching crew - skip writers if we have billing members
    priority_roles = ['creator', 'producer', 'script editor']
    if not cast_matches:
        # Only include writers if no billing production members
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
                 is_paused: bool = False, volume: int = 100) -> tuple:
    """
    Draw the full music player UI.

    Returns (progress_row, lyric_row, columns)
    volume: 0-100 (VLC scale)
    """
    cols, rows = size

    RESET   = ui_utils.Colours.RESET
    MAGENTA = ui_utils.Colours.MAGENTA

    ui_utils.clear_screen()

    breadcrumbs = ui_utils._get_breadcrumb_str(cols) if NAV_STACK else "Music Player"
    print(f"{ui_utils.Colours.DIM}{breadcrumbs}{ui_utils.Colours.RESET}")
    print("─" * cols)

    # ── Right column: Cast & Crew ─────────────────────────────────────────
    # Build with unlimited width first so we can measure the natural content
    # width, then clamp it to fit within half the terminal.
    cast_people = _get_people(audio, 'TMCL')
    crew_people = _get_people(audio, 'TIPL')

    right_raw = []
    if cast_people:
        right_raw.append(f"{ui_utils.Colours.YELLOW}--- CAST ---{RESET}")
        right_raw.extend(_build_cast_lines(cast_people, 9999))
    if crew_people:
        if right_raw:
            right_raw.append("")
        right_raw.append(f"{ui_utils.Colours.CYAN}--- PRODUCTION ---{RESET}")
        top_cast_names = [name for _, name in cast_people[:4]]
        right_raw.extend(_build_crew_lines(crew_people, 9999, cast_names=top_cast_names))

    # Natural visible width of the right column (ignoring ANSI codes)
    right_natural_w = max((_visible_len(l) for l in right_raw), default=0)

    # Divider position: give the right column exactly what it needs
    # (plus a 2-char gutter), but never more than 45 % of the terminal.
    MAX_RIGHT_FRAC = 0.45
    right_col_width  = min(right_natural_w, int(cols * MAX_RIGHT_FRAC))
    right_col_width  = max(right_col_width, 0)
    # left gets everything else minus the divider (3 chars: space │ space)
    left_col_width   = max(20, cols - right_col_width - 3) if right_col_width else cols - 1

    # ── Left column: Metadata ─────────────────────────────────────────────
    left_lines = []

    label_file   = "FILE:"
    max_val_file = left_col_width - len(label_file) - 1
    display_path = ui_utils.truncate_text(file_path, max_val_file, placeholder="...", front=True)
    left_lines.append(f"{MAGENTA}{label_file:<2}{RESET} {display_path}")

    for tag in sorted(audio.keys()):
        if any(tag.startswith(x) for x in ('USLT', 'SYLT', 'COMM', 'APIC', 'TMCL', 'TIPL', 'PRIV', 'TXXX', 'TSOA', 'TSOP', 'TSO2', 'GRP1')):
            continue
        label         = TAG_MAP.get(tag, tag)
        val           = str(audio[tag])
        display_label = label[:12]
        max_val_w     = left_col_width - 15
        val           = ui_utils.truncate_text(val, max_val_w, placeholder="..")
        left_lines.append(f"{MAGENTA}{display_label:<12}:{RESET} {val}")

    # ── Draw grid ─────────────────────────────────────────────────────────
    # Re-build right lines now we know the actual right_col_width
    right_lines = []
    if cast_people:
        right_lines.append(f"{ui_utils.Colours.YELLOW}--- CAST ---{RESET}")
        right_lines.extend(_build_cast_lines(cast_people, right_col_width))
    if crew_people:
        if right_lines:
            right_lines.append("")
        right_lines.append(f"{ui_utils.Colours.CYAN}--- PRODUCTION ---{RESET}")
        right_lines.extend(_build_crew_lines(crew_people, right_col_width, cast_names=[name for _, name in cast_people[:4]]))

    num_rows = max(len(left_lines), len(right_lines))
    for i in range(num_rows):
        l_text = left_lines[i]  if i < len(left_lines)  else ""
        r_text = right_lines[i] if i < len(right_lines) else ""
        l_vis  = _visible_len(l_text)
        pad    = " " * max(0, left_col_width - l_vis)
        if right_lines:
            print(f"{l_text}{pad} │ {r_text}")
        else:
            print(l_text)

    header_height = num_rows + 2
    print("─" * cols)

    # Art
    max_art_h = max(2, rows - header_height - 10)
    art_str = pre_art if pre_art else get_ascii(file_path, width=cols)
    art_lines = art_str.splitlines()[:max_art_h]
    print("\n".join(art_lines))

    prog_row = header_height + len(art_lines) + 2
    ctrl_row = prog_row + 1
    lyric_row = ctrl_row + 3

    # Play/Pause + Volume (VLC 0-100 scale → block display)
    pp_icon = " ⏸  PAUSED" if is_paused else " ⏵  PLAYING"
    v_blocks = [" ", "▂", "▃", "▅", "▆", "▇"]
    v_idx = min(int((volume / 100) * 5), len(v_blocks) - 1)
    vol_bar = ''.join(v_blocks[:v_idx + 1])
    print(f"\033[{ctrl_row};1H{pp_icon}  |  VOL: {vol_bar} {volume}%")

    return prog_row, lyric_row, cols


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
    """
    Main music player engine (VLC backend).

    Controls:
        ␣ / P         pause/resume
        N             back to menu
        Q             quit all
        ← / →         seek ±5s
        , / .         seek ±30s
        J / L         seek ±1s
        + / -         volume ±10
        ↑ / ↓         navigate USLT lyrics (unsynced only)
    """
    manual_line_index = None
    arrow_key_time = None
    uslt_time_offset = 0.0

    # Data loading
    if preloaded_data:
        audio = preloaded_data['audio']
        duration = preloaded_data['duration']
        pre_art = preloaded_data['art']
    else:
        try:
            audio = ID3(file_path)
            duration = get_song_duration(file_path)
            pre_art = None
        except Exception:
            return {"status": "ERROR"}

    # Parse lyrics
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

    # VLC init — stderr suppressed inside _make_player for the init phase.
    # We suppress again for the playback loop duration here.
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    mp = _make_player(file_path)  # _make_player does NOT touch stderr itself

    last_size = ui_utils.get_terminal_size()
    last_lyric_idx = -1

    with raw_mode(sys.stdin):
        mp.play()
        # Give VLC a moment to start and populate get_length()
        time.sleep(0.3)

        # If duration wasn't preloaded, pull from VLC
        if not duration or duration <= 0:
            vlc_len = mp.get_length()
            duration = vlc_len / 1000.0 if vlc_len > 0 else 999.0

        volume = mp.audio_get_volume()  # sync with VLC's actual volume

        prog_row, lyric_row, current_width = draw_full_ui(
            file_path, audio, pre_art, last_size, is_paused=False, volume=volume
        )

        track_start = time.time()

        while True:
            current_size = ui_utils.get_terminal_size()

            if current_size != last_size:
                last_size = current_size
                volume = mp.audio_get_volume()
                prog_row, lyric_row, current_width = draw_full_ui(
                    file_path, audio, pre_art, last_size,
                    is_paused=(mp.get_state() == vlc.State.Paused),
                    volume=volume
                )
                last_lyric_idx = -1

            # Elapsed from VLC directly
            elapsed_ms = mp.get_time()
            elapsed = elapsed_ms / 1000.0 if elapsed_ms >= 0 else 0.0

            state = mp.get_state()
            if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                break
            if duration and elapsed >= duration:
                break

            # Input handling
            key = get_key_non_blocking()
            if key:
                clear_escape_buffer()
                arrow = is_arrow_key(key)
                is_paused = (state == vlc.State.Paused)

                if key in (' ', 'p', 'P'):
                    mp.pause()  # toggles
                    time.sleep(0.05)
                    is_paused = (mp.get_state() == vlc.State.Paused)
                    volume = mp.audio_get_volume()
                    prog_row, lyric_row, current_width = draw_full_ui(
                        file_path, audio, pre_art, last_size, is_paused, volume
                    )
                    last_lyric_idx = -1

                elif arrow == 'C':  # Right → seek forward 5s
                    _handle_seek(mp, elapsed, duration, 5)

                elif arrow == 'D':  # Left → seek backward 5s
                    _handle_seek(mp, elapsed, duration, -5)

                elif arrow in ('A', 'B') and is_uslt:
                    current_idx = find_current_uslt_line(line_times, elapsed + uslt_time_offset)
                    target_idx = max(0, current_idx - 1) if arrow == 'A' else min(len(line_times) - 1, current_idx + 1)
                    uslt_time_offset = line_times[target_idx][0] - elapsed
                    manual_line_index = target_idx
                    arrow_key_time = time.time()

                elif key == ',':
                    _handle_seek(mp, elapsed, duration, -30)

                elif key == '.':
                    _handle_seek(mp, elapsed, duration, 30)

                elif key.lower() == 'j':
                    _handle_seek(mp, elapsed, duration, -1)

                elif key.lower() == 'l':
                    _handle_seek(mp, elapsed, duration, 1)

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
                    volume = new_vol
                    # Update just the ctrl row
                    v_blocks = [" ", "▂", "▃", "▅", "▆", "▇"]
                    v_idx = min(int((volume / 100) * 5), len(v_blocks) - 1)
                    pp_icon = " ⏸  PAUSED" if is_paused else " ⏵  PLAYING"
                    sys.stdout.write(f"\033[{prog_row + 1};1H\033[K{pp_icon}  |  VOL: {''.join(v_blocks[:v_idx+1])} {volume}%")
                    sys.stdout.flush()

                elif key in ('-', '_'):
                    new_vol = max(0, mp.audio_get_volume() - 10)
                    mp.audio_set_volume(new_vol)
                    volume = new_vol
                    v_blocks = [" ", "▂", "▃", "▅", "▆", "▇"]
                    v_idx = min(int((volume / 100) * 5), len(v_blocks) - 1)
                    pp_icon = " ⏸  PAUSED" if is_paused else " ⏵  PLAYING"
                    sys.stdout.write(f"\033[{prog_row + 1};1H\033[K{pp_icon}  |  VOL: {''.join(v_blocks[:v_idx+1])} {volume}%")
                    sys.stdout.flush()

            # Progress UI
            update_progress_ui(prog_row, elapsed, duration, current_width)

            # Lyric rendering
            if sylt_data:
                if is_uslt:
                    if manual_line_index is not None and arrow_key_time and time.time() - arrow_key_time > 0.5:
                        manual_line_index = None

                    current_idx = find_current_uslt_line(line_times, elapsed + uslt_time_offset)

                    if current_idx != last_lyric_idx or manual_line_index is not None:
                        draw_uslt_window(
                            lyric_row, uslt_lines, line_times, elapsed + uslt_time_offset,
                            width=current_width, manual_idx=manual_line_index
                        )
                        last_lyric_idx = current_idx
                else:
                    current_idx = max(
                        (i for i, (_, ts) in enumerate(sylt_data) if ts <= elapsed_ms),
                        default=-1
                    )
                    if current_idx >= 0 and current_idx != last_lyric_idx:
                        draw_lyric_window(lyric_row, sylt_data, current_idx, width=current_width)
                        last_lyric_idx = current_idx
            else:
                if last_lyric_idx == -1:
                    sys.stdout.write(f"\033[{lyric_row};0H" + " " * current_width + "\n")
                    sys.stdout.write(f"{ui_utils.Colours.DIM}[ No Lyrics Found ]{ui_utils.Colours.RESET}")
                    sys.stdout.flush()
                    last_lyric_idx = -2

            time.sleep(0.05)

    mp.stop()
    os.dup2(old_stderr, 2)
    os.close(old_stderr)
    log_listening_history(file_path, track_start, time.time())
    ui_utils.clear_screen()
    return {"status": "FINISHED"}