"""Audio value editors: the volume adjustment (RVA2) and the equaliser."""
from __future__ import annotations
import sys
from src.utils.prompt_core import (
    _IS_WINDOWS, _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, _read_key,
    _cols, _Widget, _hint_pin_target, screen_takeover_next,
)
from src.utils import ui_utils
from src.state import QuitToTerminal
from src.utils.prompt.chrome import CHROME_HANDLED, CHROME_REDRAW, append_chrome, chrome_hint_lines, consume_chrome
from src.utils.prompt.text import text
from src.utils.prompt_core import C


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

    band_x = _eq_band_x(n, plot_w)
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
            elif key in ('ESC', 'CTRL_C'):      # Ctrl-C cancels, as in every widget
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


def _eq_band_x(n: int, plot_w: int) -> list[int]:
    """Plot column of each of `n` bands — for drawing them and for clicks."""
    return [min(plot_w - 1, int((i + 0.5) * plot_w / n)) for i in range(n)] if n else []


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
        append_chrome(lines, _pairs, _hint_cells, i_key=True)
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
            elif key in ('ESC', 'CTRL_C'):      # Ctrl-C cancels, as in every widget
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
                x = col - 5  # the 3-col dB label + a space; the plot starts at col 5
                if 0 <= x < plot_w:
                    bx = _eq_band_x(n, plot_w)
                    cursor = min(range(n), key=lambda i: abs(bx[i] - x))
                    note = ""; _render()

    finally:
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result
