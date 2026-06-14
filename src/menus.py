"""
Navigation menus and user interaction handlers.

All top-level menu functions for browsing, searching, and settings.
Uses tag_registry for consistent metadata field handling.
"""

from __future__ import annotations
import os
import time
import string
from src.utils.ui_utils import roman, Colors as C

from src.utils import prompt, ui_utils
from src.music_library import (
    build_library, save_library_cache,
    load_xml_database, start_background_sync,
    get_grouped_data, get_group_sort_key, search_library, sort_library_logic
)
from src.history import get_history, clear_history
from src.playback.playback import music_player
from src.playback.lyrics.lyric_timer import sync_lyrics
from src.config import load_config, save_config
from src.state import NAV_STACK
from src.id3.id3_browser import id3_browser
from src.id3.bulk_id3_manager import bulk_id3_manager
from src.id3.tag_registry import TAG_REGISTRY


# ============================================================================
# Constants
# ============================================================================

TRACK_ACTIONS = ["Play", "Sync Lyrics"]
TRACK_ACTION_WITH_EDITOR = ["Play", "Sync Lyrics", "Edit Metadata"]

# Browse categories with their metadata fields
BROWSE_CATEGORIES = {
    "By Artist": "artist",
    "By Album": "album",
    "By Genre": "genre",
    "By Grouping": "grouping",
    "Compilations": "album_artist",
}

# Search field names (aligned with metadata)
SEARCH_FIELDS = {
    "Title": "title",
    "Artist": "artist",
    "Album": "album",
    "Genre": "genre",
    "Performers": "performers",
}


# ============================================================================
# Header Helper
# ============================================================================

def _menu_header(title: str, subtitle: str | None = None):
    """
    Resize-aware header builder for prompt.select.
    
    Single bold title, optional dim subtitle, clean divider.
    """
    def _build() -> list[str]:
        cols = ui_utils.get_terminal_width()
        title_str = f"{C.BOLD}{title}{C.RESET}"
        subtitle_str = f"  {C.DIM}{subtitle}{C.RESET}" if subtitle else ""
        lines = [
            "",
            f"  {title_str}{subtitle_str}",
            f"{C.DIM}{ui_utils.divider(cols, '─')}{C.RESET}",
        ]
        return lines
    
    return _build


# ============================================================================
# Action Handlers
# ============================================================================

def _get_track_actions(config: dict) -> list[str]:
    """Get available track actions based on config."""
    actions = list(TRACK_ACTIONS)
    if config.get("show_metadata_editor", True):
        actions.append("Edit Metadata")
    actions.append(".. Back")
    return actions


def _perform_track_action(action: str, file_path: str, library: list,
                          is_grouping: bool = False) -> str | None:
    """
    Execute track action.
    
    Args:
        action: Action name
        file_path: Path to track
        library: Music library
        is_grouping: Track is in compilation context
    
    Returns:
        "QUIT_ALL" to exit, None to continue
    """
    if action == "Play":
        ui_utils.clear_screen()
        res = music_player(file_path, is_grouping=is_grouping)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    
    elif action == "Sync Lyrics":
        ui_utils.clear_screen()
        sync_lyrics(file_path)
        ui_utils.clear_screen()
    
    elif action == "Edit Metadata":
        ui_utils.clear_screen()
        id3_browser(file_path, library=library)
        ui_utils.clear_screen()
    
    return None


# ============================================================================
# Search Handler
# ============================================================================

def handle_search(library: list) -> str | None:
    """
    Handle search menu and track selection.
    
    Returns:
        "QUIT_ALL" if user quit, None otherwise
    """
    if not library:
        print("Library is empty. Scan a directory first.")
        time.sleep(1.5)
        return None
    
    # Select search targets
    search_choices = [
        prompt.Choice(label, field, default)
        for label, field, default in [
            ("Title", "title", True),
            ("Artist", "artist", True),
            ("Album", "album", True),
            ("Genre", "genre", False),
            ("Performers", "performers", False),
        ]
    ]
    
    search_targets = prompt.checkbox("Search within:", choices=search_choices)
    if not search_targets:
        return None
    
    query = prompt.text("Search:")
    if not query:
        return None
    
    # Find matches
    matches = search_library(library, query, search_targets)
    if not matches:
        print("No matching tracks found.")
        time.sleep(1.5)
        return None
    
    # Show results (top 20)
    choices = [
        prompt.Choice(
            title=f"{s['title']} — {s['artist']} ({s['album']})",
            value=s['path']
        )
        for s in matches[:20]
    ]
    
    selected = prompt.select(
        f"{len(matches)} match(es) — top 20:",
        choices=choices + [".. Back"],
        header=_menu_header("Search Results", query),
    )
    
    if not selected or selected == ".. Back":
        return None
    
    # Show track actions
    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)
    
    config = load_config()
    action = prompt.select(
        "Action:",
        choices=_get_track_actions(config),
        header=_menu_header(track_title),
    )
    
    if action and action != ".. Back":
        return _perform_track_action(action, selected, library)
    
    return None


