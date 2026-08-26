"""Terminal primitives shared across prompt widgets."""
from __future__ import annotations
import re
import sys
import os
import math
import textwrap
import time
import select as _sel
from typing import Any, Callable, Literal, overload

from src.utils import ui_utils
C = ui_utils.Colors

_IS_WINDOWS = os.name == "nt"

_COLUMNS_MAX_WIDTH = 160   # cap effective width for table layout even on ultra-wide terminals
_EDGE_MARGIN       = 2     # right-side padding for pinned columns
_MIN_COL_FLOOR     = 6     # a squeezed column shrinks to at most this before it stops giving up space
_MIN_PIN_GAP       = 2     # reserved breathing space between the left block and right-pinned columns

tty: Any
termios: Any
msvcrt: Any

if _IS_WINDOWS:
    import msvcrt
else:
    import tty
    import termios


def _get_term_attrs(fd: int):
    """Current termios attributes for fd, or None on Windows."""
    return None if _IS_WINDOWS else termios.tcgetattr(fd)


def _set_raw(fd: int) -> None:
    """Put the terminal into raw mode (no-op on Windows)."""
    if not _IS_WINDOWS:
        tty.setraw(fd)


def _restore_term_attrs(fd: int, old):
    """Restore terminal attributes captured before raw mode."""
    if not _IS_WINDOWS and old is not None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


_np_prev_h = [0]
_np_prev_lines: list = [None]
_np_prev_sig: list = [object()]          # last-drawn track identity (never == a real sig)
_np_last_draw = [0.0]
_status_prev_active = [False]            # was a background task shown last idle tick?

# Self-pipe so background threads can wake the menu poll to repaint the box the
# instant playback state changes (#14) — see ui_utils.pulse_now_playing(). The
# poll's select() watches the read end alongside stdin; a pulse makes it return
# immediately and repaint, rather than waiting on the next keystroke or timeout.
try:
    _wake_r, _wake_w = os.pipe()
    os.set_blocking(_wake_r, False)
    os.set_blocking(_wake_w, False)
except OSError:                          # no pipes (e.g. odd sandbox) — degrade gracefully
    _wake_r = _wake_w = -1


def _poke_now_playing_wake() -> None:
    """Write one byte to the wake pipe (coalesced by the reader; never blocks)."""
    if _wake_w >= 0:
        try:
            os.write(_wake_w, b'.')
        except (BlockingIOError, OSError):
            pass                          # pipe full already → a wake is pending anyway


if _wake_r >= 0:
    ui_utils.set_now_playing_waker(_poke_now_playing_wake)


# ---------------------------------------------------------------------------
# Persistent screen model.
#
# One entry per screen row holding what is currently displayed there, shared by
# every writer (widget frames, the now-playing box, the status bar). A repaint
# writes only the rows whose content actually changed and never erases a row
# before rewriting it — the old "erase to end of screen, then redraw everything"
# frame is what made the whole screen flicker and the miniplayer blink out and
# back on every keystroke. Rows are also written *absolutely*, with no newlines,
# so a line-buffered stdout cannot flush a half-drawn frame.
_screen: dict[int, str] = {}


def screen_invalidate() -> None:
    """Forget what is on screen — after a full clear, a resize, or a write by
    something that doesn't go through here (the player view)."""
    _screen.clear()


def _register_screen_hooks() -> None:
    """Let `ui_utils.clear_screen()` (and the alt-screen switch) drop the model."""
    ui_utils.set_screen_invalidator(screen_invalidate)


_takeover_pending = [False]


def screen_takeover_next() -> None:
    """Take the screen over on the next frame *without* clearing it first.

    Called where a widget used to clear on entry: the next paint overwrites the
    rows it needs and blanks whatever the previous screen left behind, in the
    same flush. That removes the blank flash between every screen — a clear is
    only genuinely needed when the terminal reflowed (resize) or something
    painted outside this model.
    """
    _takeover_pending[0] = True


def _takeover_rows(frame: dict) -> dict:
    """Add blanks for rows the previous screen owned that `frame` doesn't."""
    if not _takeover_pending[0]:
        return frame
    _takeover_pending[0] = False
    out = dict(frame)
    for row in _screen:
        out.setdefault(row, "")
    return out


def screen_rows() -> set:
    """Every row the painter currently believes it knows the content of."""
    return set(_screen)


def screen_forget_rows(first: int, last: int) -> None:
    """Forget rows `first`..`last` inclusive (another writer owns them now)."""
    for r in range(first, last + 1):
        _screen.pop(r, None)


def screen_row_paint(row: int, text: str, extra: str = "") -> str:
    """The escape string that paints one row as `text` with `extra` layered on
    top, or "" when the row already reads exactly that way.

    `extra` is for absolute overlays that write a few columns of a row something
    else owns (the volume bar sits on the album art's rows). Both layers are part
    of the row's identity, so a row repaints when *either* changes — and a row
    whose overlay went away is erased rather than keeping stale glyphs. A blank
    row is content too: "" differs from anything previously drawn there.
    """
    key = f"{text}\x00{extra}"
    if _screen.get(row) == key:
        return ""
    _screen[row] = key
    return f"\033[{row};1H\033[2K{text}{extra}"


def screen_row_segment(row: int, text: str) -> str:
    """Paint one row of plain text (no overlay) — see `screen_row_paint`."""
    return screen_row_paint(row, text)


def screen_paint(rows: dict, *, cursor: tuple | None = None,
                 hide_cursor: bool = True, save_cursor: bool = False) -> None:
    """Paint `rows` ({1-based row: text}) as one buffered, single-syscall frame.

    Unchanged rows cost nothing. `cursor` places the caret and shows it (text
    inputs); `save_cursor` wraps the frame in DEC save/restore so a caret
    elsewhere is left alone (background repaints).
    """
    rows = _takeover_rows(rows)
    parts: list[str] = []
    for row in sorted(rows):
        seg = screen_row_segment(row, rows[row])
        if seg:
            parts.append(seg)
    if not parts:
        return                             # nothing changed: draw nothing at all
    body = "".join(parts)
    if save_cursor:
        out = "\0337" + body + "\0338"
    else:
        out = (C.HIDE if hide_cursor else "") + body
        if cursor is not None:
            out += f"\033[{cursor[0]};{cursor[1]}H" + C.SHOW
    sys.stdout.write(out)
    sys.stdout.flush()


