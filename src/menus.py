"""Top-level TUI menu hierarchy: browse, search, history, settings, and playback."""
from __future__ import annotations
import os
import random
import string
import datetime

from src.utils.ui_utils import roman, Colors as C

from src.utils import prompt
from src.utils import ui_utils
from src.music_library import (
    build_library, save_library_cache,
    start_background_sync,
    get_grouped_data, get_group_sort_key, sort_library_logic,
    to_num
)
from src.history import get_history, clear_history, get_recent_paths
from src import search as _search
from src.playback.playback import music_player
from src.playback.session import SESSION, REPEAT_OFF, REPEAT_ONE, REPEAT_ALL, active_session, is_client
from src.lyrics.lyrics_editor import lyrics_editor, find_lyrics
from src.config import load_config, save_config
from src.state import NAV_STACK
from src.id3.id3_browser import inspect_tag_loop
from src.id3.bulk_id3_manager import bulk_id3_manager
from src.id3.tag_registry import TAG_REGISTRY

# Structured column layouts for browse lists (no string parsing — each Choice
# carries explicit `cells`).
_TRACK_COLUMNS = [
    prompt.Column(style='primary', max_frac=0.5),                # title (truncates)
    prompt.Column(style='dynamic-dim', flex=True, align='left', gap=3, priority=1),  # featured artist — drops first when narrow
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=3),  # duration (pinned right, kept)
]
_ALBUM_COLUMNS = [
    prompt.Column(style='primary', flex=True),                   # album name
    prompt.Column(style='dynamic-dim', flex=True, align='left', gap=3),  # album artist
]

def _idx_of(choices: list, value, default: int = 0) -> int:
    """Index of the choice whose value == `value` (for restoring the cursor on back)."""
    for i, c in enumerate(choices):
        cv = c.value if isinstance(c, prompt.Choice) else c
        if cv == value:
            return i
    return default


def _menu_header(title: str, subtitle: str | None = None):
    """Return a lazy header builder callable for prompt.select's header= parameter."""

    def _build() -> list[str]:
        cols = ui_utils.get_terminal_width()
        # Title + optional subtitle on one line, then a thin divider
        title_str    = f"{C.BOLD}{title}{C.RESET}"
        subtitle_str = f"  {C.DIM}{subtitle}{C.RESET}" if subtitle else ""
        lines = [
            f"  {title_str}{subtitle_str}",
            f"{C.DIM}{ui_utils.divider(cols, '─')}{C.RESET}",
        ]
        return lines

    return _build


# --- Browse sort options -------------------------------------------------
# Each context offers a list of (mode, label) pairs; the first entry is the
# default and is always alphabetical by name.
_GROUP_SORTS = {
    "Artists": [("name", "Name (A–Z)"), ("name_desc", "Name (Z–A)"), ("tracks", "Most tracks")],
    "Genres":  [("name", "Name (A–Z)"), ("name_desc", "Name (Z–A)"), ("tracks", "Most tracks")],
    "Albums":  [("name", "Name (A–Z)"), ("artist", "Album artist"),
                ("year_new", "Year (newest)"), ("year_old", "Year (oldest)")],
}
_ALBUM_SORTS = [("name", "Name (A–Z)"), ("year_new", "Year (newest)"), ("year_old", "Year (oldest)")]


def _year_of(songs: list) -> int:
    """First usable release year among a group's songs, else 0."""
    for s in songs:
        try:
            y = int(str(s.get('year')))
        except (ValueError, TypeError):
            continue
        if y:
            return y
    return 0


def _album_artist_of(songs: list) -> str:
    """First non-empty album artist (falling back to artist) among a group's songs."""
    for s in songs:
        aa = (s.get('album_artist') or s.get('artist') or '').strip()
        if aa:
            return aa
    return ''


def _sort_groups(names: list, grouped: dict, cat_key: str, mode: str) -> list:
    """Order group names by the chosen sort mode; ties fall back to name order."""
    def nk(n: str) -> str:
        """Sort key for group n (also used as the tiebreaker for other modes)."""
        return get_group_sort_key(n, grouped[n], cat_key)
    if mode == "name_desc":
        return sorted(names, key=nk, reverse=True)
    if mode == "artist":
        return sorted(names, key=lambda n: (_album_artist_of(grouped[n]).lower(), nk(n)))
    if mode == "year_new":
        return sorted(names, key=lambda n: (-_year_of(grouped[n]), nk(n)))
    if mode == "year_old":
        return sorted(names, key=lambda n: (_year_of(grouped[n]) or 99999, nk(n)))
    if mode == "tracks":
        return sorted(names, key=lambda n: (-len(grouped[n]), nk(n)))
    return sorted(names, key=nk)  # "name" (default, alphabetical)


def _sort_label(options: list, mode: str) -> str:
    """Display label for mode, falling back to the first option's label if unmatched."""
    for v, lbl in options:
        if v == mode:
            return lbl
    return options[0][1]


def _pick_sort(current: str, options: list, header) -> str:
    """Show a sort picker; return the chosen mode (unchanged if cancelled)."""
    choices = [
        prompt.Choice(title=f"{'✔ ' if v == current else '  '}{lbl}", value=v)
        for v, lbl in options
    ]
    sel = prompt.select("Sort by:", choices=choices, header=header,
                        index=_idx_of(choices, current))
    return sel or current


# Search scope cycled with Tab in the live search screen (default: all fields).
_ALL_SEARCH_FIELDS = ['title', 'artist', 'album', 'genre', 'people']
_SCOPE_CYCLE = ['all', 'title', 'artist', 'album', 'genre', 'people']

