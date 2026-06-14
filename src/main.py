"""
Music Player Application

Terminal-based music player with lyrics sync, library browsing, and metadata management.
Main entry point. Orchestrates all modules.
"""

import os
import threading

from src.config import load_config, save_config
from src.music_library import (
    build_library, load_library_cache, save_library_cache,
    load_xml_database, start_background_sync
)
from src.menus import main_menu
from src.id3.tag_registry import TAG_REGISTRY, get_preferred_tag_name
from src.utils import prompt, ui_utils


def _init_tag_preferences(config: dict) -> dict:
    """Initialize tag name preferences from registry if not already set."""
    if not config.get('tag_name_preferences'):
        config['tag_name_preferences'] = {
            tag_id: info.name[0]
            for tag_id, info in TAG_REGISTRY.items()
        }
    return config


def _load_metadata_db(config: dict) -> tuple:
    """Load XML metadata database if configured and exists."""
    xml_db_path = config.get("xml_db_path", "")
    if not os.path.isfile(xml_db_path):
        return None, set()
    
    xml_db, xml_title_keys = load_xml_database(xml_db_path)
    if xml_db:
        print(f"✓ Metadata database loaded: {len(xml_db)} tracks")
    return xml_db, xml_title_keys


def _reenrich_default_tracks(library: list, xml_db: dict, xml_title_keys: set) -> None:
    """Background thread to enrich tracks still at default values."""
    from src.music_library import get_metadata
    
    changed = False
    for song in library:
        if song.get("artist") == "Unknown Artist" or song.get("album") == "Unknown Album":
            fresh = get_metadata(song["path"], xml_db=xml_db, xml_title_keys=xml_title_keys)
            song.update(fresh)
            changed = True
    
    if changed:
        save_library_cache(library, _async=False)


def main() -> None:
    """Main application entry point."""
    config = load_config()
    config = _init_tag_preferences(config)
    
    # Load metadata database if configured
    xml_db, xml_title_keys = _load_metadata_db(config)
    
    # Load or build library
    library = load_library_cache()
    
    if library:
        print(f"✓ Library loaded: {len(library)} tracks")
        if xml_db:
            start_background_sync(library, xml_db, xml_title_keys)
            # Re-enrich tracks still at defaults in background
            threading.Thread(
                target=_reenrich_default_tracks,
                args=(library, xml_db, xml_title_keys),
                daemon=True
            ).start()
        
        library_ref = [library]
        main_menu(library_ref)
        save_config(config)
        return
    
    # First run: prompt for music directory
    ui_utils.clear_screen()
    root = config.get("music_directory") or prompt.path("Select your Music Directory:")
    
    if not root or not os.path.isdir(root):
        print("No valid directory selected.")
        return
    
    config["music_directory"] = root
    save_config(config)
    
    print("Building library...")
    library = build_library(
        root,
        xml_db=xml_db,
        xml_title_keys=xml_title_keys,
        ignore_hidden=config.get("ignore_hidden_files", False)
    )
    
    save_library_cache(library, _async=False)
    print(f"✓ Library built: {len(library)} tracks")
    
    if xml_db:
        start_background_sync(library, xml_db, xml_title_keys)
    
    main_menu([library])


if __name__ == "__main__":
    main()