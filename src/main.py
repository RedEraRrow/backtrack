import os
import threading
import time

from src.config import load_config, save_config
from src.music_library import (
    build_library, load_library_cache, save_library_cache,
    load_xml_database, start_background_sync
)
from src.menus import main_menu
from src.id3.tag_registry import TAG_REGISTRY
from src.state import QuitToTerminal
from src.utils import prompt, ui_utils


def _init_tag_preferences(config: dict) -> dict:
    if not config.get('tag_name_preferences'):
        config['tag_name_preferences'] = {
            tag_id: info.name[0]
            for tag_id, info in TAG_REGISTRY.items()
        }
    return config


def _load_metadata_db(config: dict) -> tuple:
    xml_db_path = config.get("xml_db_path", "")
    if not os.path.isfile(xml_db_path):
        return None, set()

    xml_db, xml_title_keys = load_xml_database(xml_db_path)
    if xml_db:
        ui_utils.show_status(f"Metadata DB: {len(xml_db)} tracks")
    return xml_db, xml_title_keys


def _reenrich_default_tracks(library: list, xml_db: dict, xml_title_keys: set) -> None:
    from src.music_library import get_metadata

    changed = False
    for song in library:
        if song.get("artist") == "Unknown Artist" or song.get("album") == "Unknown Album":
            fresh = get_metadata(song["path"], xml_db=xml_db, xml_title_keys=xml_title_keys)
            song.update(fresh)
            changed = True

    if changed:
        save_library_cache(library, _async=False)


def _run(config: dict) -> None:
    config = _init_tag_preferences(config)
    # Persist immediately so first-run tag preferences survive an instant quit;
    # the settings menu also autosaves, so "save & quit" (q) needs nothing more.
    save_config(config)

    xml_db, xml_title_keys = _load_metadata_db(config)
    library = load_library_cache()

    if library:
        ui_utils.show_status(f"Library: {len(library)} tracks")
        if xml_db:
            start_background_sync(library, xml_db, xml_title_keys)
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
    root = os.path.abspath(os.path.expanduser(root)) if root else root

    if not root or not os.path.isdir(root):
        ui_utils.show_loading("No valid directory selected.")
        time.sleep(1.5)
        return

    config["music_directory"] = root
    save_config(config)

    ui_utils.show_loading("Building library…")
    library = build_library(
        root,
        xml_db=xml_db,
        xml_title_keys=xml_title_keys,
        ignore_hidden=config.get("ignore_hidden_files", False)
    )

    save_library_cache(library, _async=False)
    ui_utils.show_status(f"Library built: {len(library)} tracks")

    if xml_db:
        start_background_sync(library, xml_db, xml_title_keys)

    main_menu([library])


def main() -> None:
    config = load_config()
    ui_utils.enter_alt_screen()
    try:
        _run(config)
    except QuitToTerminal:
        pass  # Shift-Q from anywhere in the menus — unwind straight to the shell.
    finally:
        ui_utils.exit_alt_screen()


if __name__ == "__main__":
    main()
