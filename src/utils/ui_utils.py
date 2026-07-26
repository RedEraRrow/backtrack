"""Terminal helpers: ANSI colours, sizing, formatting, status bar, progress bar."""
from __future__ import annotations
import sys
import shutil
import signal
import textwrap
import time as _time
from typing import Any
import re
from collections import OrderedDict

from src.state import NAV_STACK

_resize_flag = False

def _sigwinch_handler(signum: int, frame: Any) -> None:
    global _resize_flag
    _resize_flag = True

signal.signal(signal.SIGWINCH, _sigwinch_handler)


def consume_resize() -> bool:
    """Returns True (and clears flag) if terminal was resized since last call."""
    global _resize_flag
    if _resize_flag:
        _resize_flag = False
        return True
    return False

# Global content margins.  All widgets and the playback UI read from here —
# change these two values to tune the whole app at once.
MARGIN_H = 2   # columns reserved on each horizontal side (left and right)
MARGIN_V = 1   # rows reserved on each vertical side (top and bottom)

class Colors:
    PRIMARY = "\033[1;37m" # White
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


def set_status(task_id: str, message: str | None) -> None:
    """Update or remove a background task status."""
    if message is None:
        BACKGROUND_TASKS.pop(task_id, None)
    else:
        BACKGROUND_TASKS[task_id] = message


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

    if _toast_message and _time.time() > _toast_expiry:
        _toast_message = ""

    sep = f"  {Colors.DIM}·{Colors.RESET}  "

    right_parts: list[str] = []
    for msg in BACKGROUND_TASKS.values():
        right_parts.append(f"{Colors.CYAN}●{Colors.RESET} {Colors.DIM}{msg}{Colors.RESET}")
    if _toast_message:
        right_parts.append(f"{Colors.DIM}{_toast_message}{Colors.RESET}")
    right = sep.join(right_parts)

    crumb = _get_breadcrumb_str(cols // 2) if NAV_STACK else ""
    left = f"  {Colors.DIM}{crumb}{Colors.RESET}" if crumb else ""

    if left and right:
        gap = max(2, cols - visual_len(left) - visual_len(right) - 2)
        return left + " " * gap + right + "  "
    if left:
        return left
    if right:
        return "  " + right + "  "
    return ""

def get_terminal_size(default: tuple = (80, 24)) -> tuple:
    try:
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return default

def get_terminal_width(default: int = 80) -> int:
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    _, rows = get_terminal_size((default, default))
    return rows


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
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
    return re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', s)

def visual_len(s: str) -> int:
    return len(strip_ansi(s))


def divider(width: int | None = None, char: str = "─") -> str:
    width = width or get_terminal_width()
    return char * width


def wrap_text(text: str, max_width: int = 80, margin: int = 6) -> list:
    wrap_width = max(20, max_width - margin)
    lines = []
    for line in text.split('\n'):
        if not line.strip():
            lines.append("")
        else:
            lines.extend(textwrap.wrap(line, width=wrap_width, drop_whitespace=False) or [""])
    return lines


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
    sep = " > "
    full_path = sep.join(NAV_STACK)

    max_length = width - 1

    if len(full_path) > max_length:
        available_space = max_length - 3  # Leave 3 spaces for "..."
        if available_space > 0:
            full_path = "..." + full_path[-available_space:]
        else:
            full_path = full_path[-max_length:]

    return full_path

def roman(num):

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


