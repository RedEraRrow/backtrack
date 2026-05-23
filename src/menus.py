"""
Navigation menus and user interaction handlers.

All top-level menu functions for browsing, searching, and settings.
"""

import os
import time
import string

from src import prompt
from src import ui_utils
from src.music_library import (
    build_library, load_library_cache, save_library_cache,
    load_xml_database, get_grouped_data, get_group_sort_key, search_library, sort_library_logic
)
from src.history import get_history, clear_history
from src.playback import musicplayer
from src.lyric_timer import sync_lyrics
from src.config import load_config, save_config
from src.state import NAV_STACK
from src.metadata_browser import inspect_tag_loop, bulk_tag_manager, browse_metadata


# ── Header helper ─────────────────────────────────────────────────────────────

def _menu_header(title: str, subtitle: str | None = None):
    """
    Returns a resize-aware callable for prompt.select's header= parameter.

    Renders a divider + bold title line, plus an optional dimmed subtitle.
    Re-evaluated on every draw so it always fills the current terminal width.
    """
    C = ui_utils.Colours

    def _build() -> list[str]:
        cols = ui_utils.get_terminal_width()
        lines = [
            f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}",
            f"  {C.PRIMARY}{C.BOLD}{title}{C.RESET}",
        ]
        if subtitle:
            lines.append(f"  {C.DIM}{subtitle}{C.RESET}")
        lines.append(f"{C.DIM}{ui_utils.divider(cols)}{C.RESET}")
        return lines

    return _build


# ── Search ────────────────────────────────────────────────────────────────────

