"""The activity centre."""
from __future__ import annotations
from backbone.ui import Colors as C
from backbone import prompt
from backbone.prompt import core as pc
from backbone import keys, ui


def activity_centre() -> None:
    """A live panel of current background activities. Opens from Settings or by
    clicking the status-bar ● beacon. Lists each running job with its live status
    and a pulsing dot, updating as they start/finish; closes on Esc / b / q, and
    shows a placeholder when nothing is running."""
    import sys
    import time
    from backbone.terminal_input import raw_mode, get_key_non_blocking

    _hint_pairs = [(keys.label("list.back", most=2), "back")]
    hint_cells: dict = {}

    w = pc._Widget(sys.stdin.fileno())

    def _draw() -> None:
        tasks = list(ui.BACKGROUND_TASKS.values())
        body = [f"  {C.DIM}{len(tasks)} running{C.RESET}" if tasks else f"  {C.DIM}Nothing running right now.{C.RESET}",
                ""] + [f"  {ui.pulse_circle()}  {msg}" for msg in tasks]
        # The hints pin above the miniplayer and status bar like every other
        # screen's, and pick up the transport keys while audio is playing.
        lines, _dx = prompt.boxed_chrome(body, "Activity", _hint_pairs, hint_cells, help_key=True)
        w.render(lines)

    with raw_mode(sys.stdin):
        sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse
        sys.stdout.flush()
        try:
            last = None
            pc.screen_takeover_next()           # paint over the screen before, no flash
            while True:
                if ui.consume_resize():
                    ui.clear_screen()
                    w.anchor_reset()
                    last = None
                # Re-key on the pulse frame only while active, so an idle panel is static.
                frame = int(time.time() * 6) if ui.has_background_tasks() else 0
                sig = (tuple(sorted(ui.BACKGROUND_TASKS.items())), frame)
                if sig != last:
                    _draw()
                    last = sig
                key = get_key_non_blocking()
                if key:
                    # Transport keys and clicks (on the hints or the now-playing box)
                    # act here, like every other screen; a clicked `esc/b`
                    # comes back as its key.
                    _ch = prompt.consume_chrome(key, hint_cells)
                    if _ch in (prompt.CHROME_HANDLED, prompt.CHROME_REDRAW):
                        if _ch is prompt.CHROME_REDRAW:
                            w.anchor_reset()
                        last = None                     # repaint
                        continue
                    if _ch is not None:
                        key = _ch
                    elif key.startswith('MOUSE_CLICK:'):
                        key = ''
                    if key == '\x1b' or keys.action(key, 'list') in ('list.back', 'list.quit'):
                        break
                time.sleep(0.08)
        finally:
            sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse
            sys.stdout.flush()
    ui.clear_screen()
