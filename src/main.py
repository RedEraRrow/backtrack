"""
Music Player Application

Terminal-based music player with lyrics sync, library browsing, and metadata management.

Main entry point. Orchestrates all modules.
"""

import os
from src import prompt

from src.config import load_config, save_config
from src.music_library import build_library, load_library_cache, save_library_cache, load_xml_database
from src.menus import main_menu


def main() -> None:
    """Main application entry point."""
    
    # Load or create config
    config = load_config()
    
    # Load XML metadata database (iTunes Library.xml)
    _src_dir = os.path.dirname(os.path.abspath(__file__))
    xml_db = load_xml_database(os.path.join(_src_dir, "../data/Library.xml"))
    if xml_db:
        print(f"✓ Metadata database loaded: {len(xml_db)} tracks")
    
    # Load or build library
    library = load_library_cache()

    if not library:
        # First run: ask user for music directory
        root = config.get("music_directory") or prompt.path(
            "Select your Music Directory:"
        )
        
        if root and os.path.isdir(root):
            config["music_directory"] = root
            save_config(config)
            
            print("Building library...")
            library = build_library(root, xml_db=xml_db)
            save_library_cache(library)
            print(f"✓ Library built: {len(library)} tracks")
        else:
            print("No valid directory selected.")
            return
    elif xml_db:
        # Re-enrich cached library — fills fields that were unknown at build time
        from src.music_library import get_metadata
        needs_save = False
        for song in library:
            if song.get("artist") == "Unknown Artist" or song.get("album") == "Unknown Album":
                fresh = get_metadata(song["path"], xml_db=xml_db)
                song.update(fresh)
                needs_save = True
        if needs_save:
            save_library_cache(library)

    if library:
        print(f"✓ Library loaded: {len(library)} tracks\n")
        library_ref = [library]  # Mutable container for settings menu
        main_menu(library_ref)
    else:
        print("Library is empty.")


if __name__ == "__main__":
    main()