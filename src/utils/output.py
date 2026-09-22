"""The one place the CLI decides what its output looks like.

Two modes, one set of calls. A command says *what* it produced — a table, a
record, an event, a failure — and never how to render it, so adding `--json`
needed no second code path and a new command gets both modes for free.

Human tables are laid out by `prompt_core._table_widths` / `_render_table_row`,
the same engine every list in the app uses, so a CLI table and a browse list
agree about widths, truncation and which column drops first on a narrow
terminal. Colour comes from `ui_utils.Colors`, switched off process-wide by
`ui_utils.set_colour` rather than checked here.

**Piping.** A list command prints its table when stdout is a terminal and one
path per line when it is not, which is what `ls` does and what makes
`backtrack track list | backtrack tag read` work without a flag. `--json`
overrides both.

Exit codes are the module's other half: a command returns one, `cli.main`
passes it to the shell.
"""
from __future__ import annotations

import json
import sys

from src.utils import prompt_core as pc
from src.utils import ui_utils

# Exit codes. 0/1 are the shell's own conventions; the rest name the failures a
# script would want to branch on without parsing a message.
OK = 0            # it worked
FAIL = 1          # it didn't, for a reason with no more specific code
USAGE = 2         # the arguments were wrong
NOT_FOUND = 3     # the thing asked for isn't there
EXISTS = 4        # the thing asked for is there already
NO_TOOL = 5       # a required external tool (ffmpeg, VLC) is missing

# Bumped when a field changes meaning or goes away — never for an addition, so a
# consumer can add fields without a version bump breaking it.
SCHEMA_VERSION = 1

_json = False
_quiet = False


def configure(*, json_mode: bool = False, quiet: bool = False,
              colour: bool | None = None) -> None:
    """Fix the output mode for the run. Called once, by `cli.main`.

    `colour` defaults to `ui_utils.colour_enabled()` — a terminal with NO_COLOR
    unset — and JSON never carries colour whatever the terminal is.
    """
    global _json, _quiet
    _json, _quiet = json_mode, quiet
    if colour is None:
        colour = ui_utils.colour_enabled() and not json_mode
    ui_utils.set_colour(bool(colour))


def json_mode() -> bool:
    """Whether this run is emitting JSON."""
    return _json


def is_tty() -> bool:
    """Whether stdout is a terminal — the switch between a table and bare paths."""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _envelope(kind: str, body: dict) -> dict:
    """Wrap a payload with the fields every JSON object carries."""
    return {'schema': SCHEMA_VERSION, 'kind': kind, **body}


def _write(text: str = "") -> None:
    """One line to stdout, flushed so a long run streams rather than buffers."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def record(kind: str, body: dict, *, human: str | None = None) -> None:
    """One object: a JSON line, or `human` (falling back to aligned key/value).

    Used for the single-subject commands — `library stat`, `rip status`-shaped
    things, one track's tags.
    """
    if _json:
        _write(json.dumps(_envelope(kind, body)))
        return
    if _quiet:
        return
    if human is not None:
        _write(human)
        return
    width = max((len(str(k)) for k in body), default=0)
    dim, reset = ui_utils.Colors.DIM, ui_utils.Colors.RESET
    for key, value in body.items():
        _write(f"  {dim}{str(key).ljust(width)}{reset}  {human_value(value)}")


def human_value(value) -> str:
    """One config or record value on one line, for a person rather than a parser."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return ', '.join(str(v) for v in value) or '(none)'
    if isinstance(value, dict):
        return f"({len(value)} entries)"
    return str(value)


def table(kind: str, rows: list, columns: list, *,
          cells=None, pipe_key: str = 'path') -> None:
    """A list: JSON array, an aligned table on a terminal, or bare `pipe_key`s.

    `rows` are dicts. `columns` are `prompt_core.Column` specs, and `cells` maps
    one row to the list of strings those columns render — keeping the JSON
    (whole objects) and the table (chosen fields) from having to agree on shape.
    """
    if _json:
        _write(json.dumps(_envelope(kind, {'count': len(rows), 'items': rows})))
        return
    if _quiet:
        return
    if not rows:
        if is_tty():
            _write(f"  {ui_utils.Colors.DIM}(nothing to show){ui_utils.Colors.RESET}")
        return
    if not is_tty():
        # Piped: one path per line, so the next command can read it on stdin.
        for row in rows:
            _write(str(row.get(pipe_key, '')))
        return
    cells = cells or (lambda r: [str(v) for v in r.values()])
    body = [cells(r) for r in rows]
    eff = ui_utils.get_terminal_width() - 2 * ui_utils.MARGIN_H
    widths = pc._table_widths(body, columns, eff, 0, 0)
    for cell_row in body:
        _write(pc._render_table_row(cell_row, columns, False, widths, eff, 0))


def event(kind: str, **fields) -> None:
    """One step of a long operation: an NDJSON line, or a human progress line.

    Flushed per event, so `backtrack bulk derive --json | jq` reports each file
    as it happens rather than everything at the end.
    """
    if _json:
        _write(json.dumps(_envelope('event', {'event': kind, **fields})))
        return
    if _quiet:
        return
    detail = fields.get('detail') or fields.get('path') or ''
    mark = {'written': '✔', 'error': '✘'}.get(kind, '·')
    _write(f"  {mark} {detail}")


def note(text: str) -> None:
    """An aside for a human — a count, a "nothing to do". Never emitted as JSON.

    Anything a script needs belongs in a `record` or an `event`; this is the
    sentence a person reads and a pipeline correctly ignores.
    """
    if not _json and not _quiet:
        _write(f"  {text}")


def fail(code: int, message: str, **context) -> int:
    """Report a failure on stderr and return the exit code, for `return fail(…)`.

    Under `--json` the shape is `{"error": {"code", "message", "context"}}`, with
    `code` the symbolic name rather than the number so a consumer reads
    `not_found` instead of remembering that 3 means that.
    """
    if _json:
        body = _envelope('error', {'error': {
            'code': code_name(code), 'message': message, 'context': context}})
        sys.stderr.write(json.dumps(body) + "\n")
    else:
        accent, reset = ui_utils.Colors.ACCENT, ui_utils.Colors.RESET
        detail = ''.join(f"\n  {k}: {v}" for k, v in context.items())
        sys.stderr.write(f"{accent}✘{reset} {message}{detail}\n")
    sys.stderr.flush()
    return code


_CODE_NAMES = {OK: 'ok', FAIL: 'failed', USAGE: 'usage', NOT_FOUND: 'not_found',
               EXISTS: 'exists', NO_TOOL: 'missing_tool'}


def code_name(code: int) -> str:
    """The symbolic name for an exit code ('not_found' for 3)."""
    return _CODE_NAMES.get(code, 'failed')


def read_stdin_paths() -> list[str]:
    """Paths piped in on stdin, one per line — blank lines and comments dropped.

    The other half of `table`'s piped output, so a list of tracks flows into a
    command that takes tracks. Returns [] when stdin is a terminal, so a command
    with no arguments prompts or errors rather than hanging on a read that will
    never end.
    """
    try:
        if sys.stdin.isatty():
            return []
    except (AttributeError, ValueError):
        return []
    out = []
    for line in sys.stdin:
        line = line.strip()
        if line and not line.startswith('#'):
            out.append(line)
    return out
