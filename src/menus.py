"""Top-level TUI menu hierarchy: browse, search, history, settings, and playback."""
from __future__ import annotations
import os
import random
import string

from src.utils.ui_utils import roman, Colors as C

from src.utils import prompt
from src.utils import ui_utils
from src.music_library import (
    build_library, save_library_cache,
    load_xml_database, start_background_sync,
    get_grouped_data, get_group_sort_key, search_library, sort_library_logic,
    to_num
)
from src.history import get_history, clear_history
from src.playback.playback import music_player
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
    prompt.Column(style='dynamic-dim', flex=True, align='left', gap=3),  # featured artist (full)
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=3),  # duration (pinned right)
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


def handle_search(library: list) -> str | None:
    if not library:
        ui_utils.show_status("Library is empty. Scan a directory first.")
        return None

    search_targets = prompt.select(
        "Search within:",
        choices=[
            {"name": "Title",         "value": "title",         "checked": True},
            {"name": "Artist",        "value": "artist",        "checked": True},
            {"name": "Album",         "value": "album",         "checked": True},
            {"name": "Genre",         "value": "genre",         "checked": False},
            {"name": "People",         "value": "people",         "checked": False},
        ],
        multi=True,
    )
    if not search_targets:
        return None

    query = prompt.text("Search:")
    if not query:
        return None

    matches = search_library(library, query, search_targets)
    if not matches:
        ui_utils.show_status("No matching tracks found.")
        return None

    choices = [
        prompt.Choice(title=f"{s['title']} — {s['artist']} ({s['album']})", value=s['path'])
        for s in matches
    ]
    _cfg = load_config()
    _show_editor = _cfg.get("show_metadata_editor", True)
    _show_lyrics = _cfg.get("show_lyrics_editor", True)
    _header_choices = []
    if _show_editor:
        _header_choices.append(prompt.Choice(title=f"Edit tags — all {len(matches)} results", value="__bulk_edit__"))
    selected = prompt.select(
        f"{len(matches)} result(s):",
        choices=_header_choices + choices,
        header=_menu_header("Search Results", query),
    )
    if not selected:
        return None

    if selected == "__bulk_edit__":
        bulk_id3_manager(library, paths=[s['path'] for s in matches])
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _action_choices = ["Play"]
    if _show_lyrics and find_lyrics(selected):
        _action_choices.append("Edit Lyrics")
    if _show_editor:
        _action_choices.append("Edit Metadata")

    if len(_action_choices) == 1:
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
    elif action == "Edit Metadata":
        ui_utils.clear_screen()
        inspect_tag_loop(selected, library_metadata=song_meta, library=library)
        ui_utils.clear_screen()

    return None


