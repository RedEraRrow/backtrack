"""
Shared UI utilities and formatting functions.
Centralises all display logic to avoid circular imports and code duplication.
"""
from __future__ import annotations
import os
import sys
import shutil
import signal
import textwrap
from typing import Any

from src.state import NAV_STACK

# ============================================================================
# Resize Detection (SIGWINCH)
# ============================================================================

_resize_flag = False

def _sigwinch_handler(signum: int, frame: Any) -> None:
    """Handle terminal resize signal."""
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

# ============================================================================
# ANSI Colour Codes
# ============================================================================

class Colours:
    """ANSI colour codes for terminal output."""
    PRIMARY = "\033[1;37m"      # White
    ACCENT = "\033[1;31m"       # Red
    SUCCESS = "\033[1;32m"      # Green
    CYAN = "\033[1;36m"
    YELLOW = "\033[1;33m"
    MAGENTA = "\033[1;35m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


# ============================================================================
# Screen & Display
# ============================================================================

def clear_screen() -> None:
    """Clear terminal screen synchronously using ANSI escape."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

BACKGROUND_TASKS = {}

def set_status(task_id: str, message: str | None):
    """Update or remove a background task status."""
    if message is None:
        BACKGROUND_TASKS.pop(task_id, None)
    else:
        BACKGROUND_TASKS[task_id] = message

def get_status_line() -> str:
    """Formats all active tasks into a single line for the bottom of the screen."""
    if not BACKGROUND_TASKS:
        return ""
    
    tasks = [f"{Colours.CYAN}●{Colours.RESET} {msg}" for msg in BACKGROUND_TASKS.values()]
    return f"{Colours.DIM} │ {' | '.join(tasks)}{Colours.RESET}"

def get_terminal_size(default: tuple = (80, 24)) -> tuple:
    """Get terminal size (columns, rows), with fallback."""
    try:
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    except Exception:
        return default


def get_terminal_width(default: int = 80) -> int:
    """Get terminal width, with fallback."""
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    """Get terminal height, with fallback."""
    _, rows = get_terminal_size((default, default))
    return rows


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
    """Truncate text to fit within `max_width` characters.
    
    Args:
        text: Text to truncate
        max_width: Maximum width in characters
        placeholder: String to use for truncation indicator
        front: If True, truncate from front (keep end); if False, truncate from end (keep start)
    """
    if text is None:
        return ""
    if len(text) <= max_width:
        return text
    if max_width <= len(placeholder):
        return text[:max_width]
    
    if front:
        # Front-truncate: keep the end, truncate from beginning
        return placeholder + text[-(max_width - len(placeholder)):]
    else:
        # Back-truncate: keep the start, truncate from end
        return text[:max_width - len(placeholder)] + placeholder


def divider(width: int | None = None, char: str = "─") -> str:
    """Return a divider line matching the given width or terminal width."""
    width = width or get_terminal_width()
    return char * width


def wrap_text(text: str, max_width: int = 80, margin: int = 6) -> list:
    """Wrap text to fit terminal width."""
    wrap_width = max(20, max_width - margin)
    lines = []
    for line in text.split('\n'):
        if not line.strip():
            lines.append("")
        else:
            lines.extend(textwrap.wrap(line, width=wrap_width, drop_whitespace=False) or [""])
    return lines


# ============================================================================
# Formatting Utilities
# ============================================================================

def format_time(seconds: int | float) -> str:
    """Convert seconds to mm:ss (or longer) format."""
    seconds = int(seconds)
    intervals = [31536000, 2592000, 86400, 3600, 60, 1]
    parts = []
    rem = seconds
    
    for unit in intervals:
        parts.append(rem // unit)
        rem %= unit
    
    start = max(0, next((i for i, p in enumerate(parts[:-2]) if p > 0), len(parts) - 2))
    start = min(start, len(parts) - 2)
    
    result = [str(parts[start])]
    for p in parts[start + 1:]:
        result.append(str(p).zfill(2))
    
    return ":".join(result)


def format_duration_ms(milliseconds: int | float) -> str | None:
    """Format milliseconds as mm:ss (minutes:seconds)."""
    try:
        ms = int(milliseconds)
        mins = ms // 60000
        secs = (ms % 60000) // 1000
        return f"{mins}m {secs}s"
    except (ValueError, TypeError):
        return None


def format_file_size(size_bytes: int | float) -> str | None:
    """Format bytes as human-readable size."""
    try:
        size_bytes = int(size_bytes)
        size_mb = size_bytes / (1024 * 1024)
        return f"{size_mb:.1f} MB"
    except (ValueError, TypeError):
        return None


def format_bitrate(bitrate: int | float | str | None) -> str | None:
    """Format bitrate with units."""
    return f"{bitrate} kbps" if bitrate else None


def format_sample_rate(sample_rate: int | float | str | None) -> str | None:
    """Format sample rate with units."""
    return f"{sample_rate} Hz" if sample_rate else None

def _get_breadcrumb_str(width: int) -> str:
    """Get breadcrumb navigation string."""
    sep = " > "
    full_path = sep.join(NAV_STACK)
    
    # Max visible length to avoid wrapping to the next line
    max_length = width - 1 
    
    if len(full_path) > max_length:
        available_space = max_length - 3  # Leave 3 spaces for "..."
        if available_space > 0:
            full_path = "..." + full_path[-available_space:]
        else:
            # Extreme fallback for incredibly narrow terminal windows
            full_path = full_path[-max_length:]
            
    return full_path


# ============================================================================
# Progress Bar
# ============================================================================

def get_progress_bar(progress: float, width: int = 40) -> str:
    """
    Exact mimic of the pip/rich progress bar style.
    [━━━━━━━━━━━━━━━━━━━━━━━━╸          ]
    """
    # Constrain progress
    progress = max(0, min(1, progress))
    
    filled_width = progress * width
    whole_blocks = int(filled_width)
    remainder = filled_width - whole_blocks
    
    # The 'pip' character set for smooth transitions
    # Using '━' (Heavy Horizontal) and '╸' (Heavy Left Tip)
    bar_chars = "━" 
    
    # Build the filled portion
    bar = bar_chars * whole_blocks
    
    # Add the "smooth" tip (the 'pip' secret sauce)
    if whole_blocks < width:
        if remainder > 0.6:
            bar += "━" # Almost full
        elif remainder > 0.2:
            bar += "╸" # Partial tip
        else:
            bar += " " # Not enough for a tip yet
            
    # Fill the rest with empty space
    padding = " " * (width - len(bar))
    
    # Return with your UI colours
    return f"{Colours.DIM}[{Colours.RESET}{Colours.SUCCESS}{bar}{padding}{Colours.RESET}{Colours.DIM}]{Colours.RESET}"


# ============================================================================
# Metadata Display
# ============================================================================

def display_metadata_section(title: str, data: dict, formatted_keys: dict | None = None) -> None:
    """Display a metadata section with formatted output."""
    formatted_keys = formatted_keys or {}
    
    if not data:
        return
    
    print(f"\n{title}:")
    for key, value in data.items():
        if value is None:
            continue
        
        # Apply custom formatting if available
        if key in formatted_keys:
            value = formatted_keys[key](value)
        
        if value:
            print(f"  {key:<20}: {value}")


def get_xml_metadata_lines(metadata: dict) -> list[str]:
    """Return comprehensive XML metadata as a list of strings for UI headers."""
    xml_data = metadata.get('xml_data') or metadata
    if not xml_data:
        return []
    
    cols = get_terminal_width()
    lines = ["", divider(cols, "═"), "  LIBRARY METADATA (from Library.xml)", divider(cols, "═")]
    
    sections = {
        "Track Info": ["Name", "Artist", "Album Artist", "Composer", "Album"],
        "Disc/Track": ["Track Number", "Track Count", "Disc Number", "Disc Count"],
        "Dates": ["Year", "Release Date", "Date Added", "Date Modified"],
        "Playback": ["Play Count", "Skip Count"],
        "Technical": ["Kind", "Total Time", "Bit Rate", "Sample Rate", "Size"],
        "Protection": ["Protected", "Apple Music"],
    }
    
    formatters = {
        "Total Time": format_duration_ms,
        "Size": format_file_size,
        "Bit Rate": format_bitrate,
        "Sample Rate": format_sample_rate,
    }
    
    for section_name, fields in sections.items():
        section_data = {k: xml_data.get(k) for k in fields if xml_data.get(k)}
        if section_data:
            lines.append(f"\n{section_name}:")
            for key, value in section_data.items():
                val = formatters.get(key, lambda v: v)(value)
                if val:
                    lines.append(f"  {key:<20}: {val}")
    
    lines.append("\n" + divider(cols, "═"))
    return lines

def display_xml_metadata(metadata: dict) -> None:
    """Display comprehensive XML metadata (legacy print version)."""
    for line in get_xml_metadata_lines(metadata):
        print(line)