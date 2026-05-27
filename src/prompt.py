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
import shutil
import math
import textwrap
import time
import select as _sel
from typing import Any

from src import ui_utils

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


# ── ANSI ──────────────────────────────────────────────────────────────────────

_HIDE  = "\033[?25l"
_SHOW  = "\033[?25h"
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_CYA   = "\033[1;36m"
_GRN   = "\033[1;32m"
_WHT   = "\033[1;37m"
_ACC   = "\033[1;31m"

def _clrline():        return "\033[2K\r"
def _goto(row, col=1): return f"\033[{row};{col}H"
def _col(n):           return f"\033[{n}G"

import shutil
import math
import re

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
        if not k: return f"{_DIM}{v}{_RESET}"
        return f"{_RESET}{_DIM}[{_RESET}{_BOLD}{k}{_RESET}{_DIM}] {v}{_RESET}"

    sep = f"{_DIM} ⋅ {_RESET}"
    raw_sep_len = 3 

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT 1: Centred Long Line
    # ──────────────────────────────────────────────────────────────────────────
    raw_len = sum(len(raw) for _, _, raw in parsed_items) + raw_sep_len * (total_items - 1)
    if raw_len <= cols:
        line = sep.join(render_inline(k, v) for k, v, _ in parsed_items)
        pad = max(0, cols - raw_len) // 2
        return (" " * pad) + line

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT 2: Upside-Down Pyramid
    # ──────────────────────────────────────────────────────────────────────────
    def get_pyramid_distribution(n):
        rows = []
        current_row_size = math.ceil(math.sqrt(2 * n))
        while n > 0:
            take = min(current_row_size, n)
            rows.append(take)
            n -= take
            current_row_size = max(1, current_row_size - 1)
        return rows

    import math
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

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT 3: Grid (Side-by-side uniform columns)
    # ──────────────────────────────────────────────────────────────────────────
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

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT 4: Aligned Vertical Stack
    # Center aligned: Keys right-aligned to spine, values left-aligned from spine
    # ──────────────────────────────────────────────────────────────────────────
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
                left_side = f"{k_space_pad}{_RESET}{_DIM}[{_RESET}{_BOLD}{k}{_RESET}{_DIM}]{_RESET}"
            else:
                left_side = " " * max_k_len
                
            right_side = f"{_DIM}{v}{_RESET}"
            stack_lines.append(f"{' ' * global_pad}{left_side} {right_side}")
        return "\n".join(stack_lines)

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT 5: Split Vertical Stack (Ultimate Narrow Fallback)
    # Key on row 1, value on row 2, dot separator between pairs.
    # ──────────────────────────────────────────────────────────────────────────
    split_lines = []
    for i, (k, v, _) in enumerate(parsed_items):
        if k:
            k_raw = f"[{k}]"
            k_pad = max(0, cols - len(k_raw)) // 2
            split_lines.append(f"{' ' * k_pad}{_RESET}{_DIM}[{_RESET}{_BOLD}{k}{_RESET}{_DIM}]{_RESET}")
            
        v_pad = max(0, cols - len(v)) // 2
        split_lines.append(f"{' ' * v_pad}{_DIM}{v}{_RESET}")
        
        # Add centered separator dot between discrete blocks
        if i < total_items - 1:
            dot_pad = max(0, cols - 1) // 2
            split_lines.append(f"{' ' * dot_pad}{_DIM}⋅{_RESET}")
            
    return "\n".join(split_lines)
    
def key_style_override(key_string: str) -> str:
    """
    Stylizes hotkeys gently without bold punchiness to decrease prominence.
    """
    # Keeps arrows/enters readable but thin and dim
    return f"\033[2m{key_string}\033[22m"

def _render_status_bar():
    """Renders the status bar at the absolute bottom of the terminal."""
    cols = ui_utils.get_terminal_width()
    rows = ui_utils.get_terminal_height()
    status = ui_utils.get_status_line()
    
    if status:
        sys.stdout.write(f"\033[{rows};1H\033[2K{status}")
        sys.stdout.flush()


# ── Choice ────────────────────────────────────────────────────────────────────

