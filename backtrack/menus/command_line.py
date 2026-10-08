"""The `:` command line: any `backtrack` command, run on this window's library
and session. One line of output is a toast; more opens in a list to read."""
from __future__ import annotations

from contextlib import nullcontext

from backbone import output as out
from backbone import prompt, ui
from backbone.log import log

def command_line(library_ref: list, around=None) -> bool:
    """Ask for a command in a box over the screen, run it, show what it said.
    Returns whether that opened a screen (more than a line to show), which
    opens inside `around()` (the player steps out of view for it)."""
    from backtrack import cli
    line = prompt.overlay_text("backtrack command", hint="↵ run · --help · esc",
                               placeholder="track list --artist Eagles")
    if not line or not line.strip():
        return False
    code, text = cli.run_in_app(line, library_ref[0])
    if code != out.OK:
        log.info("command line: %r exited %s", line, out.code_name(code))
    lines = [l.rstrip() for l in text.rstrip().splitlines()]
    if len(lines) <= 1:
        said = lines[0].strip() if lines else ""
        if said or code == out.OK:
            ui.show_status(said or "Done.")
        else:
            ui.show_error(f"the command exited {out.code_name(code)}")
        return False
    with (around or nullcontext)():
        prompt.select("", choices=[prompt.Choice(title=l or " ", value=i) for i, l in enumerate(lines)],
                      header=prompt.PanelTitle(f": {line.strip()}",
                                               None if code == out.OK else f"exited {out.code_name(code)}"),
                      choose_label="Close")
    return True
