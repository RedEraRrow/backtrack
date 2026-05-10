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
    load_xml_database, get_grouped_data, search_library
)
from src.history import get_history, clear_history
from src.playback import musicplayer
from src.lyric_timer import sync_lyrics
from src.config import load_config, save_config
from src.state import NAV_STACK
from src.metadata_browser import inspect_tag_loop, bulk_tag_manager, browse_metadata


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
    selected = prompt.select(f"{len(matches)} match(es) (top 20):", choices=choices + [".. Back"])
    if not selected or selected == ".. Back":
        return None

    action = prompt.select("Action:", choices=["Play", "Sync Lyrics", "Edit Metadata", ".. Back"])
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
        song_meta = next((s for s in library if s['path'] == selected), None)
        ui_utils.clear_screen()
        inspect_tag_loop(selected, library_metadata=song_meta)
        ui_utils.clear_screen()
    
    return None


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

    selected = prompt.select("History:", choices=choices + [".. Back"])
    if not selected or selected == ".. Back":
        return None

    action = prompt.select("Action:", choices=["Replay", "Sync Lyrics", ".. Back"])
    if action == "Replay":
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


def handle_settings(library_ref: list) -> None:
    """Handle settings menu."""
    config = load_config()
    
    while True:
        ui_utils.clear_screen()
        choice = prompt.select(
            "Settings:",
            choices=[
                "Toggle Listening History",
                "Clear History Log",
                "Adjust Lyric Lead-in Time",
                "Change UI Theme",
                "Update Music Directory",
                ".. Back",
            ]
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
                "Red": "\033[1;31m",
                "Blue": "\033[1;34m",
                "Magenta": "\033[1;35m",
                "Cyan": "\033[1;36m"
            }
            pick = prompt.select("Accent:", choices=list(colours.keys()))
            if pick:
                config["theme"]["accent"] = colours[pick]
                print(f"Theme changed to {pick}")
                time.sleep(1)

        elif choice == "Update Music Directory":
            new_root = prompt.path("Music directory:")
            if new_root and os.path.isdir(new_root):
                config["music_directory"] = new_root
                print("Re-scanning library...")
                xml_db = load_xml_database("../data/Library.xml")
                new_lib = build_library(new_root, xml_db=xml_db)
                save_library_cache(new_lib)
                library_ref[0] = new_lib
                print(f"Done — {len(new_lib)} tracks.")
                time.sleep(1.5)

        save_config(config)


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
                # User explicitly skipped — advance rather than repeat
                idx += 1
                continue

        if mode == "repeat_one":
            continue

        idx += 1
        if mode == "repeat_all" and idx >= len(playlist):
            idx = 0
    
    return None