class Choice:
    __slots__ = ('title', 'value', 'checked')

    def __init__(self, title: str, value: object = None, checked: bool = False) -> None:
        """Initialize a Choice option."""
        self.title   = title
        self.value   = value if value is not None else title
        self.checked = checked


def _norm(choices: list) -> list:
    out = []
    for c in choices:
        if isinstance(c, Choice):
            out.append(c)
        elif isinstance(c, str):
            out.append(Choice(c, c))
        elif isinstance(c, dict):
            out.append(Choice(
                title   = c.get('name', c.get('title', str(c))),
                value   = c.get('value', c.get('name', str(c))),
                checked = c.get('checked', False),
            ))
        elif hasattr(c, 'title') and hasattr(c, 'value'):
            out.append(Choice(c.title, c.value, getattr(c, 'checked', False)))
        else:
            s = str(c)
            out.append(Choice(s, s))
    return out


# ── Key reader ────────────────────────────────────────────────────────────────

def _read_key(fd: int) -> str:
    """Read a single key press from file descriptor."""
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
                if seq.isdigit():
                    os.read(fd, 4)
                    return 'ESC'
                return {
                    'A': 'UP', 'B': 'DOWN', 'C': 'RIGHT', 'D': 'LEFT',
                    'H': 'HOME', 'F': 'END',
                    '5': 'PGUP', '6': 'PGDN',
                }.get(seq, 'ESC')
            return 'ESC'
        except Exception:
            return 'ESC'
    decoded = ch.decode('utf-8', errors='replace')
    if decoded in ('\r', '\n'): return 'ENTER'
    if decoded in ('\x7f', '\x08'): return 'BACKSPACE'
    if decoded == ' ':    return 'SPACE'
    if decoded == '\x03': return 'CTRL_C'
    if decoded == '\t':   return 'TAB'
    return decoded


# ── Cursor row query ──────────────────────────────────────────────────────────

def _query_cursor_row(fd: int) -> int:
    """
    Query the terminal for the current cursor row via ANSI DSR (ESC[6n).
    Returns the row number (1-based), or 1 on failure.
    """
    if _IS_WINDOWS:
        return 1

    import select as _sel
    sys.stdout.write("\033[6n")
    sys.stdout.flush()
    buf = b""
    while True:
        r, _, _ = _sel.select([sys.stdin], [], [], 0.15)
        if not r:
            break
        b = os.read(fd, 1)
        buf += b
        if b == b'R':
            break
    try:
        # Response format: \033[row;colR
        inner = buf.decode('utf-8', errors='replace').lstrip('\033[').rstrip('R')
        row, _ = inner.split(';')
        return int(row)
    except Exception:
        return 1


# ── Viewport helpers ──────────────────────────────────────────────────────────

def _visible_rows() -> int:
    """Get number of visible rows in terminal."""
    _, rows = ui_utils.get_terminal_size()
    return max(4, rows - 6)


def _cols() -> int:
    """Get number of columns in terminal."""
    return ui_utils.get_terminal_width()


def _wrap_bordered_input_lines(text: str, content_width: int) -> list[str]:
    """Wrap input text for a bordered prompt field."""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        if raw_line == "":
            lines.append("")
        else:
            wrapped = textwrap.wrap(raw_line, width=content_width, drop_whitespace=False) or [""]
            lines.extend(wrapped)
    return lines


# ── Widget: anchored block renderer ──────────────────────────────────────────

