"""
Music Player Application

Terminal-based music player with lyrics sync, library browsing, and metadata management.

Main entry point. Orchestrates all modules.
"""

import os
import threading
from src.utils import prompt

from src.config import load_config, save_config
from src.music_library import build_library, load_library_cache, save_library_cache, load_xml_database, start_background_sync
from src.menus import main_menu

def main() -> None:
    """Main application entry point."""
    
    # Load or create config
    config = load_config()
    
    # Load XML metadata database (iTunes Library.xml)
    _src_dir = os.path.dirname(os.path.abspath(__file__))
    xml_db_path = config.get("xml_db_path") if os.path.isfile(config.get("xml_db_path", "")) else None
    xml_db, xml_title_keys = load_xml_database(xml_db_path) if xml_db_path else (None, set())
    if xml_db:
        print(f"✓ Metadata database loaded: {len(xml_db)} tracks")
    else:
        xml_db, xml_title_keys = None, set()

    # Load or build library
    library = load_library_cache()
    if library:
        print(f"✓ Library loaded: {len(library)} tracks")
        if xml_db:
            start_background_sync(library, xml_db, xml_title_keys)
        
        library_ref = [library]
        main_menu(library_ref)

    if not library:
        # First run: ask user for music directory
        root = config.get("music_directory") or prompt.path(
            "Select your Music Directory:"
        )
        
        if root and os.path.isdir(root):
            config["music_directory"] = root
            save_config(config)
            
            print("Building library...")
            library = build_library(root, xml_db=xml_db, xml_title_keys=xml_title_keys, ignore_hidden=config.get("ignore_hidden_files", False))
            save_library_cache(library, _async=False)
            if xml_db:
                start_background_sync(library, xml_db, xml_title_keys)
            print(f"✓ Library built: {len(library)} tracks")
        else:
            print("No valid directory selected.")
            return
    elif xml_db:
        # Re-enrich cached tracks still at defaults — runs in background so startup is instant
        def _reenrich():
            from src.music_library import get_metadata as _gm
            changed = False
            for song in library:
                if song.get("artist") == "Unknown Artist" or song.get("album") == "Unknown Album":
                    fresh = _gm(song["path"], xml_db=xml_db, xml_title_keys=xml_title_keys)
                    song.update(fresh)
                    changed = True
            if changed:
                save_library_cache(library, _async=False)
        threading.Thread(target=_reenrich, daemon=True).start()


if __name__ == "__main__":
    main()