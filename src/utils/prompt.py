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
import datetime
import calendar as cal
import tempfile
import time
import subprocess
from typing import Any, Callable, Literal, overload

from src.utils.prompt_core import (
    _IS_WINDOWS, _COLUMNS_MAX_WIDTH, _EDGE_MARGIN,
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress,
    _col, _hint, _render_status_bar,
    Choice, Column,
    _cell_text, _style_cell, _render_cell_segments, _table_widths, _render_table_row,
    separator, _split_columns, _clip_ansi, _render_select_columns, _style_checkbox_label, _norm,
    _read_key, _read_key_raw,
    _visible_rows, _cols, _rows, _hint_lines, _wrap_bordered_input_lines,
    _Widget,
    add_hint_click_cells, now_playing_click_action, _hint_pin_target, screen_paint, screen_invalidate, screen_takeover_next,
)
from src.utils import ui_utils
from src.utils import datetime_parse as dtp
from src import state as _state
from src.state import QuitToTerminal
C = ui_utils.Colors

# Per-edit "raw text ↔ smart widget" toggle (#62). prompt_for_value enables the
# flag around a value edit; the value widgets then treat Ctrl-T as a request to
# switch modes by returning MODE_TOGGLE, and advertise it in their hint bar.
MODE_TOGGLE = object()


def _with_toggle_hint(pairs, label: str = 'raw text'):
    """Append the Ctrl-T hint to a widget's hint bar while the per-edit raw-text
    toggle is live.

    Every widget that *accepts* ^t advertises it through this, so the key is never
    silently available on one screen and absent from the bar on another. "^t"
    renders as a clickable hint key too (it synthesises the real control char).
    """
    return list(pairs) + [("^t", label)] if _value_toggle_enabled else list(pairs)

_MODE_TOGGLE_KEY = '\x14'          # Ctrl-T
_value_toggle_enabled = False
_toggle_hint_label = 'widget'      # what text()'s ^t hint calls the alternate mode
_toggle_carry: str | None = None   # in-progress text buffer handed across a Ctrl-T toggle

# Global playback hotkeys, live from any list/menu while background audio plays
# (#14). Ctrl-O reopens the full player; Ctrl-P/N/B are transport. Routed through
# registered callbacks so prompt need not import the playback layer.
_PLAYER_KEY    = '\x0f'            # Ctrl-O — open the full player view
_PLAYPAUSE_KEY = '\x10'           # Ctrl-P — play / pause
_NEXT_KEY      = '\x0e'           # Ctrl-N — next track
_PREV_KEY      = '\x02'           # Ctrl-B — previous track
_player_opener = None
_transport_handler = None


def set_player_opener(fn) -> None:
    """Register a ``callable()`` that opens the background player's full view."""
    global _player_opener
    _player_opener = fn


# --- shared widget chrome -------------------------------------------------
# Every screen owes the user the same four things: a hint bar pinned above the
# miniplayer and status bar so its keys never move, those keys clickable, the
# background-audio transport keys listed whenever the miniplayer is up, and
# clicks on the miniplayer box itself doing something. These two helpers are
# that contract in one place — `select` grew all of it first and the rest of the
# app had drifted, each widget missing a different subset.

CHROME_HANDLED = object()      # the key was consumed; carry on with the loop
CHROME_REDRAW = object()       # consumed, and the caller should repaint fully


def chrome_hint_pairs(pairs) -> list:
    """A widget's hint pairs plus the transport keys, while audio is playing.

    Only keys that will actually do something are advertised: the transport trio
    needs a handler installed and ^O needs a player to reopen. `unboxed` covers
    a terminal too narrow to draw the now-playing box — the keys are still live,
    so they are still listed.
    """
    items = list(pairs.items()) if isinstance(pairs, dict) else [tuple(p) for p in pairs]
    if ui_utils.now_playing_active() or ui_utils.now_playing_unboxed():
        if _transport_handler is not None:
            items += [("^P", "play/pause"), ("^N/^B", "next/prev")]
        if _player_opener is not None:
            items += [("^O", "player")]
    return items


def chrome_hint_lines(pairs, *, extra: str = "") -> list:
    """The hint bar as rendered lines — widgets that size a viewport need the
    row count before they lay their content out."""
    return _hint(*chrome_hint_pairs(pairs), extra=extra).splitlines()


def append_chrome(out: list, pairs, cells: dict, *, extra: str = "",
                  pin: bool = True) -> list:
    """Append the hint bar to a widget's rendered `out` lines, in place.

    Pads down to :func:`_hint_pin_target` so the bar sits just above the
    miniplayer + status bar and its keys keep the same screen position across
    redraws — otherwise a repeated click chases the bar as the content changes
    height. Records each bright key's screen cell in `cells` for
    :func:`consume_chrome` to look up.
    """
    items = chrome_hint_pairs(pairs)
    hint_lines = _hint(*items, extra=extra).splitlines()
    if pin:
        filler = _hint_pin_target() - len(out) - len(hint_lines)
        if filler > 0:
            out.extend([""] * filler)
    out.extend(f"{' ' * ui_utils.MARGIN_H}{h}" for h in hint_lines)

    cells.clear()
    if hint_lines:
        start = len(out) - len(hint_lines)
        for k in range(len(hint_lines)):
            # `_Widget.render` lays line j at terminal row anchor(1) + MARGIN_V + j.
            add_hint_click_cells(cells, out[start + k],
                                 1 + ui_utils.MARGIN_V + (start + k), items)
    return out


def consume_chrome(key: str, cells: dict):
    """Handle a transport key, a miniplayer click, or a click on a hint key.

    Returns :data:`CHROME_HANDLED` when the key is fully dealt with,
    :data:`CHROME_REDRAW` when the caller should also repaint, the synthesised
    key string when a hint was clicked (replay it through the widget's own
    switch), or None when the key is not ours.
    """
    if key == _PLAYER_KEY and _player_opener is not None:
        _player_opener()
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")   # the player took the mouse
        sys.stdout.flush()
        return CHROME_REDRAW
    if key == _PLAYPAUSE_KEY and _transport_handler is not None:
        _transport_handler('playpause')
        return CHROME_HANDLED
    if key == _NEXT_KEY and _transport_handler is not None:
        _transport_handler('next')
        return CHROME_HANDLED
    if key == _PREV_KEY and _transport_handler is not None:
        _transport_handler('prev')
        return CHROME_HANDLED

    if isinstance(key, str) and key.startswith('MOUSE_CLICK:'):
        parts = key.split(':')
        try:
            row = int(parts[2])
            col = int(parts[3]) if len(parts) > 3 else 1
        except (IndexError, ValueError):
            return None
        act = now_playing_click_action(row, col)
        if act == 'open' and _player_opener is not None:
            _player_opener()
            if not _IS_WINDOWS:
                sys.stdout.write("\033[?1000h\033[?1006h")
            sys.stdout.flush()
            return CHROME_REDRAW
        if act in ('playpause', 'next', 'prev') and _transport_handler is not None:
            _transport_handler(act)
            return CHROME_HANDLED
        hit = cells.get((row, col))
        if hit is not None:
            return hit                      # replay the clicked hint's key
    return None


def enable_mouse() -> None:
    """Turn on click + scroll reporting for a widget that wants clickable hints."""
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()


def disable_mouse() -> None:
    """Turn click reporting back off on the way out."""
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()


def set_transport_handler(fn) -> None:
    """Register ``callable(action)`` for the global transport hotkeys, where
    action is 'playpause', 'next', or 'prev'. Kept as a registered callback so
    prompt need not import the playback layer (mirrors set_player_opener)."""
    global _transport_handler
    _transport_handler = fn


_notification_opener = None


