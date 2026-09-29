"""What every prompt widget shares: the hint bar and help corner, the global
player and transport keys, mouse reporting, and the per-edit raw-text toggle."""
from __future__ import annotations
import sys
from src.utils.prompt_core import (
    _IS_WINDOWS, _hint, add_hint_click_cells, now_playing_click_action, _hint_pin_target,
    screen_invalidate, HINTS_CLICK, toggle_hints, place_help_toggle,
)
from src.utils import ui_utils


# The one key pair that moves a row up or down, wherever a list's order can be
# changed (select's on_move, list_edit).
MOVE_UP_KEY, MOVE_DOWN_KEY = 'J', 'K'


MOVE_HINT = (f"{MOVE_UP_KEY}/{MOVE_DOWN_KEY}", "move up/down")


# Per-edit "raw text ↔ smart widget" toggle (#62). prompt_for_value enables the
# flag around a value edit; the value widgets then treat Ctrl-T as a request to
# switch modes by returning MODE_TOGGLE, and advertise it in their hint bar.
MODE_TOGGLE = object()


def _with_toggle_hint(pairs, label: str = 'raw text'):
    """Append the Ctrl-T hint to a widget's hint bar while the per-edit raw-text
    toggle is live.

    Every widget that *accepts* ^t advertises it through this, so the key is never
    silently available on one screen and absent from the bar on another. "^t"
    renders as a clickable hint key too (it synthesises the real control char).
    """
    return list(pairs) + [("^t", label)] if _value_toggle_enabled else list(pairs)


_MODE_TOGGLE_KEY = '\x14'          # Ctrl-T


_value_toggle_enabled = False


_toggle_hint_label = 'widget'      # what text()'s ^t hint calls the alternate mode


_toggle_carry: str | None = None   # in-progress text buffer handed across a Ctrl-T toggle


# Global playback hotkeys, live from any list/menu while background audio plays
# (#14). Ctrl-O reopens the full player; Ctrl-P/N/B are transport. Routed through
# registered callbacks so prompt need not import the playback layer.
_PLAYER_KEY    = '\x0f'            # Ctrl-O — open the full player view


_PLAYPAUSE_KEY = '\x10'           # Ctrl-P — play / pause


_NEXT_KEY      = '\x0e'           # Ctrl-N — next track


_PREV_KEY      = '\x02'           # Ctrl-B — previous track


_player_opener = None


_transport_handler = None


def set_player_opener(fn) -> None:
    """Register a ``callable()`` that opens the background player's full view."""
    global _player_opener
    _player_opener = fn


# --- shared widget chrome -------------------------------------------------
# Every screen owes the user the same four things: a hint bar pinned above the
# miniplayer and status bar so its keys never move, those keys clickable, the
# background-audio transport keys listed whenever the miniplayer is up, and
# clicks on the miniplayer box itself doing something. These two helpers are
# that contract in one place — `select` grew all of it first and the rest of the
# app had drifted, each widget missing a different subset.

CHROME_HANDLED = object()      # the key was consumed; carry on with the loop
CHROME_REDRAW = object()       # consumed, and the caller should repaint fully


def chrome_hint_pairs(pairs) -> list:
    """A widget's hint pairs plus the transport keys, while audio is playing.

    Only keys that will actually do something are advertised: the transport trio
    needs a handler installed and ^O needs a player to reopen. `unboxed` covers
    a terminal too narrow to draw the now-playing box — the keys are still live,
    so they are still listed.
    """
    items = list(pairs.items()) if isinstance(pairs, dict) else [tuple(p) for p in pairs]
    if ui_utils.now_playing_active() or ui_utils.now_playing_unboxed():
        if _transport_handler is not None:
            items += [("^p", "play/pause"), ("^n/^b", "next/prev")]
        if _player_opener is not None:
            items += [("^o", "player")]
    return items


def chrome_hint_lines(pairs, *, extra: str = "") -> list:
    """The hint bar as rendered lines — widgets that size a viewport need the
    row count before they lay their content out."""
    return _hint(*chrome_hint_pairs(pairs), extra=extra).splitlines()


