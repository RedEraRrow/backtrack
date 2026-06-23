from __future__ import annotations
import os
import random
import string

_LETTER_FILTER_THRESHOLD = 52  # show A-Z letter filter when a browse group exceeds this count
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
from src.lyrics.lyrics_editor import lyrics_editor
from src.config import load_config, save_config
from src.state import NAV_STACK
from src.id3.id3_browser import inspect_tag_loop
from src.id3.bulk_id3_manager import bulk_id3_manager

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


def handle_search(library: list) -> str | None:
    if not library:
        ui_utils.show_status("Library is empty. Scan a directory first.")
        return None

    search_targets = prompt.checkbox(
        "Search within:",
        choices=[
            {"name": "Title",         "value": "title",         "checked": True},
            {"name": "Artist",        "value": "artist",        "checked": True},
            {"name": "Album",         "value": "album",         "checked": True},
            {"name": "Genre",         "value": "genre",         "checked": False},
            {"name": "People",         "value": "people",         "checked": False},
        ]
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
    if _show_lyrics:
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

    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        title = f"{song['title']} — {song['artist']}" if song else os.path.basename(path)
        choices.append(prompt.Choice(title=f"[{ts}] {title} ({dur})", value=path))

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
    if _cfg.get("show_lyrics_editor", True):
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

    try:
        while True:  # LEVEL 2: Group Selection
            key_map = {"Artists": "artist", "Albums": "album", "Genres": "genre"}
            grouped  = get_grouped_data(sorted_lib, key_map[cat_choice])
            _cat_key = key_map.get(cat_choice, cat_choice)
            group_names = sorted(grouped.keys(), key=lambda n: get_group_sort_key(n, grouped[n], _cat_key))

            _cfg = load_config()

            if cat_choice in ("Artists", "Albums") and len(group_names) > _LETTER_FILTER_THRESHOLD:
                _sort_keys = {n: get_group_sort_key(n, grouped[n], _cat_key) for n in group_names}

                def _letter(name: str) -> str:
                    ch = _sort_keys[name][0].upper() if _sort_keys.get(name) else "#"
                    return ch if ch in string.ascii_uppercase else "#"

                letters_found = sorted({_letter(n) for n in group_names})
                if "#" in letters_found:
                    letters_found.remove("#")
                    letters_found.append("#")

                _show_editor = _cfg.get("show_metadata_editor", True)
                _letter_choices: list = ["All"]
                if _show_editor:
                    _letter_choices.append(prompt.Choice(title=f"Edit tags — all {cat_choice.lower()}", value="__bulk_edit__"))
                _letter_choices += letters_found

                letter_sel = prompt.select(
                    "Filter by letter:",
                    choices=_letter_choices,
                    header=_menu_header(cat_choice),
                )

                if not letter_sel:
                    break

                if letter_sel == "__bulk_edit__":
                    all_paths = [s['path'] for name in group_names for s in grouped[name]]
                    bulk_id3_manager(library, paths=all_paths)
                    continue

                if letter_sel != "All":
                    group_names = [n for n in group_names if _letter(n) == letter_sel]

            # Section name at top acts as "play everything here"
            _show_editor = _cfg.get("show_metadata_editor", True)
            _group_label = f"▶  Play all {cat_choice.lower()}"
            _group_choices = [prompt.Choice(title=_group_label, value="__play_all__")]
            if _show_editor:
                _group_choices.append(prompt.Choice(title=f"Edit tags — all {cat_choice.lower()}", value="__bulk_edit__"))
            _group_choices += group_names

            selection = prompt.select(
                f"{cat_choice}:",
                choices=_group_choices,
                header=_menu_header(cat_choice),
            )

            if not selection:
                break

            if selection == "__play_all__":
                paths = [s['path'] for name in group_names for s in grouped[name]]
                res = play_queue(paths, library=library)
                if res == "QUIT_ALL":
                    NAV_STACK.clear()
                    NAV_STACK.append("Home")
                    return "QUIT_ALL"
                continue

            if selection == "__bulk_edit__":
                paths = [s['path'] for name in group_names for s in grouped[name]]
                bulk_id3_manager(library, paths=paths)
                continue

            NAV_STACK.append(selection)
            selected_songs = grouped[selection]

            while True:  # LEVEL 3: Album Selection
                if cat_choice in ("Artists", "Genres"):
                    albums = {}
                    for s in selected_songs:
                        albums.setdefault(s['album'], []).append(s)

                    album_list = sorted(
                        albums.keys(),
                        key=lambda a: (albums[a][0].get('year') or 0, a),
                        reverse=True,
                    )

                    # Always present the album list — even a single album is worth
                    # showing as its own entry (albums are distinct from the artist).
                    _show_editor = _cfg.get("show_metadata_editor", True)
                    _single_album = len(album_list) <= 1
                    _alb_choices: list = []
                    if not _single_album:
                        _alb_choices.append(prompt.Choice(title=f"▶  Play all — {selection}", value="__play_all__"))
                        if _show_editor:
                            _alb_choices.append(prompt.Choice(title=f"Edit tags — {selection}", value="__bulk_edit__"))
                    _alb_choices += album_list

                    alb = prompt.select(
                        "Albums:",
                        choices=_alb_choices,
                        header=_menu_header(selection, cat_choice),
                    )

                    if not alb:
                        break

                    if alb == "__play_all__":
                        res = play_queue([s['path'] for s in selected_songs], library=library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
                        continue

                    if alb == "__bulk_edit__":
                        bulk_id3_manager(library, paths=[s['path'] for s in selected_songs])
                        continue

                    NAV_STACK.append(alb)
                    track_paths = [s['path'] for s in albums[alb]]
                else:
                    track_paths = [s['path'] for s in selected_songs]

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

                        # Disc header
                        if has_multiple_discs and disc_val != current_disc:
                            subtitle   = t.get('disc_subtitle', '')
                            disc_title = subtitle if subtitle else f"Disc {disc_val}"
                            track_choices.append(prompt.Choice(title=f"── {disc_title} ──", value=f"__disc_{disc_val}"))
                            current_disc = disc_val
                            current_work = None

                        # Work header
                        if work and work != current_work:
                            d_pad = "  " if has_multiple_discs else ""
                            track_choices.append(prompt.Choice(title=f"{d_pad}── {work} ──", value=f"__work__{work}"))
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

                        # Extra context: featured-artist marker when the track
                        # artist differs from the album artist, plus duration.
                        # Kept as plain text — select() truncates by raw length.
                        _aa = (t.get('album_artist') or '').strip()
                        _ar = (t.get('artist') or '').strip()
                        if _ar and _aa and _ar.lower() != _aa.lower():
                            label += f"  · {_ar}"
                        _dur = t.get('duration') or 0
                        if _dur:
                            label += f"  ({ui_utils.format_time(int(_dur))})"

                        track_choices.append(prompt.Choice(title=label, value=t['path']))

                    _track_context = NAV_STACK[-1] if NAV_STACK else selection
                    _show_editor   = _cfg.get("show_metadata_editor", True)
                    # With a single track, "Play all" / "Edit tags — all" are just
                    # noise — the lone track row already does both jobs.
                    if len(final_tracks) <= 1:
                        _track_header_choices = []
                    else:
                        _track_header_choices = [
                            prompt.Choice(title=f"▶  Play all — {_track_context}", value="__play_all__")
                        ]
                        if _show_editor:
                            _track_header_choices.append(
                                prompt.Choice(title=f"Edit tags — {_track_context}", value="__bulk_edit__")
                            )

                    path_choice_obj = prompt.select(
                        "Tracks:",
                        choices=_track_header_choices + track_choices,
                        header=_menu_header(_track_context, selection if _track_context != selection else cat_choice),
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
                        _disc_choices = [prompt.Choice(title=f"▶  Play all — {disc_label}", value="__play_all__")]
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
                        _work_choices = [prompt.Choice(title=f"▶  Play all — {work_name}", value="__play_all__")]
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
                        if _cfg_track.get("show_lyrics_editor", True):
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
    while True:
        choice = prompt.select(
            "Browse by:",
            choices=["Artists", "Albums", "Genres"],
            header=_menu_header("Browse"),
        )
        if not choice:
            break
        res = browse_menu(library_ref, choice)
        if res == "QUIT_ALL":
            return "QUIT_ALL"
    return None


def main_menu(library_ref: list) -> None:
    while True:
        choice = prompt.select(
            "Main Menu:",
            choices=["Browse", "Search", "Listening History", "Settings", "Exit"],
            header=_menu_header("Music Player"),
        )

        if not choice or choice == "Exit":
            break
        elif choice == "Browse":
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
