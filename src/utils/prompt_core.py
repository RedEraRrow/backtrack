"""Terminal primitives shared across prompt widgets."""
from __future__ import annotations
import re
import sys
import os
import math
import datetime
import calendar as cal
import tempfile
import textwrap
import time
import select as _sel
import subprocess
from typing import Any, Callable, Literal, overload

from src.utils import ui_utils
from src import state as _state
from src.state import QuitToTerminal
C = ui_utils.Colors


def _check_deferred_quit() -> None:
    """Raise QuitToTerminal if an editor requested save-and-quit. Called at the
    start of navigation widgets so the pending edit is saved before unwinding."""
    if _state.QUIT_REQUESTED:
        _state.QUIT_REQUESTED = False
        raise QuitToTerminal()

_IS_WINDOWS = os.name == "nt"

_COLUMNS_MAX_WIDTH = 160   # cap effective width for table layout even on ultra-wide terminals
_EDGE_MARGIN       = 2     # right-side padding for pinned columns

tty: Any
termios: Any
msvcrt: Any

if _IS_WINDOWS:
    import msvcrt
else:
    import tty
    import termios


def _get_term_attrs(fd: int):
    return None if _IS_WINDOWS else termios.tcgetattr(fd)


def _set_raw(fd: int) -> None:
    if not _IS_WINDOWS:
        tty.setraw(fd)


def _restore_term_attrs(fd: int, old):
    if not _IS_WINDOWS and old is not None:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _wait_for_keypress(timeout: float = 0.05) -> bool:
    if _IS_WINDOWS:
        end = time.time() + timeout
        while time.time() < end:
            if msvcrt.kbhit():
                return True
            time.sleep(0.01)
        return False
    return bool(_sel.select([sys.stdin], [], [], timeout)[0])


def _clrline():        return "\033[2K\r"
def _goto(row, col=1): return f"\033[{row};{col}H"
def _col(n):           return f"\033[{n}G"


def _cols() -> int:
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
        if not k: return f"{C.DIM}{v}{C.RESET}"
        return f"{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}] {v}{C.RESET}"

    sep = f"{C.DIM} ⋅ {C.RESET}"
    raw_sep_len = len(' ⋅ ')

    # LAYOUT 1: Centred Long Line
    raw_len = sum(len(raw) for _, _, raw in parsed_items) + raw_sep_len * (total_items - 1)
    if raw_len <= cols:
        line = sep.join(render_inline(k, v) for k, v, _ in parsed_items)
        pad = max(0, cols - raw_len) // 2
        return (" " * pad) + line

    # LAYOUT 2: Upside-Down Pyramid
    def get_pyramid_distribution(n):
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

def _render_status_bar():
    rows = ui_utils.get_terminal_height()
    status = ui_utils.get_status_line()
    # \0337 / \0338 save and restore cursor position (DEC) so the cursor
    # stays at the text input caret rather than jumping to the status bar row.
    sys.stdout.write(f"\0337\033[{rows};1H\033[2K{status}\0338")
    sys.stdout.flush()


class Choice:
    __slots__ = ('title', 'value', 'checked', 'disabled', 'cells', 'cursor_title')

    def __init__(self, title: str, value: object = None, checked: bool = False,
                 disabled: bool = False, cells: list | None = None,
                 cursor_title: str | None = None) -> None:
        self.title        = title
        self.value        = value if value is not None else title
        self.checked      = checked
        self.disabled     = disabled  # non-selectable separator / section heading
        self.cells        = cells    # structured column data for columns= mode
        self.cursor_title = cursor_title  # alternate label shown when cursor is on this row


class Column:
    """A column spec for a structured select() table (no string parsing).

    style    : 'primary' | 'static-dim' | 'dynamic-dim' | 'accent' | 'normal'
    align    : 'left' | 'right'
    flex     : absorbs leftover width, truncates (use for the title column)
    pin      : laid against the right edge (e.g. duration)
    max_frac : clamp column to this fraction of total width (0.0–1.0)
    gap      : leading gap before this column
    """
    __slots__ = ('style', 'align', 'flex', 'pin', 'min_width', 'max_width', 'max_frac', 'gap')

    def __init__(self, style: str = 'normal', align: str = 'left', flex: bool = False,
                 pin: bool = False, min_width: int = 0, max_width: int | None = None,
                 max_frac: float | None = None, gap: int = 2) -> None:
        self.style     = style
        self.align     = align
        self.flex      = flex
        self.pin       = pin
        self.min_width = min_width
        self.max_width = max_width
        self.max_frac  = max_frac
        self.gap       = gap


def _cell_text(cell) -> tuple[str, str | None]:
    """Return (plain_text, style_override) for a str, (str, style) tuple, or list of segments."""
    if isinstance(cell, list):
        return "".join(str(seg[0]) if isinstance(seg, tuple) else str(seg) for seg in cell), None
    if isinstance(cell, tuple):
        return str(cell[0]), (cell[1] if len(cell) > 1 else None)
    return str(cell), None