def append_chrome(out: list, pairs, cells: dict, *, extra: str = "",
                  pin: bool = True, i_key: bool = False) -> list:
    """Append the hint bar to a widget's rendered `out` lines, in place.

    Pads down to :func:`_hint_pin_target` so the bar sits just above the
    miniplayer + status bar and its keys keep the same screen position across
    redraws — otherwise a repeated click chases the bar as the content changes
    height. Records each bright key's screen cell in `cells` for
    :func:`consume_chrome` to look up.

    The bar is empty unless hints are switched on; either way the top line
    (`out[0]`) carries the corner toggle. `i_key`: this screen leaves `i` free,
    so `i` toggles too and the corner says so.
    """
    items = chrome_hint_pairs(pairs)
    hint_lines = _hint(*items, extra=extra).splitlines()
    if pin:
        filler = _hint_pin_target() - len(out) - len(hint_lines)
        if filler > 0:
            out.extend([""] * filler)
    out.extend(f"{' ' * ui_utils.MARGIN_H}{h}" for h in hint_lines)

    cells.clear()
    if hint_lines:
        start = len(out) - len(hint_lines)
        for k in range(len(hint_lines)):
            # `_Widget.render` lays line j at terminal row anchor(1) + MARGIN_V + j.
            add_hint_click_cells(cells, out[start + k],
                                 1 + ui_utils.MARGIN_V + (start + k), items)
    place_help_toggle(out, 1 + ui_utils.MARGIN_V, cells, i_key)
    return out


def consume_chrome(key: str, cells: dict):
    """Handle a transport key, a miniplayer click, or a click on a hint key.

    Returns :data:`CHROME_HANDLED` when the key is fully dealt with,
    :data:`CHROME_REDRAW` when the caller should also repaint, the synthesised
    key string when a hint was clicked (replay it through the widget's own
    switch), or None when the key is not ours.
    """
    if key == 'FOCUS_IN':
        # Back in focus: the terminal may not have painted us meanwhile (or a
        # background track change went by), so repaint everything.
        screen_invalidate()
        return CHROME_REDRAW
    if key == HINTS_CLICK or (key == 'i' and cells.get('__i_key__')):
        toggle_hints()
        return CHROME_REDRAW            # the bar appeared or went: re-lay the screen
    if key == _PLAYER_KEY and _player_opener is not None:
        _player_opener()
        if not _IS_WINDOWS:
            sys.stdout.write("\033[?1000h\033[?1006h")   # the player took the mouse
        sys.stdout.flush()
        return CHROME_REDRAW
    if key == _PLAYPAUSE_KEY and _transport_handler is not None:
        _transport_handler('playpause')
        return CHROME_HANDLED
    if key == _NEXT_KEY and _transport_handler is not None:
        _transport_handler('next')
        return CHROME_HANDLED
    if key == _PREV_KEY and _transport_handler is not None:
        _transport_handler('prev')
        return CHROME_HANDLED

    if isinstance(key, str) and key.startswith('MOUSE_CLICK:'):
        parts = key.split(':')
        try:
            row = int(parts[2])
            col = int(parts[3]) if len(parts) > 3 else 1
        except (IndexError, ValueError):
            return None
        act = now_playing_click_action(row, col)
        if act == 'open' and _player_opener is not None:
            _player_opener()
            if not _IS_WINDOWS:
                sys.stdout.write("\033[?1000h\033[?1006h")
            sys.stdout.flush()
            return CHROME_REDRAW
        if act in ('playpause', 'next', 'prev') and _transport_handler is not None:
            _transport_handler(act)
            return CHROME_HANDLED
        hit = cells.get((row, col))
        if hit in (_PLAYER_KEY, _PLAYPAUSE_KEY, _NEXT_KEY, _PREV_KEY, HINTS_CLICK):
            return consume_chrome(hit, cells)   # a transport hint: act on it here
        if hit is not None:
            return hit                      # replay the clicked hint's key
    return None


def enable_mouse() -> None:
    """Turn on click + scroll reporting for a widget that wants clickable hints."""
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.flush()


def disable_mouse() -> None:
    """Turn click reporting back off on the way out."""
    if not _IS_WINDOWS:
        sys.stdout.write("\033[?1000l\033[?1006l")
        sys.stdout.flush()


def set_transport_handler(fn) -> None:
    """Register ``callable(action)`` for the global transport hotkeys, where
    action is 'playpause', 'next', or 'prev'. Kept as a registered callback so
    prompt need not import the playback layer (mirrors set_player_opener)."""
    global _transport_handler
    _transport_handler = fn


_notification_opener = None


def set_notification_opener(fn) -> None:
    """Register a ``callable()`` that opens the activity/notification centre —
    invoked when the status-bar ● beacon is clicked."""
    global _notification_opener
    _notification_opener = fn


def _plain(s: str) -> str:
    """The row's printed characters, one entry per *terminal column*.

    A two-cell glyph is repeated so that an index into the result is the
    column it sits in — which is what a click hit-test assumes when it asks
    whether column `col` holds a character or blank padding. Shared by
    `select` and `live_select`, so the two widgets' click behaviour can't drift.
    """
    return "".join(ch * ui_utils.char_cols(ch) for ch in ui_utils.display_text(s))
