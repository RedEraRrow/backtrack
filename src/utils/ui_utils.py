"""Terminal helpers: ANSI colours, sizing, formatting, status bar, progress bar."""
from __future__ import annotations
import sys
import shutil
import signal
import time as _time
from typing import Any
import re
from collections import OrderedDict

from src.state import NAV_STACK

_resize_flag = False
_np_layout_dirty = False

def _sigwinch_handler(signum: int, frame: Any) -> None:
    """Mark that the terminal was resized; consume_resize() picks this up."""
    global _resize_flag
    _resize_flag = True

# SIGWINCH doesn't exist on Windows — guard so importing ui_utils never raises there.
if hasattr(signal, "SIGWINCH"):
    signal.signal(signal.SIGWINCH, _sigwinch_handler)


def mark_now_playing_layout_dirty() -> None:
    """Signal that the now-playing box's height changed (it appeared or vanished,
    e.g. the player view opened in another window), so menus re-render and
    re-reserve rows for it via consume_resize()."""
    global _np_layout_dirty
    _np_layout_dirty = True


def consume_resize() -> bool:
    """True (and clears the flags) if the terminal was resized *or* the now-playing
    box changed height since last call — both need a full re-render/re-layout."""
    global _resize_flag, _np_layout_dirty
    if _resize_flag or _np_layout_dirty:
        _resize_flag = False
        _np_layout_dirty = False
        return True
    return False

# Global content margins.  All widgets and the playback UI read from here —
# change these two values to tune the whole app at once.
MARGIN_H = 2   # columns reserved on each horizontal side (left and right)
MARGIN_V = 1   # rows reserved on each vertical side (top and bottom)

