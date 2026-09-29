"""The multi-column row editor (people, timestamps, imports) behind list_edit."""
from __future__ import annotations
import re
import sys
import os
from src.utils.prompt_core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, _hint, block_cursor,
    block_cursor_width, _read_key, _visible_rows, _cols, _Widget, add_hint_click_cells,
    now_playing_click_action, _hint_pin_target, screen_takeover_next, add_help_corner,
)
from src.utils import ui_utils
from src.utils import datetime_parse as dtp
from src.state import QuitToTerminal
from src.utils.prompt import chrome
from src.utils.prompt.chrome import (
    CHROME_HANDLED, chrome_hint_pairs, CHROME_REDRAW, consume_chrome, disable_mouse, enable_mouse, MODE_TOGGLE, _MODE_TOGGLE_KEY, MOVE_DOWN_KEY, MOVE_HINT, MOVE_UP_KEY,
)
from src.utils.prompt.lists import confirm
from src.utils.prompt.text import path, system_editor_edit
from src.utils.prompt_core import C


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
    display_str = block_cursor(buf_str, edit_pos, base)
    padding = max(0, width - block_cursor_width(buf_str, edit_pos))
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
        base_hints = {"↑↓": "move", "a": "add", "e": "edit", "d": "delete", MOVE_HINT[0]: MOVE_HINT[1], "i": "import text", "f": "from file", "esc": "back", "↵": "save", "q": "quit app"}
    # Both variants answer ^t (see list_edit's key loop), so both advertise it.
    if chrome._value_toggle_enabled:
        base_hints["^t"] = "raw text"
    edit_hints = {"tab/⇧tab": "column", "esc": "back", "↵": "save"}

    out.append(f"  {C.DIM}{message}{C.RESET}")
    out.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

    avail_w = max(10, inner - 4 - (2 * (num_cols - 1)))
    col_widths = _layout_columns(num_cols, avail_w, col_ratios, col_mins)
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
        edit_hints = {"←→": "move part", "tab/⇧tab": "column",
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
        if n_hint:              # edit-mode hints too: a hint shown is a key that works
            _hp = list(active_hints)
            _start = len(lines) - n_hint
            for _k in range(n_hint):
                add_hint_click_cells(_hint_cells, lines[_start + _k],
                                     1 + ui_utils.MARGIN_V + (_start + _k), _hp)
        if lines:               # `i` imports text here, so the corner is click-only
            lines[0] = add_help_corner(lines[0], 1 + ui_utils.MARGIN_V, _hint_cells)
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
        enable_mouse()
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

            # Transport keys and miniplayer/hint clicks work in every mode of
            # this widget, so they are consumed before the mode switches below.
            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                w.anchor_reset(); _render(); continue
            if _ch is not None:
                key = _ch

            # After the replay, so a clicked ^t toggles as the typed one does.
            if chrome._value_toggle_enabled and key == _MODE_TOGGLE_KEY and not edit_mode:
                return MODE_TOGGLE  # type: ignore[return-value]

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
                    if _act == 'open' and chrome._player_opener is not None:
                        chrome._player_opener()
                        enable_mouse()
                        sys.stdout.flush()
                        w.anchor_reset(); _le_last_click = None; _render(); continue
                    if _act in ('playpause', 'next', 'prev') and chrome._transport_handler is not None:
                        chrome._transport_handler(_act); continue
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
                    _btn, _mrow = int(_parts[1]), int(_parts[2])
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

                elif key == MOVE_UP_KEY and items and not fixed_rows and cursor > 0:
                    items[cursor - 1], items[cursor] = items[cursor], items[cursor - 1]
                    cursor -= 1
                    _le_last_click = None
                    _render()

                elif key == MOVE_DOWN_KEY and items and not fixed_rows and cursor < len(items) - 1:
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
                                "# One entry per line: "
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
                    enable_mouse()   # re-arm mouse
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
        disable_mouse()
        _restore_term_attrs(fd, old)
        w.clear()

    return result
