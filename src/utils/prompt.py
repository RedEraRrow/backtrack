"""
Terminal prompt widgets — resize-aware replacements for questionary.

API:
    prompt.select(message, choices)      -> value | None
    prompt.checkbox(message, choices)    -> [value, ...] | None
    prompt.confirm(message)              -> bool
    prompt.text(message, default="")     -> str | None
    prompt.path(message)                 -> str | None

choices can be plain strings, dicts with 'name'/'value'/'checked',
or objects with .title / .value attributes.
"""
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
from typing import Any, Callable

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

def _hint(*pairs, extra="") -> str:
    """
    Highly adaptive layout engine for bottom hints.
    Cascades: Centred Long Line -> Pyramid -> Grid -> Aligned Vertical Stack -> Split Vertical Stack.
    """
    if not pairs and not extra:
        return ""

    cols = ui_utils.get_terminal_width()

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
    __slots__ = ('title', 'value', 'checked', 'disabled')

    def __init__(self, title: str, value: object = None, checked: bool = False,
                 disabled: bool = False) -> None:
        self.title    = title
        self.value    = value if value is not None else title
        self.checked  = checked
        self.disabled = disabled  # non-selectable separator / section heading


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
                }.get(seq, 'ESC')
            return 'ESC'
        except (OSError, EOFError):
            return 'ESC'
    decoded = ch.decode('utf-8', errors='replace')
    if decoded in ('\r', '\n'): return 'ENTER'
    if decoded in ('\x7f', '\x08'): return 'BACKSPACE'
    if decoded == ' ':    return 'SPACE'
    if decoded == '\x03': return 'CTRL_C'
    if decoded == '\t':   return 'TAB'
    return decoded

def _visible_rows() -> int:
    _, rows = ui_utils.get_terminal_size()
    return max(4, rows - 7)  # -7 reserves last row for the persistent status bar


def _cols() -> int:
    return ui_utils.get_terminal_width()


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
        rows = ui_utils.get_terminal_height()
        lines = lines[:rows - 1]  # reserve last row for the persistent status bar

        if self._full or self.row is None:
            # Full clear prevents any ghost lines from a previous render.
            sys.stdout.write("\033[H\033[3J\033[J" + C.HIDE)
            self.row   = 1
            self._full = False
            out = ""
        else:
            out = C.HIDE + _goto(self.row) + "\033[J"

        for line in lines:
            out += _clrline() + line + "\n"
        # Erase leftover lines from a previous taller render.
        for _ in range(max(0, self.last_h - len(lines))):
            out += _clrline() + "\n"
        self.last_h = len(lines)

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


