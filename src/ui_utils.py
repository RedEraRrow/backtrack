"""
Shared UI utilities and formatting functions.
Centralises all display logic to avoid circular imports and code duplication.
"""

import os
import sys
import shutil
import signal
import textwrap

from src.state import NAV_STACK

# ============================================================================
# Resize Detection (SIGWINCH)
# ============================================================================

_resize_flag = False

def _sigwinch_handler(signum, frame):
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

def clear_screen():
    """Clear terminal screen."""
    os.system('clear')


def get_terminal_size(default=(80, 24)):
    """Get terminal size (columns, rows), with fallback."""
    try:
        size = shutil.get_terminal_size()
        return size.columns, size.lines
    except Exception:
        return default


def get_terminal_width(default=80):
    """Get terminal width, with fallback."""
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default=24):
    """Get terminal height, with fallback."""
    _, rows = get_terminal_size((default, default))
    return rows


def truncate_text(text, max_width, placeholder="…", front=False):
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


def divider(width=None, char="─"):
    """Return a divider line matching the given width or terminal width."""
    width = width or get_terminal_width()
    return char * width


def wrap_text(text, max_width=80, margin=6):
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

def format_time(seconds):
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


def format_duration_ms(milliseconds):
    """Format milliseconds as mm:ss (minutes:seconds)."""
    try:
        ms = int(milliseconds)
        mins = ms // 60000
        secs = (ms % 60000) // 1000
        return f"{mins}m {secs}s"
    except (ValueError, TypeError):
        return None


def format_file_size(size_bytes):
    """Format bytes as human-readable size."""
    try:
        size_bytes = int(size_bytes)
        size_mb = size_bytes / (1024 * 1024)
        return f"{size_mb:.1f} MB"
    except (ValueError, TypeError):
        return None


def format_bitrate(bitrate):
    """Format bitrate with units."""
    return f"{bitrate} kbps" if bitrate else None


def format_sample_rate(sample_rate):
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

def display_metadata_section(title, data, formatted_keys=None):
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


def display_xml_metadata(metadata):
    """Display comprehensive XML metadata with organised sections."""
    xml_data = metadata.get('xml_data') or metadata
    if not xml_data:
        return
    cols = get_terminal_width()
    print("\n" + divider(cols, "═"))
    print("  LIBRARY METADATA (from Library.xml)")
    print(divider(cols, "═"))
    
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
            formatted = {k: formatters.get(k, lambda v: v)(v) for k, v in section_data.items()}
            display_metadata_section(section_name, formatted)
    
    print("\n" + divider(cols, "═"))