def set_notification_opener(fn) -> None:
    """Register a ``callable()`` that opens the activity/notification centre —
    invoked when the status-bar ● beacon is clicked."""
    global _notification_opener
    _notification_opener = fn


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
           row_actions: dict[str, Callable[[Any], None]] | None = ...,
           row_action_hints: dict[str, str] | None = ...,
           allow_back: bool = ...,
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
           row_actions: dict[str, Callable[[Any], None]] | None = ...,
           row_action_hints: dict[str, str] | None = ...,
           allow_back: bool = ...,
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
           row_actions: dict[str, Callable[[Any], None]] | None = None,
           row_action_hints: dict[str, str] | None = None,
           allow_back: bool = True,
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
        row_actions: key→callback(current row value) map. Pressing the key runs
            the callback against the highlighted row and stays in the list (like
            on_inspect, but any number of keys) — e.g. queue the current track.
        row_action_hints: key→label map surfaced in the hint bar for row_actions.
        allow_back: when False, the cancel keys (←/b/h/Esc) are ignored so the
            list can only move forward (Enter) or quit (q) — used for top-level
            menus that have nowhere to go back to.
    """
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
        """Lock selection to the category of the first checked item, disabling every non-matching row."""
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
    # Toggle-all ('a') is offered only where it can't misbehave: multi-select with
    # no category interlock and no caller shortcut already bound to 'a'.
    _toggle_all_ok = multi and interlock_category_callback is None and not (shortcuts and ('a' in shortcuts or 'A' in shortcuts))
    _back_hint = {"←/b/esc": "back"} if allow_back else {}
    if multi:
        base_hints = {"↑↓": "move", "space": "toggle", **_back_hint, "q": "quit app", "↵": "confirm"}
        if _toggle_all_ok:
            base_hints = {"↑↓": "move", "space": "toggle", "a": "all",
                          **_back_hint, "q": "quit app", "↵": "confirm"}
    else:
        base_hints = {"↑↓": "move", **_back_hint, "q": "quit app", "↵": "confirm"}

    if extra_hints:
        combined_hints = {**extra_hints, **base_hints}
    else:
        combined_hints = base_hints
    # Row-action keys (e.g. queue the current track) sit with the other action
    # hints, before the navigation keys.
    if row_action_hints:
        combined_hints = {**{k: v for k, v in combined_hints.items() if k not in base_hints},
                          **row_action_hints, **base_hints}

    _last_hlen = [0]
    # Maps a visible item index → its ANSI-stripped rendered text, so a mouse
    # click can tell whether it landed on a printed character or blank space.
    _row_plain: dict[int, str] = {}
    # Maps an absolute (row, col) on a hint line → the key that clicking that
    # bright glyph should replay through the normal key handling below.
    _hint_cells: dict[tuple[int, int], str] = {}

    def _plain(s: str) -> str:
        return re.sub(r'\x1b\[[0-9;]*[mGKFHF]', '', s)

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _lines():
        nonlocal viewport
        cols    = _cols()
        # Refresh the now-playing box height up front so this frame's row budget
        # (vis) and hint pinning match the box that render() will actually draw —
        # otherwise a just-appeared box paints over the pinned hints until the
        # next redraw (hints missing until you click/navigate).
        ui_utils.now_playing_lines(ui_utils.get_terminal_width())
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

        # The transport keys are surfaced here whenever background audio is
        # playing (recomputed each render so they appear/vanish live) — see
        # `chrome_hint_pairs`.
        hint_lines = chrome_hint_lines(combined_hints, extra=layout_constraint)

        # Non-item lines this widget emits: header + message + the two
        # above/below indicator rows (always present) + hints.
        fixed_overhead = len(h_lines) + len(hint_lines) + 3
        vis     = max(2, _visible_rows() - fixed_overhead)

        n       = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1
        # Growing the window (or deleting rows) leaves the viewport further down
        # than it needs to be — the list stayed scrolled, showing "N above" with
        # blank space below, until you navigated. Pull it back so the last row of
        # the list sits on the last visible row at most.
        viewport = max(0, min(viewport, n - vis))

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
            vis_cells = [it.cells for it in items[viewport:viewport + vis] if it.cells]
            col_widths = _table_widths(rows_cells, columns, eff,
                                       pointer_w=6 if multi else 4, right_margin=_EDGE_MARGIN,
                                       visible_cells=vis_cells)

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
        # Inset the hint block by the left margin so it never hugs an edge; _hint
        # centres within _cols() (= width-2*MARGIN_H), so this makes it symmetric.
        # Pin the hint bar to the bottom (just above the miniplayer + status) so
        # its keys keep a fixed screen position across redraws / list sizes.
        append_chrome(out, combined_hints, _hint_cells, extra=layout_constraint)
        # Hard guarantee: no rendered line ever exceeds the terminal width, so
        # the list can never wrap no matter how narrow the window is.
        _w = ui_utils.get_terminal_width()          # once per frame, not per line
        return [_clip_ansi(line, _w) for line in out]

    result = None
    _sel_last_click: int | None = None
    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        screen_takeover_next()   # paint over the previous screen, no flash
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                w.render(_lines())
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # Transport keys, clicks on the now-playing box, and clicks on our own
            # hint glyphs are all handled once, here, before the switch below:
            # box → transport/open, hint → replay its key.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _sel_last_click = None; w.anchor_reset(); w.render(_lines()); continue
            if _ch is not None:
                key = _ch                # replay the hint's key through the switch
            if   key == 'CTRL_C':                break
            elif key == 'FOCUS_IN':
                # Regained focus: repaint fully in case a background track change
                # (or the terminal not painting us while unfocused) left the list
                # or now-playing box stale — no click needed (#14). A refresh, not
                # a clear: the layout is still valid, so blanking the screen first
                # would just flash.
                _sel_last_click = None; w.refresh(); w.render(_lines())
            elif key == 'UP':           cursor = _step(cursor, -1);          _sel_last_click = None; w.render(_lines())
            elif key == 'DOWN':           cursor = _step(cursor, 1);           _sel_last_click = None; w.render(_lines())
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
            elif key in ('a', 'A') and _toggle_all_ok:
                # Toggle every selectable row at once: check all, or clear all if
                # everything is already checked.
                targets = [it for it in items if not it.disabled]
                make_checked = any(not it.checked for it in targets)
                for it in targets:
                    it.checked = make_checked
                selectable[:] = [i for i, x in enumerate(items) if not x.disabled or x.checked]
                _sel_last_click = None
                w.render(_lines())
            elif key in ('ENTER', 'RIGHT'):
                if multi:
                    result = [it.value for it in items if it.checked]; break
                elif not items[cursor].disabled:
                    result = items[cursor].value; break
            elif key in ('LEFT', 'b', 'ESC'):
                if allow_back:
                    result = None; break
                # Top-level menu: no back/cancel — only forward or quit.
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
            elif row_actions and key in row_actions and not items[cursor].disabled:
                # Act on the highlighted row (e.g. queue this track) and stay in
                # the list — the callback shows its own status; we just redraw.
                row_actions[key](items[cursor].value)
                _sel_last_click = None
                w.render(_lines())
            elif shortcuts and key in shortcuts:  result = shortcuts[key]; break
            elif key == 'SCROLL_UP':             cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_DOWN':           cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key.startswith('MOUSE_CLICK:'):
                parts = key.split(':')
                r, col = int(parts[2]), int(parts[3]) if len(parts) > 3 else 1
                # Click on the status-bar row's pulsing ● beacon → open the
                # activity centre (only while something is actually running).
                if (_notification_opener is not None
                        and r >= ui_utils.get_terminal_height()
                        and ui_utils.has_background_tasks()):
                    _notification_opener()
                    if not _IS_WINDOWS:
                        sys.stdout.write("\033[?1000h\033[?1006h")
                    sys.stdout.flush()
                    _sel_last_click = None
                    w.anchor_reset()
                    w.render(_lines())
                    continue
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
                count_of: Callable[[], int] | None = None,
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
    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    query: list[str] = list(initial_query)
    qpos             = len(query)
    items: list      = list(provider("".join(query))) if query else []
    cursor           = 0
    viewport         = 0

    base_hints = {"type": "search", "↑↓": "results", "esc": "back", "↵": "confirm"}
    if on_cycle is not None:
        base_hints["tab"] = "scope"
    hints = {**(extra_hints or {}), **base_hints}
    # Maps an absolute (row, col) on a hint line → the key clicking it replays.
    _hint_cells: dict[tuple[int, int], str] = {}

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
        """Re-run the provider for the current query and reset cursor/viewport onto the new results."""
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
        ui_utils.now_playing_lines(width)   # refresh box height (see select._lines)
        out = _header_lines()

        qtext = "".join(query)
        b, a = qtext[:qpos], qtext[qpos:]
        out.append(f"  {C.DIM}{message}{C.RESET} {b}{C.ACCENT}▏{C.RESET}{a}")
        count = ("type to search…" if not qtext else
                 ui_utils.plural(len(items) if count_of is None else count_of(), "result"))
        out.append(f"  {C.DIM}{count}{C.RESET}")

        hint_lines = chrome_hint_lines(hints)
        # out already holds header + message + count; +2 for the above/below rows.
        overhead = len(out) + len(hint_lines) + 2
        vis = max(2, _visible_rows() - overhead)

        n = len(items)
        if cursor < viewport:
            viewport = cursor
        elif cursor >= viewport + vis:
            viewport = cursor - vis + 1
        # Growing the window (or deleting rows) leaves the viewport further down
        # than it needs to be — the list stayed scrolled, showing "N above" with
        # blank space below, until you navigated. Pull it back so the last row of
        # the list sits on the last visible row at most.
        viewport = max(0, min(viewport, n - vis))
        out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}" if viewport > 0 else "")

        eff = min(cols, _COLUMNS_MAX_WIDTH)
        col_widths: list = []
        if columns:
            rows_cells = [it.cells for it in items if it.cells]
            if rows_cells:
                vis_cells = [it.cells for it in items[viewport:viewport + vis] if it.cells]
                col_widths = _table_widths(rows_cells, columns, eff,
                                           pointer_w=4, right_margin=_EDGE_MARGIN,
                                           visible_cells=vis_cells)

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
        # Inset the hint block by the left margin so it never hugs an edge; _hint
        # centres within _cols() (= width-2*MARGIN_H), so this makes it symmetric.
        _filler = _hint_pin_target() - len(out) - len(hint_lines)
        if _filler > 0:
            out.extend([""] * _filler)
        append_chrome(out, hints, _hint_cells, pin=False)
        return [_clip_ansi(line, width) for line in out]

    result = None
    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        screen_takeover_next()   # paint over the previous screen, no flash
        w.render(_lines())

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                w.render(_lines())
                continue
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)

            # Transport keys, clicks on the now-playing box, and clicks on our own
            # hint glyphs — handled once here, before the switch below.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); w.render(_lines()); continue
            if _ch is not None:
                key = _ch                # replay the hint's key through the switch

            if key == 'CTRL_C':
                raise QuitToTerminal()
            elif key == 'ESC':
                result = None
                break
            elif key in ('TAB', 'BACKTAB') and on_cycle is not None:
                # Cyclers that take a direction get -1 for Shift+Tab; older
                # no-argument ones just cycle forward either way.
                try:
                    on_cycle(-1 if key == 'BACKTAB' else 1)
                except TypeError:
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
    """Yes/no prompt; y/n or Enter (accepting `default`) answers, Ctrl-C answers no.
    The y / n / ↵ hints are clickable."""
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    w      = _Widget(fd)
    result = default
    _hint_cells: dict[tuple[int, int], str] = {}

    def _render():
        dflt = "yes" if default else "no"
        pairs = [("y", "yes"), ("n", "no"), ("↵", f"default ({dflt})"),
                 ("esc", "back")]
        head = [
            f"  {C.DIM}{message}{C.RESET}",
            f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}",
        ]
        lines = list(head)
        append_chrome(lines, pairs, _hint_cells)
        w.render(lines)

    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        _render()
        while True:
            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if key.startswith('MOUSE_CLICK:'):
                _mp = key.split(':')
                _mr = int(_mp[2]); _mc = int(_mp[3]) if len(_mp) > 3 else 1
                _hk = _hint_cells.get((_mr, _mc))
                if _hk is None:
                    continue             # modal: ignore clicks off the y/n/↵ hints
                key = _hk                # replay the hint's key
            if   key == 'CTRL_C':    result = False; break
            # Esc backs out of every other screen, so it must do something here
            # too — cancelling a yes/no question means "no".
            elif key == 'ESC':       result = False; break
            elif key == 'ENTER':     result = default; break
            elif key.lower() == 'y': result = True;  break
            elif key.lower() == 'n': result = False; break
    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def text(message: str, default: str = "") -> str | None:
    """Free-text line editor with cursor movement and wrapping; Enter returns the buffer, Ctrl-C cancels."""
    buf    = list(default)
    pos    = len(buf)
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    result = None
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    # Track how many physical lines were drawn to clear them later
    prev_lines = 0

    def _render():
        nonlocal prev_lines
        cols = _cols()
        content = "".join(buf)
        # Frame = '  '(mh) + '│ ' + content + ' │'; content = cols-4 makes the
        # frame span exactly the inter-margin width for an even 2/2 margin.
        content_width = max(1, cols - 4)

        wrapped_lines = _wrap_bordered_input_lines(content, content_width)
        pre_lines = _wrap_bordered_input_lines(content[:pos], content_width)
        cursor_row = max(0, len(pre_lines) - 1)
        cursor_col = len(pre_lines[-1]) if pre_lines else 0

        # This prompt owns the screen (it full-clears on entry), so it is laid
        # out absolutely like every other widget: message, input frame, then the
        # hint bar pinned above the miniplayer. Unlike the others it keeps a real
        # terminal caret inside the frame rather than drawing a block, so the
        # caret is re-homed by absolute position after the bar is written.
        # No "(^t widget)" suffix on the message: ^t is in the hint bar below,
        # like every other key. The title says what the screen is, not how to
        # leave it.
        out = [f"  {C.DIM}{message}{C.RESET}"]
        box_first = len(out)
        for line in wrapped_lines:
            out.append(f"  {C.DIM}│{C.RESET} {line:<{content_width}} {C.DIM}│{C.RESET}")

        pairs = [("↵", "save"), ("esc", "back")]
        if _value_toggle_enabled:
            pairs.append(("^t", _toggle_hint_label))
        append_chrome(out, pairs, _hint_cells)

        # One diffed frame (no full erase, no newlines): only the rows that
        # actually changed are written, so typing doesn't repaint the screen.
        frame = {i + 1: line for i, line in enumerate([""] * ui_utils.MARGIN_V + out)}
        for r in range(len(frame) + 1, prev_lines + ui_utils.MARGIN_V + 1):
            frame[r] = ""
        # Caret: margin(2) + '│'(1) + ' '(1) → first content column is 5.
        caret_row = 1 + ui_utils.MARGIN_V + box_first + cursor_row
        screen_paint(frame, cursor=(caret_row, cursor_col + 5))

        prev_lines = len(out)
        _render_status_bar()

    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                _render()
                continue
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch

            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                global _toggle_carry
                _toggle_carry = "".join(buf)
                return MODE_TOGGLE  # type: ignore[return-value]
            if   key == 'CTRL_C':             result = None;         break
            elif key == 'ESC':                result = None;         break
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
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui_utils.clear_screen()
        sys.stdout.write(C.HIDE)   # caret was ours; don't leave it blinking
        sys.stdout.flush()

    return result


def path(message: str, default: str = "") -> str | None:
    """Filesystem-path editor with Tab-cycling autocomplete against the current directory listing."""
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
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _completions(current: str) -> list:
        """List non-hidden entries in `current`'s directory matching its basename stub, for Tab completion."""
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
        # cols-4 makes the '  │ … │' frame span the inter-margin width (even 2/2).
        max_w   = max(1, cols - 4)

        if pos > max_w:
            display  = content[pos - max_w: pos]
            disp_pos = max_w
        else:
            display  = content[:max_w]
            disp_pos = pos

        cursor_col = len(prefix) + disp_pos

        render_stream = [
            f"  {C.DIM}{message}{C.RESET}\r\n",
            f"  {C.DIM}│{C.RESET} {display:<{max_w}} {C.DIM}│{C.RESET}"
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

        _prev_rendered = _last_rendered_lines
        _last_rendered_lines = lines_count

        # Laid out absolutely, like every other screen: the completion tooltip
        # still rides just under the frame, but the hint bar is pinned above the
        # miniplayer instead of trailing whatever the tooltip left behind.
        out = "".join(render_stream).split("\r\n")
        pairs = [("↵", "save"), ("tab/⇧tab", "complete"), ("esc", "back")]
        append_chrome(out, pairs, _hint_cells)

        # One diffed frame — see text() above.
        frame = {i + 1: line for i, line in enumerate([""] * ui_utils.MARGIN_V + out)}
        for r in range(len(frame) + 1, _prev_rendered + ui_utils.MARGIN_V + 2):
            frame[r] = ""
        # Caret sits on the frame row, one below the message.
        screen_paint(frame, cursor=(1 + ui_utils.MARGIN_V + 1, cursor_col + 1))
        _render_status_bar()

    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _tab_matches = _completions("".join(buf))
        _render()

        while True:
            if ui_utils.consume_resize(): _render()
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch

            if key == 'CTRL_C':
                result = None; break
            elif key == 'ESC':
                result = None; break
            elif key == 'ENTER':
                result = "".join(buf); break

            elif key in ('TAB', 'BACKTAB'):
                current_text = "".join(buf)
                stub = os.path.basename(current_text) if (current_text and not current_text.endswith('/')) else ""

                visible_matches = [m for m in _tab_matches if os.path.basename(m.rstrip('/')).startswith(stub)] if stub else _tab_matches

                if visible_matches:
                    if key == 'BACKTAB':
                        _tab_index -= 2      # step back past the one just offered
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
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui_utils.clear_screen()
        sys.stdout.write(C.HIDE)   # caret was ours; don't leave it blinking
        sys.stdout.flush()

    return result

# ---------------------------------------------------------------------------
# Timestamp cells — a split date/time field inside a list_edit table.
#
# The cell is a fixed mask.  Every slot that is still a placeholder letter shows
# dim, so the shape of what you are filling in is always on screen and only the
# digits have to be typed.  One caret walks the whole mask and steps over the
# separators, which is what makes the arrow keys carry from the end of one part
# straight into the next instead of needing Tab between them.
# ---------------------------------------------------------------------------
_TS_MASK  = "YYYY-MM-DD HH:MM:SS"
_TS_SLOTS = tuple(i for i, ch in enumerate(_TS_MASK) if ch.isalpha())
# Where each part starts, as an index into _TS_SLOTS: Y, M, D, h, m, s.
_TS_PARTS = ((0, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 14))


def _ts_write(buf: list, start_slot: int, digits: str) -> None:
    """Write `digits` into consecutive mask slots from `start_slot`."""
    for k, ch in enumerate(digits):
        idx = start_slot + k
        if idx < len(_TS_SLOTS):
            buf[_TS_SLOTS[idx]] = ch


def _ts_buffer(value: str) -> list:
    """A mask buffer seeded from an existing cell value (blank where unknown)."""
    buf = list(_TS_MASK)
    parsed = dtp.parse_datetime(value) if value else None
    if parsed and parsed.date:
        d = parsed.date
        if parsed.precision == 'year':
            _ts_write(buf, 0, f"{d.year:04d}")
        elif parsed.precision == 'month':
            _ts_write(buf, 0, f"{d.year:04d}{d.month:02d}")
        else:
            _ts_write(buf, 0, f"{d.year:04d}{d.month:02d}{d.day:02d}")
            if parsed.time:
                _ts_write(buf, 8, parsed.time.replace(':', ''))
    return buf


def _ts_digits(buf: list) -> str:
    """The 14 mask slots as digits, with a space wherever one is still unfilled."""
    return ''.join(buf[i] if buf[i].isdigit() else ' ' for i in _TS_SLOTS)


def _ts_value(buf: list) -> str:
    """Assemble the cell's value at whatever precision has actually been filled.

    Leaving the time blank yields a plain date, which is a valid timestamp in its
    own right — the field never forces a time it was not given.
    """
    d = _ts_digits(buf)
    if ' ' in d[0:4]:
        return ''
    if ' ' in d[4:6]:
        return d[0:4]
    if ' ' in d[6:8]:
        return f"{d[0:4]}-{d[4:6]}"
    date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    if ' ' in d[8:12]:
        return date
    if ' ' in d[12:14]:
        return f"{date} {d[8:10]}:{d[10:12]}"
    return f"{date} {d[8:10]}:{d[10:12]}:{d[12:14]}"


def _ts_step(pos: int, delta: int) -> int:
    """Move the caret one editable slot, hopping the separators between parts."""
    slots = _TS_SLOTS
    if pos in slots:
        k = slots.index(pos)
    else:                                   # sitting on a separator — snap inward
        k = 0 if delta > 0 else len(slots) - 1
    k = max(0, min(len(slots) - 1, k + delta))
    return slots[k]


def _render_timestamp_cell(buf: list, pos: int, width: int, active: bool,
                           base: str = '') -> str:
    """Draw a timestamp cell: entered digits bright, unfilled mask slots dim.

    `base` is the styling the surrounding row is already drawn in (the selected
    row's colour + bold).  Every span closes by resetting AND re-asserting it,
    because a bare reset would end the row's own styling too — the dim separator
    after the year would leave the rest of the stamp, and every column after it,
    unstyled.  Same rule the lyric renderer follows for markdown emphasis.
    """
    close = f"{C.RESET}{base}"
    out = []
    for i, ch in enumerate(buf):
        placeholder = not ch.isdigit()
        if active and i == pos:
            out.append(f"{C.INVERT}{C.BOLD}{ch}{close}")
        elif placeholder:
            # Reset before dimming: on the selected row `base` is bold, and
            # bold+dim together is contradictory (terminals disagree on which
            # wins), which left the separators as bold as the digits.
            out.append(f"{C.RESET}{C.DIM}{ch}{close}")
        else:
            out.append(ch)
    text = "".join(out)
    return text + " " * max(0, width - len(buf))


def _render_list_edit_cell(text: str, width: int, is_editing: bool, is_active_col: bool,
                           edit_buf: list[str], edit_pos: int,
                           is_timestamp: bool = False, base: str = '') -> str:
    """Render one table cell, showing the live edit buffer with a cursor block when this is the active editing column.

    `base` is the row's own styling, re-asserted after any span this cell closes
    so the rest of the row keeps it (see :func:`_render_timestamp_cell`).
    """
    if not is_editing or not is_active_col:
        if is_timestamp:
            # Always draw the mask, even for an empty cell: it shows the shape
            # waiting to be filled, and it keeps the cell a fixed width so the
            # columns after it don't slide left on an empty row.
            return _render_timestamp_cell(_ts_buffer(text), -1, width, False, base)
        return ui_utils.truncate_text(text, width)

    if is_timestamp:
        return _render_timestamp_cell(edit_buf, edit_pos, width, True, base)

    buf_str = "".join(edit_buf)
    close = f"{C.RESET}{base}"

    if edit_pos >= len(buf_str):
        display_str = buf_str + f"{C.BACK}█{close}"
    else:
        display_str = buf_str[:edit_pos] + f"{C.INVERT}{C.BOLD}{buf_str[edit_pos]}{close}" + buf_str[edit_pos+1:]

    visible_len = len(buf_str) + (1 if edit_pos >= len(buf_str) else 0)
    padding = max(0, width - visible_len)
    return display_str + (" " * padding)


def _layout_columns(num_cols: int, avail_w: int, col_ratios=None, col_mins=None) -> list:
    """Split `avail_w` across the columns, honouring per-column minimums.

    Ratios (or an even split) set the starting widths; any column below its
    minimum is then raised to it and the difference taken back from whichever
    columns have the most room to spare.  A minimum is what keeps a fixed-shape
    cell — a full timestamp, say — readable at any terminal width while the
    short columns beside it shrink instead.
    """
    if col_ratios and len(col_ratios) == num_cols:
        total = sum(col_ratios) or 1
        widths = [max(1, int(avail_w * r / total)) for r in col_ratios]
        widths[-1] = max(1, avail_w - sum(widths[:-1]))
    else:
        even = avail_w // num_cols
        widths = [even] * (num_cols - 1) + [avail_w - even * (num_cols - 1)]

    if not col_mins:
        return widths
    mins = list(col_mins)[:num_cols] + [0] * max(0, num_cols - len(col_mins))
    mins = [max(0, int(m or 0)) for m in mins]

    if sum(mins) >= avail_w:
        # Too narrow to satisfy every minimum — share it out in their proportion
        # rather than starving the last column to nothing.
        total = sum(mins) or 1
        shared = [max(1, int(avail_w * m / total)) for m in mins]
        # Hand the truncation remainder to the widest column so the row still
        # uses the full width instead of leaving a ragged gap.
        spare = avail_w - sum(shared)
        if spare > 0:
            shared[max(range(num_cols), key=lambda i: shared[i])] += spare
        return shared

    deficit = 0
    for i, m in enumerate(mins):
        if widths[i] < m:
            deficit += m - widths[i]
            widths[i] = m
    while deficit > 0:
        slack = [widths[i] - mins[i] for i in range(num_cols)]
        best = max(range(num_cols), key=lambda i: slack[i])
        if slack[best] <= 0:
            break
        take = min(deficit, slack[best])
        widths[best] -= take
        deficit -= take

    drift = avail_w - sum(widths)
    if drift:
        slack = [widths[i] - mins[i] for i in range(num_cols)]
        target = max(range(num_cols), key=lambda i: slack[i]) if drift < 0 else num_cols - 1
        widths[target] = max(1, widths[target] + drift)
    return widths


def _build_list_edit_lines(
    message: str, items: list, headers: tuple[str, ...],
    cursor: int, viewport: int,
    edit_mode: bool, edit_col: int, edit_buf: list[str], edit_pos: int,
    fixed_rows: bool = False,
    barrel_mode: bool = False, barrel_hints: list[str] | None = None, barrel_idx: int = 0,
    col_ratios: tuple | None = None, col_mins: tuple | None = None,
    col_types: dict | None = None,
) -> tuple[list[str], int, int, int]:
    """Lay out the full list_edit screen — header, column-aligned rows (or barrel-mode cell), hints —
    and report the resulting viewport/visible-row/header-row counts."""
    num_cols = len(headers)
    cols = _cols()
    ui_utils.now_playing_lines(ui_utils.get_terminal_width())   # refresh box height (see select._lines)
    c = cols - 4
    inner = c
    out = []

    if fixed_rows:
        base_hints = {"↑↓": "move", "e": "edit", "i": "import text", "f": "from file", "esc": "back", "↵": "save", "q": "quit app"}
    else:
        base_hints = {"↑↓": "move", "a": "add", "e": "edit", "d": "delete", "K/J": "reorder", "i": "import text", "f": "from file", "esc": "back", "↵": "save", "q": "quit app"}
    # Both variants answer ^t (see list_edit's key loop), so both advertise it.
    if _value_toggle_enabled:
        base_hints["^t"] = "raw text"
    edit_hints = {"tab/⇧tab": "column", "esc": "back", "↵": "save"}

    out.append(f"  {C.DIM}{message}{C.RESET}")
    out.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

    avail_w = max(10, inner - 4 - (2 * (num_cols - 1)))
    col_widths = _layout_columns(num_cols, avail_w, col_ratios, col_mins)
    col_w = col_widths[0] if num_cols > 1 else avail_w
    last_w = col_widths[-1]

    if num_cols > 1:
        h_parts = [f"{headers[i]:<{col_widths[i]}}" for i in range(num_cols - 1)]
        h_parts.append(f"{headers[-1]}")
        out.append(f"    {C.DIM}{'  '.join(h_parts)}{C.RESET}")

        u_parts = ["─" * col_widths[i] for i in range(num_cols - 1)]
        # The last column absorbs whatever width is left over, so rule it to what
        # it actually holds — otherwise the underline trails far past the content
        # as a long bar of nothing.
        _last_content = max([len(headers[-1])] + [
            len(str((list(it) if isinstance(it, (list, tuple)) else [it])[num_cols - 1]))
            for it in items
            if len(list(it) if isinstance(it, (list, tuple)) else [it]) >= num_cols] or [0])
        u_parts.append("─" * max(1, min(last_w, _last_content)))
        out.append(f"    {'  '.join(u_parts)}")
    else:
        out.append(f"    {C.DIM}{headers[0]}{C.RESET}")
        out.append(f"    {'─' * inner}")

    if edit_mode and (col_types or {}).get(edit_col) == 'timestamp':
        edit_hints = {"0-9": "fill", "←→": "move part", "tab/⇧tab": "column",
                      "esc": "back", "↵": "save"}
    if barrel_mode:
        edit_hints = {"↑↓": "cycle", "↵": "confirm", "esc": "back"}
    # Augment with the transport keys here (not at the call site) so the pairs
    # handed back for the click map match exactly what was drawn.
    active_hints = chrome_hint_pairs(edit_hints if edit_mode else base_hints)
    hint_res = _hint(*active_hints)
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
    # See select(): don't leave the list scrolled once more rows fit.
    viewport = max(0, min(viewport, n - vis))

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
                        """Preview line for the barrel-mode value one step before the current one."""
                        if not is_barrel_col:
                            return " " * w
                        if prev_text:
                            return f"{C.ACCENT}⌃{C.RESET} {C.DIM}{ui_utils.truncate_text(prev_text, w - 2)}{C.RESET}"
                        return " " * w

                    def _barrel_mid(w: int, is_barrel_col: bool, val: str) -> str:
                        """Current barrel-mode value (or the plain cell) for this column."""
                        if not is_barrel_col:
                            return f"{val:<{w}}"
                        return f"{C.PRIMARY}{C.BOLD}{ui_utils.truncate_text(cur_text, w)}{C.RESET}"

                    def _barrel_below(w: int, is_barrel_col: bool) -> str:
                        """Preview line for the barrel-mode value one step after the current one."""
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

                types = col_types or {}
                # The selected row is wrapped in colour + bold below; cells have
                # to re-assert that after any span they close, or the row loses
                # its styling from the first styled character onward.
                row_base = f"{C.PRIMARY}{C.BOLD}" if (is_sel and not edit_mode) else ''
                row_parts = []
                for j in range(num_cols - 1):
                    cw = col_widths[j]
                    ts = types.get(j) == 'timestamp'
                    cell_str = _render_list_edit_cell(str(i_vals[j]), cw, row_is_editing,
                                                      edit_col == j, edit_buf, edit_pos, ts, row_base)
                    row_parts.append(f"{cell_str:<{cw}}"
                                     if not ((row_is_editing and edit_col == j) or ts) else cell_str)

                last_cell = _render_list_edit_cell(str(i_vals[-1]), last_w, row_is_editing,
                                                   edit_col == (num_cols - 1), edit_buf, edit_pos,
                                                   types.get(num_cols - 1) == 'timestamp', row_base)
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

                cell_str = _render_list_edit_cell(
                    val_str, inner - 4, row_is_editing, True, edit_buf, edit_pos,
                    (col_types or {}).get(0) == 'timestamp',
                    f"{C.PRIMARY}{C.BOLD}" if (is_sel and not edit_mode) else '')
                if is_sel and not edit_mode:
                    out.append(f"  {cursor_glyph} {C.PRIMARY}{C.BOLD}{cell_str}{C.RESET}")
                else:
                    out.append(f"  {cursor_glyph} {cell_str}")

    # Pin the bottom separator + hint bar to the bottom of the screen.
    _filler = _hint_pin_target() - len(out) - 1 - len(hint_lines)
    if _filler > 0:
        out.extend([""] * _filler)
    out.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")
    out.extend(f"{' ' * ui_utils.MARGIN_H}{h}" for h in hint_lines)

    return out, viewport, vis, _LEDIT_HEADER_ROWS, active_hints, len(hint_lines)

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
        """Pad/collapse a split row to exactly num_cols fields, folding overflow into the last column."""
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
              col_ratios: tuple | None = None, col_hints: object = None,
              col_mins: tuple | None = None, col_types: dict | None = None) -> list | None:
    """Arrow keys navigate, 'a' adds, 'e' edits in-place, 'd' deletes, Enter saves.

    Supports in-place cell editing with Tab navigation between columns.
    fixed_rows: disables add/delete (rows can only be edited, not added or removed).
    locked_cols: set of column indices that cannot be edited.
    col_ratios: relative starting widths for the columns.
    col_mins:   per-column minimum widths, honoured before the ratios — this is
                what keeps a fixed-shape cell readable when the table is narrow.
    col_types:  {column index: type} for cells that edit as something other than
                free text. ``'timestamp'`` gives a split date/time field masked
                as ``YYYY-MM-DD HH:MM:SS``: you type only the digits, and the
                left/right arrows run past the end of one part straight into the
                next rather than needing Tab. Leaving the time blank keeps the
                value a plain date.
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

    def _is_ts(col: int) -> bool:
        """True if `col` edits as a split timestamp rather than free text."""
        return (col_types or {}).get(col) == 'timestamp'

    def _seed_edit(col: int, value: str) -> tuple:
        """Opening buffer and caret for editing `col`: a mask for a timestamp
        column, otherwise the plain text with the caret at its end."""
        if _is_ts(col):
            buf = _ts_buffer(value)
            return buf, _TS_SLOTS[0]
        buf = list(str(value))
        return buf, len(buf)

    def _get_cell_hints() -> list[str]:
        """Candidate values for the current cell from `col_hints`, or [] if unavailable."""
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
    # Maps an absolute (row, col) on a hint line → the key clicking it replays
    # (populated only in non-edit mode, where those hints are actionable).
    _hint_cells: dict[tuple[int, int], str] = {}

    def _render():
        nonlocal viewport, _le_vis, _le_header_rows
        lines, new_viewport, new_vis, new_hdr, active_hints, n_hint = _build_list_edit_lines(
            message, items, headers,
            cursor, viewport,
            edit_mode, edit_col, edit_buf, edit_pos,
            fixed_rows, barrel_mode, barrel_hints, barrel_idx,
            col_ratios, col_mins, col_types,
        )
        viewport = new_viewport
        _le_vis = new_vis
        _le_header_rows = new_hdr
        _hint_cells.clear()
        if n_hint and not edit_mode:
            _hp = list(active_hints)
            _start = len(lines) - n_hint
            for _k in range(n_hint):
                add_hint_click_cells(_hint_cells, lines[_start + _k],
                                     1 + ui_utils.MARGIN_V + (_start + _k), _hp)
        w.render(lines)

    def _commit_edit_buffer():
        """Write the in-progress edit buffer back into the current row, padding short rows to num_cols."""
        val = _ts_value(edit_buf) if _is_ts(edit_col) else "".join(edit_buf)
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
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _le_last_click = None
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY and not edit_mode:
                return MODE_TOGGLE  # type: ignore[return-value]

            # Transport keys and miniplayer/hint clicks work in every mode of
            # this widget, so they are consumed before the mode switches below.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch

            if edit_mode and barrel_mode:
                if key == 'ESC':
                    items[cursor] = edit_backup
                    edit_mode = False
                    barrel_mode = False
                    _render()

                elif key == 'UP' and barrel_hints:
                    barrel_idx = (barrel_idx - 1) % len(barrel_hints)
                    _render()

                elif key == 'DOWN' and barrel_hints:
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

                elif key in ('TAB', 'BACKTAB') and num_cols > 1:
                    if barrel_hints:
                        edit_buf = list(barrel_hints[barrel_idx])
                        edit_pos = len(edit_buf)
                    _commit_edit_buffer()
                    barrel_mode = False
                    _step = -1 if key == 'BACKTAB' else 1
                    next_col = (edit_col + _step) % num_cols
                    if locked_cols:
                        steps = 0
                        while next_col in locked_cols and steps < num_cols:
                            next_col = (next_col + _step) % num_cols
                            steps += 1
                    edit_col = next_col
                    curr = items[cursor]
                    i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                    while len(i_vals) < num_cols: i_vals.append("")
                    edit_buf, edit_pos = _seed_edit(edit_col, str(i_vals[edit_col]))
                    barrel_hints = [] if _is_ts(edit_col) else _get_cell_hints()
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

            elif edit_mode and _is_ts(edit_col):
                # Split timestamp cell: only digits go in, and the caret walks the
                # whole mask so running off the end of one part lands in the next.
                if key == 'ESC':
                    items[cursor] = edit_backup
                    edit_mode = False
                    _render()

                elif key == 'ENTER':
                    _commit_edit_buffer()
                    edit_mode = False
                    _render()

                elif key in ('TAB', 'BACKTAB') and num_cols > 1:
                    _commit_edit_buffer()
                    _step = -1 if key == 'BACKTAB' else 1
                    next_col = (edit_col + _step) % num_cols
                    if locked_cols:
                        steps = 0
                        while next_col in locked_cols and steps < num_cols:
                            next_col = (next_col + _step) % num_cols
                            steps += 1
                    edit_col = next_col
                    curr = items[cursor]
                    i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                    while len(i_vals) < num_cols: i_vals.append("")
                    edit_buf, edit_pos = _seed_edit(edit_col, str(i_vals[edit_col]))
                    barrel_hints = [] if _is_ts(edit_col) else _get_cell_hints()
                    cur_val = "".join(edit_buf)
                    if len(barrel_hints) >= 2 and cur_val in barrel_hints:
                        barrel_mode = True
                        barrel_idx = barrel_hints.index(cur_val)
                    else:
                        barrel_mode = False
                        barrel_idx = 0
                    _render()

                elif key == 'LEFT':
                    edit_pos = _ts_step(edit_pos, -1)
                    _render()

                elif key == 'RIGHT':
                    edit_pos = _ts_step(edit_pos, 1)
                    _render()

                elif key == 'HOME':
                    edit_pos = _TS_SLOTS[0]
                    _render()

                elif key == 'END':
                    edit_pos = _TS_SLOTS[-1]
                    _render()

                elif key == 'BACKSPACE':
                    # Clear the slot behind the caret and sit on it, so holding
                    # backspace rubs the stamp out right-to-left.
                    prev = _ts_step(edit_pos, -1)
                    if prev != edit_pos or edit_pos == _TS_SLOTS[0]:
                        target = prev if prev != edit_pos else edit_pos
                        edit_buf[target] = _TS_MASK[target]
                        edit_pos = target
                    _render()

                elif key == 'DELETE':
                    edit_buf[edit_pos] = _TS_MASK[edit_pos]
                    _render()

                elif len(key) == 1 and key.isdigit():
                    edit_buf[edit_pos] = key
                    nxt = _ts_step(edit_pos, 1)
                    edit_pos = nxt if nxt != edit_pos else edit_pos
                    _render()

                # Anything else (letters, punctuation) is simply not accepted —
                # the mask supplies every separator already.

            elif edit_mode:
                if key == 'ESC':
                    items[cursor] = edit_backup
                    edit_mode = False
                    _render()

                elif key == 'ENTER':
                    _commit_edit_buffer()
                    edit_mode = False
                    _render()

                elif key in ('TAB', 'BACKTAB'):
                    if num_cols > 1:
                        _commit_edit_buffer()
                        _step = -1 if key == 'BACKTAB' else 1
                        next_col = (edit_col + _step) % num_cols
                        # Skip locked columns when tabbing (either direction).
                        if locked_cols:
                            steps = 0
                            while next_col in locked_cols and steps < num_cols:
                                next_col = (next_col + _step) % num_cols
                                steps += 1
                        edit_col = next_col

                        curr = items[cursor]
                        i_vals = list(curr) if isinstance(curr, (list, tuple)) else [str(curr)]
                        while len(i_vals) < num_cols: i_vals.append("")

                        edit_buf, edit_pos = _seed_edit(edit_col, str(i_vals[edit_col]))
                        barrel_hints = [] if _is_ts(edit_col) else _get_cell_hints()
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
                if key.startswith('MOUSE_CLICK:'):
                    # Now-playing box / hint-glyph clicks first; a plain row click
                    # falls through to the row-selection handler below.
                    _mp = key.split(':')
                    _mr = int(_mp[2]); _mc = int(_mp[3]) if len(_mp) > 3 else 1
                    _act = now_playing_click_action(_mr, _mc)
                    if _act == 'open' and _player_opener is not None:
                        _player_opener()
                        if not _IS_WINDOWS:
                            sys.stdout.write("\033[?1000h\033[?1006h")
                        sys.stdout.flush()
                        w.anchor_reset(); _le_last_click = None; _render(); continue
                    if _act in ('playpause', 'next', 'prev') and _transport_handler is not None:
                        _transport_handler(_act); continue
                    _hk = _hint_cells.get((_mr, _mc))
                    if _hk is not None:
                        key = _hk        # replay the hint's key through the switch

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
                elif key == 'UP':
                    if items: cursor = (cursor - 1) % len(items)
                    _le_last_click = None
                    _render()
                elif key == 'DOWN':
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
                        edit_buf, edit_pos = _seed_edit(edit_col, str(i_vals[edit_col]))
                    else:
                        edit_buf = list(str(items[cursor]))
                        edit_pos = len(edit_buf)

                    barrel_hints = [] if _is_ts(edit_col) else _get_cell_hints()
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
                        ui_utils.clear_screen()
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
                    screen_takeover_next()   # paint over the previous screen, no flash
                    w.anchor_reset()
                    if text_input:
                        items.extend(_parse_import_rows(text_input, headers))
                        cursor = len(items) - 1 if items else 0
                    _render()

                elif key in ('q', 'Q'):
                    raise QuitToTerminal()   # q quits the app; never a way out of a widget

                elif key == 'ESC':
                    ui_utils.clear_screen()
                    # "Discard changes?" → yes = drop edits (original), no = keep edits.
                    result = initial_items if confirm("Discard changes?", default=False) else items
                    break

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def _is_leap_year(year: int) -> bool:
    """Gregorian leap-year rule."""
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


def _days_in_month(year: int, month: int) -> int:
    """Number of days in `month` of `year` (Feb accounts for leap years)."""
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    elif month in (4, 6, 9, 11):
        return 30
    elif month == 2:
        return 29 if _is_leap_year(year) else 28
    return 0


def _validate_date(year: int, month: int, day: int) -> bool:
    """True if (year, month, day) is a real calendar date."""
    if not (1 <= month <= 12):
        return False
    if not (1 <= day <= _days_in_month(year, month)):
        return False
    return True


def _parse_date(date_str: str) -> tuple[int, int, int] | None:
    """Parse a typed date to ``(year, month, day)``, or None if unreadable.

    Thin wrapper over the project-wide parser in :mod:`src.utils.datetime_parse`
    so the calendar and date/time widgets read dates by exactly the same rules as
    everywhere else.  ``dayfirst=False`` preserves this widget's long-standing
    reading of an ambiguous ``02/07/2008`` as month-first; a part over 12 still
    settles the order on its own (``13/07/2008`` is day-first either way).
    """
    return dtp.parse_date_parts(date_str, dayfirst=False)

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
    _digit_buf = ""   # accumulates a leading 1/2/3 so days 10-31 are typable

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

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
            _cal_pairs = [("↵", "save"), ("esc", "back"), ("q", "quit app"),
                          ("tab", "month/day"), ("←→", "month"),
                          ("↑↓", "year"), ("m", "manual entry")]
        else:
            _cal_pairs = [("↵", "save"), ("esc", "back"), ("q", "quit app"),
                          ("tab", "month/day"), ("←→", "±1 day"),
                          ("↑↓", "±7 days"), ("m", "manual entry")]

        append_chrome(lines, _with_toggle_hint(_cal_pairs), _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch

            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                return MODE_TOGGLE  # type: ignore[return-value]
            if key == 'ENTER':
                result = f"{y:04d}-{m:02d}-{cursor_day:02d}"
                break
            elif key == 'ESC':
                break
            elif key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget

            elif key in ('TAB', 'BACKTAB'):
                day_mode = not day_mode      # two modes: reverse is the same flip

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
                ui_utils.clear_screen()

            elif key.isdigit():
                # Accumulate a leading 1/2/3 into a two-digit day (10-31),
                # otherwise jump straight to the single-digit day.
                dm = _days_in_month(y, m)
                if _digit_buf in ("1", "2", "3") and 1 <= int(_digit_buf + key) <= dm:
                    cursor_day = int(_digit_buf + key)
                    _digit_buf = ""
                else:
                    d1 = int(key)
                    _digit_buf = key if d1 in (1, 2, 3) else ""
                    if 1 <= d1 <= dm:
                        cursor_day = d1
            else:
                _digit_buf = ""

            _render()

    finally:
        disable_mouse()
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
        """Strip everything from the first non-digit onward, keeping just the leading digit run."""
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
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

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

        h = ""
        if section == 'date':
            if not day_mode:
                h = [("←→", "month"), ("↑↓", "year"), ("tab/⇧tab", "field"),
                     ("↵", "save"), ("esc", "back"), ("q", "quit app")]
            else:
                h = [("←→↑↓", "navigate"), ("tab/⇧tab", "field"), ("↵", "save"),
                     ("esc", "back"), ("q", "quit app")]
        elif section == 'time':
            h = [("←→", "cursor"), ("tab/⇧tab", "field"), ("↵", "save"),
                 ("esc", "back"), ("q", "quit app")]

        append_chrome(lines, _with_toggle_hint(h), _hint_cells)
        w.render(lines)

    def _build_result() -> str:
        """Assemble the ISO datetime string from date + time fields, omitting a zero time-of-day or trailing zero millis."""
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
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch

            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                return MODE_TOGGLE  # type: ignore[return-value]
            if key in ('ESC', 'CTRL_C'):
                break
            if key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget

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
                        # Cycle back to the date section (docstring contract),
                        # starting at the month/year view.
                        section = 'date'
                        day_mode = False
                        tcursor = 0

            elif key == 'BACKTAB':
                # The same chain backwards: month/year ← day grid ← time fields.
                if section == 'date':
                    if day_mode:
                        day_mode = False
                    else:
                        section = 'time'
                        tcursor = len(torder) - 1
                else:
                    if tcursor > 0:
                        tcursor -= 1
                    else:
                        section = 'date'
                        day_mode = True

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
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result


def _frac_result(buffers: dict, varies: set) -> dict:
    """Collect the fraction editor's buffers, mapping an untouched *varying*
    field to None so the caller keeps each file's existing value for it."""
    out = {}
    for field in ('current', 'total'):
        text = "".join(buffers[field])
        out[field] = None if (not text and field in varies) else text
    return out


