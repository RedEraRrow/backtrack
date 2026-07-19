"""
Terminal prompt widgets — resize-aware replacements for questionary.

API:
    prompt.select(message, choices)               -> value | None
    prompt.select(message, choices, multi=True)   -> [value, ...] | None
    prompt.confirm(message)                       -> bool
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
import subprocess
from typing import Any, Callable, Literal, overload

from src.utils.prompt_core import (
    _check_deferred_quit, _IS_WINDOWS, _COLUMNS_MAX_WIDTH, _EDGE_MARGIN,
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress,
    _clrline, _goto, _col, _hint, _render_status_bar,
    Choice, Column,
    _cell_text, _style_cell, _render_cell_segments, _table_widths, _render_table_row,
    separator, _split_columns, _clip_ansi, _render_select_columns, _style_checkbox_label, _norm,
    _read_key, _read_key_raw,
    _visible_rows, _cols, _rows, _hint_lines, _wrap_bordered_input_lines,
    _Widget,
)
from src.utils import ui_utils
from src import state as _state
from src.state import QuitToTerminal
C = ui_utils.Colors


@overload
def select(message: str, choices: list, *,
           header: list | None | Callable[[], list[str]] = ...,
           extra_hints: dict[str, str] | None = ...,
           index: int = ...,
           shortcuts: dict[str, str] | None = ...,
           columns: list | None = ...,
           multi: Literal[False] = ...,
           interlock_category_callback: Callable[[Any], str] | None = ...,
           on_inspect: Callable[[Any], None] | None = ...,
           inspect_key: str = ...,
           ) -> str | None: ...
@overload
def select(message: str, choices: list, *,
           header: list | None | Callable[[], list[str]] = ...,
           extra_hints: dict[str, str] | None = ...,
           index: int = ...,
           shortcuts: dict[str, str] | None = ...,
           columns: list | None = ...,
           multi: Literal[True],
           interlock_category_callback: Callable[[Any], str] | None = ...,
           on_inspect: Callable[[Any], None] | None = ...,
           inspect_key: str = ...,
           ) -> list[Any] | None: ...
def select(message: str, choices: list, *,
           header: list | None | Callable[[], list[str]] = None,
           extra_hints: dict[str, str] | None = None,
           index: int = 0,
           shortcuts: dict[str, str] | None = None,
           columns: list | None = None,
           multi: bool = False,
           interlock_category_callback: Callable[[Any], str] | None = None,
           on_inspect: Callable[[Any], None] | None = None,
           inspect_key: str = 'd',
           ) -> str | list[Any] | None:
    """Arrow keys / jk to navigate; Enter / → to confirm; ← / b / Esc / q → None.

    When multi=True, Space toggles the current item and Enter returns a list of
    all checked values (possibly empty).  Otherwise returns the single selected
    value, or None if cancelled.

    Args:
        message:    Prompt label shown above the list.
        choices:    Items — str, dict, or Choice objects.
        header:     Optional lines rendered above the prompt.
        extra_hints: Extra key→action bindings merged into the hint bar.
        index:      Initial cursor position.
        shortcuts:  Optional key→return-value map (single-select only).
        columns:    Column layout descriptors (see Column dataclass).
        multi:      Enable multi-select mode (Space to toggle, Enter returns list).
        interlock_category_callback: When set, only one category can be checked
            at a time (multi=True only).
        on_inspect: Called with the current row's value when `inspect_key` is
            pressed; runs its own view and returns, leaving selection/checkbox
            state intact (the list redraws afterwards).
        inspect_key: Key that triggers `on_inspect` (default 'd').
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

    # Interlock state for multi-select: track which category is locked
    _locked_category: list[str | None] = [None]

    def _update_interlock() -> None:
        if not multi or interlock_category_callback is None:
            return
        checked = [it for it in items if it.checked]
        if not checked:
            _locked_category[0] = None
            for it in items:
                it.disabled = False
            return
        _locked_category[0] = interlock_category_callback(checked[0].value)
        for it in items:
            if not it.checked:
                it.disabled = (interlock_category_callback(it.value) != _locked_category[0])

    _update_interlock()

    base_hints: dict[str, str]
    if multi:
        base_hints = {"↑↓": "move", "space": "toggle", "←/b": "back", "q": "quit", "↵": "confirm"}
    else:
        base_hints = {"↑↓": "move", "←/b": "back", "q": "quit", "↵": "confirm"}

    if extra_hints:
        combined_hints = {**extra_hints, **base_hints}
    else:
        combined_hints = base_hints

    _last_hlen = [0]
    # Maps a visible item index → its ANSI-stripped rendered text, so a mouse
    # click can tell whether it landed on a printed character or blank space.
    _row_plain: dict[int, str] = {}

    def _plain(s: str) -> str:
        return re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', s)

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport
        cols    = _cols()
        h_lines = _header_lines()
        _last_hlen[0] = len(h_lines)
        _row_plain.clear()

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

        # Structured columns: compute table widths once from each item's cells.
        # Rows without cells (headings/separators) fall back to plain rendering.
        eff: int = 0
        col_widths: list[int] = []
        if columns:
            eff = min(cols, _COLUMNS_MAX_WIDTH)
            rows_cells = [it.cells for it in items if it.cells]
            col_widths = _table_widths(rows_cells, columns, eff,
                                       pointer_w=6 if multi else 4, right_margin=_EDGE_MARGIN)

        for i in range(viewport, min(viewport + vis, n)):
            if columns and items[i].cells:
                out.append(_render_table_row(
                    items[i].cells, columns, i == cursor, col_widths, eff, _EDGE_MARGIN,
                    is_checked=items[i].checked if multi else None,
                    disabled=items[i].disabled))
                _row_plain[i] = _plain(out[-1])
                continue

            _ct = items[i].cursor_title
            label = str(_ct if (_ct is not None and i == cursor) else items[i].title)
            if multi:
                max_w = cols - 9
            else:
                max_w = cols - 6
            if len(label) > max_w:
                label = label[:max_w - 1] + "…"
            if multi:
                if items[i].disabled and not items[i].checked:
                    # Dimmed (interlocked) — not selectable
                    out.append(f"   {C.DIM}• {label}{C.RESET}")
                elif i == cursor:
                    glyph = f"{C.GREEN}✔{C.RESET}" if items[i].checked else f"{C.DIM}•{C.RESET}"
                    out.append(f"  {C.ACCENT}›{C.RESET} {glyph} {C.PRIMARY}{C.BOLD}{label}{C.RESET}")
                else:
                    glyph = f"{C.GREEN}✔{C.RESET}" if items[i].checked else f"{C.DIM}•{C.RESET}"
                    out.append(f"    {glyph} {C.DIM}{label}{C.RESET}")
            elif items[i].disabled:
                # Section heading / separator — dim, no pointer, slightly outdented.
                out.append(f"  {C.DIM}{C.BOLD}{label}{C.RESET}" if label else "")
            elif i == cursor:
                out.append(f"  {C.ACCENT}›{C.RESET} {C.PRIMARY}{C.BOLD}{label}{C.RESET}")
            else:
                out.append(f"    {C.DIM}{label}{C.RESET}")
            _row_plain[i] = _plain(out[-1])

        remaining = n - viewport - vis
        out.append(f"  {C.DIM}╷ {remaining} below{C.RESET}" if remaining > 0 else "")
        out.extend(hint_lines)
        # Hard guarantee: no rendered line ever exceeds the terminal width, so
        # the list can never wrap no matter how narrow the window is.
        return [_clip_ansi(line, ui_utils.get_terminal_width()) for line in out]

    result = None
    _sel_last_click: int | None = None
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
            elif key in ('UP',   'k'):           cursor = _step(cursor, -1);          _sel_last_click = None; w.render(_lines())
            elif key in ('DOWN', 'j'):           cursor = _step(cursor, 1);           _sel_last_click = None; w.render(_lines())
            elif key == 'HOME':                  cursor = selectable[0];              _sel_last_click = None; w.render(_lines())
            elif key == 'END':                   cursor = selectable[-1];             _sel_last_click = None; w.render(_lines())
            elif key == 'PGUP':                  cursor = _nearest_selectable(max(0, cursor - _visible_rows())); _sel_last_click = None; w.render(_lines())
            elif key == 'PGDN':                  cursor = _nearest_selectable(min(len(items) - 1, cursor + _visible_rows())); _sel_last_click = None; w.render(_lines())
            elif key == 'SPACE' and multi:
                it = items[cursor]
                if not it.disabled or it.checked:
                    if interlock_category_callback and _locked_category[0] and not it.checked:
                        cat = interlock_category_callback(it.value)
                        if cat != _locked_category[0]:
                            sys.stdout.write("\a"); sys.stdout.flush(); continue
                    it.checked = not it.checked
                    _update_interlock()
                    selectable[:] = [i for i, x in enumerate(items) if not x.disabled or x.checked]
                    w.render(_lines())
            elif key in ('ENTER', 'RIGHT', 'l'):
                if multi:
                    result = [it.value for it in items if it.checked]; break
                elif not items[cursor].disabled:
                    result = items[cursor].value; break
            elif key in ('LEFT', 'b', 'h', 'ESC'): result = None; break
            elif key in ('q', 'Q'):              raise QuitToTerminal()
            elif on_inspect is not None and key == inspect_key and not items[cursor].disabled:
                # Inspect the current row (e.g. a full detail view) without
                # ending selection or losing checkbox state. The callback runs
                # its own full-screen prompt, so re-arm mouse reporting and force
                # a full redraw when it returns.
                on_inspect(items[cursor].value)
                if not _IS_WINDOWS:
                    sys.stdout.write("\033[?1000h\033[?1006h")
                sys.stdout.flush()
                _sel_last_click = None
                w.anchor_reset()
                w.render(_lines())
            elif shortcuts and key in shortcuts:  result = shortcuts[key]; break
            elif key == 'SCROLL_UP':             cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_DOWN':           cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key.startswith('MOUSE_CLICK:'):
                parts = key.split(':')
                r, col = int(parts[2]), int(parts[3]) if len(parts) > 3 else 1
                if w.row is None:
                    continue
                # render() prepends MARGIN_V blank rows before lines[0].
                # lines[] layout: H header lines, message, viewport-above
                # indicator, then items. So item[viewport] is at:
                #   terminal row = w.row + MARGIN_V + H + 2
                i = r - w.row - ui_utils.MARGIN_V - _last_hlen[0] - 2
                idx = viewport + i
                if not (0 <= idx < len(items)):
                    continue
                clickable = not items[idx].disabled

                # A click only confirms/toggles when it lands on a printed
                # character; clicking the blank space anywhere in a row (trailing
                # padding, gaps between table columns, the empty left margin) just
                # moves the highlight — it never enters.
                row_plain = _row_plain.get(idx, "")
                on_char = 0 < col <= len(row_plain) and row_plain[col - 1] != ' '
                if not on_char:
                    if clickable or (multi and items[idx].checked):
                        cursor = idx
                    _sel_last_click = None
                    w.render(_lines())
                    continue

                if multi and (clickable or items[idx].checked):
                    cursor = idx
                    it = items[cursor]
                    if not (interlock_category_callback and _locked_category[0]
                            and not it.checked
                            and interlock_category_callback(it.value) != _locked_category[0]):
                        it.checked = not it.checked
                        _update_interlock()
                        selectable[:] = [i for i, x in enumerate(items) if not x.disabled or x.checked]
                    w.render(_lines())
                elif not multi and clickable:
                    if idx == cursor or _sel_last_click == idx:
                        # Already on this item (keyboard or prior click) — confirm
                        cursor = idx
                        result = items[cursor].value
                        break
                    else:
                        _sel_last_click = idx
                        cursor = idx
                        w.render(_lines())
                elif not multi:
                    # Disabled/heading row — move cursor, reset click state
                    _sel_last_click = None
                    cursor = idx
                    w.render(_lines())

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def live_select(message: str, provider: Callable[[str], list], *,
                header: list | None | Callable[[], list[str]] = None,
                columns: list | None = None,
                extra_hints: dict[str, str] | None = None,
                on_cycle: Callable[[], None] | None = None,
                initial_query: str = "") -> Any:
    """Incremental "search box + live results" widget.

    `provider(query)` is called on each query change and returns the ranked list
    of Choice to display (cells already built, including any highlight segments).
    Letters/digits type into the query; ← → move the query caret; ↑ ↓ (and the
    scroll wheel) move through results; Enter selects the highlighted row; Esc
    cancels. Returns the chosen Choice.value, or None.
    """
    _check_deferred_quit()
    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    query: list[str] = list(initial_query)
    qpos             = len(query)
    items: list      = list(provider("".join(query))) if query else []
    cursor           = 0
    viewport         = 0

    base_hints = {"type": "search", "↑↓": "results", "esc": "back", "↵": "open"}
    if on_cycle is not None:
        base_hints["tab"] = "scope"
    hints = {**(extra_hints or {}), **base_hints}

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _selectable() -> list[int]:
        return [i for i, it in enumerate(items) if not it.disabled]

    def _step(cur: int, direction: int) -> int:
        sel = _selectable()
        if not sel:
            return cur
        if cur in sel:
            idx = sel.index(cur)
            return sel[(idx + direction) % len(sel)]
        return sel[0] if direction > 0 else sel[-1]

    def _recompute() -> None:
        nonlocal items, cursor, viewport
        try:
            items = list(provider("".join(query))) if query else []
        except Exception:
            items = []
        cursor = _step(-1, 1) if items else 0
        viewport = 0

    def _lines() -> list:
        nonlocal viewport
        width = ui_utils.get_terminal_width()
        cols  = _cols()
        out = _header_lines()

        qtext = "".join(query)
        b, a = qtext[:qpos], qtext[qpos:]
        out.append(f"  {C.DIM}{message}{C.RESET} {b}{C.ACCENT}▏{C.RESET}{a}")
        count = "type to search…" if not qtext else f"{len(items)} result(s)"
        out.append(f"  {C.DIM}{count}{C.RESET}")

        hint_lines = _hint(*list(hints.items())).splitlines()
        overhead = len(out) + len(hint_lines) + 3
        vis = max(2, _visible_rows() - overhead)

        n = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1
        out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}" if viewport > 0 else "")

        eff = min(cols, _COLUMNS_MAX_WIDTH)
        col_widths: list = []
        if columns:
            rows_cells = [it.cells for it in items if it.cells]
            if rows_cells:
                col_widths = _table_widths(rows_cells, columns, eff,
                                           pointer_w=4, right_margin=_EDGE_MARGIN)

        for i in range(viewport, min(viewport + vis, n)):
            it = items[i]
            if columns and it.cells:
                out.append(_render_table_row(it.cells, columns, i == cursor,
                                             col_widths, eff, _EDGE_MARGIN))
            elif it.disabled:
                out.append(f"  {C.DIM}{C.BOLD}{it.title}{C.RESET}" if it.title else "")
            elif i == cursor:
                out.append(f"  {C.ACCENT}›{C.RESET} {C.PRIMARY}{C.BOLD}{it.title}{C.RESET}")
            else:
                out.append(f"    {C.DIM}{it.title}{C.RESET}")

        remaining = n - viewport - vis
        out.append(f"  {C.DIM}╷ {remaining} below{C.RESET}" if remaining > 0 else "")
        out.extend(hint_lines)
        return [_clip_ansi(line, width) for line in out]

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

            if key == 'CTRL_C':
                raise QuitToTerminal()
            elif key == 'ESC':
                result = None
                break
            elif key == 'TAB' and on_cycle is not None:
                on_cycle()
                _recompute()
                w.render(_lines())
            elif key == 'ENTER':
                if items and not items[cursor].disabled:
                    result = items[cursor].value
                    break
            elif key in ('UP',):
                cursor = _step(cursor, -1); w.render(_lines())
            elif key in ('DOWN',):
                cursor = _step(cursor, 1); w.render(_lines())
            elif key == 'SCROLL_UP':
                cursor = _step(cursor, -1); w.render(_lines())
            elif key == 'SCROLL_DOWN':
                cursor = _step(cursor, 1); w.render(_lines())
            elif key == 'PGUP':
                sel = _selectable()
                if sel:
                    cursor = max(sel[0], cursor - 5)
                    if items[cursor].disabled:
                        cursor = _step(cursor, -1)
                w.render(_lines())
            elif key == 'PGDN':
                sel = _selectable()
                if sel:
                    cursor = min(sel[-1], cursor + 5)
                    if items[cursor].disabled:
                        cursor = _step(cursor, 1)
                w.render(_lines())
            elif key == 'LEFT':
                qpos = max(0, qpos - 1); w.render(_lines())
            elif key == 'RIGHT':
                qpos = min(len(query), qpos + 1); w.render(_lines())
            elif key == 'HOME':
                qpos = 0; w.render(_lines())
            elif key == 'END':
                qpos = len(query); w.render(_lines())
            elif key == 'BACKSPACE':
                if qpos > 0:
                    query.pop(qpos - 1); qpos -= 1
                    _recompute(); w.render(_lines())
            elif key == 'SPACE':
                query.insert(qpos, ' '); qpos += 1
                _recompute(); w.render(_lines())
            elif len(key) == 1 and key.isprintable():
                query.insert(qpos, key); qpos += 1
                _recompute(); w.render(_lines())
    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
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
            f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}",
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
    edit_mode: bool, edit_col: int, edit_buf: list[str], edit_pos: int,
    fixed_rows: bool = False,
    barrel_mode: bool = False, barrel_hints: list[str] | None = None, barrel_idx: int = 0,
    col_ratios: tuple | None = None,
) -> tuple[list[str], int]:
    num_cols = len(headers)
    cols = _cols()
    c = cols - 4
    inner = c
    out = []

    if fixed_rows:
        base_hints = {"↑↓": "move", "e": "edit", "i": "import text", "f": "from file", "esc": "back", "↵": "save", "q": "quit"}
    else:
        base_hints = {"↑↓": "move", "a": "add", "e": "edit", "d": "delete", "K/J": "reorder", "i": "import text", "f": "from file", "esc": "back", "↵": "save", "q": "quit"}
    edit_hints = {"tab": "next col", "esc": "cancel", "↵": "apply"}

    out.append(f"  {C.DIM}{message}{C.RESET}")
    out.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

    avail_w = max(10, inner - 4 - (2 * (num_cols - 1)))
    if col_ratios and len(col_ratios) == num_cols:
        ratio_total = sum(col_ratios)
        col_widths = [max(1, int(avail_w * r / ratio_total)) for r in col_ratios]
        col_widths[-1] = max(1, avail_w - sum(col_widths[:-1]))
    else:
        col_widths = [avail_w // num_cols] * (num_cols - 1) + [avail_w - (avail_w // num_cols) * (num_cols - 1)]
    col_w = col_widths[0] if num_cols > 1 else avail_w
    last_w = col_widths[-1]

    if num_cols > 1:
        h_parts = [f"{headers[i]:<{col_widths[i]}}" for i in range(num_cols - 1)]
        h_parts.append(f"{headers[-1]}")
        out.append(f"    {C.DIM}{'  '.join(h_parts)}{C.RESET}")

        u_parts = ["─" * col_widths[i] for i in range(num_cols - 1)]
        u_parts.append("─" * last_w)
        out.append(f"    {'  '.join(u_parts)}")
    else:
        out.append(f"    {C.DIM}{headers[0]}{C.RESET}")
        out.append(f"    {'─' * inner}")

    if barrel_mode:
        edit_hints = {"↑↓": "cycle", "↵": "pick", "esc": "cancel"}
    active_hints = edit_hints if edit_mode else base_hints
    hint_res = _hint(*active_hints.items())
    hint_raw = hint_res[0] if isinstance(hint_res, tuple) else hint_res
    hint_lines = hint_raw.split("\n") if hint_raw else []

    _LEDIT_HEADER_ROWS = 4   # message + separator + col-headers + col-underline
    _LEDIT_FOOTER_ROWS = 1   # bottom separator (hints follow immediately)
    fixed_overhead = _LEDIT_HEADER_ROWS + _LEDIT_FOOTER_ROWS + len(hint_lines)
    vis = max(2, _visible_rows() - fixed_overhead)
    n = len(items)

    if cursor < viewport:
        viewport = cursor
    elif cursor >= viewport + vis:
        viewport = cursor - vis + 1

    if n == 0:
        out.append(f"    {C.DIM}(empty list){C.RESET}")
    else:
        hints = barrel_hints or []
        n_hints = len(hints)
        cur_text  = hints[barrel_idx] if barrel_mode and hints else ""
        show_above = barrel_mode and n_hints >= 3
        prev_text = hints[(barrel_idx - 1) % n_hints] if show_above else ""
        next_text = hints[(barrel_idx + 1) % n_hints] if barrel_mode and n_hints >= 2 else ""

        for i in range(viewport, min(viewport + vis, n)):
            item = items[i]
            is_sel = (i == cursor)

            row_is_editing = (is_sel and edit_mode)
            cursor_glyph = f"{C.ACCENT}›{C.RESET}" if (is_sel and not edit_mode) else (" " if not row_is_editing else "✎")

            if num_cols > 1:
                i_vals = list(item) if isinstance(item, (list, tuple)) else [str(item)]
                while len(i_vals) < num_cols: i_vals.append("")

                if row_is_editing and barrel_mode:
                    def _barrel_above(w: int, is_barrel_col: bool) -> str:
                        if not is_barrel_col:
                            return " " * w
                        if prev_text:
                            return f"{C.ACCENT}⌃{C.RESET} {C.DIM}{ui_utils.truncate_text(prev_text, w - 2)}{C.RESET}"
                        return " " * w

                    def _barrel_mid(w: int, is_barrel_col: bool, val: str) -> str:
                        if not is_barrel_col:
                            return f"{val:<{w}}"
                        return f"{C.PRIMARY}{C.BOLD}{ui_utils.truncate_text(cur_text, w)}{C.RESET}"

                    def _barrel_below(w: int, is_barrel_col: bool) -> str:
                        if not is_barrel_col:
                            return " " * w
                        if next_text:
                            return f"{C.ACCENT}⌄{C.RESET} {C.DIM}{ui_utils.truncate_text(next_text, w - 2)}{C.RESET}"
                        return " " * w

                    mid_parts, below_parts = [], []
                    above_parts: list[str] = []
                    for j in range(num_cols - 1):
                        bc = (edit_col == j)
                        above_parts.append(_barrel_above(col_widths[j], bc))
                        mid_parts.append(_barrel_mid(col_widths[j], bc, str(i_vals[j])))
                        below_parts.append(_barrel_below(col_widths[j], bc))
                    bc_last = (edit_col == num_cols - 1)
                    above_parts.append(_barrel_above(last_w, bc_last))
                    mid_parts.append(_barrel_mid(last_w, bc_last, str(i_vals[-1])))
                    below_parts.append(_barrel_below(last_w, bc_last))

                    sep = "  "
                    if show_above:
                        out.append(f"    {sep.join(above_parts)}")
                    out.append(f"  ✎ {sep.join(mid_parts)}")
                    out.append(f"    {sep.join(below_parts)}")
                    continue

                row_parts = []
                for j in range(num_cols - 1):
                    cw = col_widths[j]
                    cell_str = _render_list_edit_cell(str(i_vals[j]), cw, row_is_editing, edit_col == j, edit_buf, edit_pos)
                    row_parts.append(f"{cell_str:<{cw}}" if not (row_is_editing and edit_col == j) else cell_str)

                last_cell = _render_list_edit_cell(str(i_vals[-1]), last_w, row_is_editing, edit_col == (num_cols - 1), edit_buf, edit_pos)
                row_parts.append(last_cell)

                row_str = "  ".join(row_parts)

                if is_sel and not edit_mode:
                    out.append(f"  {cursor_glyph} {C.PRIMARY}{C.BOLD}{row_str}{C.RESET}")
                else:
                    out.append(f"  {cursor_glyph} {row_str}")
            else:
                val_str = str(item)

                if row_is_editing and barrel_mode:
                    w = inner - 4
                    mid   = f"{C.PRIMARY}{C.BOLD}{ui_utils.truncate_text(cur_text, w)}{C.RESET}"
                    if show_above and prev_text:
                        out.append(f"    {C.ACCENT}⌃{C.RESET} {C.DIM}{ui_utils.truncate_text(prev_text, w - 2)}{C.RESET}")
                    out.append(f"  ✎ {mid}")
                    if next_text:
                        out.append(f"    {C.ACCENT}⌄{C.RESET} {C.DIM}{ui_utils.truncate_text(next_text, w - 2)}{C.RESET}")
                    continue

                cell_str = _render_list_edit_cell(val_str, inner - 4, row_is_editing, True, edit_buf, edit_pos)
                if is_sel and not edit_mode:
                    out.append(f"  {cursor_glyph} {C.PRIMARY}{C.BOLD}{cell_str}{C.RESET}")
                else:
                    out.append(f"  {cursor_glyph} {cell_str}")

    out.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")
    out.extend(hint_lines)

    return out, viewport, vis, _LEDIT_HEADER_ROWS

def _parse_import_rows(text: str, headers: tuple[str, ...]) -> list:
    """Parse pasted/imported text into list_edit rows, auto-detecting the layout.

    Handles CSV, TSV, and other separators (``;`` ``|`` ``:`` `` - ``) plus
    run-of-2+-spaces columns. Comment (`#`) and blank lines are skipped, and a
    leading header row that just repeats the column names is dropped."""
    num_cols = len(headers)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith('#')]
    if not lines:
        return []
    if num_cols <= 1:
        return list(lines)

    # Delimiter present in a majority of lines wins (tab/comma preferred).
    delim = None
    for cand in ('\t', ',', ';', '|', ' - ', ':'):
        if sum(1 for ln in lines if cand in ln) >= max(1, (len(lines) + 1) // 2):
            delim = cand
            break

    def _fit(fields: list) -> tuple:
        fields = [f.strip() for f in fields]
        if len(fields) > num_cols:                  # overflow → last column keeps the rest
            tail = (delim or ' ').join(fields[num_cols - 1:]).strip()
            fields = fields[:num_cols - 1] + [tail]
        fields += [''] * (num_cols - len(fields))
        return tuple(fields[:num_cols])

    rows: list = []
    if delim == ',':
        import csv
        import io
        for fields in csv.reader(io.StringIO('\n'.join(lines))):
            rows.append(_fit(fields))
    elif delim:
        for ln in lines:
            rows.append(_fit(ln.split(delim, num_cols - 1)))
    else:
        for ln in lines:                            # no delimiter → split on runs of spaces
            parts = re.split(r'\s{2,}', ln)
            rows.append(_fit(parts if len(parts) >= num_cols else [ln]))

    if rows and tuple(str(c).lower() for c in rows[0]) == tuple(h.lower() for h in headers):
        rows = rows[1:]                             # drop a repeated-header row
    return rows


def list_edit(message: str, initial_items: list | None = None, headers: tuple[str, ...] = ("ROLE", "NAME"),
              fixed_rows: bool = False, locked_cols: set | None = None,
              col_ratios: tuple | None = None, col_hints: object = None) -> list | None:
    """Arrow keys navigate, 'a' adds, 'e' edits in-place, 'd' deletes, Enter saves.

    Supports in-place cell editing with Tab navigation between columns.
    fixed_rows: disables add/delete (rows can only be edited, not added or removed).
    locked_cols: set of column indices that cannot be edited.
    """
    items    = list(initial_items) if initial_items else []
    cursor   = 0
    viewport = 0
    fd       = sys.stdin.fileno()
    old      = _get_term_attrs(fd)
    w        = _Widget(fd)
    num_cols = len(headers)

    edit_mode   = False
    edit_col    = 0
    edit_buf: list[str] = []
    edit_pos    = 0
    edit_backup = None
    barrel_mode  = False
    barrel_idx   = 0
    barrel_hints: list[str] = []

    def _get_cell_hints() -> list[str]:
        if col_hints is None:
            return []
        curr = items[cursor] if items else None
        if curr is None:
            return []
        row = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
        try:
            return list(col_hints(edit_col, row))  # type: ignore[operator]
        except Exception:
            return []

    _le_vis: int = 2
    _le_header_rows: int = 4

    def _render():
        nonlocal viewport, _le_vis, _le_header_rows
        lines, new_viewport, new_vis, new_hdr = _build_list_edit_lines(
            message, items, headers,
            cursor, viewport,
            edit_mode, edit_col, edit_buf, edit_pos,
            fixed_rows, barrel_mode, barrel_hints, barrel_idx,
            col_ratios,
        )
        viewport = new_viewport
        _le_vis = new_vis
        _le_header_rows = new_hdr
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
    _le_last_click: int | None = None
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
                _le_last_click = None
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if edit_mode and barrel_mode:
                if key == 'ESC':
                    items[cursor] = edit_backup
                    edit_mode = False
                    barrel_mode = False
                    _render()

                elif key in ('UP', 'k') and barrel_hints:
                    barrel_idx = (barrel_idx - 1) % len(barrel_hints)
                    _render()

                elif key in ('DOWN', 'j') and barrel_hints:
                    barrel_idx = (barrel_idx + 1) % len(barrel_hints)
                    _render()

                elif key == 'ENTER':
                    if barrel_hints:
                        edit_buf = list(barrel_hints[barrel_idx])
                        edit_pos = len(edit_buf)
                    _commit_edit_buffer()
                    barrel_mode = False
                    edit_mode = False
                    _render()

                elif key == 'TAB' and num_cols > 1:
                    if barrel_hints:
                        edit_buf = list(barrel_hints[barrel_idx])
                        edit_pos = len(edit_buf)
                    _commit_edit_buffer()
                    barrel_mode = False
                    next_col = (edit_col + 1) % num_cols
                    if locked_cols:
                        steps = 0
                        while next_col in locked_cols and steps < num_cols:
                            next_col = (next_col + 1) % num_cols
                            steps += 1
                    edit_col = next_col
                    curr = items[cursor]
                    i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                    while len(i_vals) < num_cols: i_vals.append("")
                    edit_buf = list(str(i_vals[edit_col]))
                    edit_pos = len(edit_buf)
                    barrel_hints = _get_cell_hints()
                    cur_val = "".join(edit_buf)
                    if len(barrel_hints) >= 2 and cur_val in barrel_hints:
                        barrel_mode = True
                        barrel_idx = barrel_hints.index(cur_val)
                    else:
                        barrel_mode = False
                        barrel_idx = 0
                    _render()

                elif len(key) == 1 and key.isprintable():
                    # Exit barrel → free-text mode, seed buffer with this char
                    barrel_mode = False
                    edit_buf = [key]
                    edit_pos = 1
                    _render()

            elif edit_mode:
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
                        next_col = (edit_col + 1) % num_cols
                        # Skip locked columns when tabbing.
                        if locked_cols:
                            steps = 0
                            while next_col in locked_cols and steps < num_cols:
                                next_col = (next_col + 1) % num_cols
                                steps += 1
                        edit_col = next_col

                        curr = items[cursor]
                        i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                        while len(i_vals) < num_cols: i_vals.append("")

                        edit_buf = list(str(i_vals[edit_col]))
                        edit_pos = len(edit_buf)
                        barrel_hints = _get_cell_hints()
                        cur_val = "".join(edit_buf)
                        if len(barrel_hints) >= 2 and cur_val in barrel_hints:
                            barrel_mode = True
                            barrel_idx = barrel_hints.index(cur_val)
                        else:
                            barrel_mode = False
                            barrel_idx = 0
                        _render()

                elif key == 'BACKSPACE' and edit_pos > 0:
                    edit_buf.pop(edit_pos - 1)
                    edit_pos -= 1
                    _render()

                elif key == 'BACKSPACE' and edit_pos == 0 and len(barrel_hints) >= 2:
                    barrel_mode = True
                    barrel_idx = 0
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
                elif key == 'SCROLL_UP':
                    if items: cursor = (cursor - 1) % len(items)
                    _le_last_click = None
                    _render()
                elif key == 'SCROLL_DOWN':
                    if items: cursor = (cursor + 1) % len(items)
                    _le_last_click = None
                    _render()
                elif key.startswith('MOUSE_CLICK:'):
                    _parts = key.split(':')
                    _btn, _mrow, _mcol = int(_parts[1]), int(_parts[2]), int(_parts[3])
                    if _btn == 0 and items:
                        # render() prepends MARGIN_V blank rows before lines[0]
                        _line_idx   = _mrow - 1 - ui_utils.MARGIN_V
                        _item_offset = _line_idx - _le_header_rows
                        if 0 <= _item_offset < _le_vis:
                            _clicked_idx = viewport + _item_offset
                            if 0 <= _clicked_idx < len(items):
                                if _le_last_click == _clicked_idx:
                                    cursor = _clicked_idx
                                    result = items
                                    break
                                else:
                                    _le_last_click = _clicked_idx
                                    cursor = _clicked_idx
                                    _render()
                elif key in ('UP', 'k'):
                    if items: cursor = (cursor - 1) % len(items)
                    _le_last_click = None
                    _render()
                elif key in ('DOWN', 'j'):
                    if items: cursor = (cursor + 1) % len(items)
                    _le_last_click = None
                    _render()

                elif key == 'a' and not fixed_rows:
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
                    # Start on the first non-locked column.
                    edit_col = 0
                    if locked_cols:
                        while edit_col < num_cols and edit_col in locked_cols:
                            edit_col += 1
                        if edit_col >= num_cols:
                            edit_col = 0  # all cols locked — allow no editing
                    edit_backup = items[cursor]

                    if num_cols > 1:
                        curr = items[cursor]
                        i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                        while len(i_vals) < num_cols: i_vals.append("")
                        edit_buf = list(str(i_vals[edit_col]))
                    else:
                        edit_buf = list(str(items[cursor]))

                    edit_pos = len(edit_buf)
                    barrel_hints = _get_cell_hints()
                    cur_val = "".join(edit_buf)
                    if len(barrel_hints) >= 2 and cur_val in barrel_hints:
                        barrel_mode = True
                        barrel_idx = barrel_hints.index(cur_val)
                    else:
                        barrel_mode = False
                        barrel_idx = 0
                    _render()

                elif key in ('d', 'BACKSPACE', 'DELETE') and items and not fixed_rows:
                    items.pop(cursor)
                    if items:
                        cursor = min(cursor, len(items) - 1)
                    else:
                        cursor = 0
                    _render()

                elif key == 'K' and items and not fixed_rows and cursor > 0:
                    items[cursor - 1], items[cursor] = items[cursor], items[cursor - 1]
                    cursor -= 1
                    _le_last_click = None
                    _render()

                elif key == 'J' and items and not fixed_rows and cursor < len(items) - 1:
                    items[cursor + 1], items[cursor] = items[cursor], items[cursor + 1]
                    cursor += 1
                    _le_last_click = None
                    _render()

                elif key == 'ENTER':
                    result = items
                    break

                elif key in ('i', 'f'):
                    if key == 'i':
                        if num_cols > 1:
                            template = (
                                f"# One entry per line: "
                                + " : ".join(h.lower() for h in headers)
                                + "\n# Example:\n"
                                + " : ".join(h.lower() for h in headers)
                                + "\n"
                            )
                        else:
                            template = "# One entry per line\n"
                        _restore_term_attrs(fd, old)
                        text_input = system_editor_edit(initial_text=template)
                        _set_raw(fd)
                    else:
                        # Path prompt (with completion), then auto-detect the format.
                        # Clear to a fresh screen first — path() renders inline from
                        # the cursor, so without this it draws over the list and spills.
                        _restore_term_attrs(fd, old)
                        sys.stdout.write("\033[H\033[3J\033[J")
                        sys.stdout.flush()
                        file_path = path("Import from file:")
                        _set_raw(fd)
                        text_input = None
                        if file_path:
                            try:
                                with open(os.path.expanduser(file_path), encoding='utf-8') as _fp:
                                    text_input = _fp.read()
                            except OSError:
                                ui_utils.show_status(f"Couldn't read {file_path}")
                                text_input = None
                    if not _IS_WINDOWS:
                        sys.stdout.write("\033[?1000h\033[?1006h")   # re-arm mouse
                    sys.stdout.write("\033[H\033[3J\033[J")
                    sys.stdout.flush()
                    w.anchor_reset()
                    if text_input:
                        items.extend(_parse_import_rows(text_input, headers))
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
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
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

    # Handle partial ISO dates
    if len(parts) == 1 and len(parts[0]) == 4 and parts[0].isdigit():
        y = int(parts[0])
        return (y, 1, 1) if _validate_date(y, 1, 1) else None
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        y, m = int(parts[0]), int(parts[1])
        if y > 1900 and 1 <= m <= 12:
            return (y, m, 1)

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
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

        # Month/Year display
        month_name = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][m]

        lines.append(f"  {C.BOLD}{month_name} {y}{C.RESET}")

        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

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

        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

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
    # Strip any trailing timezone (Z or ±HH:MM / ±HHMM) before parsing.
    _clean = re.sub(r'(Z|[+-]\d{2}:?\d{2})$', '', initial.strip())
    _sep = 'T' if 'T' in _clean else (' ' if ' ' in _clean else None)
    date_str, time_str = _clean.split(_sep, 1) if _sep else (_clean, "")

    parsed = _parse_date(date_str) if date_str else None
    if parsed:
        year, month, cursor_day = parsed
    else:
        _today = datetime.date.today()
        year, month, cursor_day = _today.year, _today.month, _today.day

    day_mode = False

    def _digits_only(s: str) -> str:
        return re.sub(r'\D.*', '', s)   # keep only leading digit run

    t_parts = time_str.split(':') if time_str else []
    _h  = _digits_only(t_parts[0]) if t_parts else ""
    _mi = _digits_only(t_parts[1]) if len(t_parts) > 1 else ""
    if len(t_parts) > 2:
        _sp = t_parts[2].split('.')
        _s  = _digits_only(_sp[0]) if _sp else ""
        _ms = _digits_only(_sp[1]) if len(_sp) > 1 else ""
    else:
        _s, _ms = "", ""

    tfields = {
        'hours':   list((_h  or "00")[-2:].zfill(2)),
        'minutes': list((_mi or "00")[-2:].zfill(2)),
        'seconds': list((_s  or "00")[-2:].zfill(2)),
        'millis':  list((_ms or "000")[-3:].zfill(3)),
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
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

        # Date section
        month_name = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month]
        dpfx = C.BOLD if section == 'date' else C.DIM
        lines.append(f"  {dpfx}{month_name} {year}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")
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

        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

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

        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

        if section == 'date':
            if not day_mode:
                h = _hint(("←→", "month"), ("↑↓", "year"), ("tab", "day mode"), ("↵", "save"), ("esc", "back"), ("q", "quit"))
            else:
                h = _hint(("←→↑↓", "navigate"), ("tab", "→ time"), ("↵", "save"), ("esc", "back"), ("q", "quit"))
        elif section == 'time':
            h = _hint(("←→", "cursor"), ("tab", "next field"), ("↵", "save"), ("esc", "back"), ("q", "quit"))

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
        if ms == "000":
            return f"{date_part}T{hms}"
        return f"{date_part}T{hms}.{ms}"

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

            elif key == 'TAB':
                if section == 'date':
                    if not day_mode:
                        day_mode = True
                    else:
                        day_mode = False
                        section = 'time'
                        tcursor = 0
                elif section == 'time':
                    if tcursor < len(torder) - 1:
                        tcursor += 1
                    else:
                        tcursor = 0

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
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

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
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

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
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

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
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")
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
    ("Flat",        {}),
    ("Bass boost",  {31: 6, 62: 5, 125: 3, 250: 1}),
    ("Treble boost",{4000: 2, 8000: 4, 16000: 6}),
    ("V-shape",     {31: 5, 62: 4, 125: 2, 500: -2, 1000: -3, 2000: -2, 8000: 4, 16000: 5}),
    ("Vocal",       {250: 1, 500: 2, 1000: 3, 2000: 3, 4000: 2}),
    ("Loudness",    {31: 6, 62: 4, 8000: 3, 16000: 5}),
    ("Rock",        {31: 4, 62: 3, 125: 2, 500: -1, 1000: -1, 2000: 1, 4000: 3, 8000: 4, 16000: 4}),
    ("Pop",         {125: 2, 250: 3, 500: 4, 1000: 2, 2000: 1, 4000: 1}),
    ("Jazz",        {31: 3, 62: 2, 500: 1, 1000: 2, 2000: 2, 4000: 1, 8000: 2}),
    ("Classical",   {31: 3, 62: 2, 125: 1, 4000: 1, 8000: 2, 16000: 3}),
    ("Electronic",  {31: 5, 62: 4, 125: 2, 1000: -1, 4000: 2, 8000: 3, 16000: 4}),
    ("Hip-hop",     {31: 6, 62: 5, 125: 3, 250: 1, 1000: -1, 2000: 1, 4000: 2}),
    ("R&B",         {31: 4, 62: 4, 125: 2, 500: 2, 1000: 1, 2000: 2, 4000: 2, 8000: 3}),
    ("Acoustic",    {62: 2, 125: 3, 250: 3, 500: 2, 1000: 2, 2000: 2, 4000: 1}),
    ("Dance",       {31: 4, 62: 5, 125: 3, 500: -1, 1000: -1, 4000: 3, 8000: 4, 16000: 3}),
    ("Country",     {125: 2, 250: 2, 500: 1, 2000: 2, 4000: 3, 8000: 2}),
    ("Metal",       {31: 5, 62: 4, 125: 1, 500: -2, 1000: -2, 2000: 1, 4000: 4, 8000: 5, 16000: 5}),
    ("Folk",        {125: 2, 250: 3, 500: 2, 1000: 2, 2000: 1, 4000: 1, 8000: 1}),
    ("Latin",       {31: 3, 62: 2, 250: 2, 500: 2, 1000: 2, 2000: 3, 4000: 2, 8000: 2}),
    ("Speech",      {250: 2, 500: 3, 1000: 4, 2000: 4, 4000: 3, 31: -3, 62: -2, 16000: -2}),
    ("Bass cut",    {31: -6, 62: -5, 125: -3, 250: -1}),
    ("Treble cut",  {4000: -2, 8000: -4, 16000: -6}),
    ("Piano",       {62: 2, 125: 2, 250: 1, 500: 1, 1000: 2, 2000: 3, 4000: 3, 8000: 2, 16000: 1}),
    ("Night mode",  {31: -4, 62: -3, 125: -2, 4000: -1, 8000: -2, 16000: -3}),
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
        f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}",
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