def _np_box_str(rows: int, lines: list) -> str:
    """Escape string that draws the now-playing box in the rows just above the
    breadcrumb, clearing any band a taller previous box left behind. Updates the
    shared cache so the widget render and the idle tick agree on what's shown."""
    if rows <= 1:
        _np_prev_h[0] = 0
        _np_prev_lines[0] = []
        _np_prev_sig[0] = ui_utils.now_playing_signature()
        return ""

    max_box_rows = max(0, rows - 1)
    lines = lines[-max_box_rows:]
    h = len(lines)
    band = max(_np_prev_h[0], h)
    # Rows the box no longer covers are blanked; rows it does are painted — both
    # through the shared screen model, so an unchanged box emits nothing at all
    # and a shrinking one clears exactly the rows it gave up.
    wanted: dict[int, str] = {}
    for k in range(band):
        row = rows - band + k
        if 1 <= row < rows:
            wanted[row] = ""
    for k in range(h):
        row = rows - h + k
        if 1 <= row < rows:
            wanted[row] = lines[k]
    parts: list[str] = []
    for row in sorted(wanted):
        seg = screen_row_segment(row, wanted[row])
        if seg:
            parts.append(seg)
    _np_prev_h[0] = h
    _np_prev_lines[0] = lines
    _np_prev_sig[0] = ui_utils.now_playing_signature()
    return "".join(parts)


def now_playing_height_for_layout() -> int:
    """Rows the now-playing box occupies, as the frame layout should assume."""
    return max(_np_prev_h[0], ui_utils.now_playing_height())


def now_playing_box_segment() -> str:
    """The box draw-string for embedding in a widget's own atomic flush (so
    navigation redraws it alongside the list instead of leaving it flashed out)."""
    rows = ui_utils.get_terminal_height()
    if rows <= 1:
        return ""
    cols = ui_utils.get_terminal_width()
    return _np_box_str(rows, ui_utils.now_playing_lines(cols))


def invalidate_now_playing_box() -> None:
    """Drop the last-drawn box cache so the next idle tick repaints unconditionally.
    Used on focus-in (#14): while a window is unfocused the terminal may not paint
    our box writes, yet the cache advances as if it had — leaving the box stale
    after refocus until an interaction. Forcing a repaint fixes it without a click."""
    _np_prev_lines[0] = None
    _np_prev_sig[0] = object()          # sentinel: never equal to a real signature
    _np_last_draw[0] = 0.0              # let the next poll repaint immediately


def _render_now_playing_bar() -> None:
    """Idle-tick refresh so the clock/progress advance (and a background track
    change lands) when nothing else redraws. Repaints when either the track
    identity or the styled rows changed, so an idle screen never flickers yet a
    new song is never missed (#14)."""
    rows = ui_utils.get_terminal_height()
    cols = ui_utils.get_terminal_width()
    lines = ui_utils.now_playing_lines(cols)
    if len(lines) != _np_prev_h[0]:
        # The box appeared or vanished — the menu must re-reserve rows for it.
        ui_utils.mark_now_playing_layout_dirty()
    if ui_utils.now_playing_signature() == _np_prev_sig[0] and lines == _np_prev_lines[0]:
        return
    seg = _np_box_str(rows, lines)
    if seg:                       # unchanged rows produce nothing to write
        sys.stdout.write("\0337" + seg + "\0338")
        sys.stdout.flush()


def _wait_for_keypress(timeout: float = 0.05) -> bool:
    """Block up to `timeout` seconds for a keypress; return whether one arrived.

    Also refreshes the now-playing box (#14) at ~4 Hz so background-audio status
    stays live on every widget/menu without each one needing its own tick."""
    now = time.time()
    if now - _np_last_draw[0] >= 0.12:
        _np_last_draw[0] = now
        try:
            _render_now_playing_bar()
        except Exception:
            pass
        # Keep the background-activity notice live: while a task is running the
        # status bar is re-stamped each tick so it stays up for the whole job and
        # its cyan ● pulses; one extra redraw after the last task clears the bar.
        active = ui_utils.has_background_tasks()
        if active or _status_prev_active[0]:
            try:
                _render_status_bar()
            except Exception:
                pass
        _status_prev_active[0] = active
    if _IS_WINDOWS:
        end = time.time() + timeout
        while time.time() < end:
            if msvcrt.kbhit():
                return True
            time.sleep(0.01)
        return False
    watch = [sys.stdin, _wake_r] if _wake_r >= 0 else [sys.stdin]
    ready = _sel.select(watch, [], [], timeout)[0]
    if _wake_r >= 0 and _wake_r in ready:
        try:
            os.read(_wake_r, 4096)        # drain all coalesced pulses
        except OSError:
            pass
        _np_last_draw[0] = time.time()    # this pulse counts as the tick
        try:
            _render_now_playing_bar()     # repaint immediately on a state change
        except Exception:
            pass
    return sys.stdin in ready             # a wake alone is not a keypress


def _clrline():
    """Clear the current line and return the cursor to column 1."""
    return "\033[2K\r"
def _goto(row, col=1):
    """Move the cursor to `row`, `col` (1-based)."""
    return f"\033[{row};{col}H"
def _col(n):
    """Move the cursor to column `n` on the current row."""
    return f"\033[{n}G"


def _cols() -> int:
    """Usable terminal width after subtracting the horizontal margins."""
    return max(1, ui_utils.get_terminal_width() - 2 * ui_utils.MARGIN_H)



