"""The boxed player's frame painter: the boxes' borders and titles, and
writing a frame through the screen model so only what changed reaches the
terminal. Shared by the drawing (player_ui) and the transport (player_controls)."""
from __future__ import annotations

import re
import sys

from backbone import ui
from backbone.prompt import core as pc
from backbone.ui import Colors as C
from backtrack.playback.player_art import _draw_inline_art
from backtrack.playback.player_geom import geom
from backtrack.playback.player_state import _frame


# Absolute cursor positioning (\033[<row>;<col>H): must require the trailing
# 'H' so it does NOT also match a 24-bit colour prefix like \033[38;2;r;g;bm,
# which every half-block art line starts with. Matching those made the renderer
# treat art lines as absolute (row-less), so metadata flowed to the top and drew
# ABOVE the art in the single-column layout.
_ABS_ROW_RE = re.compile(r'^\033\[\d+;\d+H')


def _frame_borders(only: int | None = None) -> dict:
    """The boxed player's boxes as overlays per row: each box's top border
    (with its title) and bottom border, and its sides on the rows between.
    The transport row is left out unless asked for (`only`): update_progress_ui
    draws its own."""
    if not _frame['boxed']:
        return {}
    out: dict = {}
    for box in _frame['boxes']:
        r0, r1, c0, c1 = box
        title = _frame['titles'].get(box, "")
        edge = C.ACCENT2 if box == _frame.get('focus') else C.FRAME     # the panel being arranged
        for r in range(r0, r1 + 1):
            if (only is not None and r != only) or (only is None and r == _frame['prog_row']):
                continue
            if r == r0:
                name = f" {ui.truncate_text(title, max(1, c1 - c0 - 6))} " if title else ""
                cell = (f"\033[{r};{c0}H{edge}╭─{C.RESET}{C.ACCENT2}{name}{C.RESET}"
                        f"{edge}{'─' * max(0, c1 - c0 - 2 - ui.visual_len(name))}╮{C.RESET}")
            elif r == r1:
                cell = f"\033[{r};{c0}H{edge}╰{'─' * (c1 - c0 - 1)}╯{C.RESET}"
            else:
                cell = f"\033[{r};{c0}H{edge}│{C.RESET}\033[{r};{c1}H{edge}│{C.RESET}"
            out.setdefault(r, []).append(cell)
    return out


_player_prev_rows = [0]          # flow-row count of the previous player frame


_player_overlay_rows = [set()]   # rows the previous frame overlaid


def _render_frame_buffer(buf: list, rows: int) -> None:
    """Flush the assembled frame, writing only the rows whose content changed.

    `buf[0]` is a clear sequence, deliberately **dropped**: erasing the whole
    screen every frame makes the player flicker on every progress tick, and with a
    diffed paint it isn't needed: rows the frame stops using are blanked
    explicitly, and `ui.clear_screen()` (entry, resize, exit) already drops
    the painter's model so the next frame repaints in full.

    `row` counts only flow (relative) lines; absolute-positioned items (the
    volume bar, controls, lyrics) pass through WITHOUT consuming a row slot;
    otherwise everything after them (e.g. the metadata) is pushed off-place.
    Their rows are forgotten, so a later flow paint of the same row still lands.
    """
    flow: dict[int, str] = {}
    overlay: dict[int, list[str]] = {}
    row = 0
    for item in buf[1:]:
        m = _ABS_ROW_RE.match(item)
        if m:
            # An overlay: it writes a few columns of a row the art (or another
            # flow line) also occupies: the volume bar sits to the right of the
            # art, on the art's own rows. It is layered *over* that row rather
            # than replacing it; dropping the row's flow content blanked the art.
            overlay.setdefault(int(m.group(0)[2:-1].split(';')[0]), []).append(item)
        else:
            row += 1
            if row <= rows:
                flow[row] = item

    # Every row this frame could need to touch: its own content, plus rows a
    # taller previous frame used, plus rows whose overlay has now gone away.
    # Blank counts as content: a row left out of this frame must be *erased*,
    # not left showing the previous screen.
    # The box's borders go over everything else on their rows, last.
    for r, cells in _frame_borders().items():
        overlay.setdefault(r, []).extend(cells)
    touched = set(flow) | set(overlay)
    touched |= {r for r in range(row + 1, _player_prev_rows[0] + 1) if r <= rows}
    touched |= {r for r in _player_overlay_rows[0] if r not in overlay and r <= rows}
    # The player owns the whole screen, so anything the painter still remembers
    # from the screen before it (the menu's now-playing box, a taller list) is
    # blanked here. There is no full clear per frame, so without this a first
    # frame shorter than the previous screen would leave its bottom rows behind.
    touched |= {r for r in pc.screen_rows() if r <= rows and r not in touched}
    touched.discard(_frame['prog_row'])     # update_progress_ui's (and the controls'), not the frame's
    touched |= set(range(1, ui.tab_rows() + 1))   # the tab bar's rows (the painter draws it there)
    _player_prev_rows[0] = row
    _player_overlay_rows[0] = set(overlay)

    painted = {r: pc.screen_row_paint(r, flow.get(r, ""), "".join(overlay.get(r, ()))) for r in sorted(touched)}
    out = "".join(painted.values())
    if out:
        sys.stdout.write(out)
        art = range(geom.art_top or 0, (geom.art_top or 0) + (geom.art_height or 0))
        first, last = (geom.art_left or 0) + 1, (geom.art_left or 0) + (geom.art_width or 0)
        if any(seg and r in art and pc.screen_painted(r, first, last) for r, seg in painted.items()):
            _draw_inline_art()  # cells under the image were rewritten: put it back