def fraction_edit(message: str = "Edit metadata pair:",
                    tag: str = "TRCK", value: str = "",
                    varies: object = ()) -> dict | None:
    """
    In-place editor for an isolated single tag's current/total values.
    Allows integers, floats, spaces, and strings.

    ``varies`` names the fields ('current' / 'total') that differ across a bulk
    selection.  Those open blank, render as a dim ``──`` placeholder, and come
    back as ``None`` if left untouched — meaning "keep each file's own value" —
    so you can set the half the files share without flattening the half they
    don't.  Typing into such a field turns it into a real value for everything.

    Returns:
        Dict with keys: {'current', 'total'} (a value may be None when it
        varies and was left alone), or None if cancelled
    """
    varies = {str(v) for v in (varies or ())}
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
    # A field that varies has no single value to show, so it starts empty and
    # picks up the dim placeholder below.
    if 'current' in varies:
        curr_val = ""
    if 'total' in varies:
        tot_val = ""

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
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

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
            if field in varies and not val_str:
                row += f" {C.DIM}(varies){C.RESET}"

        lines.append(row)
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

        append_chrome(lines, _with_toggle_hint(
            [("↵", "save"), ("tab/⇧tab", "field"),
             ("esc", "back"), ("q", "quit app")]), _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                return MODE_TOGGLE  # type: ignore[return-value]
            current_field = field_order[cursor_field]
            buf = edit_buffers[current_field]
            pos = edit_positions[current_field]

            if key == 'ENTER':
                result = _frac_result(edit_buffers, varies)
                break
            elif key == 'ESC':
                break
            elif key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif key in ('TAB', 'BACKTAB'):
                # Shift+Tab is Tab in reverse, on every screen that has fields.
                _step = -1 if key == 'BACKTAB' else 1
                cursor_field = (cursor_field + _step) % len(field_order)
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
        disable_mouse()
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
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _validate_time() -> bool:
        """True if the current H/M/S fields form a valid time."""
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
        append_chrome(lines, _with_toggle_hint(
            [('↵', 'save'), ('tab/⇧tab', 'field'),
             ('esc', 'back'), ('q', 'quit app')]), _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                return MODE_TOGGLE  # type: ignore[return-value]
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
                else:
                    ui_utils.show_status("Invalid time (need hours < 24, minutes/seconds < 60)")
            elif key == 'ESC':
                break
            elif key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif key in ('TAB', 'BACKTAB'):
                # Shift+Tab is Tab in reverse, on every screen that has fields.
                _step = -1 if key == 'BACKTAB' else 1
                cursor_field = (cursor_field + _step) % len(field_order)
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
        disable_mouse()
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
    """Format a band frequency compactly, e.g. 1000 -> '1k', 1500 -> '1.5k'."""
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
    # Three rows either side of the baseline is the comfortable minimum, but on a
    # very short terminal — especially with the miniplayer taking rows — holding
    # that floor pushed the plot over its budget and into the miniplayer. Give
    # ground to 1 row per side rather than overrun: coarse, but still readable,
    # and the numbers beside it stay exact.
    half_h = max(3, min(8, avail // 2)) if avail > 6 else max(1, min(3, avail // 2))
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
        """Glyph for band `i` at plot row `r`: full/partial block for the bar, or None outside it."""
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


def _rva2_render_lines(gain: float, message: str, avail: int | None = None) -> list[str]:
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

    # One row per dB is the ideal, but the meter must still fit above the hint
    # bar and the miniplayer — on a short terminal it would otherwise run off the
    # bottom and take its own hints with it. Widen the dB-per-row step until the
    # scale fits, keeping it symmetric so 0 dB always lands on a row.
    peak = int(_RVA2_GAIN_MAX)
    step = 1
    if avail and avail > 0:
        while 2 * (peak // step) + 1 > avail and step < peak:
            step += 1
    ups = list(range(0, peak + 1, step))
    scale = [d for d in reversed(ups) if d > 0] + [0] + [-d for d in ups if d > 0]
    half = step / 2.0

    for i, db in enumerate(scale):
        # Label every other row (and always the ends and 0) so the axis stays
        # readable whatever step we settled on.
        show = (db == 0 or i == 0 or i == len(scale) - 1 or (db % (step * 2) == 0))
        lbl = (f"{db:+d}" if db != 0 else " 0") if show else ""

        if db == 0:
            bar = f"{C.DIM}──{C.RESET}"
        elif db > 0:
            if gain >= db:
                bar = f"{C.ACCENT}██{C.RESET}"
            elif gain >= db - half:
                bar = f"{C.ACCENT}▄▄{C.RESET}"   # bottom half: bar just entered this row
            else:
                bar = "  "
        else:  # db < 0
            if gain <= db:
                bar = f"{C.DIM}██{C.RESET}"
            elif gain <= db + half:
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
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _clamp(g: float) -> float:
        return max(-_RVA2_GAIN_MAX, min(_RVA2_GAIN_MAX, round(g * 2) / 2))

    def _render():
        # Budget the meter against the rows left once this widget's own chrome
        # (message, rule, readout, trailing blank) and the hint bar — which grows
        # by two lines when the transport keys join it — are accounted for.
        _pairs = [("↑↓", "adjust"), ("⇞⇟", "±3 dB"), ("0", "zero"),
                  ("↵", "save"), ("esc", "back"), ("q", "quit app")]
        _avail = _hint_pin_target() - 4 - len(chrome_hint_lines(_pairs))
        lines = _rva2_render_lines(gain, message, avail=_avail)
        lines.append(f"  {C.ACCENT}▸{C.RESET} {C.BOLD}{gain:+.1f} dB{C.RESET}")
        append_chrome(lines, _pairs, _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch

            if key == 'ENTER':
                result = gain; break
            elif key == 'ESC':
                result = None; break
            elif key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif key in ('UP', 'SCROLL_UP'):
                gain = _clamp(gain + _RVA2_STEP); _render()
            elif key in ('DOWN', 'SCROLL_DOWN'):
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


def number_edit(message: str = "Edit number:", *, value: int = 0,
                minimum: int = 0, maximum: int | None = None, unit: str = ""):
    """Bounded-integer spinner (TBPM / TLEN / TDLY / play counts, …).

    ↑/↓ step ±1, PgUp/PgDn ±10, type digits to enter a value directly,
    Backspace deletes a digit, Home clamps to the minimum. Returns the chosen
    int, ``None`` on cancel, or :data:`MODE_TOGGLE` when Ctrl-T flips to the raw
    text field (only when the caller enabled the toggle).
    """
    def _clamp(n: int) -> int:
        n = max(minimum, n)
        if maximum is not None:
            n = min(maximum, n)
        return n

    try:
        value = _clamp(int(str(value).strip() or minimum))
    except ValueError:
        value = minimum
    buf: list[str] = []                 # typed digits; empty → show `value`

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _cur() -> int:
        return _clamp(int("".join(buf))) if buf else value

    def _render():
        shown = "".join(buf) if buf else str(value)
        unit_s = f" {unit}" if unit else ""
        bounds = f"min {minimum}" + ("" if maximum is None else f", max {maximum}")
        lines = [
            f"  {C.DIM}{message}{C.RESET}",
            "",
            f"  {C.ACCENT}▸{C.RESET} {C.BOLD}{shown}{C.RESET}{C.DIM}{unit_s}{C.RESET}   {C.DIM}({bounds}){C.RESET}",
        ]
        append_chrome(lines, _with_toggle_hint(
            [("↑↓", "±1"), ("⇞⇟", "±10"), ("0-9", "type"),
             ("↵", "save"), ("esc", "back"), ("q", "quit app")]),
                      _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if _value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                return MODE_TOGGLE
            if key == 'CTRL_C':
                result = None; break
            elif key == 'ENTER':
                result = _cur(); break
            elif key == 'ESC':
                result = None; break
            elif key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif key.isdigit():
                if len("".join(buf)) < 12:
                    buf.append(key); _render()
            elif key == 'BACKSPACE':
                if buf:
                    buf.pop(); _render()
            elif key == 'UP':
                value = _clamp(_cur() + 1); buf.clear(); _render()
            elif key == 'DOWN':
                value = _clamp(_cur() - 1); buf.clear(); _render()
            elif key == 'PGUP':
                value = _clamp(_cur() + 10); buf.clear(); _render()
            elif key == 'PGDN':
                value = _clamp(_cur() - 10); buf.clear(); _render()
            elif key == 'HOME':
                value = minimum; buf.clear(); _render()
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result


# POPM rating 0-5 stars → 0-255 byte, Windows Media Player convention.


def rating_edit(message: str = "Rating:", *, stars: int = 0, count: int = 0,
                email: str = "") -> dict | None:
    """POPM rating form: a 0-5 star rating, a play count, and the rater email.

    Tab moves between the three fields. On Rating: ←/→ (or 0-5) set the stars.
    On Plays: ↑/↓ ±1, PgUp/PgDn ±10, or type digits. On Rater: type the
    identifier text. Returns ``{'stars', 'count', 'email'}`` or ``None`` on
    cancel. A binary/asset frame, so there is no raw-text toggle.
    """
    stars = max(0, min(5, int(stars)))
    count = max(0, int(count))
    email = str(email or "")
    field = 0                            # 0 = rating, 1 = plays, 2 = rater
    cbuf: list[str] = []                 # typed play-count digits

    fd = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w = _Widget(fd)
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _count() -> int:
        return int("".join(cbuf)) if cbuf else count

    def _render():
        nonlocal count
        filled = f"{C.ACCENT}{'★' * stars}{C.RESET}"
        empty = f"{C.DIM}{'☆' * (5 - stars)}{C.RESET}"
        rlabel = "unrated" if stars == 0 else f"{stars}/5"
        cshown = "".join(cbuf) if cbuf else str(count)
        rater = email if email else f"{C.DIM}(default){C.RESET}"

        def _mark(i: int) -> str:
            return f"{C.ACCENT}▸{C.RESET}" if field == i else " "

        def _lab(i: int, text: str) -> str:
            return f"{C.BOLD}{text}{C.RESET}" if field == i else f"{C.DIM}{text}{C.RESET}"

        lines = [
            f"  {C.DIM}{message}{C.RESET}",
            "",
            f"  {_mark(0)} {_lab(0, 'Rating')}   {filled}{empty}  {C.DIM}{rlabel}{C.RESET}",
            f"  {_mark(1)} {_lab(1, 'Plays ')}   {cshown}",
            f"  {_mark(2)} {_lab(2, 'Rater ')}   {rater}",
        ]
        append_chrome(lines, [("tab/⇧tab", "field"), ("←→", "adjust"), ("0-5", "stars"),
                              ("↵", "save"), ("esc", "back"), ("q", "quit app")],
                      _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            if key == 'CTRL_C':
                result = None; break
            elif key == 'ENTER':
                result = {'stars': stars, 'count': _count(), 'email': email}; break
            elif key == 'ESC':
                result = None; break
            elif key in ('TAB', 'BACKTAB'):
                count = _count(); cbuf.clear()
                field = (field + (-1 if key == 'BACKTAB' else 1)) % 3
                _render()
            # 'q' quits only outside the free-text Rater field (an email may contain 'q').
            elif key in ('q', 'Q') and field != 2:   # field 2 is the e-mail text
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif field == 0:
                if key in ('LEFT', 'DOWN'):
                    stars = max(0, stars - 1); _render()
                elif key in ('RIGHT', 'UP'):
                    stars = min(5, stars + 1); _render()
                elif key.isdigit() and 0 <= int(key) <= 5:
                    stars = int(key); _render()
            elif field == 1:
                if key in ('UP', 'RIGHT'):
                    count = _count() + 1; cbuf.clear(); _render()
                elif key in ('DOWN', 'LEFT'):
                    count = max(0, _count() - 1); cbuf.clear(); _render()
                elif key == 'PGUP':
                    count = _count() + 10; cbuf.clear(); _render()
                elif key == 'PGDN':
                    count = max(0, _count() - 10); cbuf.clear(); _render()
                elif key.isdigit():
                    if len("".join(cbuf)) < 12:
                        cbuf.append(key); _render()
                elif key == 'BACKSPACE':
                    if cbuf:
                        cbuf.pop(); _render()
            elif field == 2:
                if key == 'BACKSPACE':
                    email = email[:-1]; _render()
                elif len(key) == 1 and key.isprintable():
                    email += key; _render()
    finally:
        disable_mouse()
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
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

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
        # Size the plot to the rows left above the pinned hint bar and the
        # miniplayer, not to the whole terminal — it used to draw over both.
        _pairs = [("↑↓", "gain"), ("←→", "band"), ("⇞⇟", "±3"), ("a", "add"),
                  ("d", "delete"), ("0", "zero"), ("f", "flat"), ("p", "preset"),
                  ("↵", "save"), ("esc", "back"), ("q", "quit app")]
        lines = _eq_render_lines(bands, cursor, message, status, _cols(),
                                 _hint_pin_target() - len(chrome_hint_lines(_pairs)))
        append_chrome(lines, _pairs, _hint_cells)
        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()

        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                w.anchor_reset()
                _render()
                continue
            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)
            # A transport key, a click on the miniplayer, or a click on one of our
            # own hint keys — handled the same way on every screen.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch
            n = len(bands)

            if key == 'ENTER':
                result = _save(); break
            elif key == 'ESC':
                result = None; break
            elif key in ('q', 'Q'):
                raise QuitToTerminal()   # q quits the app; it never just leaves a widget
            elif key == 'LEFT' and n:
                cursor = (cursor - 1) % n; note = ""; _render()
            elif key == 'RIGHT' and n:
                cursor = (cursor + 1) % n; note = ""; _render()
            elif key == 'UP' and n:
                bands[cursor][1] = _clamp(bands[cursor][1] + _EQ_STEP); _render()
            elif key == 'DOWN' and n:
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
                screen_takeover_next()   # paint over the previous screen, no flash
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
        # The editor owned the screen and left its own cursor visible: forget what
        # we thought was on screen and hide the cursor again before the caller
        # repaints, so no caret is left blinking over our frame.
        screen_invalidate()
        sys.stdout.write(C.HIDE)
        sys.stdout.flush()
        with open(temp_path, 'r', encoding='utf-8') as f:
            result = f.read().strip()
        return result if result else None
    except (OSError, subprocess.CalledProcessError) as e:
        ui_utils.show_status(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
