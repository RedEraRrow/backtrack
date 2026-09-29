"""What every menu screen shares: headers, list columns, the settings-row
glyphs, the Browse categories, and small helpers for cursors and saving."""
from __future__ import annotations
from src.utils.ui_utils import Colors as C
from src.utils import prompt
from src.utils import ui_utils
from src.music_library import derive_album_credit
from src.config import load_config, update_config
from src.config import setting


# Structured column layouts for browse lists (no string parsing: each Choice
# carries explicit `cells`).
_TRACK_COLUMNS = [
    prompt.Column(style='primary', max_frac=0.5),                # title (truncates)
    prompt.Column(style='dynamic-dim', flex=True, align='left', priority=1),  # featured artist: drops first when narrow
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # duration (pinned right, kept)
]


_ALBUM_COLUMNS = [
    prompt.Column(style='primary', flex=True),                   # album name
    prompt.Column(style='dynamic-dim', flex=True, align='left'),  # album artist
]


def _idx_of(choices: list, value, default: int = 0) -> int:
    """Index of the choice whose value == `value` (for restoring the cursor on back)."""
    for i, c in enumerate(choices):
        cv = c.value if isinstance(c, prompt.Choice) else c
        if cv == value:
            return i
    return default


def _menu_header(title: str, subtitle: str | None = None):
    """Return a lazy header builder callable for prompt.select's header= parameter.

    The header names the screen, so the accompanying select() message is left
    empty ("") whenever it would only say the same thing one line further down
    ("Albums" over "Albums:"). Pass a message only when it tells you something
    the header does not: "Action:" under a track title, "Sort by:" over a list
    of sort modes.
    """

    def _build() -> list[str]:
        cols = ui_utils.get_terminal_width()
        # Title + optional subtitle on one line, then a thin divider
        title_str    = f"{C.BOLD}{title}{C.RESET}"
        subtitle_str = f"  {C.DIM}{subtitle}{C.RESET}" if subtitle else ""
        lines = [
            f"  {title_str}{subtitle_str}",
            f"{C.DIM}{ui_utils.divider(cols, '─')}{C.RESET}",
        ]
        return lines

    return _build


def _album_artist_of(songs: list) -> str:
    """First non-empty album artist among a group's songs.

    With no album artist, fall back to the credit derived from the track casts,
    the same anchor rule the artist grouping uses, so the displayed credit and
    the group a track is filed under can never disagree.
    """
    album_artists = [
        (s.get('album_artist') or '').strip()
        for s in songs
        if s.get('album_artist')
    ]
    if album_artists:
        return album_artists[0]

    return derive_album_credit(songs)


def _commit(cfg: dict, *keys: str) -> None:
    """Save these keys of a screen's working config (update_config: into the
    config as it is on disk now), and bring the working copy up to date with
    anything saved meanwhile."""
    fresh = update_config({k: cfg[k] for k in keys})
    cfg.clear()
    cfg.update(fresh)


def _disc_track_cell(song: dict) -> str:
    """Compact disc/track indicator: '1·05' when multi-disc, else '05' (or '')."""
    trk = str(song.get('track', '') or '').strip()
    disc = str(song.get('disc', '') or '').strip()
    total_discs = str(song.get('total_discs', '') or '').strip()
    if not trk or trk == '0':
        return ""
    trk = trk.zfill(2)
    multi = (disc and disc not in ('0', '1')) or (total_discs and total_discs not in ('0', '1'))
    return f"{disc}·{trk}" if multi and disc else trk


def _autoplay() -> bool:
    """Whether selecting a track should play immediately, skipping the action menu."""
    return bool(setting(load_config(), 'autoplay_on_select'))


# Settings rows carry their current state in a right-hand column, so every
# setting can be read without changing it: a tick/cross for the on/off ones, the
# value itself for the rest. Labels are sentence case, like every other screen.
_SETTINGS_COLUMNS = [
    prompt.Column(style='primary'),                 # label, sized to its content
    prompt.Column(style='dynamic-dim', flex=True),  # state, left-aligned just after
]


# The on/off pair: filled and hollow, not a tick and a cross: ✘ reads as
# *invalid* rather than *off*. ● and ○ differ only in fill, which is exactly
# the difference.
ON_GLYPH, OFF_GLYPH = "●", "○"


def _state_glyph(value) -> str:
    """Tick or cross for a boolean setting's current state."""
    return ON_GLYPH if value else OFF_GLYPH


def _space_toggles(values) -> dict:
    """select() kwargs making space flip the rows in `values` (the on/off
    ones): select() returns ("__space__", row), and space does nothing on any
    other row."""
    return {"on_inspect": lambda v: ("__space__", v) if v in values else None,
            "inspect_key": "SPACE"}


# Browse categories: key → (label, field grouped by, drills into an album list
# first, offers the A-Z letter index). Which appear, and in what order, is the
# `browse_menu` setting; "libraries" is the Libraries submenu, not a field.
BROWSE_CATEGORIES = {
    'artists':   ("Artists",   'artist',   True,  True),
    'albums':    ("Albums",    'album',    False, True),
    'genres':    ("Genres",    'genre',    True,  False),
    'composers': ("Composers", 'composer', True,  True),
    'lyricists': ("Lyricists", 'lyricist', True,  True),
    'people':    ("People",    'people',   True,  True),
    'years':     ("Years",     'year',     True,  False),
    'decades':   ("Decades",   'decade',   True,  False),
    'groupings': ("Groupings", 'grouping', True,  False),
    'works':     ("Works",     'work',     False, True),
    'libraries': ("Libraries", None,       False, False),
}


DEFAULT_BROWSE_MENU = ['artists', 'albums', 'genres', 'libraries']


def browse_menu_keys(cfg: dict) -> list:
    """The Browse menu's categories, in order: the setting, less unknown keys."""
    keys = [k for k in (cfg.get("browse_menu") or DEFAULT_BROWSE_MENU) if k in BROWSE_CATEGORIES]
    return list(dict.fromkeys(keys)) or list(DEFAULT_BROWSE_MENU)
