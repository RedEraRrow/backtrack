"""The player's transport: the controls row and its icons, the progress bar
and the times, the volume, the tiny player a small window gets, and which
action a click on any of them means."""
from __future__ import annotations

import sys
import time

from backbone import keys, nav, ui
from backbone.log import log
from backbone.prompt import core as pc
from backbone.prompt.core import _hint, add_hint_click_cells
from backbone.ui import Colors as C
from backtrack import tuning as tune
from backtrack.config import setting
from backtrack.music_library import chapter_at
from backtrack.playback.panel_state import chapters_listed
from backtrack.playback.player_frame import _render_frame_buffer
from backtrack.playback.player_geom import geom
from backtrack.playback.player_state import _frame, _ui_state
from backtrack.playback.queue_pane import has_queue


def write_controls(row: int, status: str) -> None:
    """Redraw the transport controls in place: in the boxed player, at their
    own place on the transport row (the progress bar shares it); else the
    controls row, from its first column."""
    if _frame['boxed'] and _frame['bar']:
        text = status.strip()
        gap = max(0, _frame['bar'][0] - _frame['icons_col'] - ui.visual_len(text))
        # From the inset after the box's side, which no other paint covers.
        sys.stdout.write(pc.screen_span_paint(row, _frame['icons_col'] - 1, ' ' + text + ' ' * gap))
    else:
        width = ui.get_terminal_width()
        sys.stdout.write(pc.screen_span_paint(row, 1, ui.clip_ansi(status, width)
                                              + ' ' * max(0, width - ui.visual_len(status))))


# (prev, play, pause, next) transport glyphs, keyed by the Nerd Font setting:
# Material Design icons when on, else the Unicode media symbols.
_TRANSPORT_ICONS = {
    False: ("⏮", "⏵", "⏸", "⏭"),
    True: ("\U000F04AE", "\U000F040A", "\U000F03E4", "\U000F04AD"),
}


# Clickable-control geometry (set by _controls_line / the draw): transport-icon
# columns on the controls row, the active hint pairs, and the hint-glyph cell map.
_last_transport_cols: dict[str, int] = {}


_last_controls_hint_pairs: list = []


_last_hint_cells: dict[tuple[int, int], str] = {}


def timer_extra(rate: float = 1.0, sleep_left: float | None = None) -> str:
    """What follows the time on the progress row: the speed when it isn't 1×,
    and the sleep timer's countdown."""
    out = ''
    if rate and rate != 1.0:
        out += f"  ×{rate:g}"
    if sleep_left is not None:
        out += f"  ◐ {ui.format_time(int(sleep_left))}"
    return out


def chapter_time_on() -> bool:
    """Whether the time shown is the chapter's rather than the whole file's: one
    setting, so the player and the now-playing box always agree."""
    from backtrack.music_library import _live_config
    return bool(setting(_live_config(), 'player_chapter_time'))


def toggle_chapter_time() -> None:
    """Swap between the chapter's time and the whole file's, and remember it."""
    try:
        from backtrack.config import update_config
        update_config({'player_chapter_time': not chapter_time_on()})
    except Exception as exc:
        log.warning("couldn't save player_chapter_time: %s", exc)


def shown_time(elapsed: float, duration: float, chapters: list | None) -> tuple[float, float, tuple | None]:
    """The time to show, as (position, length), and the chapter playing as
    (start, end) fractions of the file for the progress bar, or None without chapters."""
    ci = chapter_at(chapters or [], elapsed)
    if ci < 0 or not duration:
        return elapsed, duration, None
    start = chapters[ci][0]
    end = chapters[ci + 1][0] if ci + 1 < len(chapters) else duration
    span = (start / duration, end / duration)
    if chapter_time_on():
        return elapsed - start, end - start, span
    return elapsed, duration, span


