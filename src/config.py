"""
Configuration management for Music Player.

Handles loading, saving, and accessing application configuration.
"""

import json
import os


# Default configuration constants
DEFAULT_CONFIG = {
    "theme": {
        "primary": "\033[1;37m",
        "accent": "\033[1;31m",
        "success": "\033[1;32m",
    },
    "history_enabled": True,
    "search_weights": {"title": 10, "artist": 8, "album": 5},
    "lyric_lead_in": 2.0,
    "ascii_width": 80,
    "music_directory": "",
    "player_view": "default",
    "show_metadata_editor": True,
}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "../config/config.json")


def load_config() -> dict:
    """Load configuration from file or create defaults if missing."""
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG
    
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    
    # Ensure all default keys exist
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    
    return cfg


def save_config(config: dict) -> None:
    """Save configuration to file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)