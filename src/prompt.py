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

import sys
import os
import tty
import termios

from src import ui_utils


# ── ANSI ──────────────────────────────────────────────────────────────────────

_HIDE  = "\033[?25l"
_SHOW  = "\033[?25h"
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_CYA   = "\033[1;36m"
_GRN   = "\033[1;32m"

def _clrline():        return "\033[2K\r"
def _goto(row, col=1): return f"\033[{row};{col}H"
def _col(n):           return f"\033[{n}G"


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
           header: list | None = None) -> str | None:
    """Arrow keys / jk navigate, Enter selects, q / Ctrl-C → None.

    Args:
        message: Prompt label shown above the list.
        choices: Items to choose from (str, dict, or Choice).
        header:  Optional list of plain strings rendered above the prompt on
                 every draw, including after resize.  Pass a callable
                 ``() -> list[str]`` to recompute on each draw (e.g. to
                 reflect terminal width).
    """
    items = _norm(choices)
    if not items:
        return None

    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = termios.tcgetattr(fd)
    w        = _Widget(fd)

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport
        cols    = _cols()
        h_lines = _header_lines()
        vis     = max(2, _visible_rows() - len(h_lines))
        n       = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1

        out = h_lines[:]
        out.append(f"{_CYA}{_BOLD}{message}{_RESET}")
        out.append(f"  {_DIM}↑ {viewport} more{_RESET}" if viewport > 0 else "")

        for i in range(viewport, min(viewport + vis, n)):
            label = str(items[i].title)
            max_w = cols - 6
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            if i == cursor:
                out.append(f"  {_CYA}▶{_RESET} {_BOLD}{label}{_RESET}")
            else:
                out.append(f"    {label}")

        remaining = n - viewport - vis
        out.append(f"  {_DIM}↓ {remaining} more{_RESET}" if remaining > 0 else "")
        out.append(f"{_DIM}  ↑↓ jk navigate   Enter select   q cancel{_RESET}")
        return out

    result = None
    try:
        tty.setraw(fd)
        # Clear screen on entry so previous menu content never bleeds through.
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        w.render(_lines())

        import select as _sel
        while True:
            if ui_utils.consume_resize():
                w.anchor_reset()
                w.render(_lines())
                continue

            r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
            if not r:
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        w.clear()

    return result


# ── checkbox() ────────────────────────────────────────────────────────────────

def checkbox(message: str, choices: list,
             header: list | None = None) -> list | None:
    """Space toggles, a all, Enter confirms, q / Ctrl-C → None.

    Args:
        message: Prompt label shown above the list.
        choices: Items to choose from (str, dict, or Choice).
        header:  Optional list of plain strings rendered above the prompt on
                 every draw, including after resize.  Pass a callable
                 ``() -> list[str]`` to recompute on each draw.
    """
    items    = _norm(choices)
    checked  = [c.checked for c in items]
    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = termios.tcgetattr(fd)
    w        = _Widget(fd)

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport
        cols    = _cols()
        h_lines = _header_lines()
        vis     = max(2, _visible_rows() - len(h_lines))
        n       = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1

        out = h_lines[:]
        out.append(f"{_CYA}{_BOLD}{message}{_RESET}")
        out.append(f"  {_DIM}↑ {viewport} more{_RESET}" if viewport > 0 else "")

        for i in range(viewport, min(viewport + vis, n)):
            label = str(items[i].title)
            max_w = cols - 8
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            tick = f"{_GRN}✓{_RESET}" if checked[i] else " "
            if i == cursor:
                out.append(f"  {_CYA}▶{_RESET} [{tick}] {_BOLD}{label}{_RESET}")
            else:
                out.append(f"    [{tick}] {label}")

        remaining = n - viewport - vis
        out.append(f"  {_DIM}↓ {remaining} more{_RESET}" if remaining > 0 else "")
        out.append(f"{_DIM}  ↑↓ navigate   Space toggle   a all   Enter confirm  ({sum(checked)} selected){_RESET}")
        return out

    result = None
    try:
        tty.setraw(fd)
        # Clear screen on entry so previous menu content never bleeds through.
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        w.render(_lines())

        import select as _sel
        while True:
            if ui_utils.consume_resize():
                w.anchor_reset()
                w.render(_lines())
                continue

            r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
            if not r:
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        w.clear()

    return result


# ── confirm() ─────────────────────────────────────────────────────────────────