def update_progress_ui(row: int, elapsed: float, duration: float, width: int,
                       chapters: list | None = None, extra: str = '') -> None:
    """Update the default progress bar display. With chapters, the one playing
    is picked out on the bar, and `t` shows its time instead of the file's.
    `extra` follows the time (timer_extra)."""
    shown_at, shown_len, span = shown_time(elapsed, duration, chapters)
    elapsed_str = ui.format_time(int(shown_at))
    duration_str = ui.format_time(int(shown_len))
    # The elapsed time is padded to the duration's width so the bar holds its
    # length while the clock runs; swapping to a shorter time (`t`) widens it.
    full = f"{elapsed_str.rjust(len(duration_str))} / {duration_str}{extra}"

    if _frame['boxed'] and _frame['bar']:
        # The boxed player: the bar's own stretch of the transport row.
        left_pad, container_w = _frame['bar'][0] - 1 - ui.MARGIN_H, _frame['bar'][1] + 2 * ui.MARGIN_H
    elif geom.art_width and geom.art_width > 0:
        container_w = geom.art_width
        left_pad = geom.art_left if geom.art_left is not None else 0
    else:
        container_w = width
        left_pad = 0
    # Inset by the side margin at both ends, so the row never meets the edge.
    container_w -= 2 * ui.MARGIN_H
    left_pad += ui.MARGIN_H

    # What fits, giving way in turn and never cut short: the bar, then the
    # length (the position alone), then the time altogether.
    percent = max(0.0, min(elapsed / duration, 1.0)) if duration else 0.0
    caps = not _frame['boxed']
    bar_width = container_w - len(full) - 2 - ui.progress_caps_width(caps)
    if bar_width >= 3:
        bar, timer_text = ui.get_progress_bar(percent, bar_width, span, caps), "  " + full
    else:
        bar, bar_width = "", 0
        timer_text = next((t for t in (full, elapsed_str) if ui.visual_len(t) <= container_w), "")

    # Remember where the bar landed so a click on it can be mapped back to a
    # position: cell 0 sits just past get_progress_bar's left cap.
    geom.prog_row, geom.prog_col, geom.prog_w = row, left_pad + 1 + len(ui.progress_caps(caps)[0]), bar_width

    # From the bar's own column, and blanked only to the container's end: the
    # box's border (and a panel beside) stay where they are.
    line = f"{bar}{timer_text}"
    # Boxed, it covers the box's inside up to the border, the blank before the
    # border included (nothing else writes that cell, so whatever the screen
    # before left there would stay); the border is the frame's. Else it
    # clears on to the margin.
    reach = container_w + (1 if _frame['boxed'] and _frame['bar'] else ui.MARGIN_H)
    out = pc.screen_span_paint(row, left_pad + 1, line + ' ' * max(0, reach - ui.visual_len(line)))
    for _r0, _r1, c0, c1 in (box for box in _frame['boxes'] if box[0] < row < box[1]) if _frame['boxed'] else ():
        out += pc.screen_span_paint(row, c0, f"{C.FRAME}│{C.RESET}") + pc.screen_span_paint(row, c1, f"{C.FRAME}│{C.RESET}")
    sys.stdout.write(out)               # cell by cell: nothing else on the row is disturbed
    sys.stdout.flush()


VOLUME_W = 20                    # the volume as it changes: ♪, the bar, the percentage


def _volume_bar_w(width: int) -> int:
    return max(4, width - 7)                   # "♪ " before it, " 100%" after


def volume_text(width: int) -> str:
    """The volume: ♪, a bar, the level (the level alone when narrow), for
    whichever session this window plays or has joined."""
    from backtrack.playback.session import active_session
    try:
        v = max(0, min(100, int(active_session().get_volume())))
    except Exception:
        return f"{C.DIM}♪{C.RESET}"
    if width < 11:                             # too narrow for a bar: the level alone
        return f"{C.DIM}♪{C.RESET} {v}%"
    bw = _volume_bar_w(width)
    filled = round(bw * v / 100)
    return (f"{C.DIM}♪{C.RESET} {C.STRONG}{'━' * filled}{C.RESET}{C.DIM}{'─' * (bw - filled)}{C.RESET}"
            f" {v:>3}%")


