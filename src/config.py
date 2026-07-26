"""User configuration: load/save the JSON config in the platform config dir."""
import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "theme": {
        "primary": "\033[1;37m",
        "accent": "\033[1;31m",
        "success": "\033[1;32m",
    },
    "history_enabled": True,
    "search_weights": {"title": 10, "artist": 8, "album": 5},
    "lyric_lead_in": 2.0,
    "art_width": 80,
    "music_directory": "",
    "player_view": "default",
    "ignore_hidden_files": False,
    "show_metadata_editor": True,
    "show_lyrics_editor": True,
    "tag_name_preferences": {},
    "sort_list_delimiter": "/",
    "name_corpus": "full",
    "plain_text_editing": False,
    "autoplay_on_select": False,
}

def _default_config_dir() -> Path:
    if os.name == "nt":
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "Backtrack"
        return Path.home() / "AppData" / "Roaming" / "Backtrack"

    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "backtrack"
    return Path.home() / ".config" / "backtrack"

CONFIG_DIR = Path(os.getenv("BACKTRACK_CONFIG_DIR") or _default_config_dir())
CONFIG_FILE = CONFIG_DIR / "config.json"

def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)

    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)

    return cfg

def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
