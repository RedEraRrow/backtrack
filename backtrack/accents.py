"""The two accent colours: the ones chosen in Settings, or, with "Colours from
album art" on, two taken from the playing track's cover."""
from __future__ import annotations

from backbone import ui
from backtrack.config import setting


def apply(path: str | None = None, cfg: dict | None = None) -> None:
    """Set the accents for `path` playing (None: nothing is): its cover's
    colours in art mode when it has a colourful cover, else the chosen ones.
    `cfg`: settings not saved yet (Settings' own); else the saved ones."""
    from backtrack.music_library import _live_config
    cfg = cfg if cfg is not None else _live_config()
    pair = None
    if path and setting(cfg, 'accent_from_art'):
        from backtrack.playback.player_art import art_accents
        pair = art_accents(path)
    first, second = pair or (setting(cfg, 'accent_colour'), setting(cfg, 'accent_colour_2'))
    ui.set_accent(first)
    ui.set_accent(second, secondary=True)
