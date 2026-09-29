"""Picking from lists: select (single, multi, with row actions), live_select
(a list driven by a query), and confirm."""
from __future__ import annotations
import re
import sys
from typing import Any, Callable, Literal, overload
from src.utils.prompt_core import (
    _COLUMNS_MAX_WIDTH, _EDGE_MARGIN, _get_term_attrs, _set_raw, _restore_term_attrs,
    _wait_for_keypress, _table_widths, _render_table_row, _clip_ansi, _norm, block_cursor,
    _read_key, _visible_rows, _cols, _Widget, _hint_pin_target, screen_takeover_next,
)
from src.utils import ui_utils
from src.state import QuitToTerminal
from src.utils.prompt import chrome
from src.utils.prompt.chrome import (
    append_chrome, CHROME_HANDLED, chrome_hint_lines, CHROME_REDRAW, consume_chrome, disable_mouse, enable_mouse, MOVE_DOWN_KEY, MOVE_HINT, MOVE_UP_KEY, _plain,
)
from src.utils.prompt_core import C
from src.utils.prompt_core import edit_line


class ListPlace:
    """Where a list that is rebuilt each time round was left: the highlighted
    row's value, so a re-sort or an edit comes back to the same item, and its
    position for when that item has gone. Pass the same one to every select()
    of that list; reset() lands the next one on the first row."""
    def __init__(self) -> None:
        self.value: Any = None
        self.pos = 0

    def reset(self) -> None:
        self.value, self.pos = None, 0


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
           place: ListPlace | None = ...,
           actions: list[tuple[str, str, str]] | None = ...,
           on_move: Callable[[Any, int], bool] | None = ...,
           ) -> Any: ...


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
           place: ListPlace | None = ...,
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
           row_edit: Callable[[Any], list] | None = None,
           row_edit_commit: Callable[[Any, str], None] | None = None,
           row_edit_col: int = 1,
           row_edit_key: str = 'e',
           allow_back: bool = True,
           actions: list[tuple[str, str, str]] | None = None,
           on_move: Callable[[Any, int], bool] | None = None,
           place: ListPlace | None = None,
           ) -> Any:
    """Arrow keys to navigate; Enter / → to confirm; ← / b / Esc → None; q quits the app.

    When multi=True, Space toggles the current item and Enter returns a list of
    all checked values (possibly empty).  Otherwise returns the single selected
    value, or None if cancelled.

    Args:
        message:    Prompt label shown above the list.
        choices:    Items: str, dict, or Choice objects.
        header:     Optional lines rendered above the prompt.
        extra_hints: Extra key→action bindings merged into the hint bar.
        index:      Initial cursor position.
        place:      A ListPlace to start from and record where the list was left
                    (instead of index), for a list rebuilt each time round.
        shortcuts:  Optional key→return-value map (single-select only).
        columns:    Column layout descriptors (see Column dataclass).
        multi:      Enable multi-select mode (Space to toggle, Enter returns list).
        interlock_category_callback: When set, only one category can be checked
            at a time (multi=True only).
        on_inspect: Called with the current row's value when `inspect_key` is
            pressed; runs its own view and returns, leaving selection/checkbox
            state intact (the list redraws afterwards). If it returns something
            other than None, select() returns that instead, for a caller that
            must rebuild the list after the view changed what it shows.
        inspect_key: Key that triggers `on_inspect` (default 'd').
        row_actions: key→callback(current row value) map. Pressing the key runs
            the callback against the highlighted row and stays in the list (like
            on_inspect, but any number of keys), e.g. queue the current track.
            A callback that returns something other than None ends the list
            with that as the result, for a caller that must rebuild it.
        row_action_hints: key→label map surfaced in the hint bar for row_actions.
        row_edit:   row value → the values `row_edit_key` cycles that row through,
            its current one first. Each press steps to the next, and one step past
            the last is an inline text field seeded from where you left off, so a
            value that isn't on the list can just be typed. ↑↓ cycle too (a way
            back out of the field), ↵ commits, Esc abandons. Columns-only, since
            the edit happens inside a cell.
        row_edit_commit: called with (row value, chosen text) on ↵.
        row_edit_col: which cell of the row the editing happens in.
        row_edit_key: the key that opens the cycle and advances it (default 'e').
        allow_back: when False, the cancel keys (←/b/Esc) are ignored so the
            list can only move forward (Enter) or quit (q), used for top-level
            menus that have nowhere to go back to.
        actions:    (key, label, value) list-wide actions (play all, shuffle…),
            kept out of the rows so the cursor only moves through the list
            itself: each is listed first in the hint bar, and its key (or a
            click on it there) returns value.
        on_move:    (row value, -1 up / +1 down) → whether the caller moved it.
            MOVE_UP_KEY / MOVE_DOWN_KEY call it for the highlighted row, and on
            True the row swaps with its neighbour on screen and the cursor
            follows. Never called past a separator or the ends of the list.
    """
    items = _norm(choices)
    if actions:
        shortcuts   = {**(shortcuts or {}), **{k: v for k, _label, v in actions}}
        extra_hints = {**{k: label for k, label, _v in actions}, **(extra_hints or {})}
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

    if place is not None:
        index = next((i for i, it in enumerate(items)
                      if place.value is not None and not it.disabled and it.value == place.value),
                     place.pos)
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
    _back_hint = {"esc/b": "back"} if allow_back else {}
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
    if on_move is not None:
        combined_hints = {**{k: v for k, v in combined_hints.items() if k not in base_hints},
                          MOVE_HINT[0]: MOVE_HINT[1], **base_hints}

    # Inline row edit (opt-in, see row_edit): the cycle sits at _edit_i over
    # _edit_opts, with one position past the end being the text field. While
    # typing, every key belongs to the buffer, including 'q' and the cycle key
    # itself, which is why leaving the field is ↑↓/↵/Esc and nothing else.
    _edit_on    = False
    _edit_opts: list = []
    _edit_i     = 0
    _edit_buf: list = []
    _edit_pos   = 0
    _edit_hints = {row_edit_key: "next", "↑↓": "cycle",
                   "↵": "set", "esc": "cancel"}
    if row_edit is not None:
        combined_hints = {row_edit_key: "edit",
                          **{k: v for k, v in combined_hints.items() if k != row_edit_key}}

    _last_hlen = [0]
    # Maps a visible item index → its ANSI-stripped rendered text, so a mouse
    # click can tell whether it landed on a printed character or blank space.
    _row_plain: dict[int, str] = {}
    # Maps an absolute (row, col) on a hint line → the key that clicking that
    # bright glyph should replay through the normal key handling below.
    _hint_cells: dict[tuple[int, int], str] = {}

    def _header_lines() -> list[str]:
        if header is None:
            return []
        return header() if callable(header) else list(header)

    def _i_free() -> bool:
        """Whether `i` can toggle the hints here: not bound by this list, and
        not being typed into a cell."""
        return not (_edit_on or 'i' in (shortcuts or {}) or 'i' in (row_actions or {})
                    or (on_inspect is not None and inspect_key == 'i')
                    or (row_edit is not None and row_edit_key == 'i'))

    def _editing_text() -> bool:
        """Whether the cycle has stepped past its options into the text field."""
        return _edit_i >= len(_edit_opts)

    def _edit_cell() -> list:
        """The cell under edit, as styled segments: a cycled option, or the live
        text field. Segments rather than raw ANSI: the table measures a cell by
        the length of its text, so escape codes inside one would be counted as
        visible and the cell truncated to nothing."""
        if not _editing_text():
            # ▾ marks a value being stepped through rather than one already set.
            return [("▾ ", 'accent'), (_edit_opts[_edit_i], 'primary')]
        text = "".join(_edit_buf)
        head = [("✎ ", 'accent')]
        if _edit_pos >= len(text):
            return head + [(text, 'primary'), (" ", 'cursor')]
        return head + [(text[:_edit_pos], 'primary'), (text[_edit_pos], 'cursor'),
                       (text[_edit_pos + 1:], 'primary')]

    def _lines():
        nonlocal viewport
        cols    = _cols()
        # Refresh the now-playing box height up front so this frame's row budget
        # (vis) and hint pinning match the box that render() will actually draw;
        # otherwise a just-appeared box paints over the pinned hints until the
        # next redraw (hints missing until you click/navigate).
        ui_utils.now_playing_lines(ui_utils.get_terminal_width())
        h_lines = _header_lines()
        _last_hlen[0] = len(h_lines)
        _row_plain.clear()

        max_header_w = 0
        for hl in h_lines:
            plain_hl = ui_utils.strip_ansi(hl)
            plain_hl = re.sub(r'[╭─│╰╮╯┌┐└┘├┤┬┴┼═║╔╗╚╝]', '', plain_hl).strip()
            max_header_w = max(max_header_w, ui_utils.visual_len(plain_hl))

        layout_constraint = " " * max_header_w if (0 < max_header_w < cols - 20) else ""

        # The transport keys are surfaced here whenever background audio is
        # playing (recomputed each render so they appear/vanish live); see
        # `chrome_hint_pairs`.
        # One source for both the row budget below and the bar actually painted
        # at the end of this function: they must agree or the list mis-sizes.
        hints_now  = _edit_hints if _edit_on else combined_hints
        hint_lines = chrome_hint_lines(hints_now, extra=layout_constraint)

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
        # than it needs to be, leaving "N above" with blank space below. Pull it
        # back so the last row of the list sits on the last visible row at most.
        viewport = max(0, min(viewport, n - vis))

        out = h_lines[:]
        out.append(f"  {C.DIM}{message}{C.RESET}")
        out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}" if viewport > 0 else "")

        # Structured columns: compute table widths once from each item's cells.
        # Rows without cells (headings/separators) fall back to plain rendering.
        eff: int = 0
        col_widths: list[int] = []

        def _cells_of(i: int) -> list:
            """A row's cells, with the edited one swapped in while it is live."""
            cells = items[i].cells
            if _edit_on and i == cursor and cells and 0 <= row_edit_col < len(cells):
                cells = list(cells)
                cells[row_edit_col] = _edit_cell()
            return cells

        if columns:
            eff = min(cols, _COLUMNS_MAX_WIDTH)
            rows_cells = [_cells_of(i) for i in range(len(items)) if items[i].cells]
            vis_cells = [_cells_of(i) for i in range(viewport, min(viewport + vis, len(items)))
                         if items[i].cells]
            col_widths = _table_widths(rows_cells, columns, eff,
                                       pointer_w=6 if multi else 4, right_margin=_EDGE_MARGIN,
                                       visible_cells=vis_cells)

        for i in range(viewport, min(viewport + vis, n)):
            if columns and items[i].cells:
                out.append(_render_table_row(
                    _cells_of(i), columns, i == cursor, col_widths, eff, _EDGE_MARGIN,
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
                    # Dimmed (interlocked), not selectable
                    out.append(f"   {C.DIM}• {label}{C.RESET}")
                elif i == cursor:
                    glyph = f"{C.GREEN}✔{C.RESET}" if items[i].checked else f"{C.DIM}•{C.RESET}"
                    out.append(f"  {C.ACCENT}›{C.RESET} {glyph} {C.PRIMARY}{C.BOLD}{label}{C.RESET}")
                else:
                    glyph = f"{C.GREEN}✔{C.RESET}" if items[i].checked else f"{C.DIM}•{C.RESET}"
                    out.append(f"    {glyph} {C.DIM}{label}{C.RESET}")
            elif items[i].disabled:
                # Section heading / separator: dim, no pointer, slightly outdented.
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
        append_chrome(out, hints_now, _hint_cells, extra=layout_constraint, i_key=_i_free())
        # Hard guarantee: no rendered line ever exceeds the terminal width, so
        # the list can never wrap no matter how narrow the window is.
        _w = ui_utils.get_terminal_width()          # once per frame, not per line
        return [_clip_ansi(line, _w) for line in out]

    result = None
    _sel_last_click: int | None = None
    try:
        _set_raw(fd)
        enable_mouse()
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
            if _edit_on:
                # Editing owns every key: nothing here may fall through to the
                # list's own navigation, and while the text field is live that
                # includes 'q' and the cycle key (an artist name may contain
                # either). ↑↓ step the cycle, which is also the way back out of
                # the field and on to the next option.
                if key == 'ESC':
                    _edit_on = False
                elif key == 'ENTER':
                    chosen = ("".join(_edit_buf) if _editing_text()
                              else _edit_opts[_edit_i]).strip()
                    if chosen and row_edit_commit is not None:
                        row_edit_commit(items[cursor].value, chosen)
                    _edit_on = False
                elif key in ('UP', 'DOWN') or (key == row_edit_key and not _editing_text()):
                    step = -1 if key == 'UP' else 1
                    was = (_edit_opts[_edit_i] if not _editing_text()
                           else "".join(_edit_buf))
                    _edit_i = (_edit_i + step) % (len(_edit_opts) + 1)
                    if _editing_text():
                        # Seed the field from wherever the cycle left off, so it
                        # opens on something to amend rather than empty.
                        _edit_buf = list(was)
                        _edit_pos = len(_edit_buf)
                elif _editing_text() and (new_pos := edit_line(_edit_buf, _edit_pos, key)) is not None:
                    _edit_pos = new_pos
                _sel_last_click = None
                w.render(_lines())
                continue

            if   key == 'CTRL_C':                break
            elif key == row_edit_key and row_edit is not None and not items[cursor].disabled:
                # Open the cycle on the row's current value; a second press steps
                # to the next option (see the edit block above).
                _edit_opts = [str(o) for o in (row_edit(items[cursor].value) or []) if str(o)]
                _edit_i = 0
                _edit_buf = list(_edit_opts[0]) if _edit_opts else []
                _edit_pos = len(_edit_buf)
                _edit_on = True
                _sel_last_click = None
                w.render(_lines())
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
                # Top-level menu: no back/cancel, only forward or quit.
            elif key in ('q', 'Q'):              raise QuitToTerminal()
            elif on_inspect is not None and key == inspect_key and not items[cursor].disabled:
                # Inspect the current row (e.g. a full detail view) without
                # ending selection or losing checkbox state. The callback runs
                # its own full-screen prompt, so re-arm mouse reporting and force
                # a full redraw when it returns.
                _ret = on_inspect(items[cursor].value)
                if _ret is not None:
                    result = _ret; break
                enable_mouse()
                sys.stdout.flush()
                _sel_last_click = None
                w.anchor_reset()
                w.render(_lines())
            elif row_actions and key in row_actions and not items[cursor].disabled:
                # Act on the highlighted row (e.g. queue this track) and stay in
                # the list; the callback shows its own status; we just redraw.
                _ret = row_actions[key](items[cursor].value)
                if _ret is not None:
                    result = _ret; break
                _sel_last_click = None
                w.render(_lines())
            elif (on_move is not None and key in (MOVE_UP_KEY, MOVE_DOWN_KEY)
                  and not items[cursor].disabled):
                delta = -1 if key == MOVE_UP_KEY else 1
                j = cursor + delta
                if 0 <= j < len(items) and not items[j].disabled and on_move(items[cursor].value, delta):
                    items[cursor], items[j] = items[j], items[cursor]
                    cursor = j
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
                if (chrome._activity_opener is not None
                        and r >= ui_utils.get_terminal_height()
                        and ui_utils.has_background_tasks()):
                    chrome._activity_opener()
                    enable_mouse()
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
                # moves the highlight; it never enters.
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
                        # Already on this item (keyboard or prior click): confirm
                        cursor = idx
                        result = items[cursor].value
                        break
                    else:
                        _sel_last_click = idx
                        cursor = idx
                        w.render(_lines())
                elif not multi:
                    # Disabled/heading row: move cursor, reset click state
                    _sel_last_click = None
                    cursor = idx
                    w.render(_lines())

    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()
        if place is not None and items:
            place.value, place.pos = items[cursor].value, cursor

    return result


def live_select(message: str, provider: Callable[[str], list], *,
                count_of: Callable[[], int] | None = None,
                header: list | None | Callable[[], list[str]] = None,
                columns: list | None = None,
                extra_hints: dict[str, str] | None = None,
                on_cycle: Callable[[int], None] | None = None,
                cycle_key: str | None = None,
                section_nav: bool = False,
                row_actions: dict[str, Callable[[Any], None]] | None = None,
                placeholder: str = "type to search…",
                initial_query: str = "") -> Any:
    """Incremental "search box + live results" widget.

    `provider(query)` is called on each query change and returns the ranked list
    of Choice to display (cells already built, including any highlight segments).
    Letters/digits type into the query; ← → move the query caret; ↑ ↓ (and the
    scroll wheel) move through results; Enter selects the highlighted row; Esc
    cancels. Returns the chosen Choice.value, or None.

    `row_actions`: key -> callback(current row value), same shape as
    `select`'s: the only per-row hotkey mechanism available here, since every
    other key types into the query. Bind non-printable keys only (e.g. a
    Ctrl-combo); the callback runs and the list stays open, redrawing after.

    `count_of()` gives the number shown as "N results" when the list holds
    more than the matches (e.g. section headings); by default it is len(list).

    `on_cycle(step)` is called when `cycle_key` is pressed (e.g. to change the
    search scope), then the results are recomputed.

    `section_nav` makes Tab / Shift-Tab jump between section headings, and dims
    the rows outside the section under the cursor.

    `initial_query` pre-fills the query, caret at its end.

    `placeholder` is greyed out inside the empty field, behind the caret, and
    goes as soon as there is a query to show in its place.
    """
    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    query: list[str] = list(initial_query)
    qpos             = len(query)
    items: list      = list(provider("".join(query)))
    cursor           = 0
    viewport         = 0
    _sel_last_click: int | None = None
    # Maps a visible item index → its ANSI-stripped rendered text, so a mouse
    # click can tell whether it landed on a printed character or blank space
    # (shared hit-test convention with `select`).
    _row_plain: dict[int, str] = {}
    _fixed_rows = [0]   # header + query + count + above-indicator lines, this frame

    base_hints = {"↑↓": "results", "esc": "back", "↵": "confirm"}
    if section_nav:
        base_hints["tab"] = "section"
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

    def _headings() -> list[int]:
        """Indices of the section heading rows (disabled rows carrying a title)."""
        return [i for i, it in enumerate(items) if it.disabled and it.title]

    def _owners() -> list[int]:
        """Per row, the index of the heading that owns it (-1 above the first).

        Built in one pass and reused for the whole frame; resolving each row
        against the heading list separately is quadratic, and this runs on every
        keystroke of a live search.
        """
        out: list[int] = []
        cur = -1
        for i, it in enumerate(items):
            if it.disabled and it.title:
                cur = i
            out.append(cur)
        return out

    def _section_of(idx: int) -> int:
        """Index of the heading that owns row `idx`, or -1 above the first one."""
        owners = _owners()
        return owners[idx] if 0 <= idx < len(owners) else -1

    def _jump_section(direction: int) -> int:
        """First selectable row of the next/previous section, wrapping around."""
        heads = _headings()
        if not heads:
            return cursor
        here = _section_of(cursor)
        order = [-1] + heads if _section_of(0) == -1 and heads[0] > 0 else heads
        try:
            pos = order.index(here)
        except ValueError:
            pos = 0
        target = order[(pos + direction) % len(order)]
        start = 0 if target == -1 else target + 1
        for i in range(start, len(items)):
            if items[i].disabled:
                if i in heads and i != target:
                    break
                continue
            return i
        return cursor

    def _recompute() -> None:
        """Re-run the provider for the current query and reset cursor/viewport onto the new results."""
        nonlocal items, cursor, viewport, _sel_last_click
        # Called for the empty query too: the provider owns what a query yields,
        # including "nothing", and anything it reported for the previous one
        # (result counts, section tallies) has to be cleared rather than left
        # standing over an empty box.
        try:
            items = list(provider("".join(query)))
        except Exception:
            items = []
        cursor = _step(-1, 1) if items else 0
        viewport = 0
        _sel_last_click = None

    def _lines() -> list:
        nonlocal viewport
        width = ui_utils.get_terminal_width()
        cols  = _cols()
        ui_utils.now_playing_lines(width)   # refresh box height (see select._lines)
        out = _header_lines()

        qtext = "".join(query)
        # An empty message means the header already names the screen, so the query
        # then starts at the normal margin rather than behind a stray space.
        _label = f"{C.DIM}{message}{C.RESET} " if message else ""
        # A block cursor sitting on the character, not a bar drawn between two:
        # the query stays still as the caret walks it. Empty, the block sits on
        # the placeholder's first letter (where typing will start) with the
        # rest of the hint dimmed behind it.
        if qtext:
            _field = block_cursor(qtext, qpos)
        elif placeholder:
            _field = block_cursor(placeholder, 0, base=C.DIM) + C.RESET
        else:
            _field = block_cursor("", 0)
        out.append(f"  {_label}{_field}")
        count = ("" if not qtext else
                 ui_utils.plural(len(items) if count_of is None else count_of(), "result"))
        out.append(f"  {C.DIM}{count}{C.RESET}" if count else "")

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
        # than it needs to be, leaving "N above" with blank space below. Pull it
        # back so the last row of the list sits on the last visible row at most.
        viewport = max(0, min(viewport, n - vis))
        out.append(f"  {C.DIM}╵ {viewport} above{C.RESET}" if viewport > 0 else "")
        _fixed_rows[0] = len(out)   # rows before the first item: the click-math offset
        _row_plain.clear()

        eff = min(cols, _COLUMNS_MAX_WIDTH)
        col_widths: list = []
        if columns:
            rows_cells = [it.cells for it in items if it.cells]
            if rows_cells:
                vis_cells = [it.cells for it in items[viewport:viewport + vis] if it.cells]
                col_widths = _table_widths(rows_cells, columns, eff,
                                           pointer_w=4, right_margin=_EDGE_MARGIN,
                                           visible_cells=vis_cells)

        owners = _owners() if section_nav else []
        focus = (owners[cursor] if section_nav and 0 <= cursor < len(owners) else None)
        for i in range(viewport, min(viewport + vis, n)):
            it = items[i]
            if columns and it.cells:
                out.append(_render_table_row(it.cells, columns, i == cursor,
                                             col_widths, eff, _EDGE_MARGIN,
                                             dim=section_nav and owners[i] != focus))
            elif it.disabled:
                out.append(f"  {C.DIM}{C.BOLD}{it.title}{C.RESET}" if it.title else "")
            elif i == cursor:
                out.append(f"  {C.ACCENT}›{C.RESET} {C.PRIMARY}{C.BOLD}{it.title}{C.RESET}")
            else:
                out.append(f"    {C.DIM}{it.title}{C.RESET}")
            _row_plain[i] = _plain(out[-1])

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
        enable_mouse()
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
            # hint glyphs, handled once here, before the switch below.
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
            elif row_actions and isinstance(key, str) and key in row_actions and items:
                row_actions[key](items[cursor].value)
                w.render(_lines())
            elif key in ('TAB', 'BACKTAB') and section_nav:
                cursor = _jump_section(-1 if key == 'BACKTAB' else 1)
                w.render(_lines())
            elif cycle_key is not None and key == cycle_key and on_cycle is not None:
                on_cycle(1)                      # on_cycle(step): step through the scopes
                _recompute()
                w.render(_lines())
            elif key == 'ENTER':
                if items and not items[cursor].disabled:
                    result = items[cursor].value
                    break
            elif key in ('UP',):
                cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif key in ('DOWN',):
                cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_UP':
                cursor = _step(cursor, -1); _sel_last_click = None; w.render(_lines())
            elif key == 'SCROLL_DOWN':
                cursor = _step(cursor, 1); _sel_last_click = None; w.render(_lines())
            elif key.startswith('MOUSE_CLICK:') and w.row is not None:
                # Same two-click convention as `select`: a click on a row not
                # already highlighted moves the cursor there; clicking it again
                # (or a row already under the cursor) confirms, so one click can't
                # accidentally jump straight into a result.
                parts = key.split(':')
                r = int(parts[2]) if len(parts) > 2 else 0
                col = int(parts[3]) if len(parts) > 3 else 1
                i = r - w.row - ui_utils.MARGIN_V - _fixed_rows[0]
                idx = viewport + i
                if 0 <= idx < len(items):
                    clickable = not items[idx].disabled
                    row_plain = _row_plain.get(idx, "")
                    on_char = 0 < col <= len(row_plain) and row_plain[col - 1] != ' '
                    if not on_char:
                        if clickable:
                            cursor = idx
                        _sel_last_click = None
                        w.render(_lines())
                    elif clickable:
                        if idx == cursor or _sel_last_click == idx:
                            cursor = idx
                            result = items[cursor].value
                            break
                        _sel_last_click = idx
                        cursor = idx
                        w.render(_lines())
                    else:
                        _sel_last_click = None
                        cursor = idx
                        w.render(_lines())
            elif key == 'PGUP':
                sel = _selectable()
                if sel:
                    cursor = max(sel[0], cursor - 5)
                    if items[cursor].disabled:
                        cursor = _step(cursor, -1)
                _sel_last_click = None
                w.render(_lines())
            elif key == 'PGDN':
                sel = _selectable()
                if sel:
                    cursor = min(sel[-1], cursor + 5)
                    if items[cursor].disabled:
                        cursor = _step(cursor, 1)
                _sel_last_click = None
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
        disable_mouse()
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
        append_chrome(lines, pairs, _hint_cells, i_key=True)
        w.render(lines)

    try:
        _set_raw(fd)
        enable_mouse()
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
            # too: cancelling a yes/no question means "no".
            elif key == 'ESC':       result = False; break
            elif key == 'ENTER':     result = default; break
            elif key.lower() == 'y': result = True;  break
            elif key.lower() == 'n': result = False; break
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result