def select(message: str, choices: list,
           header: list | None | Callable[[], list[str]] = None,
           extra_hints: dict[str, str] | None = None,
           index: int = 0,
           shortcuts: dict[str, str] | None = None,
           columns: bool = False) -> str | None:
    """Arrow keys / jk navigate, Enter / → selects, ← / b / q / Ctrl-C → None.

    Shift-Q exits the whole app straight to the terminal (raises QuitToTerminal).

    Args:
        message: Prompt label shown above the list.
        choices: Items to choose from (str, dict, or Choice).
        header:  Optional list of plain strings rendered above the prompt.
        extra_hints: Optional dict of custom key-action bindings to merge
                     (e.g., {'space': 'toggle', 'a': 'all'}).
        index:   Initial cursor position (clamped to the choices range). Lets
                 callers reopen a menu on the previously highlighted row.
    """
    _check_deferred_quit()
    items = _norm(choices)
    if not items:
        return None

    selectable = [i for i, it in enumerate(items) if not it.disabled]
    if not selectable:
        return None

    def _step(cur: int, direction: int) -> int:
        """Move to the next selectable row, skipping disabled separators."""
        n = len(items)
        nxt = (cur + direction) % n
        steps = 0
        while items[nxt].disabled and steps < n:
            nxt = (nxt + direction) % n
            steps += 1
        return nxt

    def _nearest_selectable(idx: int) -> int:
        """Closest selectable row to idx (used after page jumps / clamps)."""
        return min(selectable, key=lambda s: abs(s - idx))

    cursor   = max(0, min(index, len(items) - 1))
    if items[cursor].disabled:
        cursor = _step(cursor, 1)
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)

    base_hints = {
        "↑↓": "move",
        "←/b": "back",
        "q": "quit",
        "↵": "confirm"
    }

    if extra_hints:
        combined_hints = {**extra_hints, **base_hints}
    else:
        combined_hints = base_hints

    _last_hlen = [0]

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport
        cols    = _cols()
        h_lines = _header_lines()
        _last_hlen[0] = len(h_lines)

        max_header_w = 0
        for hl in h_lines:
            plain_hl = re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', hl)
            plain_hl = re.sub(r'[╭─│╰╮╯┌┐└┘├┤┬┴┼═║╔╗╚╝]', '', plain_hl).strip()
            if len(plain_hl) > max_header_w:
                max_header_w = len(plain_hl)

        layout_constraint = " " * max_header_w if (0 < max_header_w < cols - 20) else ""

        hint_lines = _hint(*list(combined_hints.items()), extra=layout_constraint).splitlines()

        fixed_overhead = len(h_lines) + len(hint_lines) + 2
        vis     = max(2, _visible_rows() - fixed_overhead)

        n       = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1

        out = h_lines[:]
        out.append(f"  {C.DIM}{message}{C.RESET}")
        out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}" if viewport > 0 else "")

        # Pre-compute column widths over only the genuine columnar rows (those
        # carrying a `|` divider); other rows render normally and don't inflate widths.
        if columns:
            parsed = {k: _split_columns(str(items[k].title))
                      for k in range(n)
                      if not items[k].disabled and '|' in str(items[k].title)}
            col_label_w = max((len(p[0]) for p in parsed.values()), default=0)
            col_type_w = max((len(p[1]) for p in parsed.values()), default=0)
            # Cap widths so a row always fits `cols` (otherwise it wraps to the
            # next line instead of truncating). Reserve room for the value column.
            col_type_w = min(col_type_w, max(4, cols // 5))
            col_label_w = max(8, min(col_label_w, cols - col_type_w - 18))

        for i in range(viewport, min(viewport + vis, n)):
            if columns and i in parsed:
                out.append(_render_select_columns(
                    parsed[i], i == cursor, col_label_w, col_type_w, cols))
                continue

            label = str(items[i].title)
            max_w = cols - 6
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            if items[i].disabled:
                # Section heading / separator — dim, no pointer, slightly outdented.
                out.append(f"  {C.DIM}{C.BOLD}{label}{C.RESET}" if label else "")
            elif i == cursor:
                out.append(f"  {C.ACCENT}›{C.RESET} {C.PRIMARY}{C.BOLD}{label}{C.RESET}")
            else:
                out.append(f"    {C.DIM}{label}{C.RESET}")

        remaining = n - viewport - vis
        out.append(f"  {C.DIM}╷ {remaining} below{C.RESET}" if remaining > 0 else "")
        out.extend(hint_lines)
        return out

    result = None
    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            if   key == 'CTRL_C':                break
            elif key in ('UP',   'k'):           cursor = _step(cursor, -1);          w.render(_lines())
            elif key in ('DOWN', 'j'):           cursor = _step(cursor, 1);           w.render(_lines())
            elif key == 'HOME':                  cursor = selectable[0];              w.render(_lines())
            elif key == 'END':                   cursor = selectable[-1];             w.render(_lines())
            elif key == 'PGUP':                  cursor = _nearest_selectable(max(0, cursor - _visible_rows())); w.render(_lines())
            elif key == 'PGDN':                  cursor = _nearest_selectable(min(len(items) - 1, cursor + _visible_rows())); w.render(_lines())
            elif key in ('ENTER', 'RIGHT', 'l'):
                if not items[cursor].disabled:
                    result = items[cursor].value; break
            elif key in ('LEFT', 'b', 'h', 'ESC'): result = None; break
            elif key in ('q', 'Q'):              raise QuitToTerminal()
            elif shortcuts and key in shortcuts:  result = shortcuts[key]; break
            elif key == 'SCROLL_UP':             cursor = _step(cursor, -1); w.render(_lines())
            elif key == 'SCROLL_DOWN':           cursor = _step(cursor, 1); w.render(_lines())
            elif key.startswith('MOUSE_CLICK:'):
                parts = key.split(':')
                r = int(parts[2])
                col = int(parts[3]) if len(parts) > 3 else 1
                if w.row is None:
                    continue
                i = r - w.row - _last_hlen[0] - 2
                idx = viewport + i
                if 0 <= idx < len(items) and not items[idx].disabled:
                    # Only act when the click lands on the row's text, not the
                    # empty space to its right. Rows render as 4 lead columns
                    # ("  › " / "    ") followed by the (possibly truncated) label.
                    label = str(items[idx].title)
                    max_w = _cols() - 6
                    shown_len = min(len(label), max_w)
                    if 1 <= col <= 4 + shown_len:
                        cursor = idx
                        result = items[cursor].value
                        break
                    cursor = idx
                    w.render(_lines())

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result

def checkbox(
    message: str,
    choices: list[Any],
    interlock_category_callback: Callable[[Any], str] | None = None,
    header: list | None | Callable[[], list[str]] = None
) -> list[Any] | None:
    """
    Checkbox picker with category interlocking support.
    Proper ANSI handling, cursor visibility, and responsive layout.
    """
    _check_deferred_quit()
    items = _norm(choices)
    if not items:
        return []

    raw_items = [{'obj': item, 'dimmed': False} for item in items]
    index = 0
    locked_category = None

    def update_interlock_states(structured_list: list):
        nonlocal locked_category
        checked_items = [i for i in structured_list if i['obj'].checked]

        if not checked_items or interlock_category_callback is None:
            locked_category = None
            for i in structured_list:
                i['dimmed'] = False
            return

        locked_category = interlock_category_callback(checked_items[0]['obj'].value)
        for i in structured_list:
            item_cat = interlock_category_callback(i['obj'].value)
            i['dimmed'] = (item_cat != locked_category)

    def _ansi_len(s: str) -> int:
        return len(re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', s))

    def _next_index(current: int, structured_list: list, direction: int) -> int:
        n = len(structured_list)
        steps = 0
        idx = (current + direction) % n
        while structured_list[idx]['dimmed'] and steps < n:
            idx = (idx + direction) % n
            steps += 1
        return idx if steps < n else current

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    _set_raw(fd)
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000h\033[?1006h")
    sys.stdout.write(C.HIDE)
    sys.stdout.flush()

    _cb_hlen = [0]
    _row_extent: list[tuple[int, int]] = []  # per-row (text_end_col, fraction_visible_len)
    w = _Widget(fd)

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines() -> list[str]:
        cols = _cols()
        out = _header_lines()
        _cb_hlen[0] = len(out)
        _row_extent.clear()
        out.append(f"  {C.DIM}{message}{C.RESET}")

        # Build structured items and calculate column widths
        structured_items = []
        max_label_w = 0
        max_type_w = 0
        max_frac_w = 0

        for item in raw_items:
            # Strip any ANSI from the title before column parsing — otherwise
            # reset codes like \x1b[0m leak their "0m" into the type column.
            title = re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', str(item['obj'].title))
            fraction_part = ""
            value_part = ""

            frac_match = re.search(r"(\d+/\d+)\s*$", title)
            if frac_match:
                fraction_part = frac_match.group(1)
                title = title[:frac_match.start()].rstrip()

            if re.search(r"\s*\|\s*", title):
                left_side, right_side = re.split(r"\s*\|\s*", title, maxsplit=1)
                value_part = right_side.strip()
                title = left_side.rstrip()

            type_tag = ""
            type_match = re.search(r"(?:\[([^\]]+)\]|([a-zA-Z\s\d]+))\s*$", title)
            if type_match:
                raw_type = type_match.group(1) or type_match.group(2)
                type_tag = raw_type.strip()  # preserve case (e.g. friendly names)
                title = title[:type_match.start()].rstrip()

            label_name = title.rstrip()
            max_label_w = max(max_label_w, len(label_name))
            max_type_w = max(max_type_w, len(type_tag))
            max_frac_w = max(max_frac_w, len(fraction_part))

            structured_items.append({
                'obj': item['obj'],
                'label': label_name,
                'type': type_tag,
                'value': value_part,
                'fraction': fraction_part,
                'dimmed': item['dimmed']
            })

        update_interlock_states(structured_items)
        for i, s_item in enumerate(structured_items):
            raw_items[i]['dimmed'] = s_item['dimmed']

        for idx, item in enumerate(structured_items):
            is_current = (idx == index)

            # Build components with proper color handling
            if item['dimmed']:
                state_glyph = f"{C.DIM}•{C.RESET}"
                label_str = _style_checkbox_label(item['label'], False, True)
                type_str = f"{C.DIM}{item['type']}{C.RESET}" if item['type'] else ""
                value_str = f"{C.DIM}{item['value']}{C.RESET}" if item['value'] else ""
                pointer = " "
            elif is_current:
                state_glyph = f"{C.GREEN}✔{C.RESET}" if item['obj'].checked else f"{C.DIM}•{C.RESET}"
                label_str = _style_checkbox_label(item['label'], True, False)
                type_str = f"{C.DIM}{item['type']}{C.RESET}" if item['type'] else ""
                value_str = f"{C.PRIMARY}{C.BOLD}{item['value']}{C.RESET}" if item['value'] else ""
                pointer = "›"
            else:
                state_glyph = f"{C.GREEN}✔{C.RESET}" if item['obj'].checked else f"{C.DIM}•{C.RESET}"
                label_str = _style_checkbox_label(item['label'], False, False)
                type_str = f"{C.DIM}{item['type']}{C.RESET}" if item['type'] else ""
                value_str = item['value'] if item['value'] else ""
                pointer = " "

            pad_label = " " * (max_label_w - len(item['label']) + 2)
            pad_type = " " * (max_type_w - len(item['type']) + 2) if max_type_w else "  "

            frac_str = item['fraction']
            if frac_str:
                pad_frac = " " * (max_frac_w - len(frac_str))
                if is_current:
                    frac_str = f"{pad_frac}{C.PRIMARY}{frac_str}{C.RESET}"
                else:
                    frac_str = f"{pad_frac}{C.DIM}{frac_str}{C.RESET}"

            left_part = f"  {pointer} {state_glyph}  {label_str}{pad_label}{type_str}{pad_type}"
            if value_str:
                sep = f"{C.DIM}|{C.RESET} " if is_current or not item['dimmed'] else "| "
                left_part += f"{sep}{value_str}"

            left_visible = _ansi_len(left_part)
            frac_visible = _ansi_len(frac_str) if frac_str else 0
            space_available = cols - left_visible - frac_visible - 2

            if space_available > 0:
                line = left_part + (" " * space_available) + frac_str
                _row_extent.append((left_visible, frac_visible))
            else:
                max_left = cols - frac_visible - 5
                truncated = left_part[:max_left] + "…"
                line = truncated + (" " * (cols - _ansi_len(truncated) - frac_visible)) + frac_str
                _row_extent.append((_ansi_len(truncated), frac_visible))

            out.append(line)

        out.append("")
        hint_str = _hint(
            ("↑↓", "move"),
            ("space", "toggle"),
            ("←/b", "back"),
            ("q", "quit"),
            ("↵", "confirm")
        )
        if isinstance(hint_str, tuple):
            hint_str = hint_str[0]
        out.extend(hint_str.split("\n") if hint_str else [])

        return out

    result = None
    try:
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if key == 'CTRL_C':
                break
            elif key in ('UP', 'k'):
                index = _next_index(index, raw_items, -1)
                w.render(_lines())
            elif key in ('DOWN', 'j'):
                index = _next_index(index, raw_items, 1)
                w.render(_lines())
            elif key == 'SCROLL_UP':
                index = _next_index(index, raw_items, -1)
                w.render(_lines())
            elif key == 'SCROLL_DOWN':
                index = _next_index(index, raw_items, 1)
                w.render(_lines())
            elif key.startswith('MOUSE_CLICK:'):
                parts = key.split(':')
                r = int(parts[2])
                col = int(parts[3]) if len(parts) > 3 else 1
                if w.row is None:
                    continue
                i = r - w.row - _cb_hlen[0] - 1
                # Only toggle when the click lands on the row's text or its
                # right-aligned fraction, not the empty gap between them.
                _on_text = False
                if 0 <= i < len(_row_extent):
                    cols_now = _cols()
                    text_end, frac_vis = _row_extent[i]
                    _on_text = (col <= text_end) or (frac_vis and col >= cols_now - frac_vis)
                if 0 <= i < len(raw_items) and not raw_items[i]['dimmed'] and _on_text:
                    index = i
                    target = raw_items[index]
                    if interlock_category_callback and locked_category:
                        target_cat = interlock_category_callback(target['obj'].value)
                        if target_cat != locked_category and not target['obj'].checked:
                            sys.stdout.write("\a")
                            sys.stdout.flush()
                            w.render(_lines())
                            continue
                    target['obj'].checked = not target['obj'].checked
                    w.render(_lines())
            elif key == 'SPACE':
                target = raw_items[index] if index < len(raw_items) else None
                if target and interlock_category_callback and locked_category:
                    target_cat = interlock_category_callback(target['obj'].value)
                    if target_cat != locked_category and not target['obj'].checked:
                        sys.stdout.write("\a")
                        sys.stdout.flush()
                        continue
                if target:
                    target['obj'].checked = not target['obj'].checked
                w.render(_lines())
            elif key == 'ENTER':
                result = [item['obj'].value for item in raw_items if item['obj'].checked]
                break
            elif key in ('LEFT', 'b', 'h', 'ESC'):
                break  # back / cancel → None
            elif key in ('q', 'Q'):
                raise QuitToTerminal()

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        sys.stdout.write(C.SHOW)
        sys.stdout.flush()
        w.clear()

    return result


def confirm(message: str, default: bool = False) -> bool:
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    w      = _Widget(fd)
    result = default

    def _render():
        cols = ui_utils.get_terminal_width()
        dflt = "yes" if default else "no"
        lines = [
            f"  {C.DIM}{message}{C.RESET}",
            f"{C.DIM}{'─' * cols}{C.RESET}",
        ]
        lines.extend([f"  {s}" for s in _hint(
            ("y", "yes"), ("n", "no"), ("↵", f"default ({dflt})")
        ).splitlines()])
        w.render(lines)

    try:
        _set_raw(fd)
        _render()
        while True:
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            if   key == 'CTRL_C':    result = False; break
            elif key == 'ENTER':     result = default; break
            elif key.lower() == 'y': result = True;  break
            elif key.lower() == 'n': result = False; break
    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def text(message: str, default: str = "") -> str | None:
    buf    = list(default)
    pos    = len(buf)
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    result = None

    # Track how many physical lines were drawn to clear them later
    prev_lines = 0

    def _render():
        nonlocal prev_lines
        cols = _cols()
        content = "".join(buf)
        content_width = max(1, cols - 6)

        wrapped_lines = _wrap_bordered_input_lines(content, content_width)
        pre_lines = _wrap_bordered_input_lines(content[:pos], content_width)
        cursor_row = max(0, len(pre_lines) - 1)
        cursor_col = len(pre_lines[-1]) if pre_lines else 0
        total_rows = len(wrapped_lines)

        if prev_lines > 0:
            sys.stdout.write(f"\r\033[{prev_lines}A")
        sys.stdout.write(f"\r\033[J{C.HIDE}")

        sys.stdout.write(f"\r  {C.DIM}{message}{C.RESET}\r\n")

        for i, line in enumerate(wrapped_lines):
            sys.stdout.write(f"\r  {C.DIM}│{C.RESET} {line:<{content_width}} {C.DIM}│{C.RESET}")
            if i < total_rows - 1:
                sys.stdout.write("\r\n")

        rows_to_move_up = (total_rows - 1) - cursor_row
        if rows_to_move_up > 0:
            sys.stdout.write(f"\033[{rows_to_move_up}A")
        col_offset = cursor_col + 4  # 2 spaces + "│ "
        if col_offset > 0:
            sys.stdout.write(f"\r\033[{col_offset}C")
        else:
            sys.stdout.write("\r")

        sys.stdout.write(C.SHOW)
        sys.stdout.flush()

        prev_lines = 1 + total_rows
        _render_status_bar()

    try:
        _set_raw(fd)
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()
        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                _render()
                continue
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            if   key == 'CTRL_C':             result = None;         break
            elif key == 'ENTER':              result = "".join(buf); break
            elif key == 'BACKSPACE' and pos > 0:
                buf.pop(pos - 1); pos -= 1; _render()
            elif key == 'LEFT' and pos > 0:
                pos -= 1; _render()
            elif key == 'RIGHT' and pos < len(buf):
                pos += 1; _render()
            elif key == 'UP':
                pos = max(0, pos - _cols()); _render()
            elif key == 'DOWN':
                pos = min(len(buf), pos + _cols()); _render()
            elif key == 'HOME':
                pos = 0; _render()
            elif key == 'END':
                pos = len(buf); _render()
            elif key == 'SPACE':
                buf.insert(pos, ' '); pos += 1; _render()
            elif len(key) == 1 and key.isprintable():
                buf.insert(pos, key); pos += 1; _render()
    finally:
        _restore_term_attrs(fd, old)
        sys.stdout.write("\033[H\033[3J\033[J" + C.SHOW)
        sys.stdout.flush()

    return result


def path(message: str, default: str = "") -> str | None:
    buf          = list(default)
    pos          = len(buf)
    fd           = sys.stdin.fileno()
    old          = _get_term_attrs(fd)
    result       = None
    _tab_matches : list = []
    _tab_index   = 0

    # Tracks the exact number of rows written in the previous render cycle
    # to roll back cleanly without scrolling or flickering the viewport.
    _last_rendered_lines = 1

    def _completions(current: str) -> list:
        try:
            expanded = os.path.expanduser(current)
            # Find the root lookup folder depending on whether the path target is a valid directory
            base     = expanded if os.path.isdir(expanded) else os.path.dirname(expanded) or "."
            stub     = "" if os.path.isdir(expanded) else os.path.basename(expanded)
            return sorted(
                os.path.join(base, e)
                for e in os.listdir(base)
                if e.startswith(stub) and not e.startswith('.')
            )
        except OSError:
            return []

    def _render():
        nonlocal _last_rendered_lines, _tab_matches
        cols    = _cols()
        content = "".join(buf)
        prefix  = "  │ "
        max_w   = max(1, cols - 6)

        if pos > max_w:
            display  = content[pos - max_w: pos]
            disp_pos = max_w
        else:
            display  = content[:max_w]
            disp_pos = pos

        cursor_col = len(prefix) + disp_pos

        clear_code = ""
        if _last_rendered_lines > 1:
            clear_code += f"\033[{_last_rendered_lines - 1}A"
        clear_code += "\r"

        render_stream = [
            clear_code,
            f"\033[K  {C.DIM}{message}{C.RESET}\r\n",
            f"\033[K  {C.DIM}│{C.RESET} {display:<{max_w}} {C.DIM}│{C.RESET}"
        ]

        lines_count = 2

        # Do not output autocomplete options when at a subdirectory juncture or when empty
        should_show_hints = content and not content.endswith('/') and not content.endswith(os.path.sep)

        visible_matches = []
        if should_show_hints and _tab_matches:
            stub = os.path.basename(content)
            for m in _tab_matches:
                name = os.path.basename(m.rstrip('/'))
                if name.startswith(stub):
                    visible_matches.append(m)

        if visible_matches:
            render_stream.append("\r\n\033[K")
            lines_count += 1

            start_pad = min(cursor_col, max(0, cols - 35))
            render_stream.append(" " * start_pad)

            tooltip_parts = []
            current_len = start_pad

            for idx, match in enumerate(visible_matches[:5]):
                name = os.path.basename(match.rstrip('/'))
                if os.path.isdir(match):
                    name += "/"

                if idx == (_tab_index % len(visible_matches)):
                    item_str = f"{C.INVERT}{C.BOLD}{name}{C.RESET}"
                    visible_len = len(name)
                else:
                    item_str = f"{C.DIM}{name}{C.RESET}"
                    visible_len = len(name)

                if current_len + visible_len + 2 > cols:
                    render_stream.append("  ".join(tooltip_parts) + "\r\n\033[K" + " " * start_pad)
                    lines_count += 1
                    tooltip_parts = [item_str]
                    current_len = start_pad + visible_len
                else:
                    tooltip_parts.append(item_str)
                    current_len += visible_len + 2

            if tooltip_parts:
                render_stream.append("  ".join(tooltip_parts))

            if len(visible_matches) > 5:
                render_stream.append(f" {C.DIM}(+{len(visible_matches)-5}){C.RESET}")

        _last_rendered_lines = lines_count

        move_back_lines = lines_count - 2
        adjust_cursor = f"\033[{move_back_lines}A" if move_back_lines > 0 else ""

        sys.stdout.write(
            C.HIDE + "".join(render_stream) +
            adjust_cursor + _col(cursor_col + 1) + C.SHOW
        )
        sys.stdout.flush()
        _render_status_bar()

    try:
        _set_raw(fd)
        _tab_matches = _completions("".join(buf))
        _render()

        while True:
            if ui_utils.consume_resize(): _render()
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            if key == 'CTRL_C':
                result = None; break
            elif key == 'ENTER':
                result = "".join(buf); break

            elif key == 'TAB':
                current_text = "".join(buf)
                stub = os.path.basename(current_text) if (current_text and not current_text.endswith('/')) else ""

                visible_matches = [m for m in _tab_matches if os.path.basename(m.rstrip('/')).startswith(stub)] if stub else _tab_matches

                if visible_matches:
                    completed = visible_matches[_tab_index % len(visible_matches)]
                    if os.path.isdir(completed) and not completed.endswith("/"):
                        completed += "/"

                    buf[:] = list(completed)
                    pos = len(buf)
                    _tab_index += 1

                    _tab_matches = _completions("".join(buf))
                _render()
                continue

            elif key == 'BACKSPACE' and pos > 0:
                buf.pop(pos - 1); pos -= 1
                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()
            elif key == 'SPACE':
                buf.insert(pos, ' '); pos += 1
                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()
            elif key == 'LEFT'  and pos > 0:
                pos -= 1; _render()
            elif key == 'RIGHT' and pos < len(buf):
                pos += 1; _render()
            elif key == 'HOME':
                pos = 0; _render()
            elif key == 'END':
                pos = len(buf); _render()
            elif len(key) == 1 and key.isprintable():
                buf.insert(pos, key); pos += 1

                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()

    finally:
        _restore_term_attrs(fd, old)
        sys.stdout.write("\033[H\033[3J\033[J" + C.SHOW)
        sys.stdout.flush()

    return result

def _render_list_edit_cell(text: str, width: int, is_editing: bool, is_active_col: bool, edit_buf: list[str], edit_pos: int) -> str:
    if not is_editing or not is_active_col:
        return ui_utils.truncate_text(text, width)

    buf_str = "".join(edit_buf)

    if edit_pos >= len(buf_str):
        display_str = buf_str + f"{C.BACK}█{C.RESET}"
    else:
        display_str = buf_str[:edit_pos] + f"{C.INVERT}{C.BOLD}{buf_str[edit_pos]}{C.RESET}" + buf_str[edit_pos+1:]

    visible_len = len(buf_str) + (1 if edit_pos >= len(buf_str) else 0)
    padding = max(0, width - visible_len)
    return display_str + (" " * padding)


def _build_list_edit_lines(
    message: str, items: list, headers: tuple[str, ...],
    cursor: int, viewport: int,
    edit_mode: bool, edit_col: int, edit_buf: list[str], edit_pos: int
) -> tuple[list[str], int]:
    num_cols = len(headers)
    cols = _cols()
    c = cols - 4
    inner = c
    out = []

    base_hints = {"↑↓": "move", "a": "add", "e": "edit", "d": "delete", "i": "import", "esc": "back", "↵": "save", "q": "quit"}
    edit_hints = {"tab": "next col", "esc": "cancel", "↵": "apply"}

    out.append(f"  {C.DIM}{message}{C.RESET}")
    out.append(f"{C.DIM}{'─' * cols}{C.RESET}")

    avail_w = max(10, inner - 4 - (2 * (num_cols - 1)))
    col_w = avail_w // num_cols
    last_w = avail_w - (col_w * (num_cols - 1))

    if num_cols > 1:
        h_parts = [f"{headers[i]:<{col_w}}" for i in range(num_cols - 1)]
        h_parts.append(f"{headers[-1]}")
        out.append(f"    {C.DIM}{'  '.join(h_parts)}{C.RESET}")

        u_parts = ["─" * col_w for _ in range(num_cols - 1)]
        u_parts.append("─" * last_w)
        out.append(f"    {'  '.join(u_parts)}")
    else:
        out.append(f"    {C.DIM}{headers[0]}{C.RESET}")
        out.append(f"    {'─' * inner}")

    active_hints = edit_hints if edit_mode else base_hints
    hint_res = _hint(*active_hints.items())
    hint_raw = hint_res[0] if isinstance(hint_res, tuple) else hint_res
    hint_lines = hint_raw.split("\n") if hint_raw else []

    fixed_overhead = 6 + len(hint_lines)
    vis = max(2, _visible_rows() - fixed_overhead)
    n = len(items)

    if cursor < viewport:
        viewport = cursor
    elif cursor >= viewport + vis:
        viewport = cursor - vis + 1

    if n == 0:
        out.append(f"    {C.DIM}(empty list){C.RESET}")
    else:
        for i in range(viewport, min(viewport + vis, n)):
            item = items[i]
            is_sel = (i == cursor)

            row_is_editing = (is_sel and edit_mode)
            cursor_glyph = f"{C.ACCENT}›{C.RESET}" if (is_sel and not edit_mode) else (" " if not row_is_editing else f"✎")

            if num_cols > 1:
                i_vals = list(item) if isinstance(item, (list, tuple)) else [str(item)]
                while len(i_vals) < num_cols: i_vals.append("")

                row_parts = []
                for j in range(num_cols - 1):
                    cell_str = _render_list_edit_cell(str(i_vals[j]), col_w, row_is_editing, edit_col == j, edit_buf, edit_pos)
                    row_parts.append(f"{cell_str:<{col_w}}" if not (row_is_editing and edit_col == j) else cell_str)

                last_cell = _render_list_edit_cell(str(i_vals[-1]), last_w, row_is_editing, edit_col == (num_cols - 1), edit_buf, edit_pos)
                row_parts.append(last_cell)

                row_str = "  ".join(row_parts)

                if is_sel and not edit_mode:
                    out.append(f"  {cursor_glyph} {C.PRIMARY}{C.BOLD}{row_str}{C.RESET}")
                elif is_sel and edit_mode:
                    out.append(f"  {cursor_glyph} {row_str}")
                else:
                    out.append(f"  {cursor_glyph} {row_str}")
            else:
                val_str = str(item)
                cell_str = _render_list_edit_cell(val_str, inner - 4, row_is_editing, True, edit_buf, edit_pos)
                if is_sel and not edit_mode:
                    out.append(f"  {cursor_glyph} {C.PRIMARY}{C.BOLD}{cell_str}{C.RESET}")
                else:
                    out.append(f"  {cursor_glyph} {cell_str}")

    out.append(f"{C.DIM}{'─' * cols}{C.RESET}")
    out.extend(hint_lines)

    return out, viewport

def list_edit(message: str, initial_items: list | None = None, headers: tuple[str, ...] = ("ROLE", "NAME")) -> list | None:
    """Arrow keys navigate, 'a' adds, 'e' edits in-place, 'd' deletes, Enter saves.

    Supports in-place cell editing with Tab navigation between columns.
    """
    items    = list(initial_items) if initial_items else []
    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)
    num_cols = len(headers)

    edit_mode = False
    edit_col  = 0
    edit_buf  = []
    edit_pos  = 0
    edit_backup = None

    def _render():
        nonlocal viewport
        lines, new_viewport = _build_list_edit_lines(
            message, items, headers,
            cursor, viewport,
            edit_mode, edit_col, edit_buf, edit_pos
        )
        viewport = new_viewport
        w.render(lines)

    def _commit_edit_buffer():
        val = "".join(edit_buf)
        if num_cols > 1:
            curr = list(items[cursor]) if isinstance(items[cursor], (list, tuple)) else [str(items[cursor])]
            while len(curr) < num_cols: curr.append("")
            curr[edit_col] = val
            items[cursor] = tuple(curr)
        else:
            items[cursor] = val

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if edit_mode:
                if key == 'ESC':
                    items[cursor] = edit_backup
                    edit_mode = False
                    _render()

                elif key == 'ENTER':
                    _commit_edit_buffer()
                    edit_mode = False
                    _render()

                elif key == 'TAB':
                    if num_cols > 1:
                        _commit_edit_buffer()
                        edit_col = (edit_col + 1) % num_cols

                        curr = items[cursor]
                        i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                        while len(i_vals) < num_cols: i_vals.append("")

                        edit_buf = list(str(i_vals[edit_col]))
                        edit_pos = len(edit_buf)
                        _render()

                elif key == 'BACKSPACE' and edit_pos > 0:
                    edit_buf.pop(edit_pos - 1)
                    edit_pos -= 1
                    _render()

                elif key == 'LEFT' and edit_pos > 0:
                    edit_pos -= 1
                    _render()

                elif key == 'RIGHT' and edit_pos < len(edit_buf):
                    edit_pos += 1
                    _render()

                elif key == 'HOME':
                    edit_pos = 0
                    _render()

                elif key == 'END':
                    edit_pos = len(edit_buf)
                    _render()

                elif key == 'SPACE':
                    edit_buf.insert(edit_pos, ' ')
                    edit_pos += 1
                    _render()

                elif len(key) == 1 and key.isprintable():
                    edit_buf.insert(edit_pos, key)
                    edit_pos += 1
                    _render()

            else:
                if key == 'CTRL_C':
                    break
                elif key in ('UP', 'k'):
                    if items: cursor = (cursor - 1) % len(items)
                    _render()
                elif key in ('DOWN', 'j'):
                    if items: cursor = (cursor + 1) % len(items)
                    _render()

                elif key == 'a':
                    empty_item = tuple(["" for _ in range(num_cols)]) if num_cols > 1 else ""
                    items.append(empty_item)
                    cursor = len(items) - 1

                    edit_mode = True
                    edit_col = 0
                    edit_buf = []
                    edit_pos = 0
                    edit_backup = empty_item
                    _render()

                elif key == 'e' and items:
                    edit_mode = True
                    edit_col = 0
                    edit_backup = items[cursor]

                    if num_cols > 1:
                        curr = items[cursor]
                        i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                        while len(i_vals) < num_cols: i_vals.append("")
                        edit_buf = list(str(i_vals[0]))
                    else:
                        edit_buf = list(str(items[cursor]))

                    edit_pos = len(edit_buf)
                    _render()

                elif key in ('d', 'BACKSPACE', 'DELETE') and items:
                    items.pop(cursor)
                    if items:
                        cursor = min(cursor, len(items) - 1)
                    else:
                        cursor = 0
                    _render()

                elif key == 'ENTER':
                    result = items
                    break

                elif key == 'i':
                    _restore_term_attrs(fd, old)
                    text_input = system_editor_edit(initial_text="")
                    _set_raw(fd)
                    sys.stdout.write("\033[H\033[3J\033[J")
                    sys.stdout.flush()
                    w.anchor_reset()
                    if text_input:
                        for _line in text_input.splitlines():
                            _line = _line.strip()
                            if not _line:
                                continue
                            if num_cols > 1:
                                _role, _, _name = _line.partition(':')
                                items.append((_role.strip(), _name.strip()))
                            else:
                                items.append(_line)
                        cursor = len(items) - 1 if items else 0
                    _render()

                elif key in ('q', 'Q'):
                    # Save current state, then quit on the next menu.
                    _state.QUIT_REQUESTED = True
                    result = items
                    break

                elif key == 'ESC':
                    ui_utils.clear_screen()
                    result = items if confirm("Discard changes?", default=False) else initial_items
                    break

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def _is_leap_year(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    elif month in (4, 6, 9, 11):
        return 30
    elif month == 2:
        return 29 if _is_leap_year(year) else 28
    return 0


def _validate_date(year: int, month: int, day: int) -> bool:
    if not (1 <= month <= 12):
        return False
    if not (1 <= day <= _days_in_month(year, month)):
        return False
    return True


def _parse_date(date_str: str) -> tuple[int, int, int] | None:
    """
    Parse a date string (flexible format).
    Accepts: YYYY-MM-DD, YYYY/MM/DD, MM/DD/YYYY, etc.
    Returns (year, month, day) or None if invalid.
    """
    if not date_str:
        return None

    # Remove common separators
    parts = re.split(r'[-/\s.]', date_str.strip())
    parts = [p for p in parts if p]

    if len(parts) != 3:
        return None

    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None

    # Detect format based on size and ranges
    if nums[0] > 1900:  # First is year (YYYY-MM-DD or YYYY/MM/DD)
        year, month, day = nums[0], nums[1], nums[2]
    elif nums[2] > 1900:  # Last is year (MM/DD/YYYY or DD/MM/YYYY)
        # Heuristic: if first <= 12, assume MM/DD/YYYY; else DD/MM/YYYY
        if nums[0] <= 12:
            month, day, year = nums[0], nums[1], nums[2]
        else:
            day, month, year = nums[0], nums[1], nums[2]
    else:
        return None

    if _validate_date(year, month, day):
        return (year, month, day)
    return None

def calendar_select(message: str = "Select date:", initial: str = "") -> str | None:
    """
    Interactive calendar widget for date selection.
    Allows month/year navigation and in-place day selection.

    Args:
        message: Prompt label
        initial: Initial date (YYYY-MM-DD or flexible format)

    Returns:
        Selected date as YYYY-MM-DD string, or None if cancelled
    """
    if initial:
        parsed = _parse_date(initial)
        if parsed:
            y, m, d = parsed
        else:
            # Fallback to today
            today = datetime.date.today()
            y, m, d = today.year, today.month, today.day
    else:
        today = datetime.date.today()
        y, m, d = today.year, today.month, today.day

    cursor_day = d
    day_mode = False  # False: Navigates Month/Year | True: Navigates Days

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)

    def _render():
        cols = ui_utils.get_terminal_width()
        c = cols - 4
        lines = []

        # Header
        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        # Month/Year display
        month_name = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m]

        lines.append(f"  {C.BOLD}{month_name} {y}{C.RESET}")

        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        # Day headers
        day_headers = "Mo Tu We Th Fr Sa Su"
        lines.append(f"  {day_headers}")

        # Calendar grid
        cal_obj = cal.monthcalendar(y, m)

        for week in cal_obj:
            week_parts = []
            for day in week:
                if day == 0:
                    week_parts.append("   ")
                else:
                    is_selected = (day == cursor_day)
                    if is_selected:
                        style = f"{C.ACCENT}{C.BOLD}" if day_mode else f"{C.BOLD}"
                        week_parts.append(f"{style}{day:2d}{C.RESET} ")
                    else:
                        week_parts.append(f"{day:2d} ")
            lines.append(f"  {''.join(week_parts)}")

        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        if not day_mode:
            shortcuts = _hint(
                ("↵", "confirm"),
                ("esc", "back"), ("q", "quit"),
                ("tab", "switch to day mode"),
                ("←→", "month"),
                ("↑↓", "year"),
                ("m", "manual entry"),
            )
        else:
            shortcuts = _hint(
                ("↵", "confirm"),
                ("esc", "back"), ("q", "quit"),
                ("tab", "switch to month/year"),
                ("←→", "±1 day"),
                ("↑↓", "±7 days"),
                ("m", "manual entry"),
            )

        shortcuts = shortcuts.splitlines()
        lines.extend([f"  {s}" for s in shortcuts])

        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if key == 'ENTER':
                result = f"{y:04d}-{m:02d}-{cursor_day:02d}"
                break
            elif key == 'ESC':
                break
            elif key in ('q', 'Q'):
                _state.QUIT_REQUESTED = True
                result = f"{y:04d}-{m:02d}-{cursor_day:02d}"
                break

            elif key == 'TAB':
                day_mode = not day_mode

            elif key == 'RIGHT':
                if not day_mode:
                    m += 1
                    if m > 12:
                        m = 1
                        y += 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    cursor_day += 1
                    if cursor_day > _days_in_month(y, m):
                        m += 1
                        if m > 12:
                            m = 1
                            y += 1
                        cursor_day = 1

            elif key == 'LEFT':
                if not day_mode:
                    m -= 1
                    if m < 1:
                        m = 12
                        y -= 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    cursor_day -= 1
                    if cursor_day < 1:
                        m -= 1
                        if m < 1:
                            m = 12
                            y -= 1
                        cursor_day = _days_in_month(y, m)

            elif key == 'UP':
                if not day_mode:
                    y -= 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    cursor_day -= 7
                    if cursor_day < 1:
                        m -= 1
                        if m < 1:
                            m = 12
                            y -= 1
                        # Wraps into the last day of the previous month
                        cursor_day = _days_in_month(y, m)

            elif key == 'DOWN':
                if not day_mode:
                    y += 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    cursor_day += 7
                    if cursor_day > _days_in_month(y, m):
                        m += 1
                        if m > 12:
                            m = 1
                            y += 1
                        # Wraps into the first day of the next month
                        cursor_day = 1

            elif key == 'm':
                w.clear()
                manual = text("Enter date (YYYY-MM-DD):", default=f"{y:04d}-{m:02d}-{cursor_day:02d}")
                if manual:
                    parsed = _parse_date(manual)
                    if parsed:
                        y, m, d = parsed
                        cursor_day = d
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()

            elif key.isdigit() and int(key) >= 1 and int(key) <= 9:
                day = int(key)
                if day <= _days_in_month(y, m):
                    cursor_day = day

            _render()

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def datetime_edit(message: str = "Edit date and time:", initial: str = "") -> str | None:
    """
    Combined single-screen date + time editor.

    Date section: calendar grid (TAB toggles month/year ↔ day navigation).
    Time section: HH:MM:SS.ms fields (TAB advances field).
    TAB from date-day-mode → time section; TAB from last time field → date.
    ENTER saves from any position. Returns ISO 8601 string or None if cancelled.
    """
    _sep = 'T' if 'T' in initial else (' ' if ' ' in initial else None)
    date_str, time_str = initial.split(_sep, 1) if _sep else (initial, "")

    parsed = _parse_date(date_str) if date_str else None
    if parsed:
        year, month, cursor_day = parsed
    else:
        _today = datetime.date.today()
        year, month, cursor_day = _today.year, _today.month, _today.day

    day_mode = False

    t_parts = time_str.split(':')
    _h = t_parts[0] if t_parts and t_parts[0] else "00"
    _mi = t_parts[1] if len(t_parts) > 1 else "00"
    if len(t_parts) > 2:
        _sp = t_parts[2].split('.')
        _s, _ms = (_sp[0] if _sp else "00"), (_sp[1] if len(_sp) > 1 else "000")
    else:
        _s, _ms = "00", "000"

    tfields = {
        'hours':   list(_h[-2:].zfill(2)),
        'minutes': list(_mi[-2:].zfill(2)),
        'seconds': list(_s[-2:].zfill(2)),
        'millis':  list(_ms[-3:].zfill(3)),
    }
    torder  = ['hours', 'minutes', 'seconds', 'millis']
    tmaxlen = {'hours': 2, 'minutes': 2, 'seconds': 2, 'millis': 3}
    tcursor = 0
    tpos    = {k: len(tfields[k]) for k in torder}

    section = 'date'

    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    def _render():
        cols = ui_utils.get_terminal_width()
        lines = []

        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        # Date section
        month_name = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month]
        dpfx = C.BOLD if section == 'date' else C.DIM
        lines.append(f"  {dpfx}{month_name} {year}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")
        lines.append("  Mo Tu We Th Fr Sa Su")

        for week in cal.monthcalendar(year, month):
            parts = []
            for day in week:
                if day == 0:
                    parts.append("   ")
                elif day == cursor_day and section == 'date':
                    style = f"{C.ACCENT}{C.BOLD}" if day_mode else C.BOLD
                    parts.append(f"{style}{day:2d}{C.RESET} ")
                else:
                    parts.append(f"{day:2d} ")
            lines.append(f"  {''.join(parts)}")

        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        # Time section
        tpfx = C.BOLD if section == 'time' else C.DIM
        row = f"  {tpfx}Time{C.RESET}  "
        for i, field in enumerate(torder):
            val = "".join(tfields[field])
            pos = tpos[field]
            if section == 'time' and i == tcursor:
                if pos >= len(val):
                    cell = val + f"{C.BACK}█{C.RESET}"
                else:
                    cell = val[:pos] + f"{C.INVERT}{C.BOLD}{val[pos]}{C.RESET}" + val[pos+1:]
            else:
                cell = f"{C.DIM}{val.ljust(tmaxlen[field], '0')}{C.RESET}"
            row += cell
            if i == 0:   row += ":"
            elif i == 1: row += ":"
            elif i == 2: row += "."
        lines.append(row)

        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        if section == 'date':
            if not day_mode:
                h = _hint(("←→", "month"), ("↑↓", "year"), ("tab", "day mode"), ("↵", "save"), ("esc", "back"), ("q", "quit"))
            else:
                h = _hint(("←→↑↓", "navigate"), ("tab", "→ time"), ("↵", "save"), ("esc", "back"), ("q", "quit"))
        else:
            h = _hint(("←→", "cursor"), ("tab", "next / → date"), ("↵", "save"), ("esc", "back"), ("q", "quit"))

        lines.extend([f"  {s}" for s in h.splitlines()])
        w.render(lines)

    def _build_result() -> str:
        h  = "".join(tfields['hours']).zfill(2)
        mi = "".join(tfields['minutes']).zfill(2)
        s  = "".join(tfields['seconds']).zfill(2)
        ms = "".join(tfields['millis']).zfill(3)
        hms = f"{h}:{mi}:{s}"
        date_part = f"{year:04d}-{month:02d}-{cursor_day:02d}"
        if hms == "00:00:00":
            return date_part
        return f"{date_part}T{hms}" if ms == "000" else f"{date_part}T{hms}.{ms}"

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if key in ('ESC', 'CTRL_C'):
                break
            if key in ('q', 'Q'):
                _state.QUIT_REQUESTED = True
                result = _build_result()
                break

            if key == 'ENTER':
                result = _build_result()
                break

            if key == 'TAB':
                if section == 'date':
                    if not day_mode:
                        day_mode = True
                    else:
                        day_mode = False
                        section = 'time'
                        tcursor = 0
                else:
                    if tcursor < len(torder) - 1:
                        tcursor += 1
                    else:
                        tcursor = 0
                        section = 'date'

            elif section == 'date':
                if key == 'RIGHT':
                    if not day_mode:
                        month += 1
                        if month > 12: month, year = 1, year + 1
                        cursor_day = min(cursor_day, _days_in_month(year, month))
                    else:
                        cursor_day += 1
                        if cursor_day > _days_in_month(year, month):
                            month += 1
                            if month > 12: month, year = 1, year + 1
                            cursor_day = 1
                elif key == 'LEFT':
                    if not day_mode:
                        month -= 1
                        if month < 1: month, year = 12, year - 1
                        cursor_day = min(cursor_day, _days_in_month(year, month))
                    else:
                        cursor_day -= 1
                        if cursor_day < 1:
                            month -= 1
                            if month < 1: month, year = 12, year - 1
                            cursor_day = _days_in_month(year, month)
                elif key == 'UP':
                    if not day_mode:
                        year -= 1
                        cursor_day = min(cursor_day, _days_in_month(year, month))
                    else:
                        cursor_day -= 7
                        if cursor_day < 1:
                            month -= 1
                            if month < 1: month, year = 12, year - 1
                            cursor_day = _days_in_month(year, month)
                elif key == 'DOWN':
                    if not day_mode:
                        year += 1
                        cursor_day = min(cursor_day, _days_in_month(year, month))
                    else:
                        cursor_day += 7
                        if cursor_day > _days_in_month(year, month):
                            month += 1
                            if month > 12: month, year = 1, year + 1
                            cursor_day = 1

            else:  # time section
                cur_f = torder[tcursor]
                buf   = tfields[cur_f]
                pos   = tpos[cur_f]
                maxl  = tmaxlen[cur_f]

                if key == 'BACKSPACE':
                    if pos > 0:
                        buf.pop(pos - 1)
                        tpos[cur_f] = pos - 1
                elif key == 'DELETE':
                    if pos < len(buf):
                        buf.pop(pos)
                elif key == 'LEFT':
                    tpos[cur_f] = max(0, pos - 1)
                elif key == 'RIGHT':
                    tpos[cur_f] = min(len(buf), pos + 1)
                elif key == 'HOME':
                    tpos[cur_f] = 0
                elif key == 'END':
                    tpos[cur_f] = len(buf)
                elif key.isdigit() and len(buf) < maxl:
                    buf.insert(pos, key)
                    tpos[cur_f] = pos + 1

            _render()

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def fraction_edit(message: str = "Edit metadata pair:",
                    tag: str = "TRCK", value: str = "") -> dict | None:
    """
    In-place editor for an isolated single tag's current/total values.
    Allows integers, floats, spaces, and strings.

    Returns:
        Dict with keys: {'current', 'total'} or None if cancelled
    """
    tag_config = {
        "TRCK": ("Track", "of"),
        "TPOS": ("Disc", "of"),
        "MVIN": ("Movement", "of"),
    }
    lbl_idx, lbl_tot = tag_config.get(tag.upper(), ("Index", "of"))

    # 2. Extract baseline values from the value string (e.g., "3.5/12" -> current="3.5", total="12")
    parts = str(value).split('/') if '/' in str(value) else str(value).split('⁄') if '⁄' in str(value) else [value, ""] if value else ["", ""]
    curr_val = parts[0].strip()
    tot_val = parts[1].strip() if len(parts) > 1 else ""

    field_order = ['current', 'total']
    field_labels = {'current': lbl_idx, 'total': lbl_tot}

    cursor_field = 0
    edit_buffers = {
        'current': list(curr_val),
        'total': list(tot_val)
    }
    edit_positions = {k: len(edit_buffers[k]) for k in field_order}

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)

    def _render():
        cols = ui_utils.get_terminal_width()
        c = cols - 4
        lines = []

        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        row = "  "
        for i, field in enumerate(field_order):
            if i > 0:
                row += " "

            label = field_labels[field]
            val_str = "".join(edit_buffers[field])

            if i == cursor_field:
                pos = edit_positions[field]
                if pos >= len(val_str):
                    display = val_str + f"{C.BACK}█{C.RESET}"
                else:
                    display = val_str[:pos] + f"{C.INVERT}{C.BOLD}{val_str[pos]}{C.RESET}" + val_str[pos+1:]
                row += f"{label} {display}"
            else:
                if not val_str:
                    row += f"{C.DIM}{label} ──{C.RESET}"
                else:
                    row += f"{label} {val_str}"

        lines.append(row)
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        shortcuts = _hint(
            ("↵", "save"),
            ("tab", "next field"),
            ("esc", "back"), ("q", "quit"),
        )
        shortcuts = shortcuts.splitlines()
        lines.extend([f"  {s}" for s in shortcuts])

        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            current_field = field_order[cursor_field]
            buf = edit_buffers[current_field]
            pos = edit_positions[current_field]

            if key == 'ENTER':
                result = {'current': "".join(edit_buffers['current']), 'total': "".join(edit_buffers['total'])}
                break
            elif key == 'ESC':
                break
            elif key in ('q', 'Q'):
                _state.QUIT_REQUESTED = True
                result = {'current': "".join(edit_buffers['current']), 'total': "".join(edit_buffers['total'])}
                break
            elif key == 'TAB':
                cursor_field = (cursor_field + 1) % len(field_order)
            elif key == 'BACKSPACE':
                if pos > 0:
                    buf.pop(pos - 1)
                    edit_positions[current_field] = pos - 1
            elif key == 'DELETE':
                if pos < len(buf):
                    buf.pop(pos)
            elif key == 'LEFT':
                edit_positions[current_field] = max(0, pos - 1)
            elif key == 'RIGHT':
                edit_positions[current_field] = min(len(buf), pos + 1)
            elif len(key) == 1 and (key.isalnum() or key in ".- "):
                buf.insert(pos, key)
                edit_positions[current_field] = pos + 1

            _render()

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result