def handle_search(library: list) -> str | None:
    """Handle search menu and track selection."""
    if not library:
        print("Library is empty. Scan a directory first.")
        time.sleep(1.5)
        return None

    search_targets = prompt.checkbox(
        "Search within:",
        choices=[
            {"name": "Title",     "value": "title",  "checked": True},
            {"name": "Artist",    "value": "artist", "checked": True},
            {"name": "Album",     "value": "album",  "checked": True},
            {"name": "Genre",     "value": "genre",  "checked": False},
            {"name": "File Path", "value": "path",   "checked": False},
        ]
    )
    if not search_targets:
        return None

    query = prompt.text("Search:")
    if not query:
        return None

    matches = search_library(library, query, search_targets)
    if not matches:
        print("No matching tracks found.")
        time.sleep(1.5)
        return None

    choices = [
        prompt.Choice(title=f"{s['title']} — {s['artist']} ({s['album']})", value=s['path'])
        for s in matches[:20]
    ]
    selected = prompt.select(
        f"{len(matches)} match(es) — top 20:",
        choices=choices + [".. Back"],
        header=_menu_header("Search Results", query),
    )
    if not selected or selected == ".. Back":
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _show_editor = load_config().get("show_metadata_editor", True)
    _action_choices = ["Play", "Sync Lyrics"]
    if _show_editor:
        _action_choices.append("Edit Metadata")
    _action_choices.append(".. Back")

    action = prompt.select(
        "Action:",
        choices=_action_choices,
        header=_menu_header(track_title),
    )
    if action == "Play":
        ui_utils.clear_screen()
        res = musicplayer(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif action == "Sync Lyrics":
        ui_utils.clear_screen()
        sync_lyrics(selected)
        ui_utils.clear_screen()
    elif action == "Edit Metadata":
        ui_utils.clear_screen()
        inspect_tag_loop(selected, library_metadata=song_meta, library=library)
        ui_utils.clear_screen()

    return None


# ── History ───────────────────────────────────────────────────────────────────

def handle_history(library: list) -> str | None:
    """Handle listening history menu."""
    history_entries = get_history(limit=30)

    if not history_entries:
        print("No listening history available.")
        time.sleep(1.5)
        return None

    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        title = f"{song['title']} — {song['artist']}" if song else os.path.basename(path)
        choices.append(prompt.Choice(title=f"[{ts}] {title} ({dur})", value=path))

    selected = prompt.select(
        "History:",
        choices=choices + [".. Back"],
        header=_menu_header("Listening History"),
    )
    if not selected or selected == ".. Back":
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    action = prompt.select(
        "Action:",
        choices=["Play", "Sync Lyrics", ".. Back"],
        header=_menu_header(track_title),
    )
    if action == "Play":
        ui_utils.clear_screen()
        res = musicplayer(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif action == "Sync Lyrics":
        ui_utils.clear_screen()
        sync_lyrics(selected)
        ui_utils.clear_screen()

    return None


# ── Settings ──────────────────────────────────────────────────────────────────

def handle_settings(library_ref: list) -> None:
    """Handle settings menu."""
    config = load_config()

    while True:
        choice = prompt.select(
            "Settings:",
            choices=[
                "Toggle Listening History",
                "Clear History Log",
                "Adjust Lyric Lead-in Time",
                "Change UI Theme",
                "Change Player View",
                "Toggle Metadata Editor",
                "Update Music Directory",
                ".. Back",
            ],
            header=_menu_header("Settings"),
        )

        if not choice or choice == ".. Back":
            break

        elif choice == "Toggle Listening History":
            config["history_enabled"] = not config["history_enabled"]
            print(f"History: {'ENABLED' if config['history_enabled'] else 'DISABLED'}")
            time.sleep(1)

        elif choice == "Clear History Log":
            if prompt.confirm("Sure? Cannot be undone."):
                if clear_history():
                    print("History cleared.")
                else:
                    print("Failed to clear history.")
                time.sleep(1)

        elif choice == "Adjust Lyric Lead-in Time":
            val = prompt.text(f"Lead-in seconds (current {config['lyric_lead_in']}):")
            if val and val.replace('.', '', 1).isdigit():
                config["lyric_lead_in"] = float(val)
                print(f"Updated to {config['lyric_lead_in']} seconds")
                time.sleep(1)

        elif choice == "Change UI Theme":
            colours = {
                "Red":     "\033[1;31m",
                "Blue":    "\033[1;34m",
                "Magenta": "\033[1;35m",
                "Cyan":    "\033[1;36m",
            }
            pick = prompt.select(
                "Accent colour:",
                choices=list(colours.keys()) + [".. Back"],
                header=_menu_header("Settings", "Change UI Theme"),
            )
            if pick and pick != ".. Back":
                config["theme"]["accent"] = colours[pick]
                print(f"Theme changed to {pick}")
                time.sleep(1)

        elif choice == "Change Player View":
            view_options = [
                prompt.Choice("Default  — metadata + ASCII art",       value="default"),
                prompt.Choice("iPod 2G  — classic Now Playing screen", value="ipod"),
            ]
            pick = prompt.select(
                "Player view:",
                choices=view_options + [prompt.Choice(".. Back", value=".. Back")],
                header=_menu_header("Settings", "Change Player View"),
            )
            if pick and pick != ".. Back":
                config["player_view"] = pick
                label = "Default" if pick == "default" else "iPod 2G"
                print(f"Player view set to: {label}")
                time.sleep(1)

        elif choice == "Toggle Metadata Editor":
            config["show_metadata_editor"] = not config.get("show_metadata_editor", True)
            state = "VISIBLE" if config["show_metadata_editor"] else "HIDDEN"
            print(f"Metadata editor: {state}")
            time.sleep(1)

        elif choice == "Update Music Directory":
            new_root = prompt.path("Music directory:")
            if new_root and os.path.isdir(new_root):
                config["music_directory"] = new_root
                print("Re-scanning library...")
                xml_db, xml_title_keys = load_xml_database("../data/Library.xml")
                new_lib = build_library(new_root, xml_db=xml_db, xml_title_keys=xml_title_keys)
                save_library_cache(new_lib, _async=False)
                library_ref[0] = new_lib
                print(f"Done — {len(new_lib)} tracks.")
                time.sleep(1.5)

        save_config(config)


# ── Queue playback ────────────────────────────────────────────────────────────

def play_queue(paths: list, mode: str = "linear") -> str | None:
    """
    Play a queue of songs.

    Args:
        paths: List of file paths to play
        mode: "linear", "shuffle", "repeat_one", or "repeat_all"
    """
    import random

    playlist = list(paths)
    if mode == "shuffle":
        random.shuffle(playlist)

    idx = 0
    while idx < len(playlist):
        result = musicplayer(playlist[idx])
        if isinstance(result, dict):
            status = result.get("status", "")
            if status == "QUIT_ALL":
                return "QUIT_ALL"
            if status == "PREVIOUS":
                idx = max(0, idx - 1)
                continue
            if status == "BACK" and mode == "repeat_one":
                idx += 1
                continue

        if mode == "repeat_one":
            continue

        idx += 1
        if mode == "repeat_all" and idx >= len(playlist):
            idx = 0

    return None


# ── Browse ────────────────────────────────────────────────────────────────────

def browse_menu(library_ref: list) -> str | None:
    """Handle library browsing menu."""
    library = library_ref[0]
    sorted_lib = sort_library_logic(library)

    NAV_STACK.append("Browse")

    try:
        while True:  # LEVEL 1: Browse By
            cat_choice = prompt.select(
                "Browse by:",
                choices=["Artists", "Albums", "Genres", "Groupings", ".. Back"],
                header=_menu_header("Library"),
            )

            if not cat_choice or cat_choice == ".. Back":
                break

            NAV_STACK.append(cat_choice)

            while True:  # LEVEL 2: Group Selection
                key_map = {"Artists": "artist", "Albums": "album", "Genres": "genre", "Groupings": "grouping"}
                grouped  = get_grouped_data(sorted_lib, key_map[cat_choice])
                _cat_key = key_map.get(cat_choice, cat_choice)
                group_names = sorted(grouped.keys(), key=lambda n: get_group_sort_key(n, grouped[n], _cat_key))

                if cat_choice in ("Artists", "Albums"):
                    _sort_keys = {n: get_group_sort_key(n, grouped[n], _cat_key) for n in group_names}

                    def _letter(name: str) -> str:
                        ch = _sort_keys[name][0].upper() if _sort_keys.get(name) else "#"
                        return ch if ch in string.ascii_uppercase else "#"

                    letters_found = sorted({_letter(n) for n in group_names})
                    if "#" in letters_found:
                        letters_found.remove("#")
                        letters_found.append("#")

                    letter_sel = prompt.select(
                        "Filter by letter:",
                        choices=["All"] + letters_found + [".. Back"],
                        header=_menu_header(cat_choice),
                    )

                    if not letter_sel or letter_sel == ".. Back":
                        break

                    if letter_sel != "All":
                        group_names = [n for n in group_names if _letter(n) == letter_sel]

                # Section name at top acts as "play everything here"
                _show_editor = load_config().get("show_metadata_editor", True)
                _group_label = f"▶  Play all {cat_choice.lower()}"
                _group_choices = [prompt.Choice(title=_group_label, value="__play_all__")]
                if _show_editor:
                    _group_choices.append(prompt.Choice(title=f"Edit tags — all {cat_choice.lower()}", value="__bulk_edit__"))
                _group_choices += group_names
                _group_choices.append(".. Back")

                selection = prompt.select(
                    f"{cat_choice}:",
                    choices=_group_choices,
                    header=_menu_header(cat_choice),
                )

                if not selection or selection == ".. Back":
                    break

                if selection == "__play_all__":
                    paths = [s['path'] for name in group_names for s in grouped[name]]
                    res = play_queue(paths)
                    if res == "QUIT_ALL":
                        NAV_STACK.clear()
                        NAV_STACK.append("Home")
                        return "QUIT_ALL"
                    continue

                if selection == "__bulk_edit__":
                    paths = [s['path'] for name in group_names for s in grouped[name]]
                    bulk_tag_manager(library, paths=paths)
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

                        _show_editor = load_config().get("show_metadata_editor", True)
                        _alb_choices = [prompt.Choice(title=f"▶  Play all — {selection}", value="__play_all__")]
                        if _show_editor:
                            _alb_choices.append(prompt.Choice(title=f"Edit tags — {selection}", value="__bulk_edit__"))
                        _alb_choices += album_list
                        _alb_choices.append(".. Back")

                        alb = prompt.select(
                            "Albums:",
                            choices=_alb_choices,
                            header=_menu_header(selection, cat_choice),
                        )

                        if not alb or alb == ".. Back":
                            break

                        if alb == "__play_all__":
                            res = play_queue([s['path'] for s in selected_songs])
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                            continue

                        if alb == "__bulk_edit__":
                            bulk_tag_manager(library, paths=[s['path'] for s in selected_songs])
                            continue

                        NAV_STACK.append(alb)
                        track_paths = [s['path'] for s in albums[alb]]
                    else:
                        track_paths = [s['path'] for s in selected_songs]

                    while True:  # LEVEL 4: Track Selection
                        # Re-derive from library each iteration so tag edits show immediately
                        path_set    = set(track_paths)
                        final_tracks = [t for t in library if t['path'] in path_set]

                        def get_num(val):
                            try:
                                return float(str(val).split('/')[0])
                            except (ValueError, TypeError):
                                return 0.0

                        final_tracks.sort(key=lambda x: (
                            get_num(x.get('disc', 1)),
                            get_num(x.get('track', 0)),
                        ))

                        discs = set(str(t.get('disc', '1')) for t in final_tracks)
                        has_multiple_discs = len(discs) > 1

                        track_choices = []
                        current_disc  = None
                        disc_track_map = {}

                        for t in final_tracks:
                            disc_val = str(t.get('disc', '1'))
                            disc_track_map.setdefault(disc_val, []).append(t['path'])
                            if has_multiple_discs and disc_val != current_disc:
                                subtitle   = t.get('disc_subtitle', '')
                                disc_title = subtitle if subtitle else f"Disc {disc_val}"
                                track_choices.append(prompt.Choice(title=f"── {disc_title} ──", value=f"__disc_{disc_val}"))
                                current_disc = disc_val

                            indent = "  " if has_multiple_discs else ""
                            track_choices.append(
                                prompt.Choice(
                                    title=f"{indent}{str(t.get('track', '0')).zfill(2)} — {t.get('title', 'Unknown')}",
                                    value=t['path'],
                                )
                            )

                        # Determine header title: album name if we came via album, else artist/group name
                        _track_context = NAV_STACK[-1] if NAV_STACK else selection
                        _show_editor   = load_config().get("show_metadata_editor", True)
                        _track_header_choices = [
                            prompt.Choice(title=f"▶  Play all — {_track_context}", value="__play_all__")
                        ]
                        if _show_editor:
                            _track_header_choices.append(
                                prompt.Choice(title=f"Edit tags — {_track_context}", value="__bulk_edit__")
                            )

                        path_choice_obj = prompt.select(
                            "Tracks:",
                            choices=_track_header_choices + track_choices + [".. Back"],
                            header=_menu_header(_track_context, selection if _track_context != selection else cat_choice),
                        )

                        if not path_choice_obj or path_choice_obj == ".. Back":
                            break

                        if path_choice_obj == "__play_all__":
                            res = play_queue([t['path'] for t in final_tracks])
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                            continue

                        if path_choice_obj == "__bulk_edit__":
                            bulk_tag_manager(library, paths=[t['path'] for t in final_tracks])
                            continue

                        # Disc header selected — offer play or bulk edit for that disc
                        if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__disc_"):
                            disc_val   = path_choice_obj[len("__disc_"):]
                            disc_paths = disc_track_map.get(disc_val, [])
                            subtitle   = next(
                                (t.get('disc_subtitle', '') for t in final_tracks if str(t.get('disc', '1')) == disc_val),
                                '',
                            )
                            disc_label   = f"Disc {disc_val}" + (f" — {subtitle}" if subtitle else "")
                            _show_editor = load_config().get("show_metadata_editor", True)
                            _disc_choices = [prompt.Choice(title=f"▶  Play all — {disc_label}", value="__play_all__")]
                            if _show_editor:
                                _disc_choices.append(prompt.Choice(title=f"Edit tags — {disc_label}", value="__bulk_edit__"))
                            _disc_choices.append(".. Back")

                            disc_action = prompt.select(
                                "Disc:",
                                choices=_disc_choices,
                                header=_menu_header(disc_label, _track_context),
                            )
                            if disc_action == "__play_all__":
                                res = play_queue(disc_paths)
                                if res == "QUIT_ALL":
                                    return "QUIT_ALL"
                            elif disc_action == "__bulk_edit__":
                                bulk_tag_manager(library, paths=disc_paths)
                            continue

                        # LEVEL 5: Track action
                        selected_track = next((t for t in final_tracks if t['path'] == path_choice_obj), None)
                        track_title    = selected_track['title'] if selected_track else os.path.basename(path_choice_obj)
                        track_artist   = selected_track.get('artist', '') if selected_track else ''
                        NAV_STACK.append(track_title)

                        while True:
                            _cfg = load_config()
                            _action_choices = ["Play", "Sync Lyrics"]
                            if _cfg.get("show_metadata_editor", True):
                                _action_choices.append("Edit Metadata")
                            _action_choices.append(".. Back")

                            action = prompt.select(
                                "Action:",
                                choices=_action_choices,
                                header=_menu_header(track_title, track_artist),
                            )

                            if not action or action == ".. Back":
                                break

                            if action == "Play":
                                ui_utils.clear_screen()
                                res = musicplayer(path_choice_obj)
                                ui_utils.clear_screen()
                                if res and res.get("status") == "QUIT_ALL":
                                    return "QUIT_ALL"

                            elif action == "Sync Lyrics":
                                ui_utils.clear_screen()
                                sync_lyrics(path_choice_obj)
                                ui_utils.clear_screen()

                            elif action == "Edit Metadata":
                                ui_utils.clear_screen()
                                inspect_tag_loop(path_choice_obj, library_metadata=selected_track, library=library)
                                ui_utils.clear_screen()

                        NAV_STACK.pop()

                    if cat_choice in ("Artists", "Genres"):
                        NAV_STACK.pop()
                    else:
                        break

                NAV_STACK.pop()

            NAV_STACK.pop()

    finally:
        if NAV_STACK and NAV_STACK[-1] == "Browse":
            NAV_STACK.pop()


# ── Main menu ─────────────────────────────────────────────────────────────────

def main_menu(library_ref: list) -> None:
    """Main menu loop."""
    while True:
        choice = prompt.select(
            "Main Menu:",
            choices=["Browse Library", "Search", "Listening History", "Metadata Browser", "Settings", "Exit"],
            header=_menu_header("Music Player"),
        )

        if not choice or choice == "Exit":
            break
        elif choice == "Browse Library":
            res = browse_menu(library_ref)
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
        elif choice == "Metadata Browser":
            browse_metadata(library_ref[0])
        elif choice == "Settings":
            handle_settings(library_ref)