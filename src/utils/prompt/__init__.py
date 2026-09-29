"""
Terminal prompt widgets — resize-aware replacements for questionary.

API:
    prompt.select(message, choices)               -> value | None
    prompt.select(message, choices, multi=True)   -> [value, ...] | None
    prompt.confirm(message)                       -> bool
    prompt.text(message, default="")     -> str | None
    prompt.path(message)                 -> str | None

choices can be plain strings, dicts with 'name'/'value'/'checked',
or objects with .title / .value attributes.
"""
from src.utils.prompt_core import (  # noqa: F401
    Choice, Column, separator, HINTS_CLICK, add_help_corner, add_hint_click_cells,
    help_corner_text, hints_visible, rounded_header, toggle_hints,
)
from src.utils.prompt.chrome import (  # noqa: F401
    CHROME_HANDLED, CHROME_REDRAW, MODE_TOGGLE, MOVE_DOWN_KEY, MOVE_HINT, MOVE_UP_KEY,
    append_chrome, chrome_hint_lines, chrome_hint_pairs, consume_chrome,
    disable_mouse, enable_mouse,
    set_activity_opener, set_player_opener, set_transport_handler,
)
from src.utils.prompt.text import path, system_editor_edit, text  # noqa: F401
from src.utils.prompt.lists import ListPlace, confirm, live_select, select  # noqa: F401
from src.utils.prompt.list_edit import list_edit  # noqa: F401
from src.utils.prompt.dates import calendar_select, datetime_edit  # noqa: F401
from src.utils.prompt.values import fraction_edit, number_edit, rating_edit, time_edit  # noqa: F401
from src.utils.prompt.audio import equaliser_edit, rva2_edit  # noqa: F401
