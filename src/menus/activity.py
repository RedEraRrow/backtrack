"""The activity centre."""
from __future__ import annotations
from backbone.ui import Colors as C
from backbone import prompt
from backbone import prompt_core
from backbone import ui as ui_utils


def activity_centre() -> None:
    """A live panel of current background activities. Opens from Settings or by
    clicking the status-bar ● beacon. Lists each running job with its live status
    and a pulsing dot, updating as they start/finish; closes on Esc / b / q, and
    shows a placeholder when nothing is running."""
    import sys
    import time
    from backbone.terminal_input import raw_mode, get_key_non_blocking

    _hint_pairs = [("esc/b", "back")]
    hint_cells: dict = {}

    def _draw() -> None:
        tasks = list(ui_utils.BACKGROUND_TASKS.values())
        hint_cells.clear()
        out = ["\033[H\033[3J\033[J" + C.HIDE,
               "\n" + prompt.add_help_corner(f"  {C.BOLD}Activity{C.RESET}", 2, hint_cells, True)]
        out.append(f"   {C.DIM}{len(tasks)} running{C.RESET}\n\n" if tasks else "\n\n")
        if tasks:
            for msg in tasks:
                out.append(f"   {ui_utils.pulse_circle()}  {msg}\n")
        else:
            out.append(f"   {C.DIM}Nothing running right now.{C.RESET}\n")
        body = "".join(out) + "\n\n"
        # Pin the hints to the bottom, above the now-playing box and status bar, so
        # their keys hold a fixed position as the running-task list grows and
        # shrinks underneath them, and pick up the transport keys while audio
        # is playing, like every other screen.
        pairs = prompt.chrome_hint_pairs(_hint_pairs)
        hint = prompt_core._hint(*pairs)
        hint_lines = hint.split('\n')
        used = body.count('\n')
        pad = max(0, prompt_core._hint_pin_target() - used - len(hint_lines))
        sys.stdout.write(body + "\n" * pad + hint)
        sys.stdout.flush()
        first_row = 1 + used + pad
        for k, line in enumerate(hint_lines):
            prompt.add_hint_click_cells(hint_cells, line, first_row + k, pairs)

    with raw_mode(sys.stdin):
        sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse
        sys.stdout.flush()
        try:
            last = None
            while True:
                # Re-key on the pulse frame only while active, so an idle panel is static.
                frame = int(time.time() * 6) if ui_utils.has_background_tasks() else 0
                sig = (tuple(sorted(ui_utils.BACKGROUND_TASKS.items())), frame)
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
                        last = None                     # repaint
                        continue
                    if _ch is not None:
                        key = _ch
                    elif key.startswith('MOUSE_CLICK:'):
                        key = ''
                    if key in ('b', 'B', 'q', 'Q', '\x1b') or key == 'ESC':
                        break
                time.sleep(0.08)
        finally:
            sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse
            sys.stdout.flush()
    ui_utils.clear_screen()