# ============================================================================
# History Handler
# ============================================================================

def handle_history(library: list) -> str | None:
    """
    Handle listening history menu.
    
    Returns:
        "QUIT_ALL" if user quit, None otherwise
    """
    history_entries = get_history(limit=30)
    
    if not history_entries:
        print("No listening history available.")
        time.sleep(1.5)
        return None
    
    # Build history choices
    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        title = f"{song['title']} — {song['artist']}" if song else os.path.basename(path)
        choices.append(
            prompt.Choice(title=f"[{ts}] {title} ({dur})", value=path)
        )
    
    selected = prompt.select(
        "History:",
        choices=choices + [".. Back"],
        header=_menu_header("Listening History"),
    )
    
    if not selected or selected == ".. Back":
        return None
    
    # Show track actions
    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)
    
    action = prompt.select(
        "Action:",
        choices=["Play", "Sync Lyrics", ".. Back"],
        header=_menu_header(track_title),
    )
    
    if action == "Play":
        ui_utils.clear_screen()
        res = music_player(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif action == "Sync Lyrics":
        ui_utils.clear_screen()
        sync_lyrics(selected)
        ui_utils.clear_screen()
    
    return None


# ============================================================================
# Settings Handler
# ============================================================================

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
                "Toggle Hidden Files",
                "Toggle Metadata Editor",
                "Update Music Directory",
                "Update iTunes Library XML Path",
                "Refine Tag Names",
                ".. Back",
            ],
            header=_menu_header("Settings"),
        )
        
        if not choice or choice == ".. Back":
            break
        
        elif choice == "Toggle Listening History":
            enabled = config.get("history_enabled", True)
            config["history_enabled"] = not enabled
            save_config(config)
            status = "ENABLED" if config["history_enabled"] else "DISABLED"
            print(f"History: {status}")
            time.sleep(1)
        
        elif choice == "Clear History Log":
            confirm = prompt.confirm("Sure? Cannot be undone.")
            if confirm:
                if clear_history():
                    print("History cleared.")
                else:
                    print("Failed to clear history.")
                time.sleep(1)
        
        elif choice == "Adjust Lyric Lead-in Time":
            current = config.get("lyric_lead_in_seconds", 1.0)
            val = prompt.text(f"Lead-in seconds (current {current}):")
            if val and val.replace('.', '', 1).isdigit():
                config["lyric_lead_in_seconds"] = float(val)
                save_config(config)
                print(f"Updated to {config['lyric_lead_in_seconds']} seconds")
                time.sleep(1)
        
        elif choice == "Toggle Hidden Files":
            enabled = config.get("ignore_hidden_files", False)
            config["ignore_hidden_files"] = not enabled
            save_config(config)
            state = "ON" if config["ignore_hidden_files"] else "OFF"
            print(f"Hidden file filter: {state}")
            if config["ignore_hidden_files"]:
                print("Rebuild library via Update Music Directory to apply.")
            time.sleep(1.5)
        
        elif choice == "Toggle Metadata Editor":
            enabled = config.get("show_metadata_editor", True)
            config["show_metadata_editor"] = not enabled
            save_config(config)
            state = "VISIBLE" if config["show_metadata_editor"] else "HIDDEN"
            print(f"Metadata editor: {state}")
            time.sleep(1)
        
        elif choice == "Update Music Directory":
            new_root = prompt.path("Music directory:")
            if new_root and os.path.isdir(new_root):
                config["music_directory"] = new_root
                save_config(config)
                print("Re-scanning library...")
                
                xml_db_path = config.get("xml_db_path")
                xml_db, xml_title_keys = (
                    load_xml_database(xml_db_path)
                    if xml_db_path and os.path.isfile(xml_db_path)
                    else (None, set())
                )
                
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
                
                if xml_db:
                    start_background_sync(existing_lib, xml_db, xml_title_keys)
                
                print(f"Done — {len(new_lib)} tracks.")
                time.sleep(1.5)
        
        elif choice == "Update iTunes Library XML Path":
            new_path = prompt.path("iTunes Library XML file:")
            if new_path and os.path.isfile(new_path):
                config["xml_db_path"] = new_path
                save_config(config)
                
                xml_db, xml_title_keys = load_xml_database(new_path)
                if xml_db:
                    print(f"Metadata database loaded: {len(xml_db)} tracks")
                    start_background_sync(library_ref[0], xml_db, xml_title_keys)
                else:
                    print("Failed to load XML database.")
                time.sleep(1.5)
        
        elif choice == "Refine Tag Names":
            tag_prefs = config.get('tag_name_preferences', {})
            if not tag_prefs:
                # Initialize from TAG_REGISTRY if not set
                tag_prefs = {tag_id: info.name[0] for tag_id, info in TAG_REGISTRY.items()}
            
            # Convert to list format for list_edit
            tag_list = [(tag_id, name) for tag_id, name in tag_prefs.items()]
            
            updated = prompt.list_edit(
                "Tag Names",
                tag_list,
                ("TAG ID", "Display Name")
            )
            
            if updated:
                config['tag_name_preferences'] = {tag_id: name for tag_id, name in updated}
                save_config(config)
                print("Updated tag name preferences.")
                time.sleep(1)