def _hint(*pairs, extra="") -> str:
    """
    Highly adaptive layout engine for bottom hints.
    Cascades: Centred Long Line -> Pyramid -> Grid -> Aligned Vertical Stack -> Split Vertical Stack.
    """
    if not pairs and not extra:
        return ""

    cols = _cols()

    # Parse items into structured tuples: (key, value, raw_string_for_math)
    parsed_items = []
    for k, v in pairs:
        parsed_items.append((k, v, f"[{k}] {v}"))

    if extra:
        plain_extra = re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', extra).strip()
        if plain_extra:
            m = re.match(r'\[(.*?)\]\s*(.*)', plain_extra)
            if m:
                parsed_items.append((m.group(1), m.group(2), f"[{m.group(1)}] {m.group(2)}"))
            else:
                parsed_items.append(("", plain_extra, plain_extra))

    total_items = len(parsed_items)

    def render_inline(k, v):
        """Render one [key] value pair inline, dimmed with a bold key."""
        if not k: return f"{C.DIM}{v}{C.RESET}"
        return f"{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}] {v}{C.RESET}"

    # Interpunct (·) — the one separator used everywhere: hint bars, player
    # details, bulk headers, multi-value fields.
    sep = f"{C.DIM} · {C.RESET}"
    raw_sep_len = len(' · ')

    # LAYOUT 1: Centred Long Line
    raw_len = sum(len(raw) for _, _, raw in parsed_items) + raw_sep_len * (total_items - 1)
    if raw_len <= cols:
        line = sep.join(render_inline(k, v) for k, v, _ in parsed_items)
        pad = max(0, cols - raw_len) // 2
        return (" " * pad) + line

    # LAYOUT 2: Upside-Down Pyramid
    def get_pyramid_distribution(n):
        """Row sizes for an upside-down pyramid: start near sqrt(2n) and shrink
        by one each row until all n items are placed."""
        rows = []
        current_row_size = math.ceil(math.sqrt(2 * n))
        while n > 0:
            take = min(current_row_size, n)
            rows.append(take)
            n -= take
            current_row_size = max(1, current_row_size - 1)
        return rows

    pyr_dist = get_pyramid_distribution(total_items)
    if len(pyr_dist) > 1 and pyr_dist[0] > pyr_dist[-1]:
        fits = True
        pyr_lines = []
        idx = 0
        for r in pyr_dist:
            row_items = parsed_items[idx:idx+r]
            r_raw_len = sum(len(raw) for _, _, raw in row_items) + raw_sep_len * (len(row_items) - 1)

            if r_raw_len > cols:
                fits = False
                break

            line = sep.join(render_inline(k, v) for k, v, _ in row_items)
            pad = max(0, cols - r_raw_len) // 2
            pyr_lines.append((" " * pad) + line)
            idx += r

        if fits:
            return "\n".join(pyr_lines)

    # LAYOUT 3: Grid (Side-by-side uniform columns)
    max_item_len = max((len(raw) for _, _, raw in parsed_items), default=0)
    gutter = 4
    col_width = max_item_len + gutter
    possible_cols = max(1, cols // col_width)

    if possible_cols >= 2:
        grid_lines = []
        for i in range(0, total_items, possible_cols):
            row = parsed_items[i:i+possible_cols]
            raw_row_len = sum(col_width for _ in row) - gutter
            formatted_parts = []
            for k, v, raw in row:
                space_padding = " " * (col_width - len(raw))
                formatted_parts.append(render_inline(k, v) + space_padding)

            row_str = "".join(formatted_parts).rstrip()
            pad = max(0, cols - raw_row_len) // 2
            grid_lines.append((" " * pad) + row_str)
        return "\n".join(grid_lines)

    # LAYOUT 4: Aligned Vertical Stack
    # Center aligned: Keys right-aligned to spine, values left-aligned from spine
    max_k_len = max((len(f"[{k}]") for k, _, _ in parsed_items if k), default=0)
    max_v_len = max((len(v) for _, v, _ in parsed_items), default=0)
    total_stack_w = max_k_len + 1 + max_v_len # key + space + value

    if total_stack_w <= cols:
        stack_lines = []
        global_pad = max(0, cols - total_stack_w) // 2

        for k, v, _ in parsed_items:
            if k:
                k_raw = f"[{k}]"
                k_space_pad = " " * (max_k_len - len(k_raw))
                left_side = f"{k_space_pad}{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}]{C.RESET}"
            else:
                left_side = " " * max_k_len

            right_side = f"{C.DIM}{v}{C.RESET}"
            stack_lines.append(f"{' ' * global_pad}{left_side} {right_side}")
        return "\n".join(stack_lines)

    # LAYOUT 5: Split Vertical Stack (Ultimate Narrow Fallback)
    # Key on row 1, value on row 2, dot separator between pairs.
    split_lines = []
    for i, (k, v, _) in enumerate(parsed_items):
        if k:
            k_raw = f"[{k}]"
            k_pad = max(0, cols - len(k_raw)) // 2
            split_lines.append(f"{' ' * k_pad}{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}]{C.RESET}")

        v_pad = max(0, cols - len(v)) // 2
        split_lines.append(f"{' ' * v_pad}{C.DIM}{v}{C.RESET}")

        # Add centered separator dot between discrete blocks
        if i < total_items - 1:
            dot_pad = max(0, cols - 1) // 2
            split_lines.append(f"{' ' * dot_pad}{C.DIM}⋅{C.RESET}")

    return "\n".join(split_lines)

# --- Clickable hints & now-playing box hit-testing -------------------------
# Hint keys render as ``[key] label`` with only ``key`` bold/bright; a click is
# actionable only when it lands on those bright glyphs. Multi-key labels split
# into separate buttons: a '/' between keys is a non-clickable separator, and an
# adjacent arrow cluster (``↑↓``, ``←→``) is one button per arrow. Each button
# maps to the SAME synthesised key the keyboard produces, so the widgets need no
# extra per-key logic — a click just replays that key through their normal switch.

_HINT_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKFHF]')
_HINT_ARROWS = {'↑': 'UP', '↓': 'DOWN', '←': 'LEFT', '→': 'RIGHT'}
_HINT_WORDS = {
    'space': 'SPACE', 'spc': 'SPACE', 'esc': 'ESC', 'tab': 'TAB', '↵': 'ENTER',
    'pgup': 'PGUP', 'pgdn': 'PGDN', '⇧tab': 'BACKTAB', 'home': 'HOME', 'end': 'END',
}


def _hint_key_tokens(key: str) -> list[tuple[int, int, str]]:
    """Split a hint key label into clickable ``(offset, glyph_len, synth_key)``
    buttons. ``offset`` is 0-based within the key text; the '/' joiners it skips
    over are left non-clickable."""
    segs = key.split('/') if (key and key != '/' and '/' in key) else [key]
    tokens: list[tuple[int, int, str]] = []
    off = 0
    for si, seg in enumerate(segs):
        if si > 0:
            off += 1                       # the '/' separator column (not clickable)
        if not seg:
            continue
        if all(c in _HINT_ARROWS for c in seg):     # e.g. "↑↓" → one button per arrow
            for c in seg:
                tokens.append((off, 1, _HINT_ARROWS[c]))
                off += 1
        elif seg.lower() in _HINT_WORDS:            # "space"/"esc"/"tab"/"↵"/"PgUp"…
            tokens.append((off, len(seg), _HINT_WORDS[seg.lower()]))
            off += len(seg)
        elif len(seg) == 2 and seg[0] == '^':       # "^N" → Ctrl-N control char
            tokens.append((off, 2, chr(ord(seg[1].upper()) - 64)))
            off += 2
        else:                                       # a single glyph / plain letter
            tokens.append((off, len(seg), _HINT_WORDS.get(seg.lower(), seg)))
            off += len(seg)
    return tokens


def add_hint_click_cells_auto(cells: dict, line: str, base_row: int,
                              left_inset: int = 0) -> None:
    """Like add_hint_click_cells but auto-detects ``[key]`` groups in the plain
    text (no pairs needed). Use only on lines known to be a hint bar — arbitrary
    bracketed text (e.g. a lyric ``[Chorus]``) would be picked up as a key."""
    plain = _HINT_ANSI_RE.sub('', line)
    for m in re.finditer(r'\[([^\[\]]+)\]', plain):
        key_col0 = m.start() + 1
        for off, glen, synth in _hint_key_tokens(m.group(1)):
            for c in range(glen):
                cells[(base_row, left_inset + key_col0 + off + c + 1)] = synth