def time_edit(message: str = "Edit time:", initial: str = "00:00:00") -> str | None:
    """
    In-place editor for time input (HH:MM:SS).
    Supports milliseconds and auto-validation.

    Args:
        message: Prompt label
        initial: Initial time (HH:MM:SS or HH:MM:SS.mmm)

    Returns:
        Formatted time string or None if cancelled
    """
    parts = initial.split(':')
    hours = parts[0] if parts and parts[0] else "00"
    minutes = parts[1] if len(parts) > 1 and parts[1] else "00"

    if len(parts) > 2:
        sec_parts = parts[2].split('.')
        seconds = sec_parts[0] if sec_parts else "00"
        millis = sec_parts[1] if len(sec_parts) > 1 else "000"
    else:
        seconds = "00"
        millis = "000"

    fields = {
        'hours': list(hours[-2:].zfill(2)),
        'minutes': list(minutes[-2:].zfill(2)),
        'seconds': list(seconds[-2:].zfill(2)),
        'millis': list(millis[-3:].zfill(3)),
    }

    field_order = ['hours', 'minutes', 'seconds', 'millis']
    field_labels = {
        'hours': 'HH',
        'minutes': 'MM',
        'seconds': 'SS',
        'millis': 'ms',
    }
    field_maxlen = {
        'hours': 2,
        'minutes': 2,
        'seconds': 2,
        'millis': 3,
    }

    cursor_field = 0
    positions = {k: len(fields[k]) for k in field_order}

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)

    def _validate_time() -> bool:
        try:
            h = int("".join(fields['hours']) or "0")
            m = int("".join(fields['minutes']) or "0")
            s = int("".join(fields['seconds']) or "0")
            return 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60
        except ValueError:
            return False

    def _render():
        cols = ui_utils.get_terminal_width()
        c = cols - 4
        lines = []

        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")

        row = "  "
        for i, field in enumerate(field_order):
            label = field_labels[field]
            value = "".join(fields[field])
            pos = positions[field]

            if i == cursor_field:
                if pos >= len(value):
                    display = value + f"{C.BACK}█{C.RESET}"
                else:
                    display = value[:pos] + f"{C.INVERT}{C.BOLD}{value[pos]}{C.RESET}" + value[pos+1:]
            else:
                display = value.ljust(field_maxlen[field], '0')

            row += display

            if i == 0:
                row += ":"
            elif i == 1:
                row += ":"
            elif i == 2:
                row += "."

        lines.append(row)
        lines.append(f"{C.DIM}{'─' * cols}{C.RESET}")
        lines.extend(_hint(('↵', 'save'), ('tab', 'next field'), ('esc', 'back'), ('q', 'quit')).splitlines())

        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            current_field = field_order[cursor_field]
            buf = fields[current_field]
            pos = positions[current_field]
            max_len = field_maxlen[current_field]

            if key == 'ENTER':
                if _validate_time():
                    h = "".join(fields['hours']).zfill(2)
                    m = "".join(fields['minutes']).zfill(2)
                    s = "".join(fields['seconds']).zfill(2)
                    ms = "".join(fields['millis']).zfill(3)
                    result = f"{h}:{m}:{s}.{ms}"
                    break
            elif key == 'ESC':
                break
            elif key in ('q', 'Q'):
                if _validate_time():
                    h = "".join(fields['hours']).zfill(2)
                    m = "".join(fields['minutes']).zfill(2)
                    s = "".join(fields['seconds']).zfill(2)
                    ms = "".join(fields['millis']).zfill(3)
                    result = f"{h}:{m}:{s}.{ms}"
                _state.QUIT_REQUESTED = True
                break
            elif key == 'TAB':
                cursor_field = (cursor_field + 1) % len(field_order)
            elif key == 'BACKSPACE':
                if pos > 0:
                    buf.pop(pos - 1)
                    positions[current_field] = pos - 1
            elif key == 'DELETE':
                if pos < len(buf):
                    buf.pop(pos)
            elif key == 'LEFT':
                positions[current_field] = max(0, pos - 1)
            elif key == 'RIGHT':
                positions[current_field] = min(len(buf), pos + 1)
            elif key == 'HOME':
                positions[current_field] = 0
            elif key == 'END':
                positions[current_field] = len(buf)
            elif key.isdigit() and len(buf) < max_len:
                buf.insert(pos, key)
                positions[current_field] = pos + 1

            _render()

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result