# ============================================================================
# Browse Handler
# ============================================================================

def _format_track_label(track: dict, has_multiple_discs: bool,
                        work_indent: bool = False) -> str:
    """Format track display label with movement info if applicable."""
    indent = "  " if has_multiple_discs else ""
    if work_indent:
        indent += "  "
    
    # Extract movement number, handling fractions like "1/1"
    mv_num = ""
    mv_num_raw = track.get('movement_number', '').strip()
    if mv_num_raw:
        try:
            # Split on "/" if present (e.g., "1/1" -> "1")
            mv_num_val = int(mv_num_raw.split('/')[0])
            mv_num = roman(mv_num_val)
        except (ValueError, IndexError):
            pass
    
    mv_name = track.get('movement_name', '').strip()
    
    if mv_name and mv_num:
        num_str = (str(track.get('track', '0')).zfill(2) + f" — {mv_num}.")
        label = f"{indent}{num_str} {mv_name}"
    else:
        label = f"{indent}{str(track.get('track', '0')).zfill(2)} — {track.get('title', 'Unknown')}"
    
    return label


def browse_menu(library_ref: list) -> str | None:
    """
    Main browse menu with proper 3-level hierarchy:
    1. Select category (genre, artist, etc.)
    2. Select album in that category
    3. Select and play track
    
    Returns:
        "QUIT_ALL" if user quit, None otherwise
    """
    library = library_ref[0]
    config = load_config()
    
    try:
        NAV_STACK.append("Browse")
        
        while True:
            # LEVEL 1: Category selection (Genre, Artist, Album, etc.)
            cat_choice = prompt.select(
                "Browse by:",
                choices=list(BROWSE_CATEGORIES.keys()) + [".. Back"],
                header=_menu_header("Browse Library"),
            )
            
            if not cat_choice or cat_choice == ".. Back":
                break
            
            category_field = BROWSE_CATEGORIES[cat_choice]
            grouped = get_grouped_data(library, category_field)
            
            if not grouped:
                print("No items in this category.")
                time.sleep(1.0)
                continue
            
            # LEVEL 2: Value selection (specific genre, artist, album, etc.)
            sorted_groups = sorted(
                grouped.items(),
                key=lambda item: get_group_sort_key(item[0], item[1], category_field)
            )
            
            group_names = [name for name, _ in sorted_groups]
            
            # Letter filtering for long lists
            if cat_choice in ("By Artist", "By Album") and len(group_names) > 20:
                sort_keys = {
                    name: get_group_sort_key(name, grouped[name], category_field)
                    for name in group_names
                }
                
                def _letter(name: str) -> str:
                    ch = sort_keys[name][0].upper() if sort_keys.get(name) else "#"
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
                    continue
                
                if letter_sel != "All":
                    group_names = [n for n in group_names if _letter(n) == letter_sel]
                
                sorted_groups = [(name, grouped[name]) for name in group_names]
            
            group_choices = [
                prompt.Choice(title=name, value=name)
                for name, _ in sorted_groups
            ]
            
            selection = prompt.select(
                cat_choice + ":",
                choices=group_choices + [".. Back"],
                header=_menu_header("Browse Library", cat_choice),
            )
            
            if not selection or selection == ".. Back":
                continue
            
            # Get tracks for this selection
            selected_tracks = next(
                (tracks for name, tracks in sorted_groups if name == selection),
                []
            )
            
            if not selected_tracks:
                continue
            
            NAV_STACK.append(selection)
            
            # LEVEL 3: Album selection (unless already browsing by album)
            if cat_choice != "By Album":
                while True:
                    # Group by album
                    albums = get_grouped_data(selected_tracks, "album")
                    sorted_albums = sorted(
                        albums.items(),
                        key=lambda item: get_group_sort_key(item[0], item[1], "album")
                    )
                    
                    album_choices = [
                        prompt.Choice(title=album_name, value=album_name)
                        for album_name, _ in sorted_albums
                    ]
                    
                    album_choice = prompt.select(
                        "Album:",
                        choices=album_choices + [".. Back"],
                        header=_menu_header(selection, cat_choice),
                    )
                    
                    if not album_choice or album_choice == ".. Back":
                        break
                    
                    album_tracks = next(
                        (tracks for name, tracks in sorted_albums if name == album_choice),
                        []
                    )
                    
                    if not album_tracks:
                        continue
                    
                    NAV_STACK.append(album_choice)
                    _show_and_play_tracks(album_tracks, album_choice, selection, library, config, cat_choice)
                    NAV_STACK.pop()
            
            else:
                # Browsing by album — show tracks directly
                _show_and_play_tracks(selected_tracks, selection, "", library, config, cat_choice)
            
            NAV_STACK.pop()
    
    finally:
        if NAV_STACK and NAV_STACK[-1] == "Browse":
            NAV_STACK.pop()
    
    return None