def volume_changed() -> None:
    """The volume moved: show it for a moment in a small box over the middle
    of the screen (nothing shows it at rest)."""
    text = volume_text(VOLUME_W)
    w = ui.visual_len(text) + 4
    cols, rows = ui.get_terminal_size()
    if rows >= 3 and cols >= w:
        lines = [ln[ui.MARGIN_H:] for ln in pc.box_lines([text], w, 3)]
    else:                                              # no room for a box: the level alone
        lines = [volume_text(1)]
    pc.screen_float(lines, tune.VOLUME_SHOWN_S)


_volume_asked: list = [None]     # (level, time) of the last step asked for


def step_volume(session, up: bool) -> int:
    """A press of a volume key: `session`'s volume (this window's, or the
    host's it has joined) a step up or down, and shown. A joined window only
    hears the host's level with its next snapshot, so a press soon after the
    last steps from the level that one asked for: five quick presses are
    five steps, not one. Returns the level asked for."""
    now = time.monotonic()
    last = _volume_asked[0]
    base = last[0] if last and now - last[1] < tune.VOLUME_PRESS_WINDOW_S else session.get_volume()
    level = session.set_volume(base + (tune.VOLUME_STEP if up else -tune.VOLUME_STEP))
    _volume_asked[0] = (level, now)
    volume_changed()
    return level


def _controls_text(is_paused: bool, n: int = 3) -> tuple[tuple, str]:
    """The transport's glyphs and their line: all three, or in a window too
    narrow for them play/pause and next (n=2), or play/pause alone (n=1)."""
    prev, play, pause, nxt = _TRANSPORT_ICONS[_ui_state['nerd_icons']]
    pp_icon = play if is_paused else pause
    # Nerd Font icons sit centred in their cells, so even gaps. The emoji-font
    # glyphs draw left of their cells; the wider gap before play keeps them even.
    gap = "   " if _ui_state['nerd_icons'] else "  "
    if n >= 3:
        return (prev, pp_icon, nxt), f"{prev}   {pp_icon}{gap}{nxt}"
    if n == 2:
        return (pp_icon, nxt), f"{pp_icon}{gap}{nxt}"
    return (pp_icon,), pp_icon


def _icons_fitting(inner: int) -> int:
    """How many transport glyphs a row `inner` columns wide keeps: they're the
    last thing to go, next before play/pause."""
    for n in (3, 2):
        if ui.visual_len(_controls_text(False, n)[1]) <= inner:
            return n
    return 1


_TINY_ROWS = 5                  # a window this short (or shorter) shows only the transport


_TINY_COLS = 10                 # likewise one this narrow, unboxed