def _style_cell(text: str, style: str, is_current: bool) -> str:
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
                  pointer_w: int, right_margin: int) -> list[int]:
    ncol = len(columns)
    content = [0] * ncol
    for cells in rows_cells:
        for i in range(min(ncol, len(cells))):
            content[i] = max(content[i], len(_cell_text(cells[i])[0]))

    widths = [0] * ncol
    flex_idx = None
    for i, col in enumerate(columns):
        w = content[i]
        if col.max_frac is not None:
            w = min(w, int(eff * col.max_frac))
        if col.max_width is not None:
            w = min(w, col.max_width)
        w = max(w, col.min_width)
        widths[i] = w
        if col.flex:
            flex_idx = i

    gaps = sum(col.gap for col in columns)
    if flex_idx is not None:
        fixed = sum(widths[i] for i in range(ncol) if i != flex_idx)
        widths[flex_idx] = max(8, eff - pointer_w - fixed - gaps - right_margin)
    return widths


def _render_table_row(cells: list, columns: list, is_current: bool,
                      widths: list[int], eff: int, right_margin: int,
                      is_checked: bool | None = None,
                      disabled: bool = False) -> str:
    if disabled:
        # Match enabled non-current prefix exactly so columns stay aligned.
        if is_checked is not None:
            left = f"    {C.DIM}•{C.RESET}"   # 4 spaces + dim bullet = same as "    •"
        else:
            left = "   "                        # 3 spaces, same as single-select non-current
        for i, col in enumerate(columns):
            if not col.pin:
                left += " " * col.gap + _render_cell_segments(
                    cells[i] if i < len(cells) else "", 'dynamic-dim', False, widths[i], col.align,
                    force_dim=True)
        right = ""
        for i, col in enumerate(columns):
            if col.pin:
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
        if not col.pin:
            left += " " * col.gap + _render_cell_segments(
                cells[i] if i < len(cells) else "", col.style, is_current, widths[i], col.align)

    right = ""
    for i, col in enumerate(columns):
        if col.pin:
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
    while True:
        key = _read_key_raw(fd)
        if key not in ('FOCUS_IN', 'FOCUS_OUT'):
            return key


def _read_key_raw(fd: int) -> str:
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
            ch2 = os.read(fd, 1)
            if ch2 == b'[':
                ch3 = os.read(fd, 1)
                seq = ch3.decode('utf-8', errors='replace')
                if seq == '<':
                    # SGR mouse event: \033[<btn;col;row{M|m}
                    buf = ''
                    while len(buf) < 24:
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
                    os.read(fd, 4)
                    return 'ESC'
                return {
                    'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                    'H': 'HOME', 'F': 'END',
                    '5': 'PGUP', '6': 'PGDN',
                    'Z': 'BACKTAB',                       # Shift+Tab
                    'I': 'FOCUS_IN', 'O': 'FOCUS_OUT',
                }.get(seq, 'ESC')
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
    return max(4, rows - 1 - 2 * ui_utils.MARGIN_V)


def _rows() -> int:
    return ui_utils.get_terminal_height()


def _hint_lines(*pairs, extra="") -> list[str]:
    return _hint(*pairs, extra=extra).splitlines()


def _wrap_bordered_input_lines(text: str, content_width: int) -> list[str]:
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
        self.fd      = fd
        self.row     = None   # anchor row, 1-based
        self.last_h  = 0
        self._full   = False  # whether we own the full screen

    def anchor_reset(self) -> None:
        """Called on resize — triggers a full-screen redraw next render."""
        self.row   = None
        self._full = True

    def render(self, lines: list) -> None:
        mv   = ui_utils.MARGIN_V
        rows = ui_utils.get_terminal_height()

        # Wrap content with vertical margins: mv blank rows on top, mv reserved
        # rows before the status bar at the bottom.
        padded: list[str] = [''] * mv + list(lines)
        padded = padded[:rows - 1 - mv]  # -1 status bar, -mv bottom margin

        if self._full or self.row is None:
            # Full clear prevents any ghost lines from a previous render.
            sys.stdout.write("\033[H\033[3J\033[J" + C.HIDE)
            self.row   = 1
            self._full = False
            out = ""
        else:
            out = C.HIDE + _goto(self.row) + "\033[J"

        for line in padded:
            out += _clrline() + line + "\n"

        # Erase leftover lines from a previous taller render.
        for _ in range(max(0, self.last_h - len(padded))):
            out += _clrline() + "\n"
        self.last_h = len(padded)

        # Stamp the status bar atomically in the same flush — no flicker.
        status = ui_utils.get_status_line()
        out += f"\033[{rows};1H\033[2K{status}"

        sys.stdout.write(out)
        sys.stdout.flush()

    def clear(self) -> None:
        sys.stdout.write("\033[H\033[3J\033[J" + C.SHOW)
        sys.stdout.flush()
        self.last_h = 0
        self.row    = None
        self._full  = True