def _show_and_play_tracks(tracks: list, context: str, category: str, 
                          library: list, config: dict, cat_choice: str) -> str | None:
    """
    Show track list and handle selection/playback.
    
    Args:
        tracks: List of tracks to display
        context: Current context (album name, etc.)
        category: Parent category (genre, artist, etc.)
        library: Full music library
        config: Application config
        cat_choice: Browse category selected
    
    Returns:
        "QUIT_ALL" if user quit, None otherwise
    """
    while True:
        final_tracks = sort_library_logic(tracks)
        
        # Check for multi-disc or classical music
        has_multiple_discs = len({t.get('disc', '1') for t in final_tracks}) > 1
        has_works = any(t.get('work', '').strip() for t in final_tracks)
        
        # Build disc and work maps
        disc_track_map = {}
        work_track_map = {}
        
        for track in final_tracks:
            if has_multiple_discs:
                disc = str(track.get('disc', '1'))
                disc_track_map.setdefault(disc, []).append(track['path'])
            
            if has_works:
                work = track.get('work', '').strip()
                if work:
                    work_track_map.setdefault(work, []).append(track['path'])
        
        # Build track choices with disc/work headers
        track_choices = []
        current_disc = None
        current_work = None
        
        for track in final_tracks:
            # Disc header
            if has_multiple_discs:
                disc = str(track.get('disc', '1'))
                if disc != current_disc:
                    subtitle = track.get('disc_subtitle', '').strip()
                    label = f"── {subtitle} ──" if subtitle else f"── Disc {disc} ──"
                    track_choices.append(
                        prompt.Choice(title=label, value=f"__disc_{disc}")
                    )
                    current_disc = disc
            
            # Work header (classical)
            if has_works:
                work = track.get('work', '').strip()
                if work and work != current_work:
                    d_pad = "  " if has_multiple_discs else ""
                    track_choices.append(
                        prompt.Choice(title=f"{d_pad}── {work} ──", value=f"__work__{work}")
                    )
                    current_work = work
            
            # Track row
            label = _format_track_label(track, has_multiple_discs, work_indent=has_works)
            track_choices.append(prompt.Choice(title=label, value=track['path']))
        
        # Header actions
        header_choices = [
            prompt.Choice(
                title=f"▶  Play all — {context}",
                value="__play_all__"
            )
        ]
        
        if config.get("show_metadata_editor", True):
            header_choices.append(
                prompt.Choice(
                    title=f"Edit tags — {context}",
                    value="__bulk_edit__"
                )
            )
        
        # Track selection
        path_choice = prompt.select(
            "Tracks:",
            choices=header_choices + track_choices + [".. Back"],
            header=_menu_header(context, category if category else cat_choice),
        )
        
        if not path_choice or path_choice == ".. Back":
            break
        
        # Handle header actions
        if path_choice == "__play_all__":
            res = _play_queue(
                [t['path'] for t in final_tracks],
                is_grouping=(cat_choice == "Compilations")
            )
            if res == "QUIT_ALL":
                return "QUIT_ALL"
            continue
        
        if path_choice == "__bulk_edit__":
            bulk_id3_manager(library, paths=[t['path'] for t in final_tracks])
            continue
        
        # Handle disc selection
        if isinstance(path_choice, str) and path_choice.startswith("__disc_"):
            disc_val = path_choice[len("__disc_"):]
            disc_paths = disc_track_map.get(disc_val, [])
            subtitle = next(
                (t.get('disc_subtitle', '') for t in final_tracks
                 if str(t.get('disc', '1')) == disc_val),
                ''
            )
            disc_label = f"{subtitle}" if subtitle else f"Disc {disc_val}"
            _handle_disc_action(disc_label, disc_paths, library, cat_choice, config)
            continue
        
        # Handle work selection
        if isinstance(path_choice, str) and path_choice.startswith("__work__"):
            work_name = path_choice[len("__work__"):]
            work_paths = work_track_map.get(work_name, [])
            _handle_work_action(work_name, work_paths, library, cat_choice, config)
            continue
        
        # Track action
        selected_track = next(
            (t for t in final_tracks if t['path'] == path_choice),
            None
        )
        
        if not selected_track:
            continue
        
        track_title = selected_track['title']
        track_artist = selected_track.get('artist', '')
        NAV_STACK.append(track_title)
        
        while True:
            action = prompt.select(
                "Action:",
                choices=_get_track_actions(config),
                header=_menu_header(track_title, track_artist),
            )
            
            if not action or action == ".. Back":
                break
            
            result = _perform_track_action(
                action, path_choice, library,
                is_grouping=(cat_choice == "Compilations")
            )
            if result == "QUIT_ALL":
                return "QUIT_ALL"
        
        NAV_STACK.pop()
    
    return None