_RVA2_GAIN_MAX = 12.0
_RVA2_STEP     = 0.5
_RVA2_COARSE   = 3.0


def _rva2_render_lines(gain: float, message: str) -> list[str]:
    """Narrow vertical gain meter: 1 row per dB, half-block for 0.5 dB precision.

    Each row at integer `db` is centered on that dB value and spans ±0.5 dB:
      Boost rows (db > 0): bar fills upward; ▄ lights first (bottom half, at gain ≥ db−0.5),
                           then █ when gain ≥ db.
      Cut rows  (db < 0): bar fills downward; ▀ lights first (top half, at gain ≤ db+0.5),
                           then █ when gain ≤ db.
    Every 0.5 dB step changes a visible half-block, so no increment is invisible.
    """
    out = [
        f"  {C.DIM}{message}{C.RESET}",
        f"{C.DIM}{'─' * 20}{C.RESET}",
    ]

    for db in range(int(_RVA2_GAIN_MAX), -int(_RVA2_GAIN_MAX) - 1, -1):
        lbl = (f"{db:+d}" if db != 0 else " 0") if db % 6 == 0 else ""

        if db == 0:
            bar = f"{C.DIM}──{C.RESET}"
        elif db > 0:
            if gain >= db:
                bar = f"{C.ACCENT}██{C.RESET}"
            elif gain >= db - 0.5:
                bar = f"{C.ACCENT}▄▄{C.RESET}"   # bottom half: bar just entered this row
            else:
                bar = "  "
        else:  # db < 0
            if gain <= db:
                bar = f"{C.DIM}██{C.RESET}"
            elif gain <= db + 0.5:
                bar = f"{C.DIM}▀▀{C.RESET}"       # top half: bar just entered this row
            else:
                bar = "  "

        out.append(f"  {C.DIM}{lbl:>3}{C.RESET} {bar}")

    out.append("")
    return out


