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
import tempfile
import textwrap
import time
import select as _sel
import subprocess
from typing import Any, Callable

from src.utils import ui_utils
C = ui_utils.Colors

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
                left_side = f"{k_space_pad}{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}]{C.RESET}"
            else:
                left_side = " " * max_k_len
                
            right_side = f"{C.DIM}{v}{C.RESET}"
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
            split_lines.append(f"{' ' * k_pad}{C.RESET}{C.DIM}[{C.RESET}{C.BOLD}{k}{C.RESET}{C.DIM}]{C.RESET}")
            
        v_pad = max(0, cols - len(v)) // 2
        split_lines.append(f"{' ' * v_pad}{C.DIM}{v}{C.RESET}")
        
        # Add centered separator dot between discrete blocks
        if i < total_items - 1:
            dot_pad = max(0, cols - 1) // 2
            split_lines.append(f"{' ' * dot_pad}{C.DIM}⋅{C.RESET}")
            
    return "\n".join(split_lines)

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
            sys.stdout.write("\033[2J\033[H" + C.HIDE)
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
        sys.stdout.write(out)
        sys.stdout.flush()

    def clear(self) -> None:
        """Clear the rendered content from the terminal."""
        if self.row is None:
            sys.stdout.write(C.SHOW)
            sys.stdout.flush()
            return
        out = _goto(self.row)
        for _ in range(self.last_h + 1):
            out += _clrline() + "\n"
        out += _goto(self.row) + C.SHOW
        sys.stdout.write(out)
        sys.stdout.flush()
        self.last_h = 0


# ── select() ─────────────────────────────────────────────────────────────────