class _Widget:
    """
    Renders a list of lines anchored to an absolute terminal row.

    On first draw it queries the current cursor row and uses that as the
    anchor. On resize it clears the entire screen and redraws from scratch —
    this is the only reliable way to prevent ghost lines when the terminal
    reflows content and changes the effective cursor position.
    """

    def __init__(self, fd: int) -> None:
        """Initialize the widget with a file descriptor."""
        self.fd      = fd
        self.row     = None   # anchor row, 1-based
        self.last_h  = 0
        self._full   = False  # whether we own the full screen

    def anchor_reset(self) -> None:
        """Called on resize — triggers a full-screen redraw next render."""
        self.row   = None
        self._full = True

    def render(self, lines: list) -> None:
        """Render lines at the anchored row, or full-screen clear + redraw on resize."""
        if self._full or self.row is None:
            # Full clear prevents any ghost lines from a previous render.
            sys.stdout.write("\033[2J\033[H" + _HIDE)
            self.row   = 1
            self._full = False
            out = ""
        else:
            out = _HIDE + _goto(self.row)

        for line in lines:
            out += _clrline() + line + "\n"
        # Erase leftover lines from a previous taller render.
        for _ in range(max(0, self.last_h - len(lines))):
            out += _clrline() + "\n"
        self.last_h = len(lines)
        sys.stdout.write(out)
        sys.stdout.flush()

    def clear(self) -> None:
        """Clear the rendered content from the terminal."""
        if self.row is None:
            sys.stdout.write(_SHOW)
            sys.stdout.flush()
            return
        out = _goto(self.row)
        for _ in range(self.last_h + 1):
            out += _clrline() + "\n"
        out += _goto(self.row) + _SHOW
        sys.stdout.write(out)
        sys.stdout.flush()
        self.last_h = 0


# ── select() ─────────────────────────────────────────────────────────────────

def select(message: str, choices: list,
           header: list | None = None,
           extra_hints: dict[str, str] | None = None) -> str | None:
    """Arrow keys / jk navigate, Enter selects, q / Ctrl-C → None.

    Args:
        message: Prompt label shown above the list.
        choices: Items to choose from (str, dict, or Choice).
        header:  Optional list of plain strings rendered above the prompt.
        extra_hints: Optional dict of custom key-action bindings to merge
                     (e.g., {'space': 'toggle', 'a': 'all'}).
    """
    items = _norm(choices)
    if not items:
        return None

    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)

    # Base keyboard mapping from your navigation options loop
    base_hints = {
        "↑↓": "move",
        "q": "quit",
        "↵": "confirm"
    }
    
    # Merge context-specific hints if provided by another menu layout
    if extra_hints:
        combined_hints = {**extra_hints, **base_hints}
    else:
        combined_hints = base_hints

    # Converts our flat dict maps into sequential pairs based on the active view
    def get_hints_for_tier(tier: int) -> list[tuple[str, str]]:
        # Wide screens (Tiers 1, 2, 3): Full labels
        if tier <= 3:
            return list(combined_hints.items())
        
        # Aligned Stack (Tier 4): Truncate compound keys to conserve width bounds
        elif tier == 4:
            tier4_hints = {}
            for k, v in combined_hints.items():
                k_short = k.split("/")[0]  # "↑↓/jk" -> "↑↓"
                tier4_hints[k_short] = v
            return list(tier4_hints.items())
            
        # Split Stack (Tier 5): Ultra narrow essential fallback
        else:
            return [("↑↓", "move"), ("q", "quit"), ("↵", "confirm")]

    last_tier = 1

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport, last_tier
        cols    = _cols()
        h_lines = _header_lines()
        
        import re
        max_header_w = 0
        for hl in h_lines:
            plain_hl = re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', hl)
            plain_hl = re.sub(r'[╭─│╰╮╯┌┐└┘├┤┬┴┼═║╔╗╚╝]', '', plain_hl).strip()
            if len(plain_hl) > max_header_w:
                max_header_w = len(plain_hl)

        layout_constraint = " " * max_header_w if (0 < max_header_w < cols - 20) else ""

        # Fetch pairs configured exactly for the current tier expectation
        active_pairs = get_hints_for_tier(last_tier)
        hint_res = _hint(*active_pairs, extra=layout_constraint)
        
        # Safe unpack handler to protect against internal layout engine API changes
        if isinstance(hint_res, tuple):
            hint_raw, tier = hint_res
        else:
            hint_raw, tier = hint_res, 1
        
        # If a tier boundary switch is encountered, shift text lists instantly
        if tier != last_tier:
            last_tier = tier
            active_pairs = get_hints_for_tier(tier)
            hint_res = _hint(*active_pairs, extra=layout_constraint)
            hint_raw = hint_res[0] if isinstance(hint_res, tuple) else hint_res

        hint_lines = hint_raw.split("\n") if hint_raw else []
        
        fixed_overhead = len(h_lines) + len(hint_lines) + 2
        vis     = max(2, _visible_rows() - fixed_overhead)
        
        n       = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1

        out = h_lines[:]
        out.append(f"  {_DIM}{message}{_RESET}")
        out.append(f"  {_DIM}╵ {viewport} above{_RESET}" if viewport > 0 else "")

        for i in range(viewport, min(viewport + vis, n)):
            label = str(items[i].title)
            max_w = cols - 6
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            if i == cursor:
                out.append(f"  {_ACC}›{_RESET} {_WHT}{_BOLD}{label}{_RESET}")
            else:
                out.append(f"    {_DIM}{label}{_RESET}")

        remaining = n - viewport - vis
        out.append(f"  {_DIM}╷ {remaining} below{_RESET}" if remaining > 0 else "")
        out.extend(hint_lines)
        return out

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            if   key == 'CTRL_C':                break
            elif key in ('UP',   'k'):           cursor = (cursor - 1) % len(items); w.render(_lines())
            elif key in ('DOWN', 'j'):           cursor = (cursor + 1) % len(items); w.render(_lines())
            elif key == 'HOME':                  cursor = 0;                          w.render(_lines())
            elif key == 'END':                   cursor = len(items) - 1;             w.render(_lines())
            elif key == 'PGUP':                  cursor = max(0, cursor - _visible_rows()); w.render(_lines())
            elif key == 'PGDN':                  cursor = min(len(items) - 1, cursor + _visible_rows()); w.render(_lines())
            elif key == 'ENTER':                 result = items[cursor].value; break
            elif key.lower() == 'q':             break

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result