def _handle_disc_action(disc_label: str, disc_paths: list, library: list,
                        cat_choice: str, config: dict) -> None:
    """Handle disc header selection."""
    disc_choices = [
        prompt.Choice(title=f"▶  Play all — {disc_label}", value="__play_all__")
    ]
    
    if config.get("show_metadata_editor", True):
        disc_choices.append(
            prompt.Choice(title=f"Edit tags — {disc_label}", value="__bulk_edit__")
        )
    
    disc_choices.append(prompt.Choice(".. Back", value=".. Back"))
    
    action = prompt.select(
        "Disc:",
        choices=disc_choices,
        header=_menu_header(disc_label, "Disc"),
    )
    
    if action == "__play_all__":
        _play_queue(disc_paths, is_grouping=(cat_choice == "Compilations"))
    elif action == "__bulk_edit__":
        bulk_id3_manager(library, paths=disc_paths)


def _handle_work_action(work_name: str, work_paths: list, library: list,
                        cat_choice: str, config: dict) -> None:
    """Handle work header selection (classical music)."""
    work_choices = [
        prompt.Choice(title=f"▶  Play all — {work_name}", value="__play_all__")
    ]
    
    if config.get("show_metadata_editor", True):
        work_choices.append(
            prompt.Choice(title=f"Edit tags — {work_name}", value="__bulk_edit__")
        )
    
    work_choices.append(prompt.Choice(".. Back", value=".. Back"))
    
    action = prompt.select(
        "Work:",
        choices=work_choices,
        header=_menu_header(work_name, "Work"),
    )
    
    if action == "__play_all__":
        _play_queue(work_paths, is_grouping=(cat_choice == "Compilations"))
    elif action == "__bulk_edit__":
        bulk_id3_manager(library, paths=work_paths)


def _play_queue(paths: list, is_grouping: bool = False) -> str | None:
    """
    Play multiple tracks.
    
    Returns:
        "QUIT_ALL" if user quit, None otherwise
    """
    for path in paths:
        ui_utils.clear_screen()
        res = music_player(path, is_grouping=is_grouping)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    return None


# ============================================================================
# Main Menu
# ============================================================================

def main_menu(library_ref: list) -> None:
    """Main menu loop."""
    while True:
        choice = prompt.select(
            "Main Menu:",
            choices=["Browse Library", "Search", "Listening History", "Settings", "Exit"],
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
        
        elif choice == "Settings":
            handle_settings(library_ref)