# Columns for live search results: title (matched chars accented) · artist ·
# album · people (whoever matched) · disc/track · duration. `title` flexes;
# the rest are width-capped so the layout stays aligned across queries.
# title · artist · album · people · disc/track · duration. On narrow terminals
# the least important columns drop first (priority; lower = dropped sooner):
# people, then disc/track, then duration, then album, then artist. Title never
# drops (no priority = essential).
_SEARCH_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', max_frac=0.20, priority=5),
    prompt.Column(style='dynamic-dim', max_frac=0.20, priority=4),
    prompt.Column(style='dynamic-dim', max_frac=0.22, priority=1),
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=2, priority=2),
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=2, priority=3),
]


def _hl_segments(value: str, tokens: list, base_style: str) -> list:
    """Split `value` into styled segments, accenting the fuzzy-matched spans."""
    value = str(value or "")
    if not value:
        return [("", base_style)]
    spans = _search.highlight_spans(value, tokens) if tokens else []
    if not spans:
        return [(value, base_style)]
    segs: list = []
    i = 0
    for a, b in spans:
        if a > i:
            segs.append((value[i:a], base_style))
        segs.append((value[a:b], 'accent'))
        i = b
    if i < len(value):
        segs.append((value[i:], base_style))
    return segs


def _disc_track_cell(song: dict) -> str:
    """Compact disc/track indicator: '1·05' when multi-disc, else '05' (or '')."""
    trk = str(song.get('track', '') or '').strip()
    disc = str(song.get('disc', '') or '').strip()
    total_discs = str(song.get('total_discs', '') or '').strip()
    if not trk or trk == '0':
        return ""
    trk = trk.zfill(2)
    multi = (disc and disc not in ('0', '1')) or (total_discs and total_discs not in ('0', '1'))
    return f"{disc}·{trk}" if multi and disc else trk


def _people_cell(value: str, tokens: list):
    """People column: the matched person(s), highlighted — empty when no person
    in this track's cast matched (so the column reads as 'who matched')."""
    if not value or not tokens:
        return ""
    parts = value.replace(';', ',').replace('/', ',').split(',')
    entries = [e.strip() for e in parts if e.strip()]
    matched = [e for e in entries if _search.highlight_spans(e, tokens)]
    if not matched:
        return ""
    segs: list = []
    for i, e in enumerate(matched):
        if i:
            segs.append((", ", 'dim'))
        segs += _hl_segments(e, tokens, 'dynamic-dim')
    return segs


def _search_result_cells(result, tokens: list) -> list:
    """Build the columned, highlighted cells for one search result row."""
    s = result.song
    title = _hl_segments(s.get('title', ''), tokens, 'primary')
    artist = _hl_segments(s.get('artist', ''), tokens, 'dynamic-dim')
    album = _hl_segments(s.get('album', ''), tokens, 'dynamic-dim')
    # Only fill the people column when people was the field that actually matched
    # (avoids weak subsequence hits populating it on a title/artist search).
    people = (_people_cell(str(s.get('people', '') or ''), tokens)
              if 'people' in result.matched_fields else "")
    # A genre hit has no column of its own; tag it onto the album when nothing
    # else in the row is highlighted, so it's still clear why the row matched.
    if 'genre' in result.matched_fields and not people and not any(
            _search.highlight_spans(str(s.get(f, '') or ''), tokens)
            for f in ('title', 'artist', 'album')):
        album = album + [("  · ", 'dim')] + _hl_segments(str(s.get('genre', '') or ''), tokens, 'dynamic-dim')
    dur = s.get('duration') or 0
    dur_str = ui_utils.format_time(int(dur)) if dur else ""
    return [title, artist, album, people, _disc_track_cell(s), dur_str]


def handle_search(library: list) -> str | None:
    """Run the live fuzzy search screen; on selecting a track, offer play/edit
    actions (or play immediately if autoplay is on or there's only one option)."""
    if not library:
        ui_utils.show_status("Library is empty. Scan a directory first.")
        return None

    _cfg = load_config()
    _show_editor = _cfg.get("show_metadata_editor", True)
    _show_lyrics = _cfg.get("show_lyrics_editor", True)
    recent = get_recent_paths()

    # Search scope, cycled in-screen with Tab (default: all fields).
    scope = {'i': 0}   # index into _SCOPE_CYCLE

    def _fields() -> list:
        """Return the field(s) to search under the current scope."""
        mode = _SCOPE_CYCLE[scope['i']]
        return _ALL_SEARCH_FIELDS if mode == 'all' else [mode]

    def _cycle() -> None:
        """Advance the search scope to the next field in the cycle."""
        scope['i'] = (scope['i'] + 1) % len(_SCOPE_CYCLE)

    # Live fuzzy search: results re-rank on every keystroke, matched characters
    # highlighted, richer rows (title · artist · album · people · disc/track · dur).
    _last: dict = {'results': []}

    def _provider(query: str) -> list:
        """Run the search for query and build the live-select choices with highlighted matches."""
        results = _search.search(library, query, _fields(), recent=recent, limit=200)
        _last['results'] = [r.song for r in results]
        tokens = _search.tokenize(query)
        choices: list = []
        if _show_editor and results:
            choices.append(prompt.Choice(
                title=f"Edit tags — all {len(results)} results", value="__bulk_edit__"))
        for r in results:
            choices.append(prompt.Choice(title=r.song.get('title', ''), value=r.song['path'],
                                         cells=_search_result_cells(r, tokens)))
        return choices

    def _hdr() -> list:
        mode = _SCOPE_CYCLE[scope['i']]
        label = "all fields" if mode == 'all' else mode
        return _menu_header("Search", f"scope: {label} · Tab to change")()

    selected = prompt.live_select(
        "Search:", _provider, columns=_SEARCH_COLUMNS,
        header=_hdr, on_cycle=_cycle)
    if not selected:
        return None

    if selected == "__bulk_edit__":
        bulk_id3_manager(library, paths=[s['path'] for s in _last['results']])
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _action_choices = ["Play"]
    if _show_lyrics and find_lyrics(selected):
        _action_choices.append("Edit Lyrics")
    if _show_editor:
        _action_choices.append("Edit Metadata")
    _action_choices += _queue_action_choices()

    if len(_action_choices) == 1 or _autoplay():
        action = "Play"
    else:
        action = prompt.select(
            "Action:",
            choices=_action_choices,
            header=_menu_header(track_title),
        )

    if action == "Play":
        ui_utils.clear_screen()
        res = music_player(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif _handle_queue_action(action, selected, track_title):
        pass
    elif action == "Edit Lyrics":
        ui_utils.clear_screen()
        lyrics_editor(selected)
        ui_utils.clear_screen()
    elif action == "Edit Metadata":
        ui_utils.clear_screen()
        inspect_tag_loop(selected, library_metadata=song_meta, library=library)
        ui_utils.clear_screen()

    return None


# Listening-history columns: title · artist · album · when (relative) · listened.
# Narrow terminals drop the least important first (priority; lower = sooner):
# album, then listened, then when, then artist. Title never drops (essential).
_HISTORY_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', max_frac=0.24, priority=4),
    prompt.Column(style='dynamic-dim', max_frac=0.24, priority=1),
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=2, priority=3),
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=2, priority=2),
]