def add_hint_click_cells(cells: dict, line: str, base_row: int, pairs,
                         left_inset: int = 0) -> None:
    """Populate ``cells`` (a ``{(row, col): synth_key}`` map) with the clickable
    bright-key glyphs found on one rendered hint ``line`` at absolute ``base_row``.
    ``pairs`` is the (key, label) sequence that produced the hint bar."""
    plain = _HINT_ANSI_RE.sub('', line)
    for k, _v in pairs:
        if not k:
            continue
        idx = plain.find(f"[{k}]")
        if idx < 0:
            continue
        key_col0 = idx + 1                 # 0-based index of key[0] (just past '[')
        for off, glen, synth in _hint_key_tokens(k):
            for c in range(glen):
                col = left_inset + key_col0 + off + c + 1   # 1-based screen column
                cells[(base_row, col)] = synth


def _hint_pin_target() -> int:
    """The flowed-line count after which a widget's hint bar sits pinned at the
    bottom — directly above the miniplayer + status bar — so its keys keep the
    same screen position across redraws (repeated clicks don't chase the bar)."""
    rows = ui_utils.get_terminal_height()
    return rows - 1 - ui_utils.MARGIN_V - max(ui_utils.now_playing_height(), ui_utils.MARGIN_V)


# Now-playing box transport-icon columns, derived from the one place the glyph
# layout is defined (ui_utils.NP_GLYPH_COLS) rather than restated here: the
# box is inset by MARGIN_H, then "│ " precedes the content, so a glyph at content
# offset `o` lands on 1-based column MARGIN_H + 3 + o. Each glyph claims its own
# column plus the space after it, so a click just to the right still lands.
def _np_glyph_cols() -> list[tuple[str, int, int]]:
    """(action, first_col, last_col) for each transport glyph in the box."""
    base = ui_utils.MARGIN_H + 3
    actions = ('prev', 'playpause', 'next')
    # A glyph claims its own cells plus the space after it, so a click just to
    # the right of a narrow glyph still lands on it.
    return [(a, base + start, base + start + width)
            for a, (start, width) in zip(actions, ui_utils.NP_GLYPH_COLS)]


def now_playing_click_action(row: int, col: int) -> str | None:
    """Classify a click against the now-playing box: ``'prev'`` / ``'playpause'``
    / ``'next'`` on the transport glyphs, ``'open'`` anywhere else in the box, or
    ``None`` when the click misses it (or no box is shown)."""
    h = _np_prev_h[0]
    if h <= 0 or not ui_utils.now_playing_active():
        return None
    rows = ui_utils.get_terminal_height()
    top = rows - h
    if not (top <= row <= rows - 1):
        return None
    if row == top + 1:                     # the content row that carries the icons
        for action, lo, hi in _np_glyph_cols():
            if lo <= col <= hi:
                return action
    return 'open'


def _render_status_bar():
    """Redraw the bottom status bar in place, saving/restoring the cursor so
    the text input caret doesn't move."""
    rows = ui_utils.get_terminal_height()
    if rows <= 0:
        return
    status = ui_utils.get_status_line()
    # \0337 / \0338 (via save_cursor) keep the caret where the text input left
    # it rather than jumping to the status row; an unchanged bar writes nothing.
    screen_paint({rows: status}, save_cursor=True)


class Choice:
    __slots__ = ('title', 'value', 'checked', 'disabled', 'cells', 'cursor_title')

    def __init__(self, title: str, value: object = None, checked: bool = False,
                 disabled: bool = False, cells: list | None = None,
                 cursor_title: str | None = None) -> None:
        """Build a selectable/checkable row for select(), defaulting value to title."""
        self.title        = title
        self.value        = value if value is not None else title
        self.checked      = checked
        self.disabled     = disabled  # non-selectable separator / section heading
        self.cells        = cells    # structured column data for columns= mode
        self.cursor_title = cursor_title  # alternate label shown when cursor is on this row


# The single inter-column gap for every list in the app. Lists used to mix 2 and
# 3 — the duration column sat a column closer to the edge in search and history
# than in browse. Narrow terminals are handled by column `priority` (columns drop)
# and by the dynamically computed pin gap, not by varying this.
COL_GAP = 3


class Column:
    """A column spec for a structured select() table (no string parsing).

    style    : 'primary' | 'static-dim' | 'dynamic-dim' | 'accent' | 'normal'
    align    : 'left' | 'right'
    flex     : absorbs leftover width, truncates (use for the title column)
    pin      : laid against the right edge (e.g. duration)
    max_frac : clamp column to this fraction of total width (0.0–1.0)
    gap      : leading gap before this column (defaults to `COL_GAP` — the one
               value every list uses; the pin block's separation from the left
               block is computed per render, so this is only the minimum)
    priority : drop-order when the row is too narrow to show every column
               readably. None (default) = essential, never dropped. A number
               marks the column droppable; the lowest-priority droppable column
               is dropped first, so give the least important columns the lowest
               numbers (e.g. 1 = first to go).
    """
    __slots__ = ('style', 'align', 'flex', 'pin', 'min_width', 'max_width', 'max_frac',
                 'gap', 'priority')

    def __init__(self, style: str = 'normal', align: str = 'left', flex: bool = False,
                 pin: bool = False, min_width: int = 0, max_width: int | None = None,
                 max_frac: float | None = None, gap: int = COL_GAP,
                 priority: float | None = None) -> None:
        """Build a column spec for a structured select() table."""
        self.style     = style
        self.align     = align
        self.flex      = flex
        self.pin       = pin
        self.min_width = min_width
        self.max_width = max_width
        self.max_frac  = max_frac
        self.gap       = gap
        self.priority  = priority


def _cell_text(cell) -> tuple[str, str | None]:
    """Return (plain_text, style_override) for a str, (str, style) tuple, or list of segments."""
    if isinstance(cell, list):
        return "".join(str(seg[0]) if isinstance(seg, tuple) else str(seg) for seg in cell), None
    if isinstance(cell, tuple):
        return str(cell[0]), (cell[1] if len(cell) > 1 else None)
    return str(cell), None


def _style_cell(text: str, style: str, is_current: bool) -> str:
    """Apply a named cell style (dim/dynamic-dim/accent/primary/normal) to text."""
    if not text:
        return ""
    if style in ('dim', 'static-dim'):
        return f"{C.DIM}{text}{C.RESET}"
    if style == 'dynamic-dim':
        return f"{C.BOLD}{C.DIM}{text}{C.RESET}" if is_current else f"{C.DIM}{text}{C.RESET}"
    if style == 'accent':
        return f"{C.ACCENT}{text}{C.RESET}"
    if style == 'primary':
        return f"{C.BOLD}{text}{C.RESET}" if is_current else text
    return text  # 'normal'


