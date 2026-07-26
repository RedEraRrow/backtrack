"""Application entry point: config, cache, library build, and menu launch."""
import os
import time

from src.config import load_config, save_config
from src.music_library import (
    build_library, load_library_cache, save_library_cache,
    start_background_sync
)
from src.menus import main_menu
from src.id3.tag_registry import TAG_REGISTRY
from src.state import QuitToTerminal
from src.utils import prompt, ui_utils


def _init_tag_preferences(config: dict) -> dict:
    """Seed tag_name_preferences from the tag registry's default names on first run."""
    if not config.get('tag_name_preferences'):
        config['tag_name_preferences'] = {
            tag_id: info.name[0]
            for tag_id, info in TAG_REGISTRY.items()
        }
    return config


def _run(config: dict) -> None:
    """Load or build the library and hand off to the main menu; first run prompts
    for a music directory and builds the cache from scratch."""
    config = _init_tag_preferences(config)
    # Persist immediately so first-run tag preferences survive an instant quit;
    # the settings menu also autosaves, so "save & quit" (q) needs nothing more.
    save_config(config)

    library = load_library_cache()

    if library:
        ui_utils.show_status(f"Library: {len(library)} tracks")
        # Keep the cache fresh in the background (adds/removes/edits).
        start_background_sync(library)

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
        ignore_hidden=config.get("ignore_hidden_files", False)
    )

    save_library_cache(library, _async=False)
    ui_utils.show_status(f"Library built: {len(library)} tracks")

    start_background_sync(library)

    main_menu([library])


def main() -> None:
    """Program entry point: set up the terminal, load config, and run the app."""
    # Enable ANSI escape processing on Windows consoles (no-op elsewhere).
    try:
        import colorama
        colorama.just_fix_windows_console()
    except Exception:
        pass

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