# ── checkbox() ────────────────────────────────────────────────────────────────

def checkbox(message: str, choices: list,
             header: list | None = None) -> list | None:
    """Space toggles, a all, Enter confirms, q / Ctrl-C → None.

    Args:
        message: Prompt label shown above the list.
        choices: Items to choose from (str, dict, or Choice).
        header:  Optional list of plain strings rendered above the prompt.
    """
    items    = _norm(choices)
    if not items:
        return None

    checked  = [c.checked for c in items]
    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)

    # Base keyboard mapping specific to checkbox navigation
    base_hints = {
        "↑↓": "move",
        "space": "toggle",
        "a": "all",
        "q": "quit",
        "↵": "confirm"
    }

    # Converts flat dict maps into sequential pairs based on the active view
    def get_hints_for_tier(tier: int) -> list[tuple[str, str]]:
        # Wide screens (Tiers 1, 2, 3): Full labels
        if tier <= 3:
            return list(base_hints.items())
        
        # Aligned Stack (Tier 4): Truncate compound keys to conserve width bounds
        elif tier == 4:
            tier4_hints = {}
            for k, v in base_hints.items():
                k_short = k.split("/")[0]  # "↑↓/jk" -> "↑↓"
                tier4_hints[k_short] = v
            return list(tier4_hints.items())
            
        # Split Stack (Tier 5): Ultra narrow essential fallback
        else:
            return [("↑↓", "move"), ("space", "toggle"), ("↵", "confirm")]

    last_tier = 1

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport, last_tier
        cols    = _cols()
        h_lines = _header_lines()
        
        import re
        max_header_w = 0
        for hl in h_lines:
            plain_hl = re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', hl)
            plain_hl = re.sub(r'[╭─│╰╮╯┌┐└┘├┤┬┴┼═║╔╗╚╝]', '', plain_hl).strip()
            if len(plain_hl) > max_header_w:
                max_header_w = len(plain_hl)

        layout_constraint = " " * max_header_w if (0 < max_header_w < cols - 20) else ""

        # Fetch pairs configured exactly for the current tier expectation
        active_pairs = get_hints_for_tier(last_tier)
        hint_res = _hint(*active_pairs, extra=layout_constraint)
        
        # Safe unpack handler to protect against engine variations
        if isinstance(hint_res, tuple):
            hint_raw, tier = hint_res
        else:
            hint_raw, tier = hint_res, 1
        
        # If a tier boundary switch is encountered, shift text lists instantly
        if tier != last_tier:
            last_tier = tier
            active_pairs = get_hints_for_tier(tier)
            hint_res = _hint(*active_pairs, extra=layout_constraint)
            hint_raw = hint_res[0] if isinstance(hint_res, tuple) else hint_res

        hint_lines = hint_raw.split("\n") if hint_raw else []
        
        # Calculate accurate viewport space factoring in multi-line dynamic hints
        fixed_overhead = len(h_lines) + len(hint_lines) + 2
        vis     = max(2, _visible_rows() - fixed_overhead)
        
        n       = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1

        n_checked = sum(checked)
        out = h_lines[:]

        checked_str = f"  {_DIM}({n_checked} selected){_RESET}" if n_checked else ""
        out.append(f"  {_DIM}{message}{_RESET}{checked_str}")
        out.append(f"  {_DIM}╵ {viewport} above{_RESET}" if viewport > 0 else "")

        for i in range(viewport, min(viewport + vis, n)):
            label = str(items[i].title)
            max_w = cols - 10
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            tick         = f"{_GRN}✓{_RESET}" if checked[i] else f"{_DIM}·{_RESET}"
            cursor_glyph = f"{_ACC}›{_RESET}" if i == cursor else " "
            label_fmt    = f"{_WHT}{_BOLD}{label}{_RESET}" if i == cursor else (label if checked[i] else f"{_DIM}{label}{_RESET}")
            out.append(f"  {cursor_glyph} {tick}  {label_fmt}")

        remaining = n - viewport - vis
        out.append(f"  {_DIM}╷ {remaining} below{_RESET}" if remaining > 0 else "")
        
        # Extend the dynamically calculated layout hints safely
        out.extend(hint_lines)
        return out

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                # Clear terminal window real estate cleanly during tier adjustments
                sys.stdout.write("\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            if   key == 'CTRL_C':      break
            elif key in ('UP',   'k'): cursor = (cursor - 1) % len(items); w.render(_lines())
            elif key in ('DOWN', 'j'): cursor = (cursor + 1) % len(items); w.render(_lines())
            elif key == 'SPACE':       checked[cursor] = not checked[cursor]; w.render(_lines())
            elif key in ('a', 'A'):
                all_on = all(checked)
                checked[:] = [not all_on] * len(items)
                w.render(_lines())
            elif key == 'ENTER':       result = [items[i].value for i, c in enumerate(checked) if c]; break
            elif key.lower() == 'q':   break

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result

# ── confirm() ─────────────────────────────────────────────────────────────────

def confirm(message: str, default: bool = False) -> bool:
    y = "Y" if default else "y"
    n = "N" if not default else "n"
    hint = f"{_DIM}[{_RESET}{_BOLD}{y}{_RESET}{_DIM}/{_RESET}{_BOLD}{n}{_RESET}{_DIM}]{_RESET}"
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    result = default
    try:
        _set_raw(fd)
        sys.stdout.write(_HIDE + f"  {_DIM}{message}{_RESET}  {hint} ")
        sys.stdout.flush()
        while True:
            key = _read_key(fd)
            if   key == 'CTRL_C':    result = False; break
            elif key == 'ENTER':     result = default; break
            elif key.lower() == 'y': result = True;  break
            elif key.lower() == 'n': result = False; break
    finally:
        _restore_term_attrs(fd, old)
        sys.stdout.write(_SHOW + "\n")
        sys.stdout.flush()
    return result


# ── text() ────────────────────────────────────────────────────────────────────

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
        cols = _cols() # Uses the existing helper in prompt.py
        content = "".join(buf)
        content_width = max(1, cols - 6)

        wrapped_lines = _wrap_bordered_input_lines(content, content_width)
        pre_lines = _wrap_bordered_input_lines(content[:pos], content_width)
        cursor_row = max(0, len(pre_lines) - 1)
        cursor_col = len(pre_lines[-1]) if pre_lines else 0
        total_rows = len(wrapped_lines)

        # 2. Draw
        if prev_lines > 0:
            sys.stdout.write(f"\r\033[{prev_lines}A")
        sys.stdout.write(f"\r\033[J{_HIDE}")

        # Label — dim, consistent with select/confirm
        sys.stdout.write(f"\r  {_DIM}{message}{_RESET}\r\n")

        # Input field — subtle left/right border glyphs to signal editable area
        for i, line in enumerate(wrapped_lines):
            sys.stdout.write(f"\r  {_DIM}│{_RESET} {line:<{content_width}} {_DIM}│{_RESET}")
            if i < total_rows - 1:
                sys.stdout.write("\r\n")

        # Cursor positioning
        rows_to_move_up = (total_rows - 1) - cursor_row
        if rows_to_move_up > 0:
            sys.stdout.write(f"\033[{rows_to_move_up}A")
        col_offset = cursor_col + 4  # 2 spaces + "│ "
        if col_offset > 0:
            sys.stdout.write(f"\r\033[{col_offset}C")
        else:
            sys.stdout.write("\r")

        sys.stdout.write(_SHOW)
        sys.stdout.flush()

        prev_lines = 1 + total_rows
        _render_status_bar()

    try:
        _set_raw(fd)
        _render()
        while True:
            if ui_utils.consume_resize(): _render()
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
        # Clean exit: ensure the terminal prompt starts below your text
        sys.stdout.write("\r\n" * 2) 
        sys.stdout.flush()
        
    return result


# ── path() ────────────────────────────────────────────────────────────────────

def path(message: str, default: str = "") -> str | None:
    buf          = list(default)
    pos          = len(buf)
    fd           = sys.stdin.fileno()
    old          = _get_term_attrs(fd)
    result       = None
    _tab_matches : list = []
    _tab_index   = 0

    def _completions(current: str) -> list:
        try:
            expanded = os.path.expanduser(current)
            base     = expanded if os.path.isdir(expanded) else os.path.dirname(expanded) or "."
            stub     = "" if os.path.isdir(expanded) else os.path.basename(expanded)
            return sorted(
                os.path.join(base, e)
                for e in os.listdir(base)
                if e.startswith(stub) and not e.startswith('.')
            )
        except Exception:
            return []

    def _render():
        cols    = _cols()
        content = "".join(buf)
        # Reserve: 2 indent + "│ " (2) + right border and padding (2)
        prefix  = "  │ "
        max_w   = max(1, cols - 6)
        if pos > max_w:
            display  = content[pos - max_w: pos]
            disp_pos = max_w
        else:
            display  = content[:max_w]
            disp_pos = pos
        # Dim the path to distinguish from the label
        sys.stdout.write(
            _HIDE + _clrline() +
            f"  {_DIM}{message}{_RESET}\r\n  {_DIM}│{_RESET} {display:<{max_w}} {_DIM}│{_RESET}" +
            _col(len(prefix) + disp_pos + 1) + _SHOW
        )
        sys.stdout.flush()
        _render_status_bar()

    try:
        _set_raw(fd)
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
                current = "".join(buf)
                if not _tab_matches:
                    _tab_matches[:] = _completions(current)
                    _tab_index = 0
                if _tab_matches:
                    completed = _tab_matches[_tab_index % len(_tab_matches)]
                    if os.path.isdir(completed): completed += "/"
                    buf[:] = list(completed); pos = len(buf)
                    _tab_index += 1; _render()
                continue
            elif key == 'BACKSPACE' and pos > 0:
                _tab_matches.clear(); buf.pop(pos - 1); pos -= 1; _render()
            elif key == 'SPACE':
                _tab_matches.clear(); buf.insert(pos, ' '); pos += 1; _render()
            elif key == 'LEFT'  and pos > 0:        pos -= 1; _render()
            elif key == 'RIGHT' and pos < len(buf): pos += 1; _render()
            elif key == 'HOME':                     pos = 0;          _render()
            elif key == 'END':                      pos = len(buf);   _render()
            elif len(key) == 1 and key.isprintable():
                _tab_matches.clear(); buf.insert(pos, key); pos += 1; _render()
    finally:
        _restore_term_attrs(fd, old)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return result