# ─── Graphic equaliser widget (EQU2) ────────────────────────────────────────
_EQ_GAIN_MAX = 12.0
_EQ_STEP = 0.5
_EQ_COARSE = 3.0
_EQ_ISO_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
_EQ_PRESETS = [
    ("Flat", {}),
    ("Bass boost", {31: 6, 62: 5, 125: 3, 250: 1}),
    ("Treble boost", {4000: 2, 8000: 4, 16000: 6}),
    ("V-shape", {31: 5, 62: 4, 125: 2, 500: -2, 1000: -3, 2000: -2, 8000: 4, 16000: 5}),
    ("Vocal", {250: 1, 500: 2, 1000: 3, 2000: 3, 4000: 2}),
    ("Loudness", {31: 6, 62: 4, 8000: 3, 16000: 5}),
]
_EQ_UP_BLOCKS = ' ▁▂▃▄▅▆▇'


def _eq_fmt_freq(freq: float) -> str:
    f = int(round(freq))
    if f >= 1000:
        k = f / 1000.0
        return f"{k:.0f}k" if k == int(k) else f"{k:.1f}k"
    return str(f)


def _eq_render_lines(bands: list, cursor: int, message: str, status: str,
                     cols: int, rows: int, show_curve: bool = True) -> list[str]:
    """Render the graphic-EQ plot: vertical bands from a 0 dB baseline, a dim
    response curve through the band tops, dB axis and frequency labels."""
    out = [
        f"  {C.DIM}{message}{C.RESET}",
        f"{C.DIM}{'─' * cols}{C.RESET}",
    ]
    n = len(bands)
    plot_w = max(10, cols - 5)              # 4 cols for the dB label + 1 gap
    avail = rows - 9
    half_h = max(3, min(8, avail // 2)) if avail > 6 else 3
    db_per_row = _EQ_GAIN_MAX / half_h
    baseline = half_h
    total_rows = 2 * half_h + 1

    band_x = [min(plot_w - 1, int((i + 0.5) * plot_w / n)) for i in range(n)] if n else []
    x_to_band = {x: i for i, x in enumerate(band_x)}

    # Interpolated response curve (linear in dB between adjacent band centres).
    curve: list = [None] * plot_w
    if show_curve and n >= 1:
        for x in range(plot_w):
            if x <= band_x[0]:
                curve[x] = bands[0][1]
            elif x >= band_x[-1]:
                curve[x] = bands[-1][1]
            else:
                for j in range(n - 1):
                    if band_x[j] <= x <= band_x[j + 1]:
                        x0, x1, g0, g1 = band_x[j], band_x[j + 1], bands[j][1], bands[j + 1][1]
                        t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
                        curve[x] = g0 + (g1 - g0) * t
                        break

    def _band_char(i: int, r: int):
        g = bands[i][1]
        if g >= 0:
            cells = g / db_per_row
            full = int(cells)
            frac = cells - full
            if r == baseline and full == 0 and frac <= 0.06:
                return '▪'                                  # zero-gain node
            if baseline - full <= r <= baseline:
                return '█'
            if r == baseline - full - 1 and frac > 0.06:
                return _EQ_UP_BLOCKS[max(1, min(7, round(frac * 8)))]
            return None
        cells = (-g) / db_per_row
        full = int(cells)
        frac = cells - full
        if baseline <= r <= baseline + full:
            return '█'
        if r == baseline + full + 1 and frac >= 0.5:
            return '▀'
        return None

    db_labels = {0: f"+{int(_EQ_GAIN_MAX)}", baseline: "0", 2 * half_h: f"-{int(_EQ_GAIN_MAX)}"}
    if half_h >= 4:
        db_labels[half_h // 2] = f"+{int(_EQ_GAIN_MAX / 2)}"
        db_labels[half_h + half_h // 2] = f"-{int(_EQ_GAIN_MAX / 2)}"

    for r in range(total_rows):
        cells = []
        for x in range(plot_w):
            ch, color = ' ', None
            if x in x_to_band:
                bc = _band_char(x_to_band[x], r)
                if bc:
                    ch = bc
                    color = C.ACCENT if x_to_band[x] == cursor else C.DIM
            if ch == ' ':
                if r == baseline:
                    ch, color = '─', C.DIM
                elif curve[x] is not None and round(baseline - curve[x] / db_per_row) == r:
                    ch, color = '·', C.DIM
            cells.append(f"{color}{ch}{C.RESET}" if color else ch)
        lbl = db_labels.get(r, "")
        out.append(f"{C.DIM}{lbl:>3}{C.RESET} " + "".join(cells))

    # Frequency labels + selection caret beneath the plot.
    axis = [' '] * plot_w
    caret = [' '] * plot_w
    for i, x in enumerate(band_x):
        lab = _eq_fmt_freq(bands[i][0])
        for k, c in enumerate(lab):
            xx = x - len(lab) // 2 + k
            if 0 <= xx < plot_w:
                axis[xx] = c
        if i == cursor and 0 <= x < plot_w:
            caret[x] = '▲'
    out.append("    " + f"{C.DIM}{''.join(axis)}{C.RESET}")
    out.append("    " + f"{C.ACCENT}{''.join(caret)}{C.RESET}")
    out.append("")
    out.append(f"  {status}")
    return out


def equaliser_edit(message: str = "Equalisation:", adjustments: list | None = None) -> list | None:
    """Interactive graphic equaliser for an EQU2 frame.

    Bands start from the standard ISO set merged with any existing custom
    frequencies. Returns a list of (frequency_hz, gain_db) for non-zero bands,
    or None if cancelled.
    """
    bands = [[float(f), float(g)] for f, g in (adjustments or [])]
    present = {round(f) for f, _ in bands}
    for f in _EQ_ISO_BANDS:
        if f not in present:
            bands.append([float(f), 0.0])
    bands.sort(key=lambda b: b[0])

    cursor = 0
    preset_idx = -1
    note = ""
    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)

    def _clamp(g: float) -> float:
        return max(-_EQ_GAIN_MAX, min(_EQ_GAIN_MAX, g))

    def _save() -> list:
        return [(float(f), round(g, 1)) for f, g in bands if abs(g) > 1e-9]

    def _render():
        nonlocal cursor
        n = len(bands)
        if n:
            cursor = max(0, min(cursor, n - 1))
            f, g = bands[cursor]
            status = f"{C.ACCENT}▸{C.RESET} {_eq_fmt_freq(f)} Hz   {C.BOLD}{g:+.1f} dB{C.RESET}"
            if note:
                status += f"   {C.DIM}· {note}{C.RESET}"
        else:
            status = f"{C.DIM}no bands — [a] add one{C.RESET}"
        lines = _eq_render_lines(bands, cursor, message, status, _cols(), _rows())
        lines.extend(_hint(
            ("↑↓", "gain"), ("←→", "band"), ("⇞⇟", "±3"), ("a", "add"),
            ("d", "del"), ("0", "zero"), ("f", "flat"), ("p", "preset"),
            ("↵", "save"), ("q", "quit"),
        ).splitlines())
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            n = len(bands)

            if key == 'ENTER':
                result = _save(); break
            elif key == 'ESC':
                result = None; break
            elif key in ('q', 'Q'):
                _state.QUIT_REQUESTED = True
                result = _save(); break
            elif key in ('LEFT', 'h') and n:
                cursor = (cursor - 1) % n; note = ""; _render()
            elif key in ('RIGHT', 'l') and n:
                cursor = (cursor + 1) % n; note = ""; _render()
            elif key in ('UP', 'k') and n:
                bands[cursor][1] = _clamp(bands[cursor][1] + _EQ_STEP); _render()
            elif key in ('DOWN', 'j') and n:
                bands[cursor][1] = _clamp(bands[cursor][1] - _EQ_STEP); _render()
            elif key == 'PGUP' and n:
                bands[cursor][1] = _clamp(bands[cursor][1] + _EQ_COARSE); _render()
            elif key == 'PGDN' and n:
                bands[cursor][1] = _clamp(bands[cursor][1] - _EQ_COARSE); _render()
            elif key == 'SCROLL_UP' and n:
                bands[cursor][1] = _clamp(bands[cursor][1] + _EQ_STEP); _render()
            elif key == 'SCROLL_DOWN' and n:
                bands[cursor][1] = _clamp(bands[cursor][1] - _EQ_STEP); _render()
            elif key == '0' and n:
                bands[cursor][1] = 0.0; _render()
            elif key in ('f', 'F'):
                for b in bands:
                    b[1] = 0.0
                note = "flattened"; _render()
            elif key in ('p', 'P'):
                preset_idx = (preset_idx + 1) % len(_EQ_PRESETS)
                name, gains = _EQ_PRESETS[preset_idx]
                bands[:] = [[float(f), float(gains.get(f, 0.0))] for f in _EQ_ISO_BANDS]
                note = f"preset: {name}"; _render()
            elif key in ('a', 'A'):
                _restore_term_attrs(fd, old)
                if not _IS_WINDOWS:
                    sys.stdout.write("\033[?1000l\033[?1006l")
                freq_str = text("Add band frequency (Hz):")
                _set_raw(fd)
                if not _IS_WINDOWS:
                    sys.stdout.write("\033[?1000h\033[?1006h")
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                if freq_str:
                    try:
                        f = float(freq_str.strip())
                        if f > 0 and round(f) not in {round(b[0]) for b in bands}:
                            bands.append([f, 0.0])
                            bands.sort(key=lambda b: b[0])
                            cursor = next(i for i, b in enumerate(bands) if round(b[0]) == round(f))
                            note = ""
                    except ValueError:
                        pass
                _render()
            elif key in ('d', 'D', 'BACKSPACE', 'DELETE') and n:
                bands.pop(cursor)
                cursor = min(cursor, len(bands) - 1) if bands else 0
                note = ""; _render()
            elif key.startswith('MOUSE_CLICK:') and n:
                parts = key.split(':')
                col = int(parts[3]) if len(parts) > 3 else 1
                plot_w = max(10, _cols() - 5)
                x = col - 6  # 4-col dB label + space, lines start at terminal col 1
                if 0 <= x < plot_w:
                    cursor = min(range(n), key=lambda i: abs(int((i + 0.5) * plot_w / n) - x))
                    note = ""; _render()

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def system_editor_edit(initial_text: str) -> str | None:
    """Open system editor for long text."""
    with tempfile.NamedTemporaryFile(suffix=".txt", mode='w+', encoding='utf-8', delete=False) as tf:
        tf.write(initial_text)
        temp_path = tf.name
    try:
        editor = os.environ.get('EDITOR', 'nano')
        subprocess.run([editor, temp_path], check=True)
        with open(temp_path, 'r', encoding='utf-8') as f:
            result = f.read().strip()
        return result if result else None
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
