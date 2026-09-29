"""Date and date-time pickers."""
from __future__ import annotations
import re
import sys
import datetime
import calendar as cal
from src.utils.prompt_core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, block_cursor,
    _read_key, _Widget, screen_takeover_next,
)
from src.utils import ui_utils
from src.utils import datetime_parse as dtp
from src.state import QuitToTerminal
from src.utils.prompt import chrome
from src.utils.prompt.chrome import CHROME_HANDLED, CHROME_REDRAW, MODE_TOGGLE, _MODE_TOGGLE_KEY, _with_toggle_hint, append_chrome, consume_chrome, disable_mouse, enable_mouse
from src.utils.prompt.text import text
from src.utils.prompt_core import C


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

            if chrome._value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                return MODE_TOGGLE  # type: ignore[return-value]
            if key == 'ENTER':
                result = f"{y:04d}-{m:02d}-{cursor_day:02d}"
                break
            elif key in ('ESC', 'CTRL_C'):      # Ctrl-C cancels, as in every widget
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
                enable_mouse()           # text() turns it off on its way out
                w.anchor_reset()

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
                cell = block_cursor(val, pos)
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

            if chrome._value_toggle_enabled and key == _MODE_TOGGLE_KEY:
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