def handle_history(library: list) -> str | None:
    history_entries = get_history(limit=30)

    if not history_entries:
        ui_utils.show_status("No listening history available.")
        return None

    def _nice_dur(raw: str) -> str:
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

    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        if song:
            title = song.get('title') or os.path.splitext(os.path.basename(path))[0]
            artist = (song.get('artist') or '').strip()
            label = f"{title} — {artist}" if artist and artist != 'Unknown Artist' else title
        else:
            label = os.path.splitext(os.path.basename(path))[0]
        ts_short = ts[:16] if len(ts) >= 16 else ts  # drop seconds
        choices.append(prompt.Choice(title=f"{label}   ({_nice_dur(dur)} · {ts_short})", value=path))

    selected = prompt.select(
        "History:",
        choices=choices,
        header=_menu_header("Listening History"),
    )
    if not selected:
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _cfg = load_config()
    _action_choices = ["Play"]
    if _cfg.get("show_lyrics_editor", True) and find_lyrics(selected):
        _action_choices.append("Edit Lyrics")

    if len(_action_choices) == 1:
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
    config = load_config()
    _cursor = 0

    while True:
        _choices: list = [
            prompt.separator("PLAYBACK"),
            "Adjust Lyric Lead-in Time",
            prompt.separator("LIBRARY"),
            "Update Music Directory",
            "Update iTunes Library XML Path",
        ]
        if config.get("xml_db_path"):
            _choices.append("Clear iTunes Library XML Path")
        _choices += [
            "Toggle Hidden Files",
            prompt.separator("EDITORS"),
            "Toggle Metadata Editor",
            "Toggle Lyrics Editor",
            "Tag Name Preferences",
            "Sort List Delimiter",
            "Name Corpus Size",
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

        if choice == "Toggle Listening History":
            config["history_enabled"] = not config["history_enabled"]
            ui_utils.show_status(f"History: {'ENABLED' if config['history_enabled'] else 'DISABLED'}")

        elif choice == "Clear History Log":
            entry_count = len(get_history(limit=10 ** 9))
            if entry_count == 0:
                ui_utils.show_status("History is already empty.")
            elif prompt.confirm(f"Delete all {entry_count} history entries? Cannot be undone."):
                ui_utils.show_status("History cleared." if clear_history() else "Failed to clear history.")

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

        elif choice == "Name Corpus Size":
            current = config.get("name_corpus", "full")
            picked = prompt.select(
                f"Name corpus for sort-order suggestions (current: {current}):",
                choices=["full — ~2700 names, all naming traditions",
                         "minimal — ~230 names, English + classical only"],
            )
            if picked:
                size = picked.split()[0]
                config["name_corpus"] = size
                import src.id3.name_corpus as _corpus_mod
                _corpus_mod._load.cache_clear()
                ui_utils.show_status(f"Name corpus set to {size!r}.")

        elif choice == "Update Music Directory":
            new_root = prompt.path("Music directory:")
            if new_root and os.path.isdir(os.path.expanduser(new_root)):
                new_root = os.path.abspath(os.path.expanduser(new_root))
                config["music_directory"] = new_root
                ui_utils.show_status("Re-scanning library...")
                xml_db, xml_title_keys = load_xml_database(config["xml_db_path"] if config["xml_db_path"] else config.get("music_directory", ""))
                new_lib = build_library(
                    new_root,
                    xml_db=xml_db,
                    xml_title_keys=xml_title_keys,
                    ignore_hidden=config.get("ignore_hidden_files", False),
                )
                save_library_cache(new_lib, _async=False)
                existing_lib = library_ref[0]
                existing_lib.clear()
                existing_lib.extend(new_lib)
                start_background_sync(existing_lib, xml_db, xml_title_keys)
                ui_utils.show_status(f"Done — {len(new_lib)} tracks.")

        elif choice == "Update iTunes Library XML Path":
            new_path = prompt.path("iTunes Library XML file:")
            if new_path and os.path.isfile(new_path):
                config["xml_db_path"] = new_path
                xml_db, xml_title_keys = load_xml_database(new_path)
                if xml_db:
                    ui_utils.show_status(f"Metadata database loaded: {len(xml_db)} tracks")
                    start_background_sync(library_ref[0], xml_db, xml_title_keys)
                else:
                    ui_utils.show_status("Failed to load XML database.")

        elif choice == "Clear iTunes Library XML Path":
            config["xml_db_path"] = ""
            _cursor = 0  # option disappears from the list, so reset the cursor
            ui_utils.show_status("iTunes Library XML path cleared.")

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

    idx = 0
    while idx < len(playlist):
        result = music_player(playlist[idx], queue_titles=titles, queue_index=idx)
        if isinstance(result, dict):
            status = result.get("status", "")
            if status == "QUIT_ALL":
                return "QUIT_ALL"
            if status == "STOP":          # 'b' — stop the queue, back to browse (#41)
                return None
            if status == "PREVIOUS":
                idx = max(0, idx - 1)
                continue

        if mode == "repeat_one":
            continue

        idx += 1
        if mode == "repeat_all" and idx >= len(playlist):
            idx = 0

    return None


def browse_menu(library_ref: list, cat_choice: str) -> str | None:
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

                        if len(_action_choices) == 1:
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
                            if len(_action_choices) == 1:
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


def main_menu(library_ref: list) -> None:
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