def select(message: str, choices: list,
           header: list | None | Callable[[], list[str]] = None,
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
        out.append(f"  {C.DIM}{message}{C.RESET}")
        out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}" if viewport > 0 else "")

        for i in range(viewport, min(viewport + vis, n)):
            label = str(items[i].title)
            max_w = cols - 6
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            if i == cursor:
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
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[2J\033[H")
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
            elif key.lower() == 'q':             result = None; break

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result

# ── checkbox() ────────────────────────────────────────────────────────────────

# CONSOLIDATED CHECKBOX FIX
# Replace the checkbox() function (lines 556-848) with this

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
    items = _norm(choices)
    if not items:
        return []
 
    raw_items = [{'obj': item, 'dimmed': False} for item in items]
    index = 0
    locked_category = None
 
    def update_interlock_states(structured_list: list):
        """Lock category on first check, dim incompatible alternatives."""
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
        """Visible length ignoring ANSI codes."""
        return len(re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', s))
 
    def _next_index(current: int, structured_list: list, direction: int) -> int:
        """Skip dimmed items."""
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
    sys.stdout.write(C.HIDE)  # Hide cursor
    sys.stdout.flush()
    
    w = _Widget(fd)
 
    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)
 
    def _lines() -> list[str]:
        cols = _cols()
        out = _header_lines()
        out.append(f"  {C.DIM}{message}{C.RESET}")
        
        # Build structured items and calculate column widths
        structured_items = []
        max_label_w = 0
        max_type_w = 0
        max_frac_w = 0
        
        for item in raw_items:
            title = str(item['obj'].title)
            fraction_part = ""
            value_part = ""
            
            # Parse fraction (e.g., "27/27" at end)
            frac_match = re.search(r"(\d+/\d+)\s*$", title)
            if frac_match:
                fraction_part = frac_match.group(1)
                title = title[:frac_match.start()].rstrip()
 
            # Split on vertical bar
            if re.search(r"\s*\|\s*", title):
                left_side, right_side = re.split(r"\s*\|\s*", title, maxsplit=1)
                value_part = right_side.strip()
                title = left_side.rstrip()
            
            # Extract category type [bracket] or trailing words
            type_tag = ""
            type_match = re.search(r"(?:\[([^\]]+)\]|([a-zA-Z\s\d]+))\s*$", title)
            if type_match:
                raw_type = type_match.group(1) or type_match.group(2)
                type_tag = raw_type.strip().lower()
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
 
        # Apply interlock filtering
        update_interlock_states(structured_items)
        for i, s_item in enumerate(structured_items):
            raw_items[i]['dimmed'] = s_item['dimmed']
 
        # Render rows
        for idx, item in enumerate(structured_items):
            is_current = (idx == index)
            
            # Build components with proper color handling
            if item['dimmed']:
                # Dimmed row
                state_glyph = f"{C.DIM}•{C.RESET}"
                label_str = f"{C.DIM}{item['label']}{C.RESET}"
                type_str = f"{C.DIM}{item['type']}{C.RESET}" if item['type'] else ""
                value_str = f"{C.DIM}{item['value']}{C.RESET}" if item['value'] else ""
                pointer = " "
            elif is_current:
                # Current row (highlighted)
                state_glyph = f"{C.GREEN}✔{C.RESET}" if item['obj'].checked else f"{C.DIM}•{C.RESET}"
                label_str = f"{C.PRIMARY}{C.BOLD}{item['label']}{C.RESET}"
                type_str = f"{C.DIM}{item['type']}{C.RESET}" if item['type'] else ""
                value_str = f"{C.PRIMARY}{C.BOLD}{item['value']}{C.RESET}" if item['value'] else ""
                pointer = "›"
            else:
                # Normal row
                state_glyph = f"{C.GREEN}✔{C.RESET}" if item['obj'].checked else f"{C.DIM}•{C.RESET}"
                label_str = item['label']
                type_str = f"{C.DIM}{item['type']}{C.RESET}" if item['type'] else ""
                value_str = item['value'] if item['value'] else ""
                pointer = " "
            
            # Layout: pointer + glyph + label + pad + type + pad + value + pad + fraction
            pad_label = " " * (max_label_w - len(item['label']) + 2)
            pad_type = " " * (max_type_w - len(item['type']) + 2) if max_type_w else "  "
            
            frac_str = item['fraction']
            if frac_str:
                pad_frac = " " * (max_frac_w - len(frac_str))
                if is_current:
                    frac_str = f"{pad_frac}{C.PRIMARY}{frac_str}{C.RESET}"
                else:
                    frac_str = f"{pad_frac}{C.DIM}{frac_str}{C.RESET}"
            
            # Build line
            left_part = f"  {pointer} {state_glyph}  {label_str}{pad_label}{type_str}{pad_type}"
            if value_str:
                sep = f"{C.DIM}|{C.RESET} " if is_current or not item['dimmed'] else "| "
                left_part += f"{sep}{value_str}"
            
            # Calculate space for fraction
            left_visible = _ansi_len(left_part)
            frac_visible = _ansi_len(frac_str) if frac_str else 0
            space_available = cols - left_visible - frac_visible - 2
            
            if space_available > 0:
                line = left_part + (" " * space_available) + frac_str
            else:
                # Truncate left side if needed
                max_left = cols - frac_visible - 5
                truncated = left_part[:max_left] + "…"
                line = truncated + (" " * (cols - _ansi_len(truncated) - frac_visible)) + frac_str
            
            out.append(line)
        
        out.append("")
        hint_str = _hint(
            ("↑↓", "move"), 
            ("space", "toggle"), 
            ("q", "quit"), 
            ("↵", "confirm")
        )
        if isinstance(hint_str, tuple):
            hint_str = hint_str[0]
        out.extend(hint_str.split("\n") if hint_str else [])
        
        return out
 
    result = None
    try:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        w.render(_lines())
 
        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
                w.anchor_reset()
                w.render(_lines())
                continue
 
            if not _wait_for_keypress(0.05):
                continue
 
            key = _read_key(fd)
            current_lines = _lines()
            structured = [s for line in [_lines()] for s in current_lines 
                         if hasattr(s, 'obj')]  # Get current structured items
            
            # Rebuild structured for navigation
            structured_items = []
            for item in raw_items:
                title = str(item['obj'].title)
                type_tag = ""
                value_part = ""
                if re.search(r"\s*\|\s*", title):
                    left, right = re.split(r"\s*\|\s*", title, maxsplit=1)
                    value_part = right.strip()
                    title = left.rstrip()
                type_match = re.search(r"(?:\[([^\]]+)\]|([a-zA-Z\s\d]+))\s*$", title)
                if type_match:
                    type_tag = (type_match.group(1) or type_match.group(2)).strip().lower()
                    title = title[:type_match.start()].rstrip()
                structured_items.append({'obj': item['obj'], 'dimmed': item['dimmed']})
            
            if key == 'CTRL_C':
                break
            elif key in ('UP', 'k'):
                index = _next_index(index, raw_items, -1)
                w.render(_lines())
            elif key in ('DOWN', 'j'):
                index = _next_index(index, raw_items, 1)
                w.render(_lines())
            elif key == 'SPACE':
                target = structured_items[index] if index < len(structured_items) else None
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
            elif key.lower() == 'q':
                break
                
    finally:
        _restore_term_attrs(fd, old)
        sys.stdout.write(C.SHOW)  # Show cursor before exit
        sys.stdout.flush()
        w.clear()
 
    return result

# ── confirm() ─────────────────────────────────────────────────────────────────

def confirm(message: str, default: bool = False) -> bool:
    y = "Y" if default else "y"
    n = "N" if not default else "n"
    hint = f"{C.DIM}[{C.RESET}{C.BOLD}{y}{C.RESET}{C.DIM}/{C.RESET}{C.BOLD}{n}{C.RESET}{C.DIM}]{C.RESET}"
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    result = default
    try:
        _set_raw(fd)
        sys.stdout.write(C.HIDE + f"  {C.DIM}{message}{C.RESET}  {hint} ")
        sys.stdout.flush()
        while True:
            key = _read_key(fd)
            if   key == 'CTRL_C':    result = False; break
            elif key == 'ENTER':     result = default; break
            elif key.lower() == 'y': result = True;  break
            elif key.lower() == 'n': result = False; break
    finally:
        _restore_term_attrs(fd, old)
        sys.stdout.write(C.SHOW + "\n")
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
        sys.stdout.write(f"\r\033[J{C.HIDE}")

        # Label — dim, consistent with select/confirm
        sys.stdout.write(f"\r  {C.DIM}{message}{C.RESET}\r\n")

        # Input field — subtle left/right border glyphs to signal editable area
        for i, line in enumerate(wrapped_lines):
            sys.stdout.write(f"\r  {C.DIM}│{C.RESET} {line:<{content_width}} {C.DIM}│{C.RESET}")
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

        sys.stdout.write(C.SHOW)
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
        except Exception:
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

        # Calculate the precise horizontal terminal column index where the blinking cursor resides
        cursor_col = len(prefix) + disp_pos

        # 1. Rewind the vertical cursor position up to the initial prompt line boundary
        clear_code = ""
        if _last_rendered_lines > 1:
            clear_code += f"\033[{_last_rendered_lines - 1}A"
        clear_code += "\r"

        # Initialize stream with the primary input boxes
        render_stream = [
            clear_code,
            f"\033[K  {C.DIM}{message}{C.RESET}\r\n",
            f"\033[K  {C.DIM}│{C.RESET} {display:<{max_w}} {C.DIM}│{C.RESET}"
        ]
        
        lines_count = 2

        # ── COMPONENT SEPARATION (Inspired by Questionary) ──
        # Do not output autocomplete options when at a subdirectory juncture or when empty
        should_show_hints = content and not content.endswith('/') and not content.endswith(os.path.sep)

        # Real-time synchronization layer: ensure entries match the current stub prefix
        visible_matches = []
        if should_show_hints and _tab_matches:
            stub = os.path.basename(content)
            for m in _tab_matches:
                name = os.path.basename(m.rstrip('/'))
                if name.startswith(stub):
                    visible_matches.append(m)

        # 2. Draw the Hovering Tooltip box immediately underneath the input caret location
        if visible_matches:
            render_stream.append("\r\n\033[K")
            lines_count += 1
            
            # Pad spaces horizontally to position suggestions neatly under the active segment word
            start_pad = min(cursor_col, max(0, cols - 35))
            render_stream.append(" " * start_pad)
            
            tooltip_parts = []
            current_len = start_pad
            
            # Cap the rendering pool to a tight top-5 slice to prevent clutter
            for idx, match in enumerate(visible_matches[:5]):
                name = os.path.basename(match.rstrip('/'))
                if os.path.isdir(match):
                    name += "/"
                
                # Highlight active completion item when rotating choices via TAB key events
                if idx == (_tab_index % len(visible_matches)):
                    item_str = f"{C.INVERT}{C.BOLD}{name}{C.RESET}"
                    visible_len = len(name)
                else:
                    item_str = f"{C.DIM}{name}{C.RESET}"
                    visible_len = len(name)
                
                # Enforce column wrap barriers gracefully
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

        # Update frame footprints for the rollback execution next loop step
        _last_rendered_lines = lines_count

        # Position the caret back on the string index tracking point safely
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
                
                # Derive matching sub-arrays
                visible_matches = [m for m in _tab_matches if os.path.basename(m.rstrip('/')).startswith(stub)] if stub else _tab_matches
                
                if visible_matches:
                    completed = visible_matches[_tab_index % len(visible_matches)]
                    if os.path.isdir(completed) and not completed.endswith("/"): 
                        completed += "/"
                    
                    # Update buffer variables natively
                    buf[:] = list(completed)
                    pos = len(buf)
                    _tab_index += 1
                    
                    # Pre-calculate adjacent children files lookup listings
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
                
                # Regenerate matching directories listings instantly on every keystroke
                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()
                
    finally:
        # Erase visible trailing residues down before dropping completely out to main screens
        clear_down = f"\033[{_last_rendered_lines - 2}B\n" if _last_rendered_lines > 2 else "\n"
        _restore_term_attrs(fd, old)
        sys.stdout.write(clear_down)
        sys.stdout.flush()
        
    return result

# ── list_edit() ────────────────────────────────────────────────────────────────────

def _render_list_edit_cell(text: str, width: int, is_editing: bool, is_active_col: bool, edit_buf: list[str], edit_pos: int) -> str:
    """Renders a single table cell, handling text truncation and cursor rendering."""
    if not is_editing or not is_active_col:
        return ui_utils.truncate_text(text, width)
        
    # We are actively editing this specific cell
    buf_str = "".join(edit_buf)
    
    # Render terminal block cursor
    if edit_pos >= len(buf_str):
        display_str = buf_str + f"{C.BACK}█{C.RESET}"
    else:
        display_str = buf_str[:edit_pos] + f"{C.INVERT}{C.BOLD}{buf_str[edit_pos]}{C.RESET}" + buf_str[edit_pos+1:]
        
    # Pad with spaces to maintain column width visually (accounting for ANSI codes)
    visible_len = len(buf_str) + (1 if edit_pos >= len(buf_str) else 0)
    padding = max(0, width - visible_len)
    return display_str + (" " * padding)


def _build_list_edit_lines(
    message: str, items: list, headers: tuple[str, ...],
    cursor: int, viewport: int,
    edit_mode: bool, edit_col: int, edit_buf: list[str], edit_pos: int
) -> tuple[list[str], int]:
    """Builds the UI lines for the list_edit widget and calculates the new viewport bounds."""
    num_cols = len(headers)
    cols = _cols()
    c = cols - 4
    inner = c
    out = []

    base_hints = {"↑↓": "move", "a": "add", "e": "edit", "d": "delete", "↵": "save", "q": "quit"}
    edit_hints = {"tab": "next col", "esc": "cancel", "↵": "apply"}

    # 1. Header and top border
    out.append(f"  {C.DIM}{message}{C.RESET}")
    out.append(f"  {C.DIM}{'─' * c}{C.RESET}")

    # 2. Dynamic Column Sizing
    avail_w = max(10, inner - 4 - (2 * (num_cols - 1)))
    col_w = avail_w // num_cols
    last_w = avail_w - (col_w * (num_cols - 1))

    # 3. Dynamic Column Headers
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

    # 4. Viewport calculation
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

    # 5. Render Items
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
                # 1-Column Formatting
                val_str = str(item)
                cell_str = _render_list_edit_cell(val_str, inner - 4, row_is_editing, True, edit_buf, edit_pos)
                if is_sel and not edit_mode:
                    out.append(f"  {cursor_glyph} {C.PRIMARY}{C.BOLD}{cell_str}{C.RESET}")
                else:
                    out.append(f"  {cursor_glyph} {cell_str}")

    # 6. Bottom border and hints
    out.append(f"  {C.DIM}{'─' * c}{C.RESET}")
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

    # --- EDIT STATE ---
    edit_mode = False
    edit_col  = 0
    edit_buf  = []
    edit_pos  = 0
    edit_backup = None

    def _render():
        """Passes current state to the renderer and updates the terminal."""
        nonlocal viewport
        lines, new_viewport = _build_list_edit_lines(
            message, items, headers,
            cursor, viewport,
            edit_mode, edit_col, edit_buf, edit_pos
        )
        viewport = new_viewport
        w.render(lines)

    def _commit_edit_buffer():
        """Saves the current edit buffer back into the items array."""
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
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            
            # ==========================================
            # MODE: EDITING
            # ==========================================
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

            # ==========================================
            # MODE: NAVIGATION
            # ==========================================
            else:
                if key == 'CTRL_C':
                    break
                elif key in ('UP', 'k'):
                    if items: cursor = (cursor - 1) % len(items)
                    _render()
                elif key in ('DOWN', 'j'):
                    if items: cursor = (cursor + 1) % len(items)
                    _render()
                    
                # --- IN-PLACE ADD ---
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
                    
                # --- IN-PLACE EDIT ---
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
                    
                # --- DELETE ---
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
                elif key.lower() == 'q':
                    ui_utils.clear_screen()
                    result = items if confirm("Discard changes?", default=False) else initial_items
                    break

    finally:
        _restore_term_attrs(fd, old)
        w.clear()

    return result



# ── calendar_select() ────────────────────────────────────────────────────────────────────

def _is_leap_year(year: int) -> bool:
    """Check if a year is a leap year (Gregorian calendar)."""
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    """Get the number of days in a given month."""
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    elif month in (4, 6, 9, 11):
        return 30
    elif month == 2:
        return 29 if _is_leap_year(year) else 28
    return 0


def _validate_date(year: int, month: int, day: int) -> bool:
    """Validate a date tuple."""
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
    import re
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
    # Parse initial date
    if initial:
        parsed = _parse_date(initial)
        if parsed:
            y, m, d = parsed
        else:
            # Fallback to today
            import datetime
            today = datetime.date.today()
            y, m, d = today.year, today.month, today.day
    else:
        import datetime
        today = datetime.date.today()
        y, m, d = today.year, today.month, today.day
    
    cursor_day = d
    day_mode = False  # False: Navigates Month/Year | True: Navigates Days
    
    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    
    def _render():
        """Render calendar grid."""
        import calendar as cal
        cols = ui_utils.get_terminal_width()
        c = cols - 4
        lines = []
        
        # Header
        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")
        
        # Month/Year display
        month_name = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m]
        
        lines.append(f"  {C.BOLD}{month_name} {y}{C.RESET}")

        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")
        
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
                        # Emphasize the cursor day more distinctly if in day mode
                        style = f"{C.ACCENT}{C.BOLD}" if day_mode else f"{C.BOLD}"
                        week_parts.append(f"{style}{day:2d}{C.RESET} ")
                    else:
                        week_parts.append(f"{day:2d} ")
            lines.append(f"  {''.join(week_parts)}")
        
        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")
        
        # Dynamic shortcuts depending on navigation mode
        if not day_mode:
            shortcuts = _hint(
                ("↵", "confirm"),
                ("q", "exit"),
                ("Tab", "switch to Day Selection"),
                ("←→", "change month"),
                ("↑↓", "change year"),
                ("m", "manual date entry"),
            )
        else:
            shortcuts = _hint(
                ("↵", "confirm"),
                ("q", "exit"),
                ("Tab", "switch to Month/Year Selection"),
                ("←→", "prev/next day"),
                ("↑↓", "-7 / +7 days"),
                ("m", "manual date entry"),
            )

        shortcuts = shortcuts.splitlines()
        lines.extend([f"  {s}" for s in shortcuts])

        w.render(lines)
    
    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        _render()
        
        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[2J\033[H")
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
            elif key == 'ESC' or key == 'q':
                break
            
            elif key == 'TAB':
                # Toggle navigation mode
                day_mode = not day_mode
            
            elif key == 'RIGHT':
                if not day_mode:
                    # Next month
                    m += 1
                    if m > 12:
                        m = 1
                        y += 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    # Next day
                    cursor_day += 1
                    if cursor_day > _days_in_month(y, m):
                        m += 1
                        if m > 12:
                            m = 1
                            y += 1
                        cursor_day = 1
                        
            elif key == 'LEFT':
                if not day_mode:
                    # Previous month
                    m -= 1
                    if m < 1:
                        m = 12
                        y -= 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    # Previous day
                    cursor_day -= 1
                    if cursor_day < 1:
                        m -= 1
                        if m < 1:
                            m = 12
                            y -= 1
                        cursor_day = _days_in_month(y, m)
                        
            elif key == 'UP':
                if not day_mode:
                    # Previous year
                    y -= 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    # Grid up (Previous week / -7 days)
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
                    # Next year
                    y += 1
                    cursor_day = min(cursor_day, _days_in_month(y, m))
                else:
                    # Grid down (Next week / +7 days)
                    cursor_day += 7
                    if cursor_day > _days_in_month(y, m):
                        m += 1
                        if m > 12:
                            m = 1
                            y += 1
                        # Wraps into the first day of the next month
                        cursor_day = 1

            elif key == 'm':
                # Manual entry
                w.clear()
                manual = text("Enter date (YYYY-MM-DD):", default=f"{y:04d}-{m:02d}-{cursor_day:02d}")
                if manual:
                    parsed = _parse_date(manual)
                    if parsed:
                        y, m, d = parsed
                        cursor_day = d
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()

            elif key.isdigit() and int(key) >= 1 and int(key) <= 9:
                # Quick day selection (1-9)
                day = int(key)
                if day <= _days_in_month(y, m):
                    cursor_day = day
            
            _render()
    
    finally:
        _restore_term_attrs(fd, old)
        w.clear()
    
    return result



# ── fraction_edit() ────────────────────────────────────────────────────────────────────

def fraction_edit(message: str = "Edit metadata pair:",
                    tag: str = "TRCK", value: str = "") -> dict | None:
    """
    In-place editor for an isolated single tag's current/total values.
    Allows integers, floats, spaces, and strings.
    
    Returns:
        Dict with keys: {'current', 'total'} or None if cancelled
    """
    # 1. Setup contextual descriptors based on the isolated tag type
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
        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")
        
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
        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")

        shortcuts = _hint(
            ("↵", "save"),
            ("Tab", "change field"),
            ("q", "cancel"),
        )
        shortcuts = shortcuts.splitlines()
        lines.extend([f"  {s}" for s in shortcuts])
        
        w.render(lines)
    
    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        _render()
        
        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[2J\033[H")
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
            elif key == 'ESC' or key == 'q':
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

# ── time_edit() ────────────────────────────────────────────────────────────────────

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
    # Parse initial time
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
        """Validate the time."""
        try:
            h = int("".join(fields['hours']) or "0")
            m = int("".join(fields['minutes']) or "0")
            s = int("".join(fields['seconds']) or "0")
            return 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60
        except ValueError:
            return False
    
    def _render():
        """Render the time editor."""
        cols = ui_utils.get_terminal_width()
        c = cols - 4
        lines = []
        
        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")
        
        # Time display with separators
        row = "  "
        for i, field in enumerate(field_order):
            label = field_labels[field]
            value = "".join(fields[field])
            pos = positions[field]
            
            if i == cursor_field:
                # Editing cursor
                if pos >= len(value):
                    display = value + f"{C.BACK}█{C.RESET}"
                else:
                    display = value[:pos] + f"{C.INVERT}{C.BOLD}{value[pos]}{C.RESET}" + value[pos+1:]
            else:
                display = value.ljust(field_maxlen[field], '0')
            
            row += display
            
            # Separators
            if i == 0:
                row += ":"
            elif i == 1:
                row += ":"
            elif i == 2:
                row += "."
        
        lines.append(row)
        lines.append(f"  {C.DIM}{'─' * c}{C.RESET}")
        lines.append(f"  {C.GREEN}↵{C.RESET} save  {C.CYAN}Tab{C.RESET} next  {C.ACCENT}q{C.RESET} cancel  (Valid: 00:00:00 - 23:59:59)")
        
        w.render(lines)
    
    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        _render()
        
        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[2J\033[H")
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
            elif key == 'ESC' or key == 'q':
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
                # Only allow digits, respect max length
                buf.insert(pos, key)
                positions[current_field] = pos + 1
            
            _render()
    
    finally:
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
    except Exception as e:
        print(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)