def confirm(message: str, default: bool = False) -> bool:
    hint   = "(Y/n)" if default else "(y/N)"
    fd     = sys.stdin.fileno()
    old    = termios.tcgetattr(fd)
    result = default
    try:
        tty.setraw(fd)
        sys.stdout.write(_HIDE + f"{_CYA}{_BOLD}{message}{_RESET} {_DIM}{hint}{_RESET} ")
        sys.stdout.flush()
        while True:
            key = _read_key(fd)
            if   key == 'CTRL_C':        result = False;   break
            elif key == 'ENTER':         result = default; break
            elif key.lower() == 'y':     result = True;    break
            elif key.lower() == 'n':     result = False;   break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write(_SHOW + "\n")
        sys.stdout.flush()
    return result


# ── text() ────────────────────────────────────────────────────────────────────

def text(message: str, default: str = "") -> str | None:
    buf    = list(default)
    pos    = len(buf)
    fd     = sys.stdin.fileno()
    old    = termios.tcgetattr(fd)
    result = None
    
    # Track how many physical lines were drawn to clear them later
    prev_lines = 0

    def _render():
        nonlocal prev_lines
        cols = _cols() # Uses the existing helper in prompt.py
        
        # 1. Layout Simulation: Map buffer characters to physical rows
        # This correctly handles \n and automatic wrapping
        physical_lines = []
        curr_line = ""
        cursor_row = 0
        cursor_col = 0
        
        for i, char in enumerate(buf):
            if i == pos:
                cursor_row = len(physical_lines)
                cursor_col = len(curr_line)
            
            if char == '\n':
                physical_lines.append(curr_line)
                curr_line = ""
            else:
                curr_line += char
                if len(curr_line) == cols:
                    physical_lines.append(curr_line)
                    curr_line = ""
        
        # Position cursor if it's at the very end of the buffer
        if pos == len(buf):
            cursor_row = len(physical_lines)
            cursor_col = len(curr_line)
        
        physical_lines.append(curr_line)
        total_rows = len(physical_lines)

        # 2. Draw
        # Move up to the start of the previous render (prompt line + text lines)
        if prev_lines > 0:
            sys.stdout.write(f"\r\033[{prev_lines}A")
        
        # Clear everything from current position to bottom
        sys.stdout.write(f"\r\033[J{_HIDE}")
        
        # Print prompt (using \r\n for raw mode)
        sys.stdout.write(f"\r{_CYA}{_BOLD}{message}{_RESET}\r\n")
        
        # Print text lines
        for i, line in enumerate(physical_lines):
            sys.stdout.write(f"\r{line}")
            if i < total_rows - 1:
                sys.stdout.write("\r\n")
        
        # 3. Precision Cursor Positioning
        # Move back up to the specific cursor row
        rows_to_move_up = (total_rows - 1) - cursor_row
        if rows_to_move_up > 0:
            sys.stdout.write(f"\033[{rows_to_move_up}A")
        
        # Move to the correct column
        if cursor_col > 0:
            sys.stdout.write(f"\r\033[{cursor_col}C")
        else:
            sys.stdout.write("\r")
            
        sys.stdout.write(_SHOW)
        sys.stdout.flush()
        
        # Total height = 1 (prompt) + number of text lines
        prev_lines = 1 + total_rows

    try:
        tty.setraw(fd)
        _render()
        import select as _sel
        while True:
            if ui_utils.consume_resize(): _render()
            r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
            if not r: continue
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        # Clean exit: ensure the terminal prompt starts below your text
        sys.stdout.write("\r\n" * 2) 
        sys.stdout.flush()
        
    return result


# ── path() ────────────────────────────────────────────────────────────────────

def path(message: str, default: str = "") -> str | None:
    buf          = list(default)
    pos          = len(buf)
    fd           = sys.stdin.fileno()
    old          = termios.tcgetattr(fd)
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
        prompt  = f"{_CYA}{_BOLD}{message}{_RESET} "
        content = "".join(buf)
        max_w   = max(1, cols - len(message) - 4)
        if pos > max_w:
            display  = content[pos - max_w: pos]
            disp_pos = max_w
        else:
            display  = content[:max_w]
            disp_pos = pos
        sys.stdout.write(
            _HIDE + _clrline() + prompt + display +
            _col(len(message) + 2 + disp_pos + 1) + _SHOW
        )
        sys.stdout.flush()

    try:
        tty.setraw(fd)
        _render()
        import select as _sel
        while True:
            if ui_utils.consume_resize(): _render()
            r, _, _ = _sel.select([sys.stdin], [], [], 0.05)
            if not r: continue
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
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return result