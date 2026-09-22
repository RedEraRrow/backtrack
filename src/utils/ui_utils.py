"""Terminal helpers: ANSI colours, sizing, formatting, status bar, progress bar."""
from __future__ import annotations
import os
import sys
import shutil
import signal
import time as _time
from typing import Any
import re
import unicodedata
from collections import OrderedDict

from src.state import NAV_STACK

_resize_flag = False
_np_layout_dirty = False

# Memoised terminal size. `get_terminal_size` was an ioctl per call and the render
# path calls it per *line* (clipping) as well as per frame; the size only changes
# on SIGWINCH, which clears this. Without SIGWINCH (Windows) it re-reads on a
# short TTL instead.
_size_cache: tuple[int, int] | None = None
_size_cache_at: float = 0.0
_SIZE_TTL = 0.25

def _sigwinch_handler(signum: int, frame: Any) -> None:
    """Mark that the terminal was resized; consume_resize() picks this up."""
    global _resize_flag, _size_cache
    _resize_flag = True
    _size_cache = None                   # the memoised size is now wrong

# SIGWINCH doesn't exist on Windows — guard so importing ui_utils never raises there.
_HAS_SIGWINCH = hasattr(signal, "SIGWINCH")
if _HAS_SIGWINCH:
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

# Now-playing box transport geometry, shared so both places that depend on it
# can't drift apart: `playback_ui.format_now_playing_bar` draws the glyphs here
# and `prompt_core.now_playing_click_action` maps a click back to the one under
# the pointer. (start column, width) of ⏸/⏵ and ⏭ in the box's content columns.
#
# No previous-track button: the box is an ambient reminder of what is playing,
# and stepping backwards from it is a rarer thing to want than the space a third
# control costs. ^B still works, and is still advertised in the hint bar.
NP_GLYPH_COLS = ((0, 2), (4, 2))
NP_TITLE_OFFSET = 8            # where the title starts, just past the glyphs

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


# The styling half of Colors — everything that paints rather than moves the
# cursor. Suppressing colour must not suppress HIDE/SHOW, which are cursor
# control and still needed on a pipe.
_STYLE_NAMES = ('PRIMARY', 'WHITE', 'ACCENT', 'CYAN', 'YELLOW', 'MAGENTA', 'GREEN',
                'DIM', 'BOLD', 'ITALIC', 'UNDERLINE', 'RESET', 'BACK', 'INVERT')
_STYLE_CODES = {name: getattr(Colors, name) for name in _STYLE_NAMES}


