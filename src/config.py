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
    "lyric_lead_in": 2.0,
    "music_directories": [],
    # Legacy single-directory key, kept in step with the first entry of
    # `music_directories` so an older build reading this file still works.
    "music_directory": "",
    "ignore_hidden_files": False,
    "show_metadata_editor": True,
    "show_lyrics_editor": True,
    "tag_name_preferences": {},
    "sort_list_delimiter": "/",
    "plain_text_editing": False,
    "autoplay_on_select": False,
    # Playback volume, 0–100. Owned by the player session: restored at launch and
    # written back whenever it changes.
    "volume": 100,
}

def _default_config_dir() -> Path:
    """The platform-conventional config directory when no override is set."""
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
    """Load the config file, creating it with defaults if missing, and filling
    in any keys added to DEFAULT_CONFIG since it was written."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG.copy()

    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)

    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)

    # Migrate a config written before multiple directories were supported.
    if not cfg.get("music_directories") and cfg.get("music_directory"):
        cfg["music_directories"] = [cfg["music_directory"]]

    return cfg


def normalise_dir(path: str) -> str:
    """Absolute, ~-expanded form of one directory path ('' stays '')."""
    p = str(path or '').strip()
    return os.path.abspath(os.path.expanduser(p)) if p else ''


def music_dirs(config: dict | None = None) -> list[str]:
    """Every configured music directory, absolute and de-duplicated.

    The single-directory `music_directory` key is honoured as a fallback, so a
    config written by an older build still scans.
    """
    cfg = config if config is not None else load_config()
    raw = cfg.get("music_directories") or []
    if isinstance(raw, str):                     # hand-edited config
        raw = [raw]
    if not raw and cfg.get("music_directory"):
        raw = [cfg["music_directory"]]

    out: list[str] = []
    seen: set[str] = set()
    for d in raw:
        full = normalise_dir(d)
        key = os.path.normcase(full)
        if full and key not in seen:
            seen.add(key)
            out.append(full)
    return out


def set_music_dirs(config: dict, dirs: list[str]) -> list[str]:
    """Store `dirs` on `config` (normalised, de-duplicated) and return the result.

    Mirrors the first entry into the legacy `music_directory` key.
    """
    config["music_directories"] = []
    config["music_directory"] = ""
    config["music_directories"] = music_dirs({"music_directories": dirs})
    if config["music_directories"]:
        config["music_directory"] = config["music_directories"][0]
    return config["music_directories"]

def save_config(config: dict) -> None:
    """Write the config dict to disk as JSON."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