def rva2_edit(message: str = "Volume adjustment:", gain: float = 0.0) -> float | None:
    """Interactive vertical gain meter for an RVA2 frame.
    Returns the chosen gain in dB, or None if cancelled.
    """
    gain = max(-_RVA2_GAIN_MAX, min(_RVA2_GAIN_MAX, gain))
    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    def _clamp(g: float) -> float:
        return max(-_RVA2_GAIN_MAX, min(_RVA2_GAIN_MAX, round(g * 2) / 2))

    def _render():
        lines = _rva2_render_lines(gain, message)
        lines.append(f"  {C.ACCENT}▸{C.RESET} {C.BOLD}{gain:+.1f} dB{C.RESET}")
        lines.extend(_hint(
            ("↑↓", "adjust"), ("⇞⇟", "±3 dB"), ("0", "zero"), ("↵", "save"), ("q", "quit"),
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

            if key == 'ENTER':
                result = gain; break
            elif key == 'ESC':
                result = None; break
            elif key in ('q', 'Q'):
                _state.QUIT_REQUESTED = True
                result = gain; break
            elif key in ('UP', 'k', 'SCROLL_UP'):
                gain = _clamp(gain + _RVA2_STEP); _render()
            elif key in ('DOWN', 'j', 'SCROLL_DOWN'):
                gain = _clamp(gain - _RVA2_STEP); _render()
            elif key == 'PGUP':
                gain = _clamp(gain + _RVA2_COARSE); _render()
            elif key == 'PGDN':
                gain = _clamp(gain - _RVA2_COARSE); _render()
            elif key == '0':
                gain = 0.0; _render()

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result


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


_EDITOR_FALLBACKS = ['micro', 'nano', 'vim', 'vi', 'emacs']


def _find_editor() -> str | None:
    """Return the editor to use: $EDITOR if set and found, else first available fallback."""
    import shutil
    env_editor = os.environ.get('EDITOR', '').strip()
    if env_editor:
        cmd = env_editor.split()[0]
        if shutil.which(cmd):
            return env_editor
    for ed in _EDITOR_FALLBACKS:
        if shutil.which(ed):
            return ed
    return None


def system_editor_edit(initial_text: str) -> str | None:
    """Open system editor for long text."""
    with tempfile.NamedTemporaryFile(suffix=".txt", mode='w+', encoding='utf-8', delete=False) as tf:
        tf.write(initial_text)
        temp_path = tf.name
    try:
        editor = _find_editor() or 'nano'
        subprocess.run(editor.split() + [temp_path], check=True)
        with open(temp_path, 'r', encoding='utf-8') as f:
            result = f.read().strip()
        return result if result else None
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
