"""Time formatting and the segmented start/end time fields (mm:ss.mmm) shared by
the lyrics editor and the trim editor."""
from __future__ import annotations
from src.utils.ui_utils import Colors as C


def _srt(t: float) -> str:
    """Format seconds as an SRT timestamp (HH:MM:SS,mmm)."""
    t = max(0.0, float(t))
    h, r = divmod(t, 3600); m, s = divmod(r, 60)
    ms = round((s % 1) * 1000); s = int(s)
    if ms == 1000: ms, s = 0, s + 1
    return f"{int(h):02d}:{int(m):02d}:{s:02d},{ms:03d}"


def _fmt(t: float | None) -> str:
    """Format seconds as MM:SS.mmm, or a dashed placeholder when t is None."""
    if t is None:
        return "──:──.───"
    t = max(0.0, float(t))
    m, s = divmod(t, 60)
    ms = round((s % 1) * 1000); s = int(s)
    if ms == 1000: ms, s = 0, s + 1
    return f"{int(m):02d}:{s:02d}.{ms:03d}"


# Segmented timestamp editor (EDIT mode): start and end as MM:SS.mmm, each field
# individually tabbable — mirrors the segmented time widget in prompt/values.py.
_EDIT_ORDER  = ['sm', 'ss', 'sms', 'em', 'es', 'ems']


_EDIT_MAXLEN = {'sm': 2, 'ss': 2, 'sms': 3, 'em': 2, 'es': 2, 'ems': 3}


_EDIT_LIM    = {'sm': 99, 'ss': 59, 'sms': 999, 'em': 99, 'es': 59, 'ems': 999}


_EDIT_START  = ('sm', 'ss', 'sms')


_EDIT_END    = ('em', 'es', 'ems')


def _ts_parts(v: float | None) -> tuple[str, str, str]:
    """(MM, SS, mmm) zero-padded strings for a timestamp, matching `_fmt`."""
    v = max(0.0, float(v or 0.0))
    m, s = divmod(v, 60)
    ms = round((s % 1) * 1000); s = int(s)
    if ms == 1000: ms, s = 0, s + 1
    return f"{int(m):02d}", f"{s:02d}", f"{ms:03d}"


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
