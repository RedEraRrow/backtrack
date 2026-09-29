"""The listening history screen."""
from __future__ import annotations
import os
import datetime
from src.utils import prompt
from src.utils import ui_utils
from src.music_library import format_tag_values
from src.history import get_history
from src.playback.playback import music_player
from src.config import load_config
from src.id3.id3_browser import inspect_tag_loop
from src.id3.bulk_id3_manager import bulk_id3_manager
from src.menus.common import _idx_of, _menu_header
from src.menus.play import _queue_shortcut_kwargs
from src.config import setting
from src.music_library import track_title


# Listening-history columns: title · artist · album · when (relative) · listened.
# Narrow terminals drop the least important first (priority; lower = sooner):
# album, then listened, then when, then artist. Title never drops (essential).
_HISTORY_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', max_frac=0.24, priority=4),
    prompt.Column(style='dynamic-dim', max_frac=0.24, priority=1),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=3),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=2),
]


def _relative_time(ts: str, now: "datetime.datetime | None" = None) -> str:
    """Human 'x ago' for a history timestamp; falls back to the raw date."""
    try:
        dt = datetime.datetime.fromisoformat(ts.strip()[:19])
    except (ValueError, AttributeError):
        return (ts or '')[:16]
    now = now or datetime.datetime.now()
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hrs = mins / 60
    if hrs < 24:
        return f"{int(hrs)}h ago"
    days = hrs / 24
    if days < 2:
        return "yesterday"
    if days < 7:
        return f"{int(days)}d ago"
    if days < 28:
        return f"{int(days / 7)}w ago"
    return dt.strftime("%d %b %Y")


def _nice_dur(raw: str) -> str:
    """Format a raw duration (e.g. '90s') as compact 'h/m/s' text."""
    try:
        secs = int(str(raw).rstrip('s'))
    except ValueError:
        return str(raw)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def handle_history(library: list) -> str | None:
    """Show the recent listening history list; on selecting an entry, offer play/edit actions."""
    cursor = 0
    while True:
        # Rebuilt each time round: playing a track adds to the history.
        res = _history_screen(library, cursor)
        if not isinstance(res, tuple):
            return res                      # backed out
        cursor = res[1]


def _history_screen(library: list, cursor: int):
    """One pass of the history list: None (back), or
    ("again", cursor) to show it again after playing or editing."""
    history_entries = get_history(limit=30)

    if not history_entries:
        ui_utils.show_status("No listening history available.")
        return None

    now = datetime.datetime.now()
    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        if song:
            title = track_title(path, song)
            artist = format_tag_values(song.get('artist'))
            album = (song.get('album') or '').strip()
            artist = '' if artist == 'Unknown Artist' else artist
            album = '' if album == 'Unknown Album' else album
        else:
            title = track_title(path)
            artist = album = ''
        choices.append(prompt.Choice(
            title=title, value=path,
            cells=[title, artist, album, _relative_time(ts, now), _nice_dur(dur)]))

    _show_editor = setting(load_config(), "show_metadata_editor")

    def _inspect_history(path: str) -> None:
        """`e`: open the metadata editor (lyrics sync and trim live inside it
        too) for the highlighted entry without leaving this list."""
        song = next((s for s in library if s['path'] == path), None)
        ui_utils.clear_screen()
        inspect_tag_loop(path, library_metadata=song, library=library)
        ui_utils.clear_screen()

    selected = prompt.select(
        "",
        choices=choices,
        columns=_HISTORY_COLUMNS,
        header=_menu_header("Listening History", f"{len(history_entries)} recent"),
        on_inspect=_inspect_history if _show_editor else None,
        inspect_key='e',
        index=min(cursor, len(choices) - 1),
        shortcuts={'E': '__bulk_edit__'} if _show_editor else None,
        extra_hints={'e': 'edit', 'E': 'edit all'} if _show_editor else None,
        **_queue_shortcut_kwargs(library),
    )
    if not selected:
        return None

    if selected == "__bulk_edit__":
        # Once each: a track played several times is in the history several times.
        bulk_id3_manager(library, paths=list(dict.fromkeys(p for _, _, p in history_entries)))
        return ("again", cursor)

    picked = _idx_of(choices, selected, cursor)
    ui_utils.clear_screen()
    music_player(selected)
    ui_utils.clear_screen()
    return ("again", picked)