def colour_enabled() -> bool:
    """Whether colour should be emitted: a terminal, and NO_COLOR unset.

    An empty NO_COLOR still counts as set — that is what the convention says,
    and `NO_COLOR=` in an environment is a deliberate act.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def set_colour(enabled: bool) -> None:
    """Turn every style code in `Colors` on or off, for the whole process.

    One switch rather than a check at each of the several hundred places a style
    is interpolated: the table renderer, the hint engine, the status bar and
    every screen already read their codes from here, so a pipe gets plain text
    without any of them knowing about it.
    """
    for name, code in _STYLE_CODES.items():
        setattr(Colors, name, code if enabled else "")


_screen_invalidator = None


def set_screen_invalidator(fn) -> None:
    """Register the painter's "forget what's on screen" hook.

    Registered by `prompt_core` (which can't be imported here — it imports this
    module), so every existing `clear_screen()` keeps meaning "the screen is now
    blank" for the diffed painter as well.
    """
    global _screen_invalidator
    _screen_invalidator = fn


def _screen_cleared() -> None:
    """Tell the painter the screen was wiped outside its own frame writes."""
    if _screen_invalidator is not None:
        try:
            _screen_invalidator()
        except Exception:
            pass


def enter_alt_screen() -> None:
    """Switch to the terminal alternate screen buffer (no scrollback).

    Also enables focus in/out reporting (\\033[?1004h) so editors can show a
    hollow cursor when the window loses focus; unsupported terminals ignore it.
    """
    sys.stdout.write("\033[?1049h\033[?1004h\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()


def exit_alt_screen() -> None:
    """Restore the main screen buffer, disable focus reporting, show the cursor."""
    sys.stdout.write("\033[?25h\033[?1004l\033[?1049l")
    sys.stdout.flush()


def clear_screen() -> None:
    """Overwrite screen content from home without triggering scrollback save.

    Leaves the cursor **hidden**: a bare clear parks it at home, where it blinks
    in the top-left corner until the next frame happens to hide it — during a
    library build or any slow step, that's a visible flashing caret.
    """
    sys.stdout.write("\033[H\033[3J\033[J" + Colors.HIDE)
    sys.stdout.flush()
    _screen_cleared()

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
        # A provider that raised tells us nothing about what's playing — keep the
        # box exactly as it was rather than blinking it out and back next tick.
        return _now_playing_lines
    _now_playing_lines = list(lines)
    return _now_playing_lines


def now_playing_active() -> bool:
    """Whether a now-playing box is currently shown (cached from the last draw)."""
    return bool(_now_playing_lines)


# Audio is playing but the box could not be drawn — the terminal is too narrow
# for it. The box normally advertises the transport keys in its own top border,
# so this is the one state where the hint bar has to advertise them instead
# (see `prompt.chrome_hint_pairs`). Deliberately *not* set when the full player
# view owns the display: that view shows its own transport.
_np_unboxed: bool = False


def set_now_playing_unboxed(value: bool) -> None:
    """Record whether transport is live with no now-playing box to advertise it."""
    global _np_unboxed
    _np_unboxed = bool(value)


def now_playing_unboxed() -> bool:
    """Whether transport is live but no box is drawn to show its keys."""
    return _np_unboxed


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


def print_inline_progress(message: str, progress: float) -> None:
    """Redraw a single in-place line for a blocking, no-other-redraw loop
    (a per-track ffmpeg pass): pulsing beacon + bar so a slow scan still
    looks alive instead of a hung terminal, inset by MARGIN_H and centred
    like the rest of the chrome. Call `clear_inline_progress()` once the
    loop finishes."""
    bar = get_progress_bar(progress, 24)
    width = get_terminal_width()
    avail = max(1, width - 2 * MARGIN_H)
    prefix_len = visual_len(f"{pulse_circle()} {bar} ")
    if prefix_len + len(message) > avail:
        message = message[:max(0, avail - prefix_len - 1)] + "…"
    content = f"{pulse_circle()} {bar} {Colors.DIM}{message}{Colors.RESET}"
    pad = max(MARGIN_H, (width - visual_len(content)) // 2)
    sys.stdout.write(f"\r{' ' * pad}{content}\033[K")
    sys.stdout.flush()


def clear_inline_progress() -> None:
    """Erase the line left by `print_inline_progress()`."""
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


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
    """Terminal (columns, rows), falling back to `default` if the query fails.

    Memoised — see `_size_cache`. Call `invalidate_terminal_size()` if something
    other than a resize could have changed it.
    """
    global _size_cache, _size_cache_at
    if _size_cache is not None:
        if _HAS_SIGWINCH or (_time.monotonic() - _size_cache_at) < _SIZE_TTL:
            return _size_cache
    try:
        size = shutil.get_terminal_size()
    except OSError:
        return default
    _size_cache = (size.columns, size.lines)
    _size_cache_at = _time.monotonic()
    return _size_cache


def invalidate_terminal_size() -> None:
    """Drop the memoised terminal size (next query re-reads it)."""
    global _size_cache
    _size_cache = None

def get_terminal_width(default: int = 80) -> int:
    """Terminal width in columns."""
    cols, _ = get_terminal_size((default, default))
    return cols


def get_terminal_height(default: int = 24) -> int:
    """Terminal height in rows."""
    _, rows = get_terminal_size((default, default))
    return rows


# ---------------------------------------------------------------------------
# Display width: one ANSI scanner, one column table, for the whole app.
#
# Every module that measures, clips or pads a styled line goes through this
# block. It used to be five divergent implementations — each with its own idea
# of which escape sequences exist — and the width maths disagreed between them,
# so a line measured in one module and clipped in another could overrun.
# ---------------------------------------------------------------------------

# The escape sequences a terminal consumes without drawing anything. In order:
# CSI (including private `?` parameters and intermediate bytes, so `\033[?25l`
# and `\033[3J` are recognised, not just SGR); the Kitty graphics protocol used
# by the album-art renderer; OSC, terminated by either ST or BEL; the remaining
# string-introducers (DCS/SOS/PM/APC); and a bare two-byte escape as a backstop.
_ANSI_RE = re.compile(
    r'(\x1b\[[0-9;?]*[ -/]*[@-~])'
    r'|(\x1b_G[^\x1b]*\x1b\\)'
    r'|(\x1b\][^\x1b\x07]*(?:\x1b\\|\x07))'
    r'|(\x1b[PX^_].*?\x1b\\)'
    r'|(\x1b.)'
)


def _scan(s: str):
    """Yield ``(is_escape, chunk)`` across `s`.

    One chunk per escape sequence, one per printable character — the single
    walk every width routine below is built on, so measuring and clipping can
    never disagree about where an escape starts or ends.
    """
    i = 0
    n = len(s)
    while i < n:
        if s[i] == '\x1b':
            m = _ANSI_RE.match(s, i)
            if m:
                yield True, m.group(0)
                i = m.end()
                continue
        yield False, s[i]
        i += 1


def strip_ansi(s: str) -> str:
    """`s` with every ANSI escape sequence removed."""
    if '\x1b' not in s:
        return s
    return _ANSI_RE.sub('', s)


# Codepoints that occupy no column of their own: the variation selectors that
# pick a glyph's text/emoji presentation, the zero-width joiner family, and
# combining marks that stack onto the character before them.
_ZERO_WIDTH = ('︎', '️', '​', '‌', '‍')

_ZERO_WIDTH_SET = frozenset(_ZERO_WIDTH)

# Codepoints a terminal draws two cells wide. Two sources: East Asian Wide and
# Fullwidth (handled below via unicodedata), and emoji-presentation characters,
# which UTR#51 says to render wide and which every modern terminal does. The
# media-control glyphs are the app's own case, and their true width is not
# knowable from Unicode alone. U+23EE ⏮ and U+23ED ⏭ are emoji-by-default; U+23F8
# ⏸ and U+23F5 ⏵ are text-by-default and "should" be one cell — but no monospace
# font on a stock macOS box carries any of them, so they are drawn by whichever
# fallback font does, and Apple Color Emoji (which has ⏮ ⏸ ⏭) draws two cells
# wide whatever the default presentation says. All four report east-asian-width
# N, so nothing available here can tell them apart.
#
# They are listed as wide deliberately, as an over-estimate. Reserving two cells
# and getting one leaves a small gap; reserving one and getting two overruns
# whatever sits to the right — and that is the miniplayer's closing border.
_WIDE_SET = frozenset('⏮⏭⏸⏵⏪⏩⏫⏬⏯⏱⏲⏰')

_cols_cache: dict = {}


def char_cols(ch: str) -> int:
    """How many terminal columns `ch` occupies: 0, 1 or 2."""
    w = _cols_cache.get(ch)
    if w is None:
        if ch in _ZERO_WIDTH_SET or unicodedata.combining(ch):
            w = 0
        elif ch in _WIDE_SET or unicodedata.east_asian_width(ch) in ('W', 'F'):
            w = 2
        else:
            w = 1
        _cols_cache[ch] = w
    return w


def display_text(s: str) -> str:
    """`s` with ANSI escapes and zero-width codepoints removed.

    Every remaining character occupies at least one column, but *not* always
    exactly one — a wide glyph still takes two, so `visual_len` is what you
    want for width maths. This is for callers that need the plain characters
    themselves (cursor hit-testing, writing a styled report out as text).
    """
    out = strip_ansi(s)
    if out.isascii():
        return out
    return ''.join(ch for ch in out if char_cols(ch) != 0)


def visual_len(s: str) -> int:
    """Columns `s` occupies on screen, ignoring ANSI escapes.

    Not the same as `len`: escapes and combining marks take no column, and an
    emoji-presentation or East-Asian-wide character takes two. The ASCII fast
    path keeps the common case a plain length — this is called per line in the
    render path.
    """
    if not s:
        return 0
    if s.isascii() and '\x1b' not in s:
        return len(s)
    return sum(char_cols(ch) for esc, ch in _scan(s) if not esc)


def clip_ansi(text: str, max_cols: int, reset: bool = True) -> str:
    """`text` truncated to `max_cols` visible columns, escapes preserved.

    Escapes ride along without being counted, so the styling that survives the
    cut still closes properly; a zero-width mark rides along with the glyph it
    belongs to; and a two-cell glyph is never split across the boundary — it is
    dropped whole, leaving a one-column gap, because half of one renders as a
    stray cell that pushes everything after it out of line.

    `reset` appends a reset when the string was actually cut, so the clipped
    line can't bleed colour into whatever is drawn after it. Callers that go on
    to concatenate more styled content onto the result pass False.
    """
    if max_cols <= 0:
        return ""
    out: list[str] = []
    visible = 0
    truncated = False
    for esc, chunk in _scan(text):
        if esc:
            out.append(chunk)
            continue
        w = char_cols(chunk)
        if w and visible + w > max_cols:
            truncated = True
            break
        out.append(chunk)
        visible += w
    res = "".join(out)
    if truncated and reset and not res.endswith(Colors.RESET):
        res += Colors.RESET
    return res


def truncate_text(text: str, max_width: int, placeholder: str = "…", front: bool = False) -> str:
    """Truncate `text` to `max_width` columns, replacing the cut end (or start,
    if `front`) with `placeholder`.

    Measured in columns rather than codepoints, so a CJK or emoji run is cut
    where it actually reaches the edge instead of a character count that
    overruns it by up to 2×.
    """
    if text is None:
        return ""
    if visual_len(text) <= max_width:
        return text
    ph_w = visual_len(placeholder)
    if max_width <= ph_w:
        return clip_ansi(text, max_width, reset=False)

    keep = max_width - ph_w
    if front:
        # Walk from the right, taking whole glyphs until `keep` columns are full.
        taken: list[str] = []
        used = 0
        for ch in reversed(text):
            w = char_cols(ch)
            if w and used + w > keep:
                break
            taken.append(ch)
            used += w
        return placeholder + "".join(reversed(taken))
    return clip_ansi(text, keep, reset=False) + placeholder


def plural(n: int, singular: str, many: str | None = None) -> str:
    """``"1 result"`` / ``"156 results"`` — the count and its noun, agreeing.

    The app used to write "156 result(s)" everywhere. "(s)" is a note to the
    reader that the program didn't know which it was; it always does.
    """
    return f"{n} {singular if abs(n) == 1 else (many or singular + 's')}"


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
    """Render NAV_STACK as a '>'-joined breadcrumb that fits `width`.

    Over-long trails shed whole path components from the front, keeping the
    deepest ones — those say where you are; the ones above are context you can
    infer. Slicing the joined string by character instead turned "Bleak
    Expectations > A Childhood Cruelly Kippered" into "…pectations > A Childhood
    Cruelly Kippered", where the leading fragment is a word that was never in
    the path and reads as one.

    The last component is kept whatever it costs: a breadcrumb that has dropped
    everything still has to name where you are. If it alone doesn't fit, it is
    truncated at its own end so it starts with something real.
    """
    if width <= 1 or not NAV_STACK:
        return ""

    sep = " > "
    max_length = max(0, width - 1)

    if len(sep.join(NAV_STACK)) <= max_length:
        return sep.join(NAV_STACK)

    # Keep the deepest components that fit, prefixed with "… > " to show the
    # trail was cut. Walk outward from the last one.
    kept: list[str] = [NAV_STACK[-1]]
    for name in reversed(NAV_STACK[:-1]):
        if len("… > " + sep.join([name] + kept)) > max_length:
            break
        kept.insert(0, name)

    out = "… > " + sep.join(kept)
    if len(out) <= max_length:
        return out
    # Not even the deepest component fits beside the marker — truncate it from
    # its end, so what remains is the start of a real name rather than the tail
    # of one. truncate_text handles a width too small for the ellipsis itself.
    return truncate_text(NAV_STACK[-1], max_length)

def roman(num):
    """Convert an integer to a Roman numeral (see `utils.numbering`, which
    owns the conversion shared with the pattern tools)."""
    from src.utils.numbering import roman as _roman
    return _roman(num)


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

