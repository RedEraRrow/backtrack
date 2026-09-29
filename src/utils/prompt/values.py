"""Small value editors: fraction pairs, times, numbers and ratings."""
from __future__ import annotations
import sys
from src.utils.prompt_core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, block_cursor,
    _read_key, _Widget, screen_takeover_next,
)
from src.utils import ui_utils
from src.state import QuitToTerminal
from src.utils.prompt import chrome
from src.utils.prompt.chrome import CHROME_HANDLED, CHROME_REDRAW, MODE_TOGGLE, _MODE_TOGGLE_KEY, _with_toggle_hint, append_chrome, consume_chrome, disable_mouse, enable_mouse
from src.utils.prompt_core import C


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
                row += f"{label} {block_cursor(val_str, edit_positions[field])}"
            else:
                if not val_str:
                    row += f"{C.DIM}{label} ──{C.RESET}"
                else:
                    row += f"{label} {val_str}"
            if field in varies and not val_str:
                row += f" {C.DIM}(varies){C.RESET}"

        lines.append(row)
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

        # No q: these fields take text, so q is a letter here.
        append_chrome(lines, _with_toggle_hint(
            [("↵", "save"), ("tab/⇧tab", "field"), ("esc", "back")]), _hint_cells)
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
            current_field = field_order[cursor_field]
            buf = edit_buffers[current_field]
            pos = edit_positions[current_field]

            if key == 'ENTER':
                result = _frac_result(edit_buffers, varies)
                break
            elif key in ('ESC', 'CTRL_C'):      # Ctrl-C cancels, as in every widget
                break
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
            elif key == 'SPACE' or (len(key) == 1 and (key.isalnum() or key in ".-")):
                buf.insert(pos, ' ' if key == 'SPACE' else key)
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
        lines = []

        lines.append(f"  {C.DIM}{message}{C.RESET}")
        lines.append(f"{C.DIM}{'─' * ui_utils.get_terminal_width()}{C.RESET}")

        row = "  "
        for i, field in enumerate(field_order):
            value = "".join(fields[field])
            pos = positions[field]

            if i == cursor_field:
                display = block_cursor(value, pos)
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
            if chrome._value_toggle_enabled and key == _MODE_TOGGLE_KEY:
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
            elif key in ('ESC', 'CTRL_C'):      # Ctrl-C cancels, as in every widget
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
        # While digits are being typed the block marks the live field; the
        # resting value carries no caret, so the screen isn't blinking at you.
        shown = block_cursor("".join(buf), len(buf)) if buf else str(value)
        unit_s = f" {unit}" if unit else ""
        bounds = f"min {minimum}" + ("" if maximum is None else f", max {maximum}")
        lines = [
            f"  {C.DIM}{message}{C.RESET}",
            "",
            f"  {C.ACCENT}▸{C.RESET} {C.BOLD}{shown}{C.RESET}{C.DIM}{unit_s}{C.RESET}   {C.DIM}({bounds}){C.RESET}",
        ]
        append_chrome(lines, _with_toggle_hint(
            [("↑↓", "±1"), ("⇞⇟", "±10"),
             ("↵", "save"), ("esc", "back"), ("q", "quit app")]),
                      _hint_cells, i_key=True)
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
        filled = f"{C.ACCENT}{'★' * stars}{C.RESET}"
        empty = f"{C.DIM}{'☆' * (5 - stars)}{C.RESET}"
        rlabel = "unrated" if stars == 0 else f"{stars}/5"
        # Plays and Rater take typing but drew nothing to type against — the
        # block shows which field has the keyboard and where the next character
        # lands. Both append at the end, so the block rides there.
        cshown = "".join(cbuf) if cbuf else str(count)
        if field == 1:
            cshown = block_cursor(cshown, len(cshown))
        if field == 2:
            rater = block_cursor(email, len(email))
        elif email:
            rater = email
        else:
            rater = f"{C.DIM}(default){C.RESET}"

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
        # The keys of the focused row: stars, the play count, or (the Rater's
        # e-mail) just typing — where q is a letter, not quit.
        pairs = [("tab/⇧tab", "field")]
        pairs += {0: [("←→", "stars")], 1: [("←→", "±1"), ("⇞⇟", "±10")]}.get(field, [])
        pairs += [("↵", "save"), ("esc", "back")] + ([("q", "quit app")] if field != 2 else [])
        append_chrome(lines, pairs, _hint_cells)
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