def _render_cell_segments(cell, style: str, is_current: bool, width: int, align: str,
                          force_dim: bool = False) -> str:
    """Render a cell (plain text or list of styled segments), truncated/padded
    to `width` and aligned."""
    if isinstance(cell, list):
        parts = []
        raw_len = 0
        remaining = width
        for seg in cell:
            t, s = (str(seg[0]), seg[1]) if isinstance(seg, tuple) else (str(seg), style)
            if force_dim:
                s = 'dynamic-dim'
            if remaining <= 0:
                t = ""
            elif len(t) > remaining:
                t = t[:max(0, remaining - 1)] + "…"
            raw_len += len(t)
            remaining -= len(t)
            parts.append(_style_cell(t, s, is_current))
        text = "".join(parts)
        pad = " " * max(0, width - raw_len)
    else:
        raw_text, override = _cell_text(cell)
        if len(raw_text) > width:
            raw_text = raw_text[:max(1, width - 1)] + "…"
        raw_len = len(raw_text)
        text = _style_cell(raw_text, override or style, is_current)
        pad = " " * max(0, width - raw_len)
    return (pad + text) if align == 'right' else (text + pad)


def _table_widths(rows_cells: list, columns: list, eff: int,
                  pointer_w: int, right_margin: int,
                  visible_cells: list | None = None) -> list[int]:
    """Compute per-column widths that fit the effective width `eff`.

    Content sets each column's natural width — scanned across *all* rows, so a
    wide entry far down the list is accounted for and the layout stays stable
    while scrolling. Natural widths are clamped by max_frac / max_width and
    floored by min_width. Then space is reconciled with the terminal:

      * blank → a droppable column with nothing to show in the *visible* window
                is dropped outright. Its natural width comes from all rows, so
                an off-screen entry would otherwise reserve a wide column that
                renders as empty space on every row you can actually see (a
                genre far down the search results, a featured artist nobody in
                view has). Width it can't use is width the title column needs;
      * drop  → if not every column can fit even at its comfortable minimum,
                drop the lowest-priority droppable column (Column.priority) and
                retry; essential columns (priority=None) are never dropped;
      * fits  → flex column(s) share the leftover evenly (each respecting its
                own max_frac / max_width cap **and its own content**) so pinned
                columns sit flush right. A flex column never grows past what it
                has to show: padding a left column out to the full width just
                buries the row's right-hand block behind a field of blanks. Any
                surplus is left unallocated and the renderer spends it as the
                single gap between the left block and the pinned block;
      * tight → the widest kept column gives up space first, one unit at a time,
                never below a readable floor, so narrow columns (durations,
                counts) stay intact and only the widest columns truncate.

    `visible_cells` is the window of rows actually on screen (defaults to
    `rows_cells`); only the blank pass uses it, so widths stay scroll-stable.

    Returns a width per column; a dropped column's width is -1 (skipped by the
    renderer, which also drops its gap).
    """
    ncol = len(columns)
    content = [0] * ncol
    for cells in rows_cells:
        for i in range(min(ncol, len(cells))):
            content[i] = max(content[i], len(_cell_text(cells[i])[0]))

    # What each column actually has to show in the window on screen.
    shown = [0] * ncol
    for cells in (rows_cells if visible_cells is None else visible_cells):
        for i in range(min(ncol, len(cells))):
            shown[i] = max(shown[i], len(_cell_text(cells[i])[0]))

    def _cap(col) -> int | None:
        """The hard upper bound a column may reach (max_frac / max_width), or None."""
        cap = int(eff * col.max_frac) if col.max_frac is not None else None
        if col.max_width is not None:
            cap = col.max_width if cap is None else min(cap, col.max_width)
        return cap

    # Natural (capped, min-floored) width each column would like.
    natural = [0] * ncol
    for i, col in enumerate(columns):
        w = content[i]
        cap = _cap(col)
        if cap is not None:
            w = min(w, cap)
        natural[i] = max(w, col.min_width)

    def _comfort(i: int) -> int:
        """Smallest width column i still reads at (its content, if that's smaller)."""
        return max(columns[i].min_width, min(natural[i], _MIN_COL_FLOOR))

    def _overhead(ks: list) -> int:
        """Fixed, non-content width for a kept set: pointer, gaps, margin, pin gap."""
        pin = any(columns[i].pin for i in ks)
        return (pointer_w + right_margin + sum(columns[i].gap for i in ks)
                + (_MIN_PIN_GAP if pin else 0))

    kept = list(range(ncol))

    # Blank pass: a droppable column with nothing to show in the visible window
    # reserves width that renders as empty space on every row on screen. Drop it
    # and give the space to the columns that do have something to say; it comes
    # back when you scroll to rows that fill it. Never drops the last column.
    blank = [i for i in kept
             if columns[i].priority is not None and shown[i] == 0 and columns[i].min_width == 0]
    if len(blank) < len(kept):
        for i in blank:
            kept.remove(i)

    # Drop pass: while even everyone's comfortable minimum can't fit, shed the
    # lowest-priority droppable column (ties: the rightmost goes first).
    while len(kept) > 1 and _overhead(kept) + sum(_comfort(i) for i in kept) > eff:
        droppable = [i for i in kept if columns[i].priority is not None]
        if not droppable:
            break
        victim = min(droppable, key=lambda i: (columns[i].priority, -i))
        kept.remove(victim)

    widths = [-1] * ncol                 # -1 = dropped (renderer skips it and its gap)
    for i in kept:
        widths[i] = natural[i]

    flex_idxs = [i for i in kept if columns[i].flex]
    has_pin = any(columns[i].pin for i in kept)
    gaps = sum(columns[i].gap for i in kept)
    budget = eff - pointer_w - gaps - right_margin
    if has_pin:
        budget -= _MIN_PIN_GAP           # reserve the left/right inter-block gap
    budget = max(0, budget)

    total = sum(widths[i] for i in kept)
    if total < budget and flex_idxs:
        # Surplus: round-robin one unit at a time into the flex columns, each
        # stopping at its own cap *or its own content*, whichever comes first —
        # growing a column past what it has to show only pads it with blanks and
        # pushes the pinned block away from the text it belongs to. Leftover is
        # deliberately unspent: _render_table_row turns it into the one gap
        # between the left block and the right-pinned block.
        surplus = budget - total
        caps = {}
        for i in flex_idxs:
            cap_i = _cap(columns[i])
            need_i = max(content[i], columns[i].min_width)
            caps[i] = need_i if cap_i is None else min(cap_i, need_i)
        progressed = True
        while surplus > 0 and progressed:
            progressed = False
            for i in flex_idxs:
                if surplus == 0:
                    break
                cap_i = caps[i]
                if cap_i is None or widths[i] < cap_i:
                    widths[i] += 1
                    surplus -= 1
                    progressed = True
    elif total > budget:
        # Over budget: shave the widest kept column repeatedly until it fits,
        # never below its floor (min_width, a readable minimum, or its own
        # content if that is already smaller). The readable minimum eases toward
        # the fair per-column share when a many-column row is genuinely cramped,
        # so the layout still fits. n and the deficit are both small.
        floor_cap = min(_MIN_COL_FLOOR, max(1, budget // len(kept)))
        floors = {i: min(widths[i], max(columns[i].min_width, floor_cap)) for i in kept}
        deficit = total - budget
        while deficit > 0:
            widest = -1
            for i in kept:
                if widths[i] > floors[i] and (widest < 0 or widths[i] > widths[widest]):
                    widest = i
            if widest < 0:
                break                    # everything at its floor — clip guard handles the rest
            widths[widest] -= 1
            deficit -= 1
    return widths


def _render_table_row(cells: list, columns: list, is_current: bool,
                      widths: list[int], eff: int, right_margin: int,
                      is_checked: bool | None = None,
                      disabled: bool = False) -> str:
    """Render one table row, laying out left-aligned and right-pinned columns
    and applying pointer/check/disabled styling."""
    if disabled:
        # Match enabled non-current prefix exactly so columns stay aligned.
        if is_checked is not None:
            left = f"    {C.DIM}•{C.RESET}"   # 4 spaces + dim bullet = same as "    •"
        else:
            left = "   "                        # 3 spaces, same as single-select non-current
        for i, col in enumerate(columns):
            if not col.pin and widths[i] >= 0:
                left += " " * col.gap + _render_cell_segments(
                    cells[i] if i < len(cells) else "", 'dynamic-dim', False, widths[i], col.align,
                    force_dim=True)
        right = ""
        for i, col in enumerate(columns):
            if col.pin and widths[i] >= 0:
                right += " " * col.gap + _render_cell_segments(
                    cells[i] if i < len(cells) else "", 'dynamic-dim', False, widths[i], col.align,
                    force_dim=True)
        if right:
            gap = max(2, (eff - right_margin) - ui_utils.visual_len(left) - ui_utils.visual_len(right))
            return left + " " * gap + right + " " * right_margin
        return left

    pointer = f"{C.ACCENT}›{C.RESET}" if is_current else " "
    if is_checked is None:
        left = f"  {pointer}"
    else:
        glyph = f"{C.GREEN}✔{C.RESET}" if is_checked else f"{C.DIM}•{C.RESET}"
        left = f"  {pointer} {glyph}"
    for i, col in enumerate(columns):
        if not col.pin and widths[i] >= 0:
            left += " " * col.gap + _render_cell_segments(
                cells[i] if i < len(cells) else "", col.style, is_current, widths[i], col.align)

    right = ""
    for i, col in enumerate(columns):
        if col.pin and widths[i] >= 0:
            right += " " * col.gap + _render_cell_segments(
                cells[i] if i < len(cells) else "", col.style, is_current, widths[i], col.align)

    if right:
        gap = max(2, (eff - right_margin) - ui_utils.visual_len(left) - ui_utils.visual_len(right))
        return left + " " * gap + right + " " * right_margin
    return left


def separator(title: str = "") -> Choice:
    """A non-selectable heading/divider row for grouping a select() list."""
    return Choice(title, value=None, disabled=True)


def _split_columns(title: str, parse_fraction: bool = False) -> tuple[str, str, str, str]:
    """Split a plain title into (label, type, value, fraction) columns, matching
    the checkbox grammar:  LABEL [type] | value   n/total.
    Type is only taken from an explicit [bracket], or a trailing word when a
    `|` value divider is present — so plain titles keep their whole label.

    parse_fraction is off by default: a trailing N/M (e.g. a track/disc/movement
    value like 3/12) must stay in the value column, not be mistaken for a count."""
    title = re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', title)
    frac = ""
    value = ""
    if parse_fraction:
        m = re.search(r"(\d+/\d+)\s*$", title)
        if m:
            frac = m.group(1)
            title = title[:m.start()].rstrip()

    had_pipe = bool(re.search(r"\s*\|\s*", title))
    if had_pipe:
        left, right = re.split(r"\s*\|\s*", title, maxsplit=1)
        value = right.strip()
        title = left.rstrip()

    type_tag = ""
    mb = re.search(r"\[([^\]]+)\]\s*$", title)
    if mb:
        type_tag = mb.group(1).strip()
        title = title[:mb.start()].rstrip()
    elif had_pipe:
        mw = re.search(r"([A-Za-z\s\d]+)\s*$", title)
        if mw:
            type_tag = mw.group(1).strip()
            title = title[:mw.start()].rstrip()

    return title.rstrip(), type_tag, value, frac


def _clip_ansi(s: str, width: int) -> str:
    """Truncate a string to `width` visible columns, preserving ANSI escape
    sequences (they don't count toward width). Guarantees the line never wraps."""
    if width <= 0:
        return ""
    out: list[str] = []
    vis = 0
    i = 0
    truncated = False
    n = len(s)
    while i < n:
        if s[i] == '\x1b':
            j = i + 1
            if j < n and s[j] == '[':  # CSI sequence: skip '[' before scanning for final byte
                j += 1
            while j < n and not ('@' <= s[j] <= '~'):
                j += 1
            if j < n:
                j += 1
            out.append(s[i:j])
            i = j
        else:
            if vis >= width:
                truncated = True
                break
            out.append(s[i])
            vis += 1
            i += 1
    res = "".join(out)
    return res + C.RESET if truncated else res


def _render_select_columns(parsed: tuple[str, str, str, str], is_current: bool,
                           label_w: int, type_w: int, cols: int) -> str:
    """Render one select() row in column layout: pointer + LABEL (friendly name
    greyed) + type (greyed) + single divider + value. Every column is truncated
    to its budget so the row always fits `cols` (no wrapping). Mirrors bulk."""
    label, type_tag, value, frac = parsed

    head, tail = (lambda m: (m.group(1), m.group(2)) if m else (label, ""))(
        re.match(r'^(\S+)\s+(\(.*\))$', label))

    # Fit "TAG (friendly name)" into label_w, keeping the closing bracket.
    if len(label) > label_w:
        if tail and len(head) + 4 <= label_w:
            inner = tail[1:-1]
            budget = label_w - len(head) - 4          # " (" + "…" + ")"
            tail = f"({inner[:max(0, budget)].rstrip()}…)"
        else:
            head = head[:max(1, label_w - 1)] + "…"
            tail = ""
    vis_label = f"{head} {tail}" if tail else head

    head_s = f"{C.PRIMARY}{C.BOLD}{head}{C.RESET}" if is_current else head
    label_s = f"{head_s} {C.DIM}{tail}{C.RESET}" if tail else head_s
    label_pad = " " * max(0, label_w - len(vis_label))

    if len(type_tag) > type_w:
        type_tag = type_tag[:max(1, type_w - 1)] + "…"
    type_s = f"{C.DIM}{type_tag}{C.RESET}" if type_tag else ""
    type_pad = " " * max(0, type_w - len(type_tag)) if type_w else ""

    pointer = f"{C.ACCENT}›{C.RESET}" if is_current else " "
    avail = max(4, cols - (label_w + type_w + 10) - (len(frac) + 2 if frac else 0))
    if value and len(value) > avail:
        value = value[:max(1, avail - 1)] + "…"
    if value:
        sep = f"{C.DIM}|{C.RESET} "
        value_s = f"{C.PRIMARY}{C.BOLD}{value}{C.RESET}" if is_current else value
    else:
        sep = ""
        value_s = ""

    row = f"  {pointer} {label_s}{label_pad}  {type_s}{type_pad}  {sep}{value_s}"
    if frac:
        row += f"  {C.DIM}{frac}{C.RESET}"
    return row


def _style_checkbox_label(label_text: str, is_current: bool, is_dimmed: bool) -> str:
    """Style a checkbox label, greying a trailing parenthetical (e.g. a friendly
    name) so it stays subordinate to the leading token: `TAG (friendly name)`.
    Visible length is unchanged, so column alignment is preserved."""
    m = re.match(r'^(\S+)\s+(\(.*\))$', label_text)
    head, tail = (m.group(1), m.group(2)) if m else (label_text, "")

    if is_dimmed:
        return f"{C.DIM}{label_text}{C.RESET}"
    head_str = f"{C.PRIMARY}{C.BOLD}{head}{C.RESET}" if is_current else head
    return f"{head_str} {C.DIM}{tail}{C.RESET}" if tail else head_str


def _norm(choices: list) -> list:
    """Normalize a mixed list of Choice/str/dict/choice-like objects into Choice instances."""
    out = []
    for c in choices:
        if isinstance(c, Choice):
            out.append(c)
        elif isinstance(c, str):
            out.append(Choice(c, c))
        elif isinstance(c, dict):
            out.append(Choice(
                title    = c.get('name', c.get('title', str(c))),
                value    = c.get('value', c.get('name', str(c))),
                checked  = c.get('checked', False),
                disabled = c.get('disabled', False),
            ))
        elif hasattr(c, 'title') and hasattr(c, 'value'):
            out.append(Choice(c.title, c.value, getattr(c, 'checked', False),
                              getattr(c, 'disabled', False)))
        else:
            s = str(c)
            out.append(Choice(s, s))
    return out


def _read_key(fd: int) -> str:
    """Read one key, transparently discarding focus in/out events."""
    while True:
        key = _read_key_raw(fd)
        if key not in ('FOCUS_IN', 'FOCUS_OUT'):
            return key


# How long to wait for the rest of an escape sequence before deciding the Esc
# was pressed on its own. A real sequence's bytes arrive in the same burst, so
# this only ever elapses for a genuine bare Esc; small enough that Esc still
# feels instant, large enough to survive a slow link.
_ESC_SEQ_TIMEOUT = 0.05


def _byte_ready(fd: int, timeout: float) -> bool:
    """True if another byte can be read from `fd` within `timeout` seconds."""
    try:
        return bool(_sel.select([fd], [], [], timeout)[0])
    except (OSError, ValueError):
        return False


def _read_key_raw(fd: int) -> str:
    """Read and decode one raw keypress, including escape sequences and mouse
    events, into a named key string."""
    if _IS_WINDOWS:
        ch = msvcrt.getwch()
        if ch in ('\x00', '\xe0'):
            ext = msvcrt.getwch()
            return {
                'H': 'UP', 'P': 'DOWN', 'K': 'LEFT', 'M': 'RIGHT',
                'G': 'HOME', 'O': 'END', 'I': 'PGUP', 'Q': 'PGDN', 'S': 'DELETE',
                'R': 'INSERT',
            }.get(ext, '')
        if ch == '\r': return 'ENTER'
        if ch == '\x08': return 'BACKSPACE'
        if ch == '\x03': return 'CTRL_C'
        if ch == '\t': return 'TAB'
        if ch == ' ': return 'SPACE'
        return ch

    ch = os.read(fd, 1)
    if ch == b'\x1b':
        try:
            # A lone Esc is just this byte; an arrow/function key sends more in
            # the same burst. Raw mode's read blocks while nothing is pending, so
            # peek first — otherwise Esc looked dead until the *next* keypress
            # arrived to unblock the read, and that keypress was then swallowed
            # as part of the sequence. Hence "Esc only works if you press twice".
            if not _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                return 'ESC'
            ch2 = os.read(fd, 1)
            if ch2 == b'O':
                # SS3: some terminals send ESC O A for the arrows while in
                # application-cursor mode.
                ss3 = os.read(fd, 1).decode('utf-8', errors='replace')
                return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                        'H': 'HOME', 'F': 'END'}.get(ss3, 'ESC')
            if ch2 == b'[':
                ch3 = os.read(fd, 1)
                seq = ch3.decode('utf-8', errors='replace')
                if seq == '<':
                    # SGR mouse event: \033[<btn;col;row{M|m}
                    buf = ''
                    while len(buf) < 24:
                        # Same guard as the bare Esc above: a truncated mouse
                        # report would otherwise block the whole UI until the
                        # next keypress arrived.
                        if not _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                            return 'ESC'
                        c = os.read(fd, 1).decode('utf-8', errors='replace')
                        if c in ('M', 'm'):
                            parts = buf.split(';')
                            if len(parts) == 3:
                                try:
                                    btn, col, row = int(parts[0]), int(parts[1]), int(parts[2])
                                    if c == 'm':
                                        return f'MOUSE_RELEASE:{btn}:{row}:{col}'
                                    if btn == 64: return 'SCROLL_UP'
                                    if btn == 65: return 'SCROLL_DOWN'
                                    if btn in (0, 1, 2): return f'MOUSE_CLICK:{btn}:{row}:{col}'
                                except ValueError:
                                    pass
                            return 'ESC'
                        buf += c
                    return 'ESC'
                if seq.isdigit():
                    # ESC [ <number> ~ — page/home/end/delete/insert. This used
                    # to swallow four bytes and report 'ESC', so PgUp and PgDn
                    # acted as "back" (the 5/6 entries in the table below were
                    # dead code, never reached).
                    num, term = seq, ''
                    while len(num) < 4 and _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                        c = os.read(fd, 1).decode('utf-8', errors='replace')
                        if c.isdigit():
                            num += c
                            continue
                        term = c
                        break
                    if term == ';':
                        # Modified form (ESC [ 1;5A = Ctrl-Up): drain to the
                        # final letter and treat it as the unmodified key.
                        while _byte_ready(fd, _ESC_SEQ_TIMEOUT):
                            c = os.read(fd, 1).decode('utf-8', errors='replace')
                            if c.isalpha():
                                term = c
                                break
                    if term.isalpha():
                        return {'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                                'H': 'HOME', 'F': 'END'}.get(term, 'ESC')
                    return {'1': 'HOME', '2': 'INSERT', '3': 'DELETE', '4': 'END',
                            '5': 'PGUP', '6': 'PGDN', '7': 'HOME', '8': 'END',
                            }.get(num, 'ESC')
                mapped = {
                    'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                    'H': 'HOME', 'F': 'END',
                    'Z': 'BACKTAB',                       # Shift+Tab
                    'I': 'FOCUS_IN', 'O': 'FOCUS_OUT',
                }.get(seq, 'ESC')
                if mapped == 'FOCUS_IN':
                    # Regained focus — force the now-playing box to repaint (it may
                    # be stale from a background change while we were unfocused).
                    invalidate_now_playing_box()
                return mapped
            return 'ESC'
        except (OSError, EOFError):
            return 'ESC'
    # A non-ASCII character (e.g. an accented letter) is 2-4 bytes in UTF-8, but
    # os.read(fd, 1) only grabbed the lead byte. Pull the continuation bytes so
    # the whole codepoint decodes to one character instead of several U+FFFD.
    b0 = ch[0]
    if b0 >= 0x80:
        if   b0 >= 0xF0: n_cont = 3
        elif b0 >= 0xE0: n_cont = 2
        elif b0 >= 0xC0: n_cont = 1
        else:            n_cont = 0   # stray continuation byte; nothing to gather
        for _ in range(n_cont):
            ch += os.read(fd, 1)
    decoded = ch.decode('utf-8', errors='replace')
    if decoded in ('\r', '\n'): return 'ENTER'
    if decoded in ('\x7f', '\x08'): return 'BACKSPACE'
    if decoded == ' ':    return 'SPACE'
    if decoded == '\x03': return 'CTRL_C'
    if decoded == '\t':   return 'TAB'
    return decoded

def _visible_rows() -> int:
    """Total lines a list widget may emit: the full terminal height minus the
    status bar (1) and the top+bottom vertical margins. Callers subtract their
    OWN chrome (header, message, indicators, hints) — do not double-count it
    here, or lists show a premature "N more" (they did, by ~5–7 rows)."""
    _, rows = ui_utils.get_terminal_size()
    # Reserve the status-bar row, plus the now-playing box's rows (#14) whenever
    # background audio is active, so lists never collide with it.
    reserve = 1 + ui_utils.now_playing_height()
    return max(4, rows - reserve - 2 * ui_utils.MARGIN_V)


def _rows() -> int:
    """Terminal height in rows."""
    return ui_utils.get_terminal_height()


def _hint_lines(*pairs, extra="") -> list[str]:
    """The hint bar rendered as a list of lines rather than one newline-joined string."""
    return _hint(*pairs, extra=extra).splitlines()


def _wrap_bordered_input_lines(text: str, content_width: int) -> list[str]:
    """Word-wrap text to `content_width`, preserving blank lines as empty entries."""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if raw_line == "":
            lines.append("")
        else:
            wrapped = textwrap.wrap(raw_line, width=content_width, drop_whitespace=False) or [""]
            lines.extend(wrapped)
    return lines


class _Widget:
    """
    Renders a list of lines anchored to an absolute terminal row.

    On first draw it queries the current cursor row and uses that as the
    anchor. On resize it clears the entire screen and redraws from scratch —
    this is the only reliable way to prevent ghost lines when the terminal
    reflows content and changes the effective cursor position.
    """

    def __init__(self, fd: int) -> None:
        """No anchor row yet — it's queried and fixed on the first render."""
        self.fd      = fd
        self.row     = None   # anchor row, 1-based
        self.last_h  = 0
        self._full   = False  # whether we own the full screen

    def anchor_reset(self) -> None:
        """Called on resize (or after another view owned the screen) — clears and
        redraws from scratch next render."""
        self.row   = None
        self._full = True

    def refresh(self) -> None:
        """Repaint every row next render *without* clearing first.

        For a stale-but-correctly-sized screen — regaining focus, a background
        track change — where a clear would only add a visible blank flash.
        """
        screen_invalidate()

    def render(self, lines: list) -> None:
        """Paint `lines` from row 1, diffed against what is already on screen.

        Only rows whose content changed are written, in one buffered frame with
        no newlines and no erase-to-end-of-screen — so the frame can't be flushed
        half-drawn, and the rows this widget doesn't own (the now-playing box, the
        status bar) are left exactly as they are instead of being wiped and
        restamped on every keystroke.
        """
        mv   = ui_utils.MARGIN_V
        rows = ui_utils.get_terminal_height()

        # Wrap content with vertical margins: mv blank rows on top, mv reserved
        # rows before the status bar at the bottom. The box's band is excluded so
        # the two writers never own the same row (a shrinking box would otherwise
        # blank rows this diff believes it still owns).
        padded: list[str] = [''] * mv + list(lines)
        limit = rows - 1 - max(mv, now_playing_height_for_layout())
        padded = padded[:max(0, limit)]

        if self._full or self.row is None:
            if self._full and _screen:
                # A real clear only for a resize (or after another view owned the
                # screen): the terminal reflowed, so nothing on it can be trusted.
                sys.stdout.write("\033[H\033[2J" + C.HIDE)
                screen_invalidate()
            else:
                # First frame of a new widget: take the screen over in the paint
                # itself, so moving between screens never shows a blank one.
                screen_takeover_next()
            self.row   = 1
            self._full = False

        frame: dict[int, str] = {i + 1: line for i, line in enumerate(padded)}
        # Blank any rows a previous, taller frame left behind.
        for i in range(len(padded), self.last_h):
            frame[i + 1] = ""
        self.last_h = len(padded)

        # The status bar and the box join the same frame, so everything lands in
        # one flush — but each row still only costs anything if it changed.
        frame[rows] = ui_utils.get_status_line()
        frame = _takeover_rows(frame)
        parts = [C.HIDE]
        for row in sorted(frame):
            parts.append(screen_row_segment(row, frame[row]))
        parts.append(now_playing_box_segment())
        out = "".join(p for p in parts if p)
        if out != C.HIDE:
            sys.stdout.write(out)
            sys.stdout.flush()

    def clear(self) -> None:
        """Clear the screen and reset anchor state, cursor still hidden.

        It used to show the cursor here, which left it blinking at home until the
        next screen painted. The cursor is only ever shown for a text caret, or by
        `ui_utils.exit_alt_screen()` when the app hands the terminal back.
        """
        sys.stdout.write("\033[H\033[3J\033[J" + C.HIDE)
        sys.stdout.flush()
        screen_invalidate()
        self.last_h = 0
        self.row    = None
        self._full  = True


_register_screen_hooks()