def _autoplay() -> bool:
    """Whether selecting a track should play immediately, skipping the action menu."""
    return bool(load_config().get('autoplay_on_select', False))


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
    history_entries = get_history(limit=30)

    if not history_entries:
        ui_utils.show_status("No listening history available.")
        return None

    now = datetime.datetime.now()
    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        if song:
            title = song.get('title') or os.path.splitext(os.path.basename(path))[0]
            artist = (song.get('artist') or '').strip()
            album = (song.get('album') or '').strip()
            artist = '' if artist == 'Unknown Artist' else artist
            album = '' if album == 'Unknown Album' else album
        else:
            title = os.path.splitext(os.path.basename(path))[0]
            artist = album = ''
        choices.append(prompt.Choice(
            title=title, value=path,
            cells=[title, artist, album, _relative_time(ts, now), _nice_dur(dur)]))

    selected = prompt.select(
        "History:",
        choices=choices,
        columns=_HISTORY_COLUMNS,
        header=_menu_header("Listening History", f"{len(history_entries)} recent"),
        **_queue_shortcut_kwargs(library),
    )
    if not selected:
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _cfg = load_config()
    _action_choices = ["Play"]
    if _cfg.get("show_lyrics_editor", True) and find_lyrics(selected):
        _action_choices.append("Edit Lyrics")

    if len(_action_choices) == 1 or _autoplay():
        action = "Play"
    else:
        action = prompt.select(
            "Action:",
            choices=_action_choices,
            header=_menu_header(track_title),
        )

    if action == "Play":
        ui_utils.clear_screen()
        res = music_player(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif action == "Edit Lyrics":
        ui_utils.clear_screen()
        lyrics_editor(selected)
        ui_utils.clear_screen()

    return None


def _handle_tag_name_preferences() -> None:
    """Settings sub-screen: edit preferred friendly names for tags via list_edit."""
    config = load_config()
    prefs: dict = dict(config.get('tag_name_preferences', {}))
    initial: list = [(tag_id, prefs.get(tag_id, "")) for tag_id in TAG_REGISTRY]

    def _tag_pref_hints(col: int, row: list) -> list:
        """Suggest the tag registry's default display name as a hint for the preferred-name column."""
        if col != 1:
            return []
        tag_id = row[0].strip() if row else ""
        info = TAG_REGISTRY.get(tag_id)
        return info.name if info else []

    result = prompt.list_edit(
        "Tag name preferences — TAG · PREFERRED NAME (blank name = use default):",
        initial_items=initial,
        headers=("TAG", "PREFERRED NAME"),
        col_ratios=(1, 4),
        col_hints=_tag_pref_hints,
        fixed_rows=True,
        locked_cols={0},
    )
    if result is None:
        return

    new_prefs: dict = {}
    for row in result:
        cols = list(row) if isinstance(row, (list, tuple)) else [str(row), ""]
        while len(cols) < 2:
            cols.append("")
        tag_id, name = str(cols[0]).strip(), str(cols[1]).strip()
        if tag_id and name and tag_id in TAG_REGISTRY:
            new_prefs[tag_id] = name

    changed = sum(1 for k, v in new_prefs.items() if prefs.get(k) != v) + \
              sum(1 for k in prefs if k not in new_prefs)
    config['tag_name_preferences'] = new_prefs
    save_config(config)
    ui_utils.show_status(f"Tag name preferences saved ({changed} changed).")


def handle_settings(library_ref: list) -> None:
    """Run the interactive settings menu loop, applying and persisting each toggled option."""
    config = load_config()
    _cursor = 0

    while True:
        _choices: list = [
            prompt.separator("PLAYBACK"),
            "Adjust Lyric Lead-in Time",
            "Toggle Auto-play on Select",
            prompt.separator("LIBRARY"),
            "Update Music Directory",
            "Activity Centre",
        ]
        _choices += [
            "Toggle Hidden Files",
            prompt.separator("EDITORS"),
            "Toggle Metadata Editor",
            "Toggle Lyrics Editor",
            "Toggle Plain-text Editing",
            "Tag Name Preferences",
            "Sort List Delimiter",
            prompt.separator("HISTORY"),
            "Toggle Listening History",
            "Clear History Log",
        ]

        choice = prompt.select(
            "Settings:",
            choices=_choices,
            header=_menu_header("Settings"),
            index=_cursor,
        )

        if not choice:
            break

        # Stay on the row that was just acted on, rather than jumping to the top.
        _cursor = next((i for i, c in enumerate(_choices)
                        if not isinstance(c, prompt.Choice) and c == choice), _cursor)

        if choice == "Activity Centre":
            notification_centre()

        elif choice == "Toggle Listening History":
            config["history_enabled"] = not config["history_enabled"]
            ui_utils.show_status(f"History: {'ENABLED' if config['history_enabled'] else 'DISABLED'}")

        elif choice == "Clear History Log":
            entry_count = len(get_history(limit=10 ** 9))
            if entry_count == 0:
                ui_utils.show_status("History is already empty.")
            elif prompt.confirm(f"Delete all {entry_count} history entries? Cannot be undone."):
                ui_utils.show_status("History cleared." if clear_history() else "Failed to clear history.")

        elif choice == "Toggle Auto-play on Select":
            config["autoplay_on_select"] = not config.get("autoplay_on_select", False)
            if config["autoplay_on_select"]:
                ui_utils.show_status("Auto-play: ON — selecting a track plays it immediately.")
            else:
                ui_utils.show_status("Auto-play: OFF — selecting shows the Play / Edit menu.")

        elif choice == "Adjust Lyric Lead-in Time":
            val = prompt.text("Lead-in seconds:", default=str(config["lyric_lead_in"]))
            if val is not None:
                try:
                    seconds = round(max(0.0, float(val)), 2)
                    config["lyric_lead_in"] = seconds
                    ui_utils.show_status(f"Lyric lead-in set to {seconds:g}s")
                except ValueError:
                    ui_utils.show_status("Enter a number (e.g. 2 or 1.5).")

        elif choice == "Toggle Metadata Editor":
            config["show_metadata_editor"] = not config.get("show_metadata_editor", True)
            state = "VISIBLE" if config["show_metadata_editor"] else "HIDDEN"
            ui_utils.show_status(f"Metadata editor: {state}")

        elif choice == "Toggle Lyrics Editor":
            config["show_lyrics_editor"] = not config.get("show_lyrics_editor", True)
            state = "VISIBLE" if config["show_lyrics_editor"] else "HIDDEN"
            ui_utils.show_status(f"Lyrics editor: {state}")

        elif choice == "Toggle Plain-text Editing":
            config["plain_text_editing"] = not config.get("plain_text_editing", False)
            if config["plain_text_editing"]:
                ui_utils.show_status("Plain-text editing: ON — raw text instead of the smart widgets.")
            else:
                ui_utils.show_status("Plain-text editing: OFF — smart date/fraction/people widgets.")

        elif choice == "Tag Name Preferences":
            _handle_tag_name_preferences()
            config = load_config()  # pick up changes written by the sub-handler
            continue

        elif choice == "Sort List Delimiter":
            current = config.get("sort_list_delimiter", "/")
            picked = prompt.select(
                f"Delimiter for multi-artist sort values (current: {current!r}):",
                choices=["/ (slash)", "| (pipe)", "; (semicolon)", ", (comma)"],
            )
            if picked:
                delim = picked.split()[0]
                config["sort_list_delimiter"] = delim
                ui_utils.show_status(f"Sort list delimiter set to {delim!r}.")

        elif choice == "Update Music Directory":
            new_root = prompt.path("Music directory:")
            if new_root and os.path.isdir(os.path.expanduser(new_root)):
                new_root = os.path.abspath(os.path.expanduser(new_root))
                config["music_directory"] = new_root
                ui_utils.show_status("Re-scanning library...")
                new_lib = build_library(
                    new_root,
                    ignore_hidden=config.get("ignore_hidden_files", False),
                )
                save_library_cache(new_lib, _async=False)
                existing_lib = library_ref[0]
                existing_lib.clear()
                existing_lib.extend(new_lib)
                start_background_sync(existing_lib)
                ui_utils.show_status(f"Done — {len(new_lib)} tracks.")

        elif choice == "Toggle Hidden Files":
            config["ignore_hidden_files"] = not config.get("ignore_hidden_files", False)
            state = "ON" if config["ignore_hidden_files"] else "OFF"
            ui_utils.show_status(f"Hidden file filter: {state}")
            if config["ignore_hidden_files"]:
                ui_utils.show_status("Rebuild library via Update Music Directory to apply.")

        save_config(config)


def play_queue(paths: list, mode: str = "linear", library: list | None = None) -> str | None:
    """Play a queue of file paths in the given mode (linear, shuffle, repeat_one, repeat_all)."""
    playlist = list(paths)
    if mode == "shuffle":
        random.shuffle(playlist)

    # Build display titles for the in-player queue view (falls back to filename).
    title_map = {}
    if library:
        title_map = {s['path']: (s.get('title') or os.path.basename(s['path'])) for s in library}
    titles = [title_map.get(p) or os.path.splitext(os.path.basename(p))[0] for p in playlist]

    # The shared session owns the queue and auto-advances in the background
    # (feature #14), so this just starts it and opens the player. Minimising the
    # player ('b'/Esc) returns here with audio still playing; Stop ('s') ends it.
    session_mode = {"repeat_one": REPEAT_ONE, "repeat_all": REPEAT_ALL}.get(mode, REPEAT_OFF)
    result = music_player(playlist[0], queue_titles=titles, queue_index=0,
                          queue_paths=playlist, mode=session_mode)
    if isinstance(result, dict) and result.get("status") == "QUIT_ALL":
        return "QUIT_ALL"
    return None


def _queue_action_choices() -> list:
    """Track-menu queue actions for the *search* results (#14), which use the
    `live_select` widget and so can't take the listing-level n/a shortcuts that
    Browse/History now use. Offered whenever something is playing or we're a
    joined window. Routes through active_session() (local host or remote)."""
    return ["Play next", "Add to queue"] if (active_session().is_active() or is_client()) else []


def _queue_shortcut_kwargs(library: list) -> dict:
    """`select()` row-action kwargs for the listing-level queue shortcuts (#14):
    ``n`` = Play next, ``a`` = Add to queue, acting on the highlighted track
    without leaving the list. Offered whenever something is playing, and also
    whenever we're a joined window (so you can build/start a queue on the host
    even if it's currently idle) — mirrors the old track-menu visibility. Routes
    through active_session() (local host or remote)."""
    if not (active_session().is_active() or is_client()):
        return {}

    def _do(action: str, value) -> None:
        # Ignore the Play-all / disc / work header rows — they aren't tracks.
        if not isinstance(value, str) or value.startswith("__"):
            return
        song = next((s for s in library if s.get('path') == value), None)
        title = song['title'] if song else os.path.basename(value)
        _handle_queue_action(action, value, title)

    return {
        'row_actions': {
            'n': lambda v: _do("Play next", v),
            'a': lambda v: _do("Add to queue", v),
        },
        'row_action_hints': {'n': 'play next', 'a': 'queue'},
    }


def _handle_queue_action(action: str | None, path: str, title: str) -> bool:
    """Dispatch a Play-next / Add-to-queue action against the active session
    (local host or remote); returns True if ``action`` was one of them."""
    if action not in ("Play next", "Add to queue"):
        return False
    a = active_session()
    if not a.is_active():
        # Nothing playing yet — queueing starts the session with this track so
        # the now-playing box/panel appear and can be opened.
        a.start(path, queue=[path], titles=[title])
        ui_utils.show_status(f"▶ {title}")
        return True
    if action == "Play next":
        a.play_next(path, title)
        ui_utils.show_status(f"Playing next: {title}")
    else:
        n = a.enqueue(path, title)
        # A remote host owns the real count, so only show it when we host locally.
        ui_utils.show_status(f"Added to queue ({n} in queue): {title}" if n else f"Added to queue: {title}")
    return True


def browse_menu(library_ref: list, cat_choice: str) -> str | None:
    """Drive the multi-level browse UI (groups → albums → tracks → actions) for one category."""
    library = library_ref[0]
    sorted_lib = sort_library_logic(library)

    NAV_STACK.append(cat_choice)
    _group_cursor  = None            # None → land on the first real row
    _group_sorts   = _GROUP_SORTS.get(cat_choice, _GROUP_SORTS["Artists"])
    _group_sort    = _group_sorts[0][0]   # default: alphabetical by name
    _letter_mode   = None            # None → decide dynamically on first render
    _letter_filter = None            # active letter, or None = show everything

    try:
        while True:  # LEVEL 2: Group Selection
            key_map = {"Artists": "artist", "Albums": "album", "Genres": "genre"}
            grouped  = get_grouped_data(sorted_lib, key_map[cat_choice])
            _cat_key = key_map.get(cat_choice, cat_choice)
            _cfg = load_config()
            _show_editor = _cfg.get("show_metadata_editor", True)

            # Name-sorted first so the letter index groups correctly.
            group_names = sorted(grouped.keys(),
                                 key=lambda n: get_group_sort_key(n, grouped[n], _cat_key))
            _sort_keys = {n: get_group_sort_key(n, grouped[n], _cat_key) for n in group_names}

            def _letter(name: str) -> str:
                """Alphabetical index letter for name, or '#' if non-alphabetic."""
                ch = _sort_keys[name][0].upper() if _sort_keys.get(name) else "#"
                return ch if ch in string.ascii_uppercase else "#"

            letters_found = sorted({_letter(n) for n in group_names})
            if "#" in letters_found:
                letters_found.remove("#")
                letters_found.append("#")

            # A letter index is offered for alphabetical categories with more than
            # one distinct letter; it defaults on when the full list would overflow
            # the screen, and can be toggled with "/".
            _can_letter = cat_choice in ("Artists", "Albums") and len(letters_found) > 1
            _vis = max(4, ui_utils.get_terminal_height() - 9)
            if _letter_mode is None:
                _letter_mode = _can_letter and len(group_names) > _vis
            if not _can_letter:
                _letter_mode = False

            # Hybrid header: Play all stays visible (also `p`); secondary actions
            # live in the hint bar (`s` sort, `e` edit, `/` toggle view).
            _play_label = f"▸  Play all {cat_choice.lower()}"
            _sc: dict = {"p": "__play_all__"}
            _eh: dict = {"p": "play all"}
            if _show_editor:
                _sc["e"] = "__bulk_edit__"
                _eh["e"] = "edit tags"
            if _can_letter:
                _sc["/"] = "__toggle__"
                _eh["/"] = "full list" if _letter_mode else "by letter"

            if _letter_mode:
                # LETTER VIEW: compact A–Z index. Play all / Edit act on everything.
                names = group_names
                _choices = [prompt.Choice(title=_play_label, value="__play_all__")]
                _choices += letters_found
                _group_cols = None
                _header = _menu_header(cat_choice, "by letter")
            else:
                # FULL LIST VIEW: every item, optionally filtered to one letter.
                names = ([n for n in group_names if _letter(n) == _letter_filter]
                         if _letter_filter else list(group_names))
                names = _sort_groups(names, grouped, _cat_key, _group_sort)
                if len(_group_sorts) > 1:
                    _sc["s"] = "__sort__"
                    _eh["s"] = "sort"
                _choices = [prompt.Choice(title=_play_label, value="__play_all__")]
                # In album browse, show the album artist beside each album (dimmed),
                # matching the artist/genre → album sublist.
                if cat_choice == "Albums":
                    for _a in names:
                        _choices.append(prompt.Choice(
                            title=_a, value=_a, cells=[_a, _album_artist_of(grouped[_a])]))
                    _group_cols = _ALBUM_COLUMNS
                else:
                    _choices += names
                    _group_cols = None
                _sub = f"letter: {_letter_filter}" if _letter_filter else None
                _header = _menu_header(cat_choice, _sub)

            if _group_cursor is None:                 # start on the first real row
                _group_cursor = 1 if len(_choices) > 1 else 0

            selection = prompt.select(
                f"{cat_choice}:",
                choices=_choices,
                header=_header,
                columns=_group_cols,
                shortcuts=_sc,
                extra_hints=_eh,
                index=_group_cursor,
            )

            if not selection:
                # Back from a letter's filtered list → return to the A–Z index,
                # not out of the whole category. (Only when we actually drilled in
                # via a letter; a plain or toggled full list still exits.)
                if _letter_filter and _can_letter:
                    _restore_letter = _letter_filter
                    _letter_mode    = True
                    _letter_filter  = None
                    _group_cursor   = (1 + letters_found.index(_restore_letter)
                                       if _restore_letter in letters_found else None)
                    continue
                break

            if selection == "__toggle__":
                _letter_mode   = not _letter_mode
                _letter_filter = None
                _group_cursor  = None
                continue

            if selection == "__sort__":
                _group_sort = _pick_sort(_group_sort, _group_sorts, _header)
                continue

            if selection == "__play_all__":
                paths = [s['path'] for name in names for s in grouped[name]]
                res = play_queue(paths, library=library)
                if res == "QUIT_ALL":
                    NAV_STACK.clear()
                    NAV_STACK.append("Home")
                    return "QUIT_ALL"
                continue

            if selection == "__bulk_edit__":
                paths = [s['path'] for name in names for s in grouped[name]]
                bulk_id3_manager(library, paths=paths)
                continue

            if _letter_mode and selection in letters_found:
                _letter_filter = selection    # drill into that letter's full list
                _letter_mode   = False
                _group_cursor  = None
                continue

            _group_cursor = _idx_of(_choices, selection)
            NAV_STACK.append(selection)
            selected_songs = grouped[selection]
            _album_cursor = None                 # None → land on the first real row
            _album_sort   = _ALBUM_SORTS[0][0]   # default: alphabetical by name

            while True:  # LEVEL 3: Album Selection
                if cat_choice in ("Artists", "Genres"):
                    albums = {}
                    for s in selected_songs:
                        albums.setdefault(s['album'], []).append(s)

                    album_list = _sort_groups(list(albums.keys()), albums, 'album', _album_sort)

                    # Always present the album list — even a single album is worth
                    # showing as its own entry (albums are distinct from the artist).
                    # Hybrid header: Play all visible (also `p`); sort/edit → hints.
                    _show_editor = _cfg.get("show_metadata_editor", True)
                    _single_album = len(album_list) <= 1
                    _alb_choices: list = []
                    _asc: dict = {}
                    _aeh: dict = {}
                    if not _single_album:
                        _alb_choices.append(prompt.Choice(title=f"▸  Play all — {selection}", value="__play_all__"))
                        _asc["p"] = "__play_all__"; _aeh["p"] = "play all"
                        if len(_ALBUM_SORTS) > 1:
                            _asc["s"] = "__sort__"; _aeh["s"] = "sort"
                        if _show_editor:
                            _asc["e"] = "__bulk_edit__"; _aeh["e"] = "edit tags"
                    # Show the album artist (dimmed) when it differs from the
                    # artist/genre we're browsing under (#33).
                    for _a in album_list:
                        _aa = (albums[_a][0].get('album_artist') or '').strip()
                        _aa = _aa if (_aa and _aa.lower() != selection.lower()) else ""
                        _alb_choices.append(prompt.Choice(title=_a, value=_a, cells=[_a, _aa]))

                    if _album_cursor is None:        # start on the first real album
                        _album_cursor = 0 if _single_album else 1

                    alb = prompt.select(
                        "Albums:",
                        choices=_alb_choices,
                        header=_menu_header(selection, cat_choice),
                        columns=_ALBUM_COLUMNS,
                        shortcuts=_asc,
                        extra_hints=_aeh,
                        index=_album_cursor,
                    )

                    if not alb:
                        break

                    if alb == "__sort__":
                        _album_sort = _pick_sort(_album_sort, _ALBUM_SORTS,
                                                 _menu_header(selection, cat_choice))
                        continue

                    if alb == "__play_all__":
                        res = play_queue([s['path'] for s in selected_songs], library=library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
                        continue

                    if alb == "__bulk_edit__":
                        bulk_id3_manager(library, paths=[s['path'] for s in selected_songs])
                        continue

                    _album_cursor = _idx_of(_alb_choices, alb)
                    NAV_STACK.append(alb)
                    track_paths = [s['path'] for s in albums[alb]]
                else:
                    track_paths = [s['path'] for s in selected_songs]

                _track_cursor = None   # None → land on the first real row
                while True:  # LEVEL 4: Track Selection
                    # Re-derive from library each iteration so tag edits show immediately
                    path_set     = set(track_paths)
                    final_tracks = [t for t in library if t['path'] in path_set]

                    final_tracks.sort(key=lambda x: (
                        to_num(x.get('disc', 1)),
                        to_num(x.get('track', 0)),
                    ))

                    discs = set(str(t.get('disc', '1')) for t in final_tracks)
                    has_multiple_discs = len(discs) > 1

                    track_choices  = []
                    current_disc   = None
                    current_work   = None
                    disc_track_map = {}
                    work_track_map = {}

                    for t in final_tracks:
                        disc_val = str(t.get('disc', '1'))
                        work     = t.get('work', '').strip()

                        disc_track_map.setdefault(disc_val, []).append(t['path'])
                        if work:
                            work_track_map.setdefault(work, []).append(t['path'])

                        # Disc header — left-bar section marker (#34)
                        if has_multiple_discs and disc_val != current_disc:
                            subtitle   = t.get('disc_subtitle', '')
                            disc_title = subtitle if subtitle else f"Disc {disc_val}"
                            track_choices.append(prompt.Choice(title=f"▎{disc_title}", value=f"__disc_{disc_val}", cursor_title=f"▍{disc_title}"))
                            current_disc = disc_val
                            current_work = None

                        # Work header — thinner bar, indented under the disc (#34)
                        if work and work != current_work:
                            d_pad = "  " if has_multiple_discs else ""
                            track_choices.append(prompt.Choice(title=f"{d_pad}▎{work}", value=f"__work__{work}", cursor_title=f"{d_pad}▍{work}"))
                            current_work = work

                        # Track row
                        base_pad = "  " if has_multiple_discs else ""
                        mv_pad   = "  " if work else ""
                        indent   = base_pad + mv_pad

                        mv_num  = roman(int(str(t.get('movement_number', '')).strip())) if t.get('movement_number') and t.get('movement_number') != "0" else ""
                        mv_name = t.get('movement_name', '').strip()

                        if mv_name and mv_num != "":
                            num_str = (str(t.get('track', '0')).zfill(2) + f" — {mv_num}.") if mv_num else str(t.get('track', '0')).zfill(2)
                            label   = f"{indent}{num_str} {mv_name}"
                        else:
                            label = f"{indent}{str(t.get('track', '0')).zfill(2)} — {t.get('title', 'Unknown')}"

                        # Structured cells: [title, featured artist, duration].
                        # The full artist is shown (only when it differs from the
                        # album artist); no string parsing, so any characters are safe.
                        _aa = (t.get('album_artist') or '').strip()
                        _ar = (t.get('artist') or '').strip()
                        _artist = _ar if (_ar and _ar.lower() != _aa.lower()) else ""
                        _dur = t.get('duration') or 0
                        _dur_str = ui_utils.format_time(int(_dur)) if _dur else ""

                        track_choices.append(prompt.Choice(
                            title=label, value=t['path'],
                            cells=[label, _artist, _dur_str]))

                    _track_context = NAV_STACK[-1] if NAV_STACK else selection
                    _show_editor   = _cfg.get("show_metadata_editor", True)
                    # Hybrid header: Play all visible (also `p`); edit → `e` hint.
                    # With a single track, even Play all is noise — the lone track
                    # row already does both jobs.
                    _tsc: dict = {}
                    _teh: dict = {}
                    if len(final_tracks) <= 1:
                        _track_header_choices = []
                    else:
                        _track_header_choices = [
                            prompt.Choice(title=f"▸  Play all — {_track_context}", value="__play_all__")
                        ]
                        _tsc["p"] = "__play_all__"; _teh["p"] = "play all"
                        if _show_editor:
                            _tsc["e"] = "__bulk_edit__"; _teh["e"] = "edit tags"

                    # Album artist shown in the header subtitle (#33).
                    _album_artist = next(
                        (t.get('album_artist', '').strip() for t in final_tracks
                         if t.get('album_artist', '').strip()), "")
                    _subtitle = _album_artist or (selection if _track_context != selection else cat_choice)

                    _all_track_choices = _track_header_choices + track_choices
                    if _track_cursor is None:        # start on the first real row
                        _track_cursor = len(_track_header_choices) if track_choices else 0

                    path_choice_obj = prompt.select(
                        "Tracks:",
                        choices=_all_track_choices,
                        header=_menu_header(_track_context, _subtitle),
                        columns=_TRACK_COLUMNS,
                        shortcuts=_tsc,
                        extra_hints=_teh,
                        index=_track_cursor,
                        **_queue_shortcut_kwargs(library),
                    )

                    if not path_choice_obj:
                        break

                    if path_choice_obj == "__play_all__":
                        res = play_queue([t['path'] for t in final_tracks], library=library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
                        continue

                    if path_choice_obj == "__bulk_edit__":
                        bulk_id3_manager(library, paths=[t['path'] for t in final_tracks])
                        continue

                    _track_cursor = _idx_of(_all_track_choices, path_choice_obj)

                    # Disc header selected — offer play or bulk edit for that disc
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__disc_"):
                        disc_val   = path_choice_obj[len("__disc_"):]
                        disc_paths = disc_track_map.get(disc_val, [])
                        subtitle   = next(
                            (t.get('disc_subtitle', '') for t in final_tracks if str(t.get('disc', '1')) == disc_val),
                            '',
                        )
                        disc_label   = subtitle if subtitle else f"Disc {disc_val}"
                        _show_editor = _cfg.get("show_metadata_editor", True)
                        _disc_choices = [prompt.Choice(title=f"▸  Play all — {disc_label}", value="__play_all__")]
                        if _show_editor:
                            _disc_choices.append(prompt.Choice(title=f"Edit tags — {disc_label}", value="__bulk_edit__"))

                        # With only "Play all" (editor hidden), skip the extra
                        # single-option screen and play the disc immediately.
                        if len(_disc_choices) == 1:
                            disc_action = "__play_all__"
                        else:
                            disc_action = prompt.select(
                                "Disc:",
                                choices=_disc_choices,
                                header=_menu_header(disc_label, _track_context),
                            )
                        if disc_action == "__play_all__":
                            res = play_queue(disc_paths, library=library)
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                        elif disc_action == "__bulk_edit__":
                            bulk_id3_manager(library, paths=disc_paths)
                        continue

                    # Work header selected — offer play or bulk edit for that work
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__work__"):
                        work_name    = path_choice_obj[len("__work__"):]
                        work_paths   = work_track_map.get(work_name, [])
                        _show_editor = _cfg.get("show_metadata_editor", True)
                        _work_choices = [prompt.Choice(title=f"▸  Play all — {work_name}", value="__play_all__")]
                        if _show_editor:
                            _work_choices.append(prompt.Choice(title=f"Edit tags — {work_name}", value="__bulk_edit__"))

                        # Only "Play all" (editor hidden) → skip the extra screen.
                        if len(_work_choices) == 1:
                            work_action = "__play_all__"
                        else:
                            work_action = prompt.select(
                                "Work:",
                                choices=_work_choices,
                                header=_menu_header(work_name, _track_context),
                            )
                        if work_action == "__play_all__":
                            res = play_queue(work_paths, library=library)
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                        elif work_action == "__bulk_edit__":
                            bulk_id3_manager(library, paths=work_paths)
                        continue

                    # LEVEL 5: Track action
                    selected_track = next((t for t in final_tracks if t['path'] == path_choice_obj), None)
                    track_title    = selected_track['title'] if selected_track else os.path.basename(path_choice_obj)
                    track_artist   = selected_track.get('artist', '') if selected_track else ''
                    NAV_STACK.append(track_title)

                    _cfg_track = load_config()
                    while True:
                        _action_choices = ["Play"]
                        if _cfg_track.get("show_lyrics_editor", True) and find_lyrics(path_choice_obj):
                            _action_choices.append("Edit Lyrics")
                        if _cfg_track.get("show_metadata_editor", True):
                            _action_choices.append("Edit Metadata")

                        if len(_action_choices) == 1 or _autoplay():
                            action = "Play"
                        else:
                            action = prompt.select(
                                "Action:",
                                choices=_action_choices,
                                header=_menu_header(track_title, track_artist),
                            )

                        if not action:
                            break

                        if action == "Play":
                            ui_utils.clear_screen()
                            res = music_player(path_choice_obj)
                            ui_utils.clear_screen()
                            if res and res.get("status") == "QUIT_ALL":
                                return "QUIT_ALL"
                            if len(_action_choices) == 1 or _autoplay():
                                break

                        elif action == "Edit Lyrics":
                            ui_utils.clear_screen()
                            lyrics_editor(path_choice_obj)
                            ui_utils.clear_screen()

                        elif action == "Edit Metadata":
                            ui_utils.clear_screen()
                            inspect_tag_loop(path_choice_obj, library_metadata=selected_track, library=library)
                            ui_utils.clear_screen()
                            _cfg_track = load_config()

                    NAV_STACK.pop()

                if cat_choice in ("Artists", "Genres"):
                    NAV_STACK.pop()
                else:
                    break

            NAV_STACK.pop()

    finally:
        if NAV_STACK and NAV_STACK[-1] == cat_choice:
            NAV_STACK.pop()


def handle_browse(library_ref: list) -> str | None:
    """Show the "Browse by" category menu and route into browse_menu for the choice."""
    _cursor = 0
    _opts = ["Artists", "Albums", "Genres"]
    while True:
        choice = prompt.select(
            "Browse by:",
            choices=_opts,
            header=_menu_header("Browse"),
            index=_cursor,
        )
        if not choice:
            break
        _cursor = _idx_of(_opts, choice)
        res = browse_menu(library_ref, choice)
        if res == "QUIT_ALL":
            return "QUIT_ALL"
    return None


def notification_centre() -> None:
    """A live panel of current background activities — opens from Settings or by
    clicking the status-bar ● beacon. Lists each running job with its live status
    and a pulsing dot, updating as they start/finish; closes on Esc / b / q, and
    shows a placeholder when nothing is running."""
    import sys
    import time
    from src.utils.terminal_input import raw_mode, get_key_non_blocking, clear_escape_buffer

    def _draw() -> None:
        tasks = list(ui_utils.BACKGROUND_TASKS.values())
        out = ["\033[H\033[3J\033[J" + C.HIDE,
               f"\n  {C.BOLD}Activity{C.RESET}"]
        out.append(f"   {C.DIM}{len(tasks)} running{C.RESET}\n\n" if tasks else "\n\n")
        if tasks:
            for msg in tasks:
                out.append(f"   {ui_utils.pulse_circle()}  {msg}\n")
        else:
            out.append(f"   {C.DIM}Nothing running right now.{C.RESET}\n")
        out.append("\n\n" + prompt._hint(("esc/b", "back")))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    with raw_mode(sys.stdin):
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
                clear_escape_buffer()
                if key in ('b', 'B', 'q', 'Q', '\x1b') or key == 'ESC':
                    break
            time.sleep(0.08)
    ui_utils.clear_screen()


def main_menu(library_ref: list) -> None:
    """Run the top-level main menu loop, dispatching to browse/search/history/settings."""
    _cursor = 0
    _opts = ["Browse", "Search", "Listening History", "Settings", "Exit"]
    while True:
        choice = prompt.select(
            "Main Menu:",
            choices=_opts,
            header=_menu_header("Music Player"),
            index=_cursor,
        )

        if not choice or choice == "Exit":
            break
        _cursor = _idx_of(_opts, choice)

        if choice == "Browse":
            res = handle_browse(library_ref)
            if res == "QUIT_ALL":
                break
        elif choice == "Search":
            res = handle_search(library_ref[0])
            if res == "QUIT_ALL":
                break
        elif choice == "Listening History":
            res = handle_history(library_ref[0])
            if res == "QUIT_ALL":
                break
        elif choice == "Settings":
            handle_settings(library_ref)
