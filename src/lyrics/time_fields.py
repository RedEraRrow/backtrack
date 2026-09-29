"""Time formatting and the segmented start/end time fields (mm:ss.mmm) shared by
the lyrics editor and the trim editor."""
from __future__ import annotations
from backbone.ui import Colors as C
from backbone import timefmt


# Segmented timestamp editor (EDIT mode): start and end as MM:SS.mmm, each field
# individually tabbable, like the segmented time widget in prompt/values.py.
_EDIT_ORDER  = ['sm', 'ss', 'sms', 'em', 'es', 'ems']


_EDIT_MAXLEN = {'sm': 2, 'ss': 2, 'sms': 3, 'em': 2, 'es': 2, 'ems': 3}


_EDIT_LIM    = {'sm': 99, 'ss': 59, 'sms': 999, 'em': 99, 'es': 59, 'ems': 999}


_EDIT_START  = ('sm', 'ss', 'sms')


_EDIT_END    = ('em', 'es', 'ems')


def _ts_parts(v: float | None) -> tuple[str, str, str]:
    """(MM, SS, mmm) zero-padded strings for a timestamp, matching timefmt.clock."""
    m, rest = timefmt.clock(v or 0.0).split(':')
    s, ms = rest.split('.')
    return m, s, ms


def _is_ms(fk: str) -> bool:
    """True if fk is a milliseconds field (start or end)."""
    return fk in ('sms', 'ems')


def _field_value(fk: str, digits: str) -> int:
    """Numeric value of a field's digits.  Milliseconds fill from the LEFT (a
    fraction of a second): '5' → 500, '50' → 500, '05' → 050.  Minutes and
    seconds are plain integers: '5' → 5."""
    if not digits:
        return 0
    return int(digits.ljust(3, '0')[:3]) if _is_ms(fk) else int(digits)


def _field_str(fk: str, value: int) -> str:
    """Canonical fixed-width digits for a value (ms always 3, else field width)."""
    return f"{value:03d}" if _is_ms(fk) else f"{value:0{_EDIT_MAXLEN[fk]}d}"


def _render_edit_fields(edit: dict) -> tuple[str, str]:
    """Render (start, end) as segmented MM:SS.mmm with a caret on the active field.
    Milliseconds show trailing zeros dimly (so a typed '5' reads as '500')."""
    flds   = edit['fields']
    active = _EDIT_ORDER[edit['fi']]
    epos   = edit['pos']

    def _fld(fk: str) -> str:
        """Render one segmented field's fixed-width cells, with a caret on the active field."""
        val = "".join(flds.get(fk, []))
        w   = _EDIT_MAXLEN[fk]
        if fk == active:
            # Render exactly `w` cells; the caret INVERTS one of them and never
            # appends, so the box keeps a constant width as you type.  Unfilled
            # cells show ms padding ('0', part of the value) or a blank for min/sec.
            cur = min(epos, w - 1)
            pad = '0' if _is_ms(fk) else ' '
            cells = []
            for i in range(w):
                filled = i < len(val)
                ch = val[i] if filled else pad
                if i == cur:
                    cells.append(f"{C.INVERT}{C.BOLD}{ch}{C.RESET}")
                elif filled:
                    cells.append(f"{C.BOLD}{ch}{C.RESET}")
                else:
                    cells.append(f"{C.DIM}{ch}{C.RESET}")
            return "".join(cells)
        if _is_ms(fk):
            return f"{C.DIM}{val.ljust(w, '0')}{C.RESET}"   # left-filled fraction → 500
        return f"{C.DIM}{val.zfill(w)}{C.RESET}"             # right-aligned count → 05

    def _sep(c: str) -> str:
        """Dim separator character (':' or '.') between fields."""
        return f"{C.DIM}{c}{C.RESET}"

    return (f"{_fld('sm')}{_sep(':')}{_fld('ss')}{_sep('.')}{_fld('sms')}",
            f"{_fld('em')}{_sep(':')}{_fld('es')}{_sep('.')}{_fld('ems')}")


def new_edit(start: float | None, end: float | None) -> tuple[dict, dict]:
    """A fresh segmented-editor state for a start/end pair, and the snapshot
    edit_changed compares against. The first digit typed replaces the field."""
    sm, ss, sms = _ts_parts(start)
    em, es, ems = _ts_parts(end)
    fields = {'sm': list(sm), 'ss': list(ss), 'sms': list(sms),
              'em': list(em), 'es': list(es), 'ems': list(ems)}
    return {'fields': fields, 'fi': 0, 'pos': 0, 'fresh': True}, {k: "".join(v) for k, v in fields.items()}


def edit_key(edit: dict, key: str) -> None:
    """One key in the segmented editor, the same in every editor that has one:
    digits fill from the left (a fresh field is replaced on the first digit),
    ↑↓ spin the value, Tab/Backtab move between fields, ms treats its digits as
    a right-padded fraction (5 → 500)."""
    fk = _EDIT_ORDER[edit['fi']]
    buf = edit['fields'][fk]
    if key in ('TAB', 'BACKTAB'):
        edit['fi'] = (edit['fi'] + (1 if key == 'TAB' else -1)) % len(_EDIT_ORDER)
        edit['pos'] = 0; edit['fresh'] = True
        return
    edit['fresh'], fresh = False, edit.get('fresh', False)
    if key == 'LEFT':
        edit['pos'] = max(0, edit['pos'] - 1)
    elif key == 'RIGHT':
        edit['pos'] = min(len(buf), edit['pos'] + 1)
    elif key in ('UP', 'DOWN'):
        v = _field_value(fk, "".join(buf)) + (1 if key == 'UP' else -1)
        buf[:] = list(_field_str(fk, max(0, min(_EDIT_LIM[fk], v)))); edit['pos'] = len(buf)
    elif key == 'BACKSPACE':
        if edit['pos'] > 0: buf.pop(edit['pos'] - 1); edit['pos'] -= 1
    elif key == 'DELETE':
        if edit['pos'] < len(buf): buf.pop(edit['pos'])
    elif key == 'HOME':
        edit['pos'] = 0
    elif key == 'END':
        edit['pos'] = len(buf)
    elif len(key) == 1 and key.isdigit():
        if fresh:
            buf[:] = [key]; edit['pos'] = 1
        elif len(buf) < _EDIT_MAXLEN[fk]:
            buf.insert(edit['pos'], key); edit['pos'] += 1
    else:
        edit['fresh'] = fresh           # not an editing key: leave the field as it was


def edit_seconds(edit: dict, keys: tuple) -> float:
    """The (minutes, seconds, ms) fields named by `keys`, as seconds."""
    m, s, ms = (_field_value(k, "".join(edit['fields'][k])) for k in keys)
    return round(m * 60 + s + ms / 1000.0, 3)


def edit_changed(edit: dict, orig: dict, keys: tuple) -> bool:
    """Whether any of the fields named by `keys` differs from the snapshot."""
    return any("".join(edit['fields'][k]) != orig[k] for k in keys)