def _draw_tiny(size: tuple, is_paused: bool) -> tuple:
    """The player in a tiny window: the transport and nothing else, boxed
    from 3 rows (and room for a glyph inside), centred; the progress
    update fits what it can beside the glyphs (update_progress_ui)."""
    cols, rows = size
    mh = ui.MARGIN_H
    boxed = rows >= 3 and cols - 2 * mh - 4 >= 2
    if boxed:
        top = max(1, (rows - 3) // 2 + 1)
        left, right = mh + 1, cols - mh
        ctrl, icons_col, inner = top + 1, left + 2, right - left - 3
        boxes = [(top, top + 2, left, right)]
    else:
        ctrl, icons_col, inner = max(1, (rows + 1) // 2), 2, cols - 1     # the glyphs from column 2
        boxes = []
    # Narrower than a box holding play/pause: that alone, unboxed (what a
    # narrower window already gave up stays given up).
    if rows >= 3 and not boxed:
        inner = 2
    n = _icons_fitting(inner)
    _frame.update(boxed=True, icons_n=n, icons_col=icons_col)
    status = _controls_line(False, is_paused, 100)[0]
    bar_l = icons_col + ui.visual_len(status.strip()) + 3
    last = icons_col + inner - 1
    _frame.update(boxed=True, boxes=boxes, titles={}, prog_row=ctrl, bar=(bar_l, max(0, last - bar_l + 1)),
                  side=False, beside=False)
    _render_frame_buffer(["\033[H\033[3J\033[J"], rows)
    write_controls(ctrl, status)
    sys.stdout.flush()
    return ctrl, ctrl, rows + 1, cols, rows


def _controls_line(is_uslt: bool, is_paused: bool, volume: int,
                   width: int | None = None,
                   has_lyrics: bool = True, has_credits: bool = True) -> tuple[str, str]:
    """Build the centred transport-controls line and the shortcuts/help hint line below it."""
    icons, controls = _controls_text(is_paused, _frame.get('icons_n') or 3)

    art_left = geom.art_left or 0
    if _frame['boxed'] and _frame['icons_col']:
        left_pad = _frame['icons_col'] - 1         # the boxed player's transport row
    elif width:
        left_pad = art_left + max(0, (width - ui.visual_len(controls)) // 2)
    elif geom.art_width:
        left_pad = art_left + max(0, (geom.art_width - ui.visual_len(controls)) // 2)
    else:
        cols = ui.get_terminal_size()[0]
        left_pad = max(0, (cols - ui.visual_len(controls)) // 2)

    status = " " * left_pad + controls
    _record_transport_cols(status, icons)

    if not pc.hints_visible():
        # Hints are off app-wide, but the player keeps its way back to them,
        # unless the help toggle is switched off (then you know to press ?).
        pairs = [(keys.label('global.help'), 'help')] if pc.help_toggle_shown() else []
        _set_controls_hint_pairs(pairs)
        return status, _hint(*pairs, always=True) if pairs else ""

    L = keys.label
    hint_args = [
        (L('player.playpause'), 'play/pause'),
        (L('player.back_5', 'player.fwd_5'), '±5s'),
        (L('player.back_1', 'player.fwd_1'), '±1s'),
        (L('player.back_30', 'player.fwd_30'), '±30s'),
    ]
    if _ui_state['debug']:                       # Diagnostics: checking end credits
        hint_args.append((L('player.near_end'), f'last {tune.NEAR_END_JUMP_S}s'))
    hint_args += [(L('volume.up', 'volume.down', first=True), 'volume'), (L('player.meta'), 'meta')]
    if nav.TABS:
        hint_args.append((L('player.tabs'), 'tabs'))
    if _ui_state.get('book'):
        hint_args.append((L('player.slower', 'player.faster'), 'speed'))
    hint_args.append((L('player.sleep'), 'sleep'))
    hint_args.append((L('player.edit'), 'edit'))
    if nav.TABS:
        hint_args.append((L('player.album', 'player.artist'), 'album/artist'))
    if _ui_state.get('chapter_title'):
        hint_args.append((L('player.chapter_time'), 'file time' if chapter_time_on() else 'chapter time'))
    if _ui_state['arranging'] is not None:       # Arrange: its keys, and nothing else to do
        hint_args = [("tab", 'next panel'), ("↑↓", 'up/down'), ("←→", 'side'), ("↵", 'done')]
        _set_controls_hint_pairs(hint_args)
        return status, _hint(*hint_args, always=True)
    if has_lyrics or has_credits or has_queue() or _ui_state.get('chapter_title'):
        hint_args.append((L('player.panel'), 'panels'))
    if chapters_listed():
        Q = lambda *n, **kw: L(*(f'player_queue.{x}' for x in n), **kw)  # noqa: E731
        hint_args += [(Q('up', 'down'), 'chapters'), (Q('play'), 'go to it')]
    elif _ui_state.get('show_queue'):
        Q = lambda *n, **kw: L(*(f'player_queue.{x}' for x in n), **kw)  # noqa: E731
        hint_args += [(Q('up', 'down'), 'queue'), (Q('play'), 'play it'), (Q('move_up', 'move_down'), 'move'),
                      (Q('remove', most=1), 'remove'), (Q('shuffle'), 'shuffle'), (Q('clear'), 'clear'),
                      (Q('undo'), 'undo')]
    if pc.help_toggle_shown():
        hint_args.append((L('global.help'), 'hide help'))
    hint_args += [(L('player.prev', 'player.next'), 'prev/next'),
                  (L('player.stop'), 'stop'), (L('player.back', first=True), 'back'), (L('player.quit'), 'quit')]

    _set_controls_hint_pairs(hint_args)
    return status, _hint(*hint_args)


def _record_transport_cols(status: str, icons: tuple[str, str, str]) -> None:
    """Store the 1-based columns of the prev / play-pause / next glyphs on the
    controls row so clicks on them can be mapped back to those actions. Columns,
    not string positions: the emoji-font glyphs are two cells wide."""
    _last_transport_cols.clear()          # in place: the one record, wherever it's read from
    _last_transport_cols.update({action: ui.visual_len(status[:i]) + 1
                                 for action, icon in zip(('prev', 'playpause', 'next'), icons)
                                 if (i := status.find(icon)) >= 0})


def _set_controls_hint_pairs(pairs: list) -> None:
    """Remember the hint pairs currently shown under the controls, for hit-testing."""
    _last_controls_hint_pairs[:] = pairs


def _place_controls(emit, ctrl_row: int, rows: int, is_uslt: bool, is_paused: bool,
                    volume: int, has_lyrics: bool, has_credits: bool) -> tuple[int, list[str]]:
    """Draw the transport line at `ctrl_row` (pulled up so its hints still fit on
    screen) with the hint lines under it, and record their click cells. Returns
    the row it used and the hint lines."""
    status_ln, shortcuts_ln = _controls_line(is_uslt, is_paused, volume,
                                             has_lyrics=has_lyrics, has_credits=has_credits)
    shortcut_lines = shortcuts_ln.splitlines() or [""]
    # A window too short for all the help shows the lines that fit under the
    # controls; only with no room for even one is the controls row pulled up.
    shortcut_lines = shortcut_lines[:max(1, rows - ui.MARGIN_V - ctrl_row)]
    ctrl_row = max(1, min(ctrl_row, rows - len(shortcut_lines) - ui.MARGIN_V))
    emit(f"\033[{ctrl_row};1H\033[K{status_ln}")
    for offset, line in enumerate(shortcut_lines, start=1):
        emit(f"\033[{ctrl_row + offset};1H\033[K{' ' * ui.MARGIN_H}{line}")
    compute_controls_hint_cells(shortcut_lines, ctrl_row + 1)
    return ctrl_row, shortcut_lines


def compute_controls_hint_cells(shortcut_lines: list[str], first_row: int) -> None:
    """Populate ``_last_hint_cells`` for hint lines drawn at ``first_row`` onward
    (each rendered with a MARGIN_H left inset, like the draw does)."""
    _last_hint_cells.clear()
    if not _last_controls_hint_pairs:
        return
    for k, line in enumerate(shortcut_lines):
        add_hint_click_cells(_last_hint_cells, ' ' * ui.MARGIN_H + line,
                             first_row + k, _last_controls_hint_pairs)


def transport_click_action(row: int, col: int, ctrl_row: int) -> str | None:
    """Return 'prev' / 'playpause' / 'next' if (row, col) hits a transport glyph
    on the controls row, else None."""
    if row != ctrl_row or not _last_transport_cols:
        return None
    for action, c in _last_transport_cols.items():
        if abs(col - c) <= 1:
            return action
    return None


def progress_from_click(row: int, col: int) -> float | None:
    """If (row, col) lands on the horizontal progress bar, return the fraction of
    the track that column represents (0.0-1.0); else None. The column either
    side of the cells (a cap, if there are any) counts as that end, so clicking
    just past either edge seeks to the start / end."""
    if geom.prog_row is None or geom.prog_col is None or geom.prog_w <= 0:
        return None
    if row != geom.prog_row:
        return None
    lo, hi = geom.prog_col - 1, geom.prog_col + geom.prog_w   # include the caps
    if not (lo <= col <= hi):
        return None
    if geom.prog_w <= 1:
        return 0.0
    frac = (col - geom.prog_col) / (geom.prog_w - 1)
    return max(0.0, min(1.0, frac))


def time_clicked(row: int, col: int) -> bool:
    """Whether (row, col) is on the time after the progress bar (it swaps it)."""
    return (geom.prog_row is not None and geom.prog_col is not None
            and row == geom.prog_row and col > geom.prog_col + geom.prog_w)


def hint_click_key(row: int, col: int) -> str | None:
    """The synthesised key for a click on a controls hint glyph, or None."""
    return _last_hint_cells.get((row, col))
