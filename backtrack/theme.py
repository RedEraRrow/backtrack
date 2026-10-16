"""The theme: the one chosen in Settings, and Terminal's two accents (from the
terminal's own colours). Album art is Terminal with its accents taken from the
playing track's cover, the chosen ones when there's none."""
from __future__ import annotations

from backbone import ui
from backtrack.config import setting

ALBUM_ART = "album_art"
THEMES = {ALBUM_ART: "Album art"}       # backtrack's own, beside backbone's ui.THEMES
ACCENTED = (ui.DEFAULT_THEME, ALBUM_ART)  # the themes that use the chosen accents


def apply(path: str | None = None, cfg: dict | None = None) -> None:
    """Paint with the chosen theme for `path` playing (None: nothing is).
    `cfg`: settings not saved yet (Settings' own); else the saved ones."""
    from backtrack.music_library import _live_config
    cfg = cfg if cfg is not None else _live_config()
    name = setting(cfg, 'theme')
    ui.set_terminal_accents(setting(cfg, 'accent_colour'), setting(cfg, 'accent_colour_2'))
    ui.set_theme(ui.DEFAULT_THEME if name == ALBUM_ART else name, overrides(name, path))


def overrides(name: str, path: str | None) -> dict | None:
    """Album art's colours over Terminal for `path` playing: its cover's two
    accents, or None (Terminal's, the chosen ones) with nothing playing, no
    colourful cover, or another theme."""
    if name != ALBUM_ART or not path:
        return None
    from backtrack.playback.player_art import art_accents
    pair = art_accents(path)
    return dict(zip(("ACCENT", "ACCENT2"), pair)) if pair else None