class Colors:
    PRIMARY = "\033[1;37m" # Bold white
    WHITE = "\033[37m" # Normal white
    ACCENT = "\033[1;31m" # Red
    CYAN = "\033[1;36m"
    YELLOW = "\033[1;33m"
    MAGENTA = "\033[1;35m"
    GREEN = "\033[1;32m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    RESET = "\033[0m"
    BACK = "\x1b[47m"
    INVERT = "\033[7m"
    HIDE = "\033[?25l"
    SHOW = "\033[?25h"


def enter_alt_screen() -> None:
    """Switch to the terminal alternate screen buffer (no scrollback).

    Also enables focus in/out reporting (\\033[?1004h) so editors can show a
    hollow cursor when the window loses focus; unsupported terminals ignore it.
    """
    sys.stdout.write("\033[?1049h\033[?1004h\033[H\033[3J\033[J")
    sys.stdout.flush()


def exit_alt_screen() -> None:
    """Restore the main screen buffer, disable focus reporting, show the cursor."""
    sys.stdout.write("\033[?25h\033[?1004l\033[?1049l")
    sys.stdout.flush()


def clear_screen() -> None:
    """Overwrite screen content from home without triggering scrollback save."""
    sys.stdout.write("\033[H\033[3J\033[J")
    sys.stdout.flush()

BACKGROUND_TASKS: dict[str, str] = {}

_toast_message: str = ""
_toast_expiry: float = 0.0

# Now-playing box (#14): the playback layer registers a provider so the widget
# layer can draw a background-audio box without importing playback (keeps the
# dependency flowing one way). provider(width) -> list[str] | None (styled rows,
# top to bottom; drawn just above the breadcrumb status line).
_now_playing_provider = None
_now_playing_lines: list[str] = []
# A cheap identity of the currently-shown track (file/generation/paused/index …).
# The idle-tick redraw keys off this so a background track change always repaints
# the box, even in the rare case two tracks render to byte-identical rows.
_now_playing_sig: tuple | None = None


def set_now_playing_provider(fn) -> None:
    """Register a ``callable(width) -> list[str] | None`` that renders the now-playing box."""
    global _now_playing_provider
    _now_playing_provider = fn


def set_now_playing_signature(sig: tuple | None) -> None:
    """Record the identity of the track the box provider just rendered (#14)."""
    global _now_playing_sig
    _now_playing_sig = sig


# Event-driven repaint (#14): background threads (a joined window's snapshot
# receiver, the host's auto-advance tick) call pulse_now_playing() when the
# now-playing state changes so the menu poll repaints the box *immediately*
# instead of only on the next keystroke. The waker is registered by the input
# layer (a self-pipe that wakes its select); this hook keeps the playback/IPC
# threads free of any input-layer import.
_np_waker = None


def set_now_playing_waker(fn) -> None:
    """Register a ``callable()`` that nudges the menu poll to repaint the box."""
    global _np_waker
    _np_waker = fn


def pulse_now_playing() -> None:
    """Ask the active menu poll to repaint the now-playing box now (no-op if no
    poll is listening — e.g. the full player view drives its own redraws)."""
    if _np_waker is not None:
        try:
            _np_waker()
        except Exception:
            pass


def now_playing_signature() -> tuple | None:
    """The identity of the currently-shown track (see :func:`set_now_playing_signature`)."""
    return _now_playing_sig


def now_playing_lines(width: int) -> list[str]:
    """The now-playing box rows for ``width`` (empty list when nothing is playing)."""
    global _now_playing_lines
    if _now_playing_provider is None:
        _now_playing_lines = []
        return []
    try:
        lines = _now_playing_provider(width) or []
    except Exception:
        lines = []
    _now_playing_lines = list(lines)
    return _now_playing_lines


def now_playing_active() -> bool:
    """Whether a now-playing box is currently shown (cached from the last draw)."""
    return bool(_now_playing_lines)


def now_playing_height() -> int:
    """How many rows the now-playing box currently occupies (0 when inactive)."""
    return len(_now_playing_lines)


def set_status(task_id: str, message: str | None) -> None:
    """Update or remove a background task status."""
    if message is None:
        BACKGROUND_TASKS.pop(task_id, None)
    else:
        BACKGROUND_TASKS[task_id] = message


def has_background_tasks() -> bool:
    """Whether any background activity is currently running (drives the live,
    pulsing status indicator so the notice stays up until the work is done)."""
    return bool(BACKGROUND_TASKS)


# 256-colour greyscale brightness ramp (dim → white → dim) for the pulsing beacon.
_PULSE_RAMP = (238, 243, 248, 253, 255, 253, 248, 243)


def pulse_circle() -> str:
    """A white ● whose brightness pulses over time — the beacon next to a running
    background activity. The status bar is re-rendered ~8 Hz while a task is
    active (see the menu idle tick), which animates this."""
    code = _PULSE_RAMP[int(_time.time() * 6) % len(_PULSE_RAMP)]
    return f"\033[38;5;{code}m●{Colors.RESET}"


def show_status(message: str, duration: float = 3.0) -> None:
    """Flash a one-shot message in the status bar for `duration` seconds."""
    global _toast_message, _toast_expiry
    _toast_message = message
    _toast_expiry = _time.time() + duration


def show_loading(message: str) -> None:
    """Clear the screen and display a greyed loading message during long operations."""
    clear_screen()
    sys.stdout.write(f"\n  {Colors.DIM}{message}{Colors.RESET}\n")
    sys.stdout.flush()


def get_status_line() -> str:
    """Return the current status bar content (breadcrumb + tasks + toast)."""
    global _toast_message, _toast_expiry
    cols = get_terminal_width()

    if cols <= 0:
        return ""

    if _toast_message and _time.time() > _toast_expiry:
        _toast_message = ""

    sep = f"  {Colors.DIM}·{Colors.RESET}  "

    right_parts: list[str] = []
    for msg in BACKGROUND_TASKS.values():
        right_parts.append(f"{pulse_circle()} {Colors.DIM}{msg}{Colors.RESET}")
    if _toast_message:
        right_parts.append(f"{Colors.DIM}{_toast_message}{Colors.RESET}")
    right = sep.join(right_parts)

    crumb = _get_breadcrumb_str(cols // 2) if NAV_STACK else ""
    left = f"  {Colors.DIM}{crumb}{Colors.RESET}" if crumb else ""

    if left and right:
        gap = max(2, cols - visual_len(left) - visual_len(right) - 2)
        status = left + " " * gap + right + "  "
    elif left:
        status = left
    elif right:
        status = "  " + right + "  "
    else:
        status = ""

    if visual_len(status) > cols:
        status = clip_ansi(status, cols)
    return status

def get_terminal_size(default: tuple = (80, 24)) -> tuple:
    """Terminal (columns, rows), falling back to `default` if the query fails."""
    try:
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return default

def get_terminal_width(default: int = 80) -> int:
    """Terminal width in columns."""
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    """Terminal height in rows."""
    _, rows = get_terminal_size((default, default))
    return rows


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
    """Truncate `text` to `max_width`, replacing the cut end (or start, if
    `front`) with `placeholder`."""
    # front=True keeps the end of the string instead of the start
    if text is None:
        return ""
    if len(text) <= max_width:
        return text
    if max_width <= len(placeholder):
        return text[:max_width]

    if front:
        return placeholder + text[-(max_width - len(placeholder)):]
    else:
        return text[:max_width - len(placeholder)] + placeholder


def strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    return re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', s)

def visual_len(s: str) -> int:
    """Length of `s` as displayed, ignoring ANSI escape sequences."""
    return len(strip_ansi(s))


def clip_ansi(text: str, max_cols: int) -> str:
    """Truncate an ANSI-styled string to `max_cols` visible characters."""
    if max_cols <= 0:
        return ""
    if visual_len(text) <= max_cols:
        return text

    result: list[str] = []
    visible = 0
    i = 0
    while i < len(text) and visible < max_cols:
        if text[i] == "\x1b":
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not (0x40 <= ord(text[j]) <= 0x7E):
                    j += 1
                if j < len(text):
                    j += 1
            elif j < len(text):
                j += 1
            result.append(text[i:j])
            i = j
        else:
            result.append(text[i])
            visible += 1
            i += 1
    clipped = "".join(result)
    if not clipped.endswith(Colors.RESET):
        clipped += Colors.RESET
    return clipped


def divider(width: int | None = None, char: str = "─") -> str:
    """A horizontal rule of `char` spanning `width` (or the terminal width)."""
    width = width or get_terminal_width()
    return char * width
def format_time(seconds: int | float) -> str:
    """Convert seconds (may be float) to a compact time string.

    Preserves sub-second precision by appending centiseconds when the
    input contains a fractional portion. Behavior for large values is
    unchanged (hours/days/etc are shown as needed).
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        total = 0.0

    int_sec = int(total)
    frac_cs = int(round((total - int_sec) * 100))  # centiseconds (0-99)

    intervals = [31536000, 2592000, 86400, 3600, 60, 1]
    parts = []
    rem = int_sec
    for unit in intervals:
        parts.append(rem // unit)
        rem %= unit

    start = max(0, next((i for i, p in enumerate(parts[:-2]) if p > 0), len(parts) - 2))
    start = min(start, len(parts) - 2)

    result = [str(parts[start])]
    for p in parts[start + 1:]:
        result.append(str(p).zfill(2))

    base = ":".join(result)
    if frac_cs:
        return f"{base}.{frac_cs:02d}"
    return base


def _get_breadcrumb_str(width: int) -> str:
    """Render NAV_STACK as a '>'-joined breadcrumb, truncated with a leading
    ellipsis to fit `width`."""
    if width <= 1:
        return ""

    sep = " > "
    full_path = sep.join(NAV_STACK)

    max_length = max(0, width - 1)

    if len(full_path) > max_length:
        available_space = max_length - 3  # Leave 3 spaces for "..."
        if available_space > 0:
            full_path = "..." + full_path[-available_space:]
        else:
            full_path = full_path[-max_length:]

    return full_path

def roman(num):
    """Convert an integer to a Roman numeral."""

    _roman_map = OrderedDict()
    _roman_map[1000] = "M"
    _roman_map[900] = "CM"
    _roman_map[500] = "D"
    _roman_map[400] = "CD"
    _roman_map[100] = "C"
    _roman_map[90] = "XC"
    _roman_map[50] = "L"
    _roman_map[40] = "XL"
    _roman_map[10] = "X"
    _roman_map[9] = "IX"
    _roman_map[5] = "V"
    _roman_map[4] = "IV"
    _roman_map[1] = "I"

    def _to_roman(num):
        """Yield Roman-numeral symbols for `num`, largest value first."""
        for r in _roman_map.keys():
            x, y = divmod(num, r)
            yield _roman_map[r] * x
            num -= (r * x)
            if num <= 0:
                break

    return "".join([a for a in _to_roman(num)])


def get_progress_bar(progress: float, width: int = 40) -> str:
    """
    Exact mimic of the pip/rich progress bar style.
    [━━━━━━━━━━━━━━━━━━━━━━━━╸          ]
    """
    progress = max(0, min(1, progress))

    filled_width = progress * width
    whole_blocks = int(filled_width)
    remainder = filled_width - whole_blocks

    bar = "━" * whole_blocks

    # Add the "smooth" tip (the 'pip' secret sauce)
    if whole_blocks < width:
        if remainder > 0.6:
            bar += "━" # Almost full
        elif remainder > 0.2:
            bar += "╸" # Partial tip
        else:
            bar += " " # Not enough for a tip yet

    padding = " " * (width - len(bar))

    return f"{Colors.DIM}[{Colors.RESET}{Colors.PRIMARY}{bar}{padding}{Colors.RESET}{Colors.DIM}]{Colors.RESET}"

