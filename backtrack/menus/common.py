"""What every menu screen shares: headers, list columns, the settings-row
the Browse categories, and small helpers for headers and saving."""
from __future__ import annotations
from backbone.ui import Colors as C
from backbone import prompt
from backbone import keys, ui
from backtrack.config import load_config, update_config
from backtrack.config import setting


# Keys of the library's lists (browse, search results, history), passed to
# prompt.select by action id so they follow the Key bindings page.
keys.define("library", "Library lists", [
    ("play_all", ("p",), "play everything listed"),
    ("shuffle", ("x",), "shuffle everything listed"),
    ("album_shuffle", ("X",), "shuffle by album"),
    ("random_album", ("R",), "play an album picked at random"),
    ("edit", ("e",), "edit the tags"),
    ("edit_all", ("E",), "edit the tags of everything listed"),
    ("sort", ("s",), "sort"),
    ("letters", ("/",), "jump by letter, or the full list"),
], within=("list", "global"))


# Structured column layouts for browse lists (no string parsing: each Choice
# carries explicit `cells`).
_TRACK_COLUMNS = [
    prompt.Column(style='static-dim', align='right', gap=0),     # number (or a chapter's time): holds still
    prompt.Column(style='primary', max_frac=0.5, scroll=True, gap=2),   # title (truncates; scrolls when highlighted)
    prompt.Column(style='dynamic-dim', flex=True, align='left', priority=1),  # featured artist: drops first when narrow
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # duration (pinned right, kept)
]


_ALBUM_COLUMNS = [
    prompt.Column(style='primary', flex=True),                   # album name
    prompt.Column(style='dynamic-dim', flex=True, align='left'),  # album artist
]


def _menu_header(title: str, subtitle: str | None = None):
    """The header for prompt.select's header= parameter: the screen's name, in
    its box's top border (with a subtitle, the box's first line).

    The header names the screen, so the accompanying select() message is left
    empty ("") whenever it would only say the same thing one line further down
    ("Albums" over "Albums:"). Pass a message only when it tells you something
    the header does not: "Action:" under a track title, "Sort by:" over a list
    of sort modes.
    """
    return prompt.PanelTitle(title, subtitle)


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