def browse_menu(library_ref: list) -> str | None:
    """Handle library browsing menu."""
    library = library_ref[0]
    NAV_STACK.append("Browse")
    
    try:
        while True:  # LEVEL 1: Browse By
            ui_utils.clear_screen()
            cat_choice = prompt.select(
                "Browse by:",
                choices=["Artists", "Albums", "Genres", "Groupings", "[Back]"]
            )

            if not cat_choice or cat_choice == "[Back]":
                ui_utils.clear_screen()
                break

            NAV_STACK.append(cat_choice)
            
            while True:  # LEVEL 2: Selection
                ui_utils.clear_screen()
                key_map = {"Artists": "artist", "Albums": "album", "Genres": "genre", "Groupings": "grouping"}
                grouped = get_grouped_data(library, key_map[cat_choice])
                group_names = sorted(grouped.keys())

                if cat_choice == "Artists":
                    letters_found = sorted({
                        (n[0].upper() if n[0].upper() in string.ascii_uppercase else "#") 
                        for n in group_names
                    })
                    if "#" in letters_found:
                        letters_found.remove("#")
                        letters_found.append("#")
                    
                    letter_sel = prompt.select(
                        "Select Category:",
                        choices=letters_found + [".. Back"]
                    )
                    
                    if not letter_sel or letter_sel == ".. Back":
                        ui_utils.clear_screen()
                        break
                    
                    group_names = [
                        n for n in group_names 
                        if (n[0].upper() == letter_sel if letter_sel != "#" 
                            else n[0].upper() not in string.ascii_uppercase)
                    ]

                selection = prompt.select(
                    f"Select {cat_choice}:",
                    choices=["[Play All]"] + group_names + [".. Back"]
                )
                
                if not selection or selection == ".. Back":
                    ui_utils.clear_screen()
                    break
                
                if selection == "[Play All]":
                    # Use the currently visible (possibly letter-filtered) names
                    paths = [s['path'] for name in group_names for s in grouped[name]]
                    res = play_queue(paths)
                    if res == "QUIT_ALL":
                        NAV_STACK.clear()
                        NAV_STACK.append("Home")
                        return "QUIT_ALL"
                    continue

                NAV_STACK.append(selection)
                selected_songs = grouped[selection]

                while True:  # LEVEL 3: Album Selection
                    ui_utils.clear_screen()
                    
                    if cat_choice in ("Artists", "Genres", "Groupings"):
                        albums = {}
                        for s in selected_songs:
                            albums.setdefault(s['album'], []).append(s)
                        
                        alb = prompt.select(
                            "Album:",
                            choices=["[Play All]", "[Bulk Edit Tags]"] + sorted(albums.keys()) + [".. Back"]
                        )
                        
                        if not alb or alb == ".. Back":
                            ui_utils.clear_screen()
                            break
                        
                        if alb == "[Play All]":
                            res = play_queue([s['path'] for s in selected_songs])
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                            continue

                        if alb == "[Bulk Edit Tags]":
                            bulk_tag_manager(library, paths=[s['path'] for s in selected_songs])
                            continue
                        
                        NAV_STACK.append(alb)
                        final_tracks = albums[alb]
                    else:
                        final_tracks = selected_songs

                    while True:  # LEVEL 4: Track Selection
                        ui_utils.clear_screen()
                        final_tracks.sort(key=lambda x: int(x.get('track', 0) or 0))
                        track_choices = [
                            prompt.Choice(
                                title=f"{str(t.get('track', '0')).zfill(2)} — {t.get('title', 'Unknown')}",
                                value=t['path']
                            )
                            for t in final_tracks
                        ]
                        
                        path_choice_obj = prompt.select(
                            "Track:",
                            choices=["[Play All]", "[Bulk Edit Tags]"] + track_choices + [".. Back"]
                        )
                        
                        if not path_choice_obj or path_choice_obj == ".. Back":
                            ui_utils.clear_screen()
                            break

                        if path_choice_obj == "[Play All]":
                            res = play_queue([t['path'] for t in final_tracks])
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                            continue

                        if path_choice_obj == "[Bulk Edit Tags]":
                            bulk_tag_manager(library, paths=[t['path'] for t in final_tracks])
                            continue

                        # LEVEL 5: Action Menu
                        selected_track = next((t for t in final_tracks if t['path'] == path_choice_obj), None)
                        NAV_STACK.append(selected_track['title'] if selected_track else path_choice_obj)
                        while True:
                            ui_utils.clear_screen()
                            action = prompt.select(
                                "Action:",
                                choices=["Play", "Sync Lyrics", "Edit Metadata", ".. Back"]
                            )
                            
                            if not action or action == ".. Back":
                                ui_utils.clear_screen()
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
                                inspect_tag_loop(path_choice_obj, library_metadata=selected_track)
                                ui_utils.clear_screen()
                        
                        NAV_STACK.pop()

                    if cat_choice in ("Artists", "Genres", "Groupings"):
                        NAV_STACK.pop()
                    else:
                        # For Albums, exit LEVEL 3 after track selection
                        break
                
                NAV_STACK.pop()
            
            NAV_STACK.pop()
    
    finally:
        if NAV_STACK and NAV_STACK[-1] == "Browse":
            NAV_STACK.pop()


def main_menu(library_ref: list) -> None:
    """Main menu loop."""
    while True:
        ui_utils.clear_screen()
        choice = prompt.select(
            "Main Menu:",
            choices=["Browse Library", "Search", "Listening History", "Metadata Browser", "Settings", "Exit"]
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