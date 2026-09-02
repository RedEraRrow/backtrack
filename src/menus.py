"""Top-level TUI menu hierarchy: browse, search, history, settings, and playback."""
from __future__ import annotations
import os
import random
import string
import datetime

from src.utils.ui_utils import roman, Colors as C

from src.utils import prompt
from src.utils import ui_utils
from src.music_library import (
    build_library, save_library_cache,
    start_background_sync,
    get_grouped_data, get_group_sort_key, sort_library_logic, derive_album_credit, format_tag_values, album_year,
    to_num
)
from src.history import get_history, clear_history, get_recent_paths
from src import search as _search
from src.playback.playback import music_player
from src.playback.session import REPEAT_OFF, REPEAT_ONE, REPEAT_ALL, active_session, is_client
from src.lyrics.lyrics_editor import lyrics_editor, find_lyrics
from src.config import load_config, save_config, music_dirs, set_music_dirs
from src.state import NAV_STACK
from src.id3.id3_browser import inspect_tag_loop
from src.id3.bulk_id3_manager import bulk_id3_manager
from src.id3.tag_registry import TAG_REGISTRY

# Structured column layouts for browse lists (no string parsing — each Choice
# carries explicit `cells`).
_TRACK_COLUMNS = [
    prompt.Column(style='primary', max_frac=0.5),                # title (truncates)
    prompt.Column(style='dynamic-dim', flex=True, align='left', priority=1),  # featured artist — drops first when narrow
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
    the header does not — "Action:" under a track title, "Sort by:" over a list
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


# --- Browse sort options -------------------------------------------------
# Each context offers a list of (mode, label) pairs; the first entry is the
# default and is always alphabetical by name.
_GROUP_SORTS = {
    "Artists": [("name", "Name (A–Z)"), ("name_desc", "Name (Z–A)"), ("tracks", "Most tracks")],
    "Genres":  [("name", "Name (A–Z)"), ("name_desc", "Name (Z–A)"), ("tracks", "Most tracks")],
    "Albums":  [("name", "Name (A–Z)"), ("artist", "Album artist"),
                ("year_new", "Year (newest)"), ("year_old", "Year (oldest)")],
}
_ALBUM_SORTS = [("name", "Name (A–Z)"), ("year_new", "Year (newest)"), ("year_old", "Year (oldest)")]


def _year_of(songs: list) -> int:
    """The year an album sorts under — see `music_library.album_year`.

    Was "the first song with an int-parseable year", which meant a dated file
    ('2005-09-15 18:30:00') counted as year-less and a compilation inherited
    whichever track happened to sort first.
    """
    return album_year(songs)


def _album_artist_of(songs: list) -> str:
    """First non-empty album artist among a group's songs.

    With no album artist, fall back to the credit derived from the track casts —
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


def _sort_groups(names: list, grouped: dict, cat_key: str, mode: str) -> list:
    """Order group names by the chosen sort mode; ties fall back to name order."""
    def nk(n: str) -> str:
        """Sort key for group n (also used as the tiebreaker for other modes)."""
        return get_group_sort_key(n, grouped[n], cat_key)
    if mode == "name_desc":
        return sorted(names, key=nk, reverse=True)
    if mode == "artist":
        return sorted(names, key=lambda n: (_album_artist_of(grouped[n]).lower(), nk(n)))
    if mode == "year_new":
        return sorted(names, key=lambda n: (-_year_of(grouped[n]), nk(n)))
    if mode == "year_old":
        return sorted(names, key=lambda n: (_year_of(grouped[n]) or 99999, nk(n)))
    if mode == "tracks":
        return sorted(names, key=lambda n: (-len(grouped[n]), nk(n)))
    return sorted(names, key=nk)  # "name" (default, alphabetical)
def _pick_sort(current: str, options: list, header) -> str:
    """Show a sort picker; return the chosen mode (unchanged if cancelled)."""
    choices = [
        prompt.Choice(title=f"{'✔ ' if v == current else '  '}{lbl}", value=v)
        for v, lbl in options
    ]
    sel = prompt.select("Sort by:", choices=choices, header=header,
                        index=_idx_of(choices, current))
    return sel or current


# Search scope cycled with Tab in the live search screen (default: all fields).
_ALL_SEARCH_FIELDS = ['title', 'artist', 'album', 'composer', 'lyricist', 'genre', 'people']
_SCOPE_CYCLE = ['all', 'title', 'artist', 'album', 'composer', 'lyricist', 'genre', 'people']

# Columns for live search results: title (matched chars accented) · artist ·
# album · people (whoever matched) · disc/track · duration. `title` flexes;
# the rest are width-capped so the layout stays aligned across queries.
# title · artist · album · people · disc/track · duration. On narrow terminals
# the least important columns drop first (priority; lower = dropped sooner):
# people, then disc/track, then duration, then album, then artist. Title never
# drops (no priority = essential).
_SEARCH_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', max_frac=0.20, priority=5),
    prompt.Column(style='dynamic-dim', max_frac=0.20, priority=4),
    prompt.Column(style='dynamic-dim', max_frac=0.22, priority=1),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=2),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=3),
]


def _hl_segments(value: str, tokens: list, base_style: str) -> list:
    """Split `value` into styled segments, accenting the fuzzy-matched spans."""
    value = str(value or "")
    if not value:
        return [("", base_style)]
    spans = _search.highlight_spans(value, tokens) if tokens else []
    if not spans:
        return [(value, base_style)]
    segs: list = []
    i = 0
    for a, b in spans:
        if a > i:
            segs.append((value[i:a], base_style))
        segs.append((value[a:b], 'accent'))
        i = b
    if i < len(value):
        segs.append((value[i:], base_style))
    return segs


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


def _people_cell(value: str, tokens: list):
    """People column: the matched person(s), highlighted — empty when no person
    in this track's cast matched (so the column reads as 'who matched')."""
    if not value or not tokens:
        return ""
    parts = value.replace(';', ',').replace('/', ',').split(',')
    entries = [e.strip() for e in parts if e.strip()]
    matched = [e for e in entries if _search.highlight_spans(e, tokens)]
    if not matched:
        return ""
    segs: list = []
    for i, e in enumerate(matched):
        if i:
            segs.append((", ", 'dim'))
        segs += _hl_segments(e, tokens, 'dynamic-dim')
    return segs


def _search_result_cells(result, tokens: list) -> list:
    """Build the columned, highlighted cells for one search result row."""
    s = result.song
    title = _hl_segments(s.get('title', ''), tokens, 'primary')
    artist = _hl_segments(format_tag_values(s.get('artist', '')), tokens, 'dynamic-dim')
    album = _hl_segments(s.get('album', ''), tokens, 'dynamic-dim')
    # Only fill the people column when people was the field that actually matched
    # (avoids weak subsequence hits populating it on a title/artist search).
    people = (_people_cell(str(s.get('people', '') or ''), tokens)
              if 'people' in result.matched_fields else "")
    # Composer, lyricist and genre have no column of their own. When one of them
    # is why the row matched, and nothing visible in the row is highlighted to
    # show it, tag it onto the album so the row still explains itself. Order is
    # most specific first — a writing credit says more than a genre.
    if not people and not any(
            _search.highlight_spans(str(s.get(f, '') or ''), tokens)
            for f in ('title', 'artist', 'album')):
        for extra in ('composer', 'lyricist', 'genre'):
            if extra in result.matched_fields:
                album = album + [("  · ", 'dim')] + _hl_segments(
                    format_tag_values(s.get(extra, '')), tokens, 'dynamic-dim')
                break
    dur = s.get('duration') or 0
    dur_str = ui_utils.format_time(int(dur)) if dur else ""
    return [title, artist, album, people, _disc_track_cell(s), dur_str]


# Sections, in the order they appear. Each caps at a handful of rows with a
# "show all" row beneath, so the whole shape of a result set stays above the
# fold instead of the first type of match filling the screen.
_SECTIONS = (('artist', 'ARTISTS', 'artist'), ('album', 'ALBUMS', 'album'),
             ('composer', 'COMPOSERS', 'composer'),
             ('lyricist', 'LYRICISTS', 'lyricist'),
             ('genre', 'GENRES', 'genre'), ('people', 'PEOPLE', 'person'))
_SECTION_CAP = 4
# Below this many characters, only entities are listed. Short prefixes match a
# huge number of tracks and almost none of them usefully; the artist or album
# you are heading for is nearly always what you meant by "jo".
_ENTITY_ONLY_UNTIL = 3


def _entity_cells(ent, tokens: list) -> list:
    """Cells for an entity row: name (highlighted) · what it is · size."""
    name = _hl_segments(ent.name, tokens, 'primary')
    sub = ent.subtitle or ''
    return [name, ent.kind, sub, "", "", ui_utils.plural(len(ent.tracks), 'track')]


def handle_search(library: list) -> str | None:
    """Run the live fuzzy search screen; on selecting a track, offer play/edit
    actions (or play immediately if autoplay is on or there's only one option).

    Results are grouped by what they are — artists, albums, genres, then the
    tracks themselves — rather than listed as one flat run. A query like "john"
    matches every episode of a series; collapsed, that is one artist row saying
    42 tracks instead of 42 rows saying the same thing in different words.
    """
    if not library:
        ui_utils.show_status("Library is empty. Scan a directory first.")
        return None

    _cfg = load_config()
    _show_editor = _cfg.get("show_metadata_editor", True)
    _show_lyrics = _cfg.get("show_lyrics_editor", True)
    recent = get_recent_paths()

    scope = {'i': 0}                       # index into _SCOPE_CYCLE
    expanded = {'kind': None}              # a section shown in full, or None

    def _fields() -> list:
        """The field(s) to search under the current scope."""
        mode = _SCOPE_CYCLE[scope['i']]
        return _ALL_SEARCH_FIELDS if mode == 'all' else [mode]

    def _cycle(step: int = 1) -> None:
        """Move the search scope along the cycle."""
        scope['i'] = (scope['i'] + step) % len(_SCOPE_CYCLE)
        expanded['kind'] = None

    _last: dict = {'results': [], 'entities': {}, 'counts': {}}

    def _provider(query: str) -> list:
        """Search, group into sections, and build the rows for the whole screen."""
        results = _search.search(library, query, _fields(), recent=recent)
        tokens = _search.tokenize(query)
        ents = _search.collect_entities(results, tokens)
        _last['results'] = [r.song for r in results]
        _last['entities'] = ents
        _last['counts'] = {k: len(v) for k, v in ents.items()}
        _last['counts']['track'] = len(results)

        choices: list = []
        show = expanded['kind']

        def _section(kind: str, label: str, singular: str, rows: list, build) -> None:
            """Append one section: heading, capped rows, and a 'show all' row."""
            if not rows or (show is not None and show != kind):
                return
            choices.append(prompt.separator(f"{label}  {len(rows)}"))
            cap = len(rows) if show == kind else _SECTION_CAP
            for item in rows[:cap]:
                choices.append(build(item))
            if len(rows) > cap:
                choices.append(prompt.Choice(
                    title=f"    show all {ui_utils.plural(len(rows), singular)}",
                    value=("__expand__", kind)))

        # Top result: the single best thing across every kind, so the most likely
        # answer is always the first row rather than buried in whichever section
        # happens to sort first.
        best = max((e for v in ents.values() for e in v),
                   key=lambda e: e.score, default=None)
        if show is None and best is not None:
            choices.append(prompt.separator("TOP RESULT"))
            choices.append(prompt.Choice(title=best.name, value=("__entity__", best),
                                         cells=_entity_cells(best, tokens)))
        else:
            best = None

        for kind, label, singular in _SECTIONS:
            rows = [e for e in ents.get(kind, []) if e is not best]
            _section(kind, label, singular, rows,
                     lambda e: prompt.Choice(title=e.name, value=("__entity__", e),
                                             cells=_entity_cells(e, tokens)))

        # Tracks last: with the entities above them, the track list is for when
        # you want a specific recording rather than a body of work.
        tracks_shown = (len(query) >= _ENTITY_ONLY_UNTIL or not any(ents.values()))
        if tracks_shown:
            _section('track', 'TRACKS', 'track', results,
                     lambda r: prompt.Choice(title=r.song.get('title', ''),
                                             value=r.song['path'],
                                             cells=_search_result_cells(r, tokens)))
        if _show_editor and results and tracks_shown and show in (None, 'track'):
            choices.append(prompt.Choice(
                title=f"    edit tags — all {ui_utils.plural(len(results), 'result')}",
                value="__bulk_edit__"))
        return choices

    def _hdr() -> list:
        mode = _SCOPE_CYCLE[scope['i']]
        label = "all fields" if mode == 'all' else mode
        singular = {kind: sing for kind, _lbl, sing in _SECTIONS}
        bits = [ui_utils.plural(v, singular.get(k, k))
                for k, v in _last['counts'].items() if v]
        sub = " · ".join(bits) if bits else f"{ui_utils.plural(len(library), 'track')} indexed"
        return _menu_header("Search", f"{sub}    ^f scope: {label}")()

    while True:
        selected = prompt.live_select(
            "", _provider, columns=_SEARCH_COLUMNS,
            header=_hdr, on_cycle=_cycle, cycle_key='\x06',   # ^F cycles scope
            section_nav=True,
            count_of=lambda: len(_last['results']))
        if not selected:
            return None
        if isinstance(selected, tuple):
            # Structured rows carry (kind, payload). An unrecognised one is
            # ignored rather than falling through to the track path, where it
            # would be handed to os.path.basename as if it were a file.
            kind = selected[0]
            if kind == "__expand__":
                expanded['kind'] = selected[1]
            elif kind == "__entity__":
                if _play_entity(selected[1], library) == "QUIT_ALL":
                    return "QUIT_ALL"
            continue
        expanded['kind'] = None
        break

    if selected == "__bulk_edit__":
        bulk_id3_manager(library, paths=[s['path'] for s in _last['results']])
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _action_choices = ["Play"]
    if _show_lyrics and find_lyrics(selected):
        _action_choices.append("Edit lyrics")
    if _show_editor:
        _action_choices.append("Edit metadata")
    _action_choices += _queue_action_choices()

    if len(_action_choices) == 1 or _autoplay():
        action = "Play"
    else:
        action = prompt.select(
            "Action:",
            choices=_action_choices,
            header=_menu_header(track_title),
        )

    if action == "Play":
        ui_utils.clear_screen()
        res = music_player(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif _handle_queue_action(action, selected, track_title):
        pass
    elif action == "Edit lyrics":
        ui_utils.clear_screen()
        lyrics_editor(selected)
        ui_utils.clear_screen()
    elif action == "Edit metadata":
        ui_utils.clear_screen()
        inspect_tag_loop(selected, library_metadata=song_meta, library=library)
        ui_utils.clear_screen()

    return None


def _play_entity(ent, library: list) -> str | None:
    """Open a chosen artist/album/genre: list its tracks and act on one."""
    tracks = ent.tracks
    choices = [prompt.Choice(title=f"▸  Play all — {ent.name}", value="__play_all__")]
    for s in tracks:
        choices.append(prompt.Choice(
            title=s.get('title', ''), value=s['path'],
            cells=[s.get('title', ''), format_tag_values(s.get('artist', '')),
                   s.get('album', ''), "", _disc_track_cell(s),
                   ui_utils.format_time(int(s.get('duration') or 0)) if s.get('duration') else ""]))
    sub = ui_utils.plural(len(tracks), 'track')
    pick = prompt.select(f"{ent.kind.title()}:", choices=choices,
                         columns=_SEARCH_COLUMNS,
                         header=_menu_header(ent.name, sub))
    if not pick:
        return None
    if pick == "__play_all__":
        return play_queue([s['path'] for s in tracks], library=library)
    ui_utils.clear_screen()
    res = music_player(pick)
    ui_utils.clear_screen()
    if res and res.get("status") == "QUIT_ALL":
        return "QUIT_ALL"
    return None


# Listening-history columns: title · artist · album · when (relative) · listened.
# Narrow terminals drop the least important first (priority; lower = sooner):
# album, then listened, then when, then artist. Title never drops (essential).
_HISTORY_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', max_frac=0.24, priority=4),
    prompt.Column(style='dynamic-dim', max_frac=0.24, priority=1),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=3),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=2),
]


def _autoplay() -> bool:
    """Whether selecting a track should play immediately, skipping the action menu."""
    return bool(load_config().get('autoplay_on_select', False))


def _relative_time(ts: str, now: "datetime.datetime | None" = None) -> str:
    """Human 'x ago' for a history timestamp; falls back to the raw date."""
    try:
        dt = datetime.datetime.fromisoformat(ts.strip()[:19])
    except (ValueError, AttributeError):
        return (ts or '')[:16]
    now = now or datetime.datetime.now()
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hrs = mins / 60
    if hrs < 24:
        return f"{int(hrs)}h ago"
    days = hrs / 24
    if days < 2:
        return "yesterday"
    if days < 7:
        return f"{int(days)}d ago"
    if days < 28:
        return f"{int(days / 7)}w ago"
    return dt.strftime("%d %b %Y")


def _nice_dur(raw: str) -> str:
    """Format a raw duration (e.g. '90s') as compact 'h/m/s' text."""
    try:
        secs = int(str(raw).rstrip('s'))
    except ValueError:
        return str(raw)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)


def handle_history(library: list) -> str | None:
    """Show the recent listening history list; on selecting an entry, offer play/edit actions."""
    history_entries = get_history(limit=30)

    if not history_entries:
        ui_utils.show_status("No listening history available.")
        return None

    now = datetime.datetime.now()
    choices = []
    for ts, dur, path in history_entries:
        song = next((s for s in library if s['path'] == path), None)
        if song:
            title = song.get('title') or os.path.splitext(os.path.basename(path))[0]
            artist = format_tag_values(song.get('artist'))
            album = (song.get('album') or '').strip()
            artist = '' if artist == 'Unknown Artist' else artist
            album = '' if album == 'Unknown Album' else album
        else:
            title = os.path.splitext(os.path.basename(path))[0]
            artist = album = ''
        choices.append(prompt.Choice(
            title=title, value=path,
            cells=[title, artist, album, _relative_time(ts, now), _nice_dur(dur)]))

    selected = prompt.select(
        "",
        choices=choices,
        columns=_HISTORY_COLUMNS,
        header=_menu_header("Listening History", f"{len(history_entries)} recent"),
        **_queue_shortcut_kwargs(library),
    )
    if not selected:
        return None

    song_meta = next((s for s in library if s['path'] == selected), None)
    track_title = song_meta['title'] if song_meta else os.path.basename(selected)

    _cfg = load_config()
    _action_choices = ["Play"]
    if _cfg.get("show_lyrics_editor", True) and find_lyrics(selected):
        _action_choices.append("Edit lyrics")
    if _cfg.get("show_metadata_editor", True):
        _action_choices.append("Edit metadata")

    if len(_action_choices) == 1 or _autoplay():
        action = "Play"
    else:
        action = prompt.select(
            "Action:",
            choices=_action_choices,
            header=_menu_header(track_title),
        )

    if action == "Play":
        ui_utils.clear_screen()
        res = music_player(selected)
        ui_utils.clear_screen()
        if res and res.get("status") == "QUIT_ALL":
            return "QUIT_ALL"
    elif action == "Edit lyrics":
        ui_utils.clear_screen()
        lyrics_editor(selected)
        ui_utils.clear_screen()
    elif action == "Edit metadata":
        ui_utils.clear_screen()
        inspect_tag_loop(selected, library_metadata=song_meta, library=library)
        ui_utils.clear_screen()

    return None


def _handle_tag_name_preferences() -> None:
    """Settings sub-screen: edit preferred friendly names for tags via list_edit."""
    config = load_config()
    prefs: dict = dict(config.get('tag_name_preferences', {}))
    initial: list = [(tag_id, prefs.get(tag_id, "")) for tag_id in TAG_REGISTRY]

    def _tag_pref_hints(col: int, row: list) -> list:
        """Suggest the tag registry's default display name as a hint for the preferred-name column."""
        if col != 1:
            return []
        tag_id = row[0].strip() if row else ""
        info = TAG_REGISTRY.get(tag_id)
        return info.name if info else []

    result = prompt.list_edit(
        "Tag name preferences — TAG · PREFERRED NAME (blank name = use default):",
        initial_items=initial,
        headers=("TAG", "PREFERRED NAME"),
        col_ratios=(1, 4),
        col_hints=_tag_pref_hints,
        fixed_rows=True,
        locked_cols={0},
    )
    if result is None:
        return

    new_prefs: dict = {}
    for row in result:
        cols = list(row) if isinstance(row, (list, tuple)) else [str(row), ""]
        while len(cols) < 2:
            cols.append("")
        tag_id, name = str(cols[0]).strip(), str(cols[1]).strip()
        if tag_id and name and tag_id in TAG_REGISTRY:
            new_prefs[tag_id] = name

    changed = sum(1 for k, v in new_prefs.items() if prefs.get(k) != v) + \
              sum(1 for k in prefs if k not in new_prefs)
    config['tag_name_preferences'] = new_prefs
    save_config(config)
    ui_utils.show_status(f"Tag name preferences saved ({changed} changed).")


def _rescan_library(config: dict, library_ref: list) -> int:
    """Rebuild the cache from every configured directory and restart the sync."""
    ui_utils.show_status("Re-scanning library…")
    new_lib = build_library(
        music_dirs(config),
        ignore_hidden=config.get("ignore_hidden_files", False),
    )
    save_library_cache(new_lib, _async=False)
    existing = library_ref[0]
    existing.clear()
    existing.extend(new_lib)
    start_background_sync(existing)
    return len(new_lib)


def _music_dirs_menu(config: dict, library_ref: list) -> None:
    """Add / remove the directories the library is built from.

    Several roots are supported (a local folder plus an external drive, say);
    they may nest, and a root that is temporarily missing keeps its tracks in the
    cache rather than losing them.
    """
    _cursor = 0
    while True:
        dirs = music_dirs(config)
        _choices: list = [prompt.Choice(title="＋  Add a directory…", value="__add__")]
        for d in dirs:
            missing = "" if os.path.isdir(d) else "  (not available)"
            _choices.append(prompt.Choice(title=f"{d}{missing}", value=d))
        if dirs:
            _choices.append(prompt.separator())
            _choices.append(prompt.Choice(title="Re-scan now", value="__rescan__"))

        _sub = f"{len(dirs)} director{'y' if len(dirs) == 1 else 'ies'}"
        choice = prompt.select("", choices=_choices,
                               header=_menu_header("Music Directories", _sub),
                               index=_cursor)
        if not choice:
            return

        _cursor = _idx_of(_choices, choice)

        if choice == "__add__":
            new_root = prompt.path("Directory to add:")
            if not new_root:
                continue
            full = os.path.abspath(os.path.expanduser(new_root))
            if not os.path.isdir(full):
                ui_utils.show_status("Not a directory.")
                continue
            if any(os.path.normcase(full) == os.path.normcase(d) for d in dirs):
                ui_utils.show_status("Already in the list.")
                continue
            set_music_dirs(config, dirs + [full])
            save_config(config)
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Added — {n} tracks.")
            continue

        if choice == "__rescan__":
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Done — {n} tracks.")
            continue

        # An existing directory row: remove it (its tracks leave the library).
        if choice in dirs:
            if len(dirs) == 1 and not prompt.confirm(
                    "That's the only directory — remove it and empty the library?"):
                continue
            if len(dirs) > 1 and not prompt.confirm(f"Remove {os.path.basename(choice) or choice}?"):
                continue
            set_music_dirs(config, [d for d in dirs if d != choice])
            save_config(config)
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Removed — {n} tracks.")


# Settings rows carry their current state in a right-hand column, so every
# setting can be read without changing it: a tick/cross for the on/off ones, the
# value itself for the rest. Labels are sentence case, like every other screen.
_SETTINGS_COLUMNS = [
    prompt.Column(style='primary'),                 # label — sized to its content
    prompt.Column(style='dynamic-dim', flex=True),  # state, left-aligned just after
]

# The app's one tick and one cross: U+2714 HEAVY CHECK MARK and U+2718 HEAVY
# BALLOT X — a matched heavy pair, the same tick the sort picker, multi-select
# rows and "Save changes" already use. (U+2717, the old cross, is drawn
# brush-style in most fonts and read as a different kind of mark.)
# A filled/hollow pair, not a tick and a cross: ✘ reads as *invalid* rather
# than *off*, and the two glyphs it paired with differed in meaning as well as
# in shape. ● and ○ differ only in fill, which is exactly the difference.
ON_GLYPH, OFF_GLYPH = "●", "○"


def _state_glyph(value) -> str:
    """Tick or cross for a boolean setting's current state."""
    return ON_GLYPH if value else OFF_GLYPH


def handle_settings(library_ref: list) -> None:
    """Run the interactive settings menu loop, applying and persisting each toggled option."""
    config = load_config()
    _cursor = 0

    while True:
        def _bool(key: str, default: bool) -> str:
            """Right-column glyph for an on/off setting."""
            return _state_glyph(config.get(key, default))

        _tasks = len(ui_utils.BACKGROUND_TASKS)
        _prefs = len(config.get("tag_name_preferences") or {})
        _hist = len(get_history(limit=10 ** 9))

        # (value, label, current state) — separators are plain strings.
        _rows: list = [
            prompt.separator("Playback"),
            ("lead_in",      "Lyric lead-in…",       f"{float(config.get('lyric_lead_in', 2.0)):g}s"),
            ("autoplay",     "Auto-play on select",  _bool("autoplay_on_select", False)),
            prompt.separator("Library"),
            ("music_dirs",   "Music directories…",   ui_utils.plural(len(music_dirs(config)), "folder")),
            ("activity",     "Activity centre…",     f"{_tasks} running" if _tasks else "idle"),
            ("hidden",       "Hidden file filter",   _bool("ignore_hidden_files", False)),
            prompt.separator("Editors"),
            ("meta_editor",  "Metadata editor",      _bool("show_metadata_editor", True)),
            ("lyrics_editor", "Lyrics editor",       _bool("show_lyrics_editor", True)),
            ("plain_text",   "Plain-text editing",   _bool("plain_text_editing", False)),
            ("tag_names",    "Tag name preferences…", ui_utils.plural(_prefs, "override") if _prefs else "none"),
            ("delimiter",    "Sort list delimiter…", config.get("sort_list_delimiter", "/")),
            prompt.separator("History"),
            ("history",      "Listening history",    _bool("history_enabled", True)),
            ("clear_history", "Clear history log…",  ui_utils.plural(_hist, "entry", "entries")),
        ]
        _labels = {r[0]: r[1] for r in _rows if isinstance(r, tuple)}
        _choices = [r if not isinstance(r, tuple)
                    else prompt.Choice(title=r[1], value=r[0], cells=[r[1], r[2]])
                    for r in _rows]

        choice = prompt.select(
            "",
            choices=_choices,
            columns=_SETTINGS_COLUMNS,
            header=_menu_header("Settings"),
            index=_cursor,
        )

        if not choice:
            break

        # Stay on the row that was just acted on, rather than jumping to the top.
        _cursor = _idx_of(_choices, choice, _cursor)

        def _toggled(key: str, default: bool, note: str = "") -> None:
            """Flip an on/off setting and report its new state the same way everywhere."""
            config[key] = not config.get(key, default)
            glyph = _state_glyph(config[key])
            ui_utils.show_status(f"{_labels[choice]} {glyph}{note}")

        if choice == "activity":
            notification_centre()

        elif choice == "history":
            _toggled("history_enabled", True)

        elif choice == "clear_history":
            if _hist == 0:
                ui_utils.show_status("History is already empty.")
            elif prompt.confirm(f"Delete all {_hist} history entries? Cannot be undone."):
                ui_utils.show_status("History cleared." if clear_history()
                                     else "Could not clear history.")

        elif choice == "autoplay":
            _toggled("autoplay_on_select", False)

        elif choice == "lead_in":
            val = prompt.text("Lead-in seconds:", default=str(config.get("lyric_lead_in", 2.0)))
            if val is not None:
                try:
                    seconds = round(max(0.0, float(val)), 2)
                    config["lyric_lead_in"] = seconds
                    ui_utils.show_status(f"Lyric lead-in set to {seconds:g}s.")
                except ValueError:
                    ui_utils.show_status("Enter a number (e.g. 2 or 1.5).")

        elif choice == "meta_editor":
            _toggled("show_metadata_editor", True)

        elif choice == "lyrics_editor":
            _toggled("show_lyrics_editor", True)

        elif choice == "plain_text":
            _toggled("plain_text_editing", False)

        elif choice == "tag_names":
            _handle_tag_name_preferences()
            config = load_config()  # pick up changes written by the sub-handler
            continue

        elif choice == "delimiter":
            current = config.get("sort_list_delimiter", "/")
            picked = prompt.select(
                f"Delimiter for multi-artist sort values (current: {current!r}):",
                choices=["/ (slash)", "| (pipe)", "; (semicolon)", ", (comma)"],
            )
            if picked:
                delim = picked.split()[0]
                config["sort_list_delimiter"] = delim
                ui_utils.show_status(f"Sort list delimiter set to {delim!r}.")

        elif choice == "music_dirs":
            _music_dirs_menu(config, library_ref)

        elif choice == "hidden":
            # Both states need a re-scan before the library reflects the change.
            _toggled("ignore_hidden_files", False, " — re-scan to apply.")

        save_config(config)


def play_queue(paths: list, mode: str = "linear", library: list | None = None) -> str | None:
    """Play a queue of file paths in the given mode (linear, shuffle, repeat_one, repeat_all)."""
    playlist = list(paths)
    if mode == "shuffle":
        random.shuffle(playlist)

    # Build display titles for the in-player queue view (falls back to filename).
    title_map = {}
    if library:
        title_map = {s['path']: (s.get('title') or os.path.basename(s['path'])) for s in library}
    titles = [title_map.get(p) or os.path.splitext(os.path.basename(p))[0] for p in playlist]

    # The shared session owns the queue and auto-advances in the background
    # (feature #14), so this just starts it and opens the player. Minimising the
    # player ('b'/Esc) returns here with audio still playing; Stop ('s') ends it.
    session_mode = {"repeat_one": REPEAT_ONE, "repeat_all": REPEAT_ALL}.get(mode, REPEAT_OFF)
    result = music_player(playlist[0], queue_titles=titles, queue_index=0,
                          queue_paths=playlist, mode=session_mode)
    if isinstance(result, dict) and result.get("status") == "QUIT_ALL":
        return "QUIT_ALL"
    return None


def _queue_action_choices() -> list:
    """Track-menu queue actions for the *search* results (#14), which use the
    `live_select` widget and so can't take the listing-level n/a shortcuts that
    Browse/History now use. Offered whenever something is playing or we're a
    joined window. Routes through active_session() (local host or remote)."""
    return ["Play next", "Add to queue"] if (active_session().is_active() or is_client()) else []


def _queue_titles_for_paths(paths: list[str], library: list) -> list[str]:
    title_map = {s['path']: (s.get('title') or os.path.splitext(os.path.basename(s['path']))[0]) for s in library}
    return [title_map.get(p) or os.path.splitext(os.path.basename(p))[0] for p in paths]


def _queue_shortcut_kwargs(library: list,
                           group_paths: dict[str, list[str]] | None = None,
                           disc_track_map: dict[str, list[str]] | None = None,
                           work_track_map: dict[str, list[str]] | None = None) -> dict:
    """`select()` row-action kwargs for queue shortcuts in browse/list menus.

    ``n`` = Play next, ``a`` = Add to queue. Works on individual track rows,
    and also on group rows when provided with a group-to-paths mapping.
    """
    if not (active_session().is_active() or is_client()):
        return {}

    def _resolve_paths(value) -> tuple[list[str] | None, str | None]:
        if not isinstance(value, str) or value.startswith("__"):
            return None, None

        if group_paths and value in group_paths:
            return group_paths[value], value

        if disc_track_map and value.startswith("__disc_"):
            disc_val = value[len("__disc_"):]
            return disc_track_map.get(disc_val), f"Disc {disc_val}"

        if work_track_map and value.startswith("__work__"):
            work_name = value[len("__work__"):]
            return work_track_map.get(work_name), work_name

        song = next((s for s in library if s.get('path') == value), None)
        if song:
            return [value], song.get('title') or os.path.basename(value)
        return None, None

    def _do(action: str, value) -> None:
        paths, title = _resolve_paths(value)
        if not paths:
            return
        _handle_queue_action(action, paths, title or '', library)

    return {
        'row_actions': {
            'n': lambda v: _do("Play next", v),
            'a': lambda v: _do("Add to queue", v),
        },
        'row_action_hints': {'n': 'play next', 'a': 'queue'},
    }


def _handle_queue_action(action: str | None, path: str | list[str], title: str,
                         library: list | None = None) -> bool:
    """Dispatch a Play-next / Add-to-queue action against the active session
    (local host or remote); returns True if ``action`` was one of them."""
    if action not in ("Play next", "Add to queue"):
        return False
    a = active_session()
    paths = [path] if isinstance(path, str) else list(path)
    if not paths:
        return False

    titles = _queue_titles_for_paths(paths, library or [])
    if not a.is_active():
        a.start(paths[0], queue=paths, titles=titles)
        ui_utils.show_status(f"▶ {titles[0] if titles else os.path.basename(paths[0])}")
        return True

    if action == "Play next":
        for p, t in zip(reversed(paths), reversed(titles)):
            a.play_next(p, t)
        if len(paths) == 1:
            ui_utils.show_status(f"Playing next: {title}")
        else:
            ui_utils.show_status(f"Playing next: {len(paths)} tracks.")
    else:
        if len(paths) == 1:
            n = a.enqueue(paths[0], title)
            ui_utils.show_status(f"Added to queue ({n} in queue): {title}" if n else f"Added to queue: {title}")
        else:
            for p in paths:
                a.enqueue(p, None)
            ui_utils.show_status(f"Added {len(paths)} tracks to queue.")
    return True


def browse_menu(library_ref: list, cat_choice: str) -> str | None:
    """Drive the multi-level browse UI (groups → albums → tracks → actions) for one category."""
    library = library_ref[0]
    sorted_lib = sort_library_logic(library)

    NAV_STACK.append(cat_choice)
    _group_cursor  = None            # None → land on the first real row
    _group_sorts   = _GROUP_SORTS.get(cat_choice, _GROUP_SORTS["Artists"])
    _group_sort    = _group_sorts[0][0]   # default: alphabetical by name
    _letter_mode   = None            # None → decide dynamically on first render
    _letter_filter = None            # active letter, or None = show everything

    try:
        while True:  # LEVEL 2: Group Selection
            key_map = {"Artists": "artist", "Albums": "album", "Genres": "genre"}
            grouped  = get_grouped_data(sorted_lib, key_map[cat_choice])
            _cat_key = key_map.get(cat_choice, cat_choice)
            _cfg = load_config()
            _show_editor = _cfg.get("show_metadata_editor", True)

            # Name-sorted first so the letter index groups correctly.
            group_names = sorted(grouped.keys(),
                                 key=lambda n: get_group_sort_key(n, grouped[n], _cat_key))
            _sort_keys = {n: get_group_sort_key(n, grouped[n], _cat_key) for n in group_names}

            def _letter(name: str) -> str:
                """Alphabetical index letter for name, or '#' if non-alphabetic."""
                ch = _sort_keys[name][0].upper() if _sort_keys.get(name) else "#"
                return ch if ch in string.ascii_uppercase else "#"

            letters_found = sorted({_letter(n) for n in group_names})
            if "#" in letters_found:
                letters_found.remove("#")
                letters_found.append("#")

            # A letter index is offered for alphabetical categories with more than
            # one distinct letter; it defaults on when the full list would overflow
            # the screen, and can be toggled with "/".
            _can_letter = cat_choice in ("Artists", "Albums") and len(letters_found) > 1
            _vis = max(4, ui_utils.get_terminal_height() - 9)
            if _letter_mode is None:
                _letter_mode = _can_letter and len(group_names) > _vis
            if not _can_letter:
                _letter_mode = False

            # Hybrid header: Play all stays visible (also `p`); secondary actions
            # live in the hint bar (`s` sort, `e` edit, `/` toggle view).
            _play_label = "▸  Play all"          # scope is in the header above
            _sc: dict = {"p": "__play_all__"}
            _eh: dict = {"p": "play all"}
            if _show_editor:
                _sc["e"] = "__bulk_edit__"
                _eh["e"] = "edit tags"
            if _can_letter:
                _sc["/"] = "__toggle__"
                _eh["/"] = "full list" if _letter_mode else "by letter"

            group_paths = None
            if _letter_mode:
                # LETTER VIEW: compact A–Z index. Play all / Edit act on everything.
                names = group_names
                _choices = [prompt.Choice(title=_play_label, value="__play_all__")]
                _choices += letters_found
                _group_cols = None
                _header = _menu_header(cat_choice, "by letter")
            else:
                # FULL LIST VIEW: every item, optionally filtered to one letter.
                names = ([n for n in group_names if _letter(n) == _letter_filter]
                         if _letter_filter else list(group_names))
                names = _sort_groups(names, grouped, _cat_key, _group_sort)
                if len(_group_sorts) > 1:
                    _sc["s"] = "__sort__"
                    _eh["s"] = "sort"
                _choices = [prompt.Choice(title=_play_label, value="__play_all__")]
                group_paths = {name: [s['path'] for s in grouped[name]] for name in names}
                # In album browse, show the album artist beside each album (dimmed),
                # matching the artist/genre → album sublist.
                if cat_choice == "Albums":
                    for _a in names:
                        _choices.append(prompt.Choice(
                            title=_a, value=_a,
                            cells=[_a, format_tag_values(_album_artist_of(grouped[_a]))]))
                    _group_cols = _ALBUM_COLUMNS
                else:
                    _choices += names
                    _group_cols = None
                _sub = f"letter: {_letter_filter}" if _letter_filter else None
                _header = _menu_header(cat_choice, _sub)

            if _group_cursor is None:                 # start on the first real row
                _group_cursor = 1 if len(_choices) > 1 else 0

            selection = prompt.select(
                "",
                choices=_choices,
                header=_header,
                columns=_group_cols,
                shortcuts=_sc,
                extra_hints=_eh,
                index=_group_cursor,
                **_queue_shortcut_kwargs(library, group_paths=group_paths),
            )

            if not selection:
                # Back from a letter's filtered list → return to the A–Z index,
                # not out of the whole category. (Only when we actually drilled in
                # via a letter; a plain or toggled full list still exits.)
                if _letter_filter and _can_letter:
                    _restore_letter = _letter_filter
                    _letter_mode    = True
                    _letter_filter  = None
                    _group_cursor   = (1 + letters_found.index(_restore_letter)
                                       if _restore_letter in letters_found else None)
                    continue
                break

            if selection == "__toggle__":
                _letter_mode   = not _letter_mode
                _letter_filter = None
                _group_cursor  = None
                continue

            if selection == "__sort__":
                _group_sort = _pick_sort(_group_sort, _group_sorts, _header)
                continue

            if selection == "__play_all__":
                # dict.fromkeys: a track under two groups (multi-value genre/artist)
                # is one entry here, not one per group it appears in.
                paths = list(dict.fromkeys(
                    s['path'] for name in names for s in grouped[name]))
                res = play_queue(paths, library=library)
                if res == "QUIT_ALL":
                    NAV_STACK.clear()
                    NAV_STACK.append("Home")
                    return "QUIT_ALL"
                continue

            if selection == "__bulk_edit__":
                # dict.fromkeys: a track under two groups (multi-value genre/artist)
                # is one entry here, not one per group it appears in.
                paths = list(dict.fromkeys(
                    s['path'] for name in names for s in grouped[name]))
                bulk_id3_manager(library, paths=paths)
                continue

            if _letter_mode and selection in letters_found:
                _letter_filter = selection    # drill into that letter's full list
                _letter_mode   = False
                _group_cursor  = None
                continue

            _group_cursor = _idx_of(_choices, selection)
            NAV_STACK.append(selection)
            selected_songs = grouped[selection]
            _album_cursor = None                 # None → land on the first real row
            _album_sort   = _ALBUM_SORTS[0][0]   # default: alphabetical by name

            while True:  # LEVEL 3: Album Selection
                if cat_choice in ("Artists", "Genres"):
                    albums = {}
                    for s in selected_songs:
                        albums.setdefault(s['album'], []).append(s)

                    album_list = _sort_groups(list(albums.keys()), albums, 'album', _album_sort)

                    # Always present the album list — even a single album is worth
                    # showing as its own entry (albums are distinct from the artist).
                    # Hybrid header: Play all visible (also `p`); sort/edit → hints.
                    _show_editor = _cfg.get("show_metadata_editor", True)
                    _single_album = len(album_list) <= 1
                    _alb_choices: list = []
                    _asc: dict = {}
                    _aeh: dict = {}
                    if not _single_album:
                        _alb_choices.append(prompt.Choice(title="▸  Play all", value="__play_all__"))
                        _asc["p"] = "__play_all__"; _aeh["p"] = "play all"
                        if len(_ALBUM_SORTS) > 1:
                            _asc["s"] = "__sort__"; _aeh["s"] = "sort"
                        if _show_editor:
                            _asc["e"] = "__bulk_edit__"; _aeh["e"] = "edit tags"
                    # Show the album artist (dimmed) when it differs from the
                    # artist/genre we're browsing under (#33).
                    for _a in album_list:
                        # Compare the *displayed* credit with the group we're under:
                        # an artist group is now named after the whole billing, so
                        # "Ada Lark, Bo Vale" matches and the column stays empty.
                        _aa = format_tag_values(_album_artist_of(albums[_a]))
                        _aa = _aa if (_aa and _aa.lower() != selection.lower()) else ""
                        _alb_choices.append(prompt.Choice(
                            title=_a, value=_a, cells=[_a, _aa]))

                    if _album_cursor is None:        # start on the first real album
                        _album_cursor = 0 if _single_album else 1

                    album_paths = {name: [s['path'] for s in albums[name]] for name in album_list}
                    alb = prompt.select(
                        "Albums:",
                        choices=_alb_choices,
                        header=_menu_header(selection, cat_choice),
                        columns=_ALBUM_COLUMNS,
                        shortcuts=_asc,
                        extra_hints=_aeh,
                        index=_album_cursor,
                        **_queue_shortcut_kwargs(library, group_paths=album_paths),
                    )

                    if not alb:
                        break

                    if alb == "__sort__":
                        _album_sort = _pick_sort(_album_sort, _ALBUM_SORTS,
                                                 _menu_header(selection, cat_choice))
                        continue

                    if alb == "__play_all__":
                        res = play_queue([s['path'] for s in selected_songs], library=library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
                        continue

                    if alb == "__bulk_edit__":
                        bulk_id3_manager(library, paths=[s['path'] for s in selected_songs])
                        continue

                    _album_cursor = _idx_of(_alb_choices, alb)
                    NAV_STACK.append(alb)
                    track_paths = [s['path'] for s in albums[alb]]
                else:
                    track_paths = [s['path'] for s in selected_songs]

                _track_cursor = None   # None → land on the first real row
                while True:  # LEVEL 4: Track Selection
                    # Re-derive from library each iteration so tag edits show immediately
                    path_set     = set(track_paths)
                    final_tracks = [t for t in library if t['path'] in path_set]

                    final_tracks.sort(key=lambda x: (
                        to_num(x.get('disc', 1)),
                        to_num(x.get('track', 0)),
                    ))

                    discs = set(str(t.get('disc', '1')) for t in final_tracks)
                    has_multiple_discs = len(discs) > 1

                    track_choices  = []
                    current_disc   = None
                    current_work   = None
                    disc_track_map = {}
                    work_track_map = {}

                    for t in final_tracks:
                        disc_val = str(t.get('disc', '1'))
                        work     = t.get('work', '').strip()

                        disc_track_map.setdefault(disc_val, []).append(t['path'])
                        if work:
                            work_track_map.setdefault(work, []).append(t['path'])

                        # Disc header — left-bar section marker (#34)
                        if has_multiple_discs and disc_val != current_disc:
                            subtitle   = t.get('disc_subtitle', '')
                            disc_title = subtitle if subtitle else f"Disc {disc_val}"
                            track_choices.append(prompt.Choice(title=f"▎{disc_title}", value=f"__disc_{disc_val}", cursor_title=f"▍{disc_title}"))
                            current_disc = disc_val
                            current_work = None

                        # Work header — thinner bar, indented under the disc (#34)
                        if work and work != current_work:
                            d_pad = "  " if has_multiple_discs else ""
                            track_choices.append(prompt.Choice(title=f"{d_pad}▎{work}", value=f"__work__{work}", cursor_title=f"{d_pad}▍{work}"))
                            current_work = work

                        # Track row
                        base_pad = "  " if has_multiple_discs else ""
                        mv_pad   = "  " if work else ""
                        indent   = base_pad + mv_pad

                        mv_num  = roman(int(str(t.get('movement_number', '')).strip())) if t.get('movement_number') and t.get('movement_number') != "0" else ""
                        mv_name = t.get('movement_name', '').strip()

                        if mv_name and mv_num != "":
                            num_str = (str(t.get('track', '0')).zfill(2) + f" — {mv_num}.") if mv_num else str(t.get('track', '0')).zfill(2)
                            label   = f"{indent}{num_str} {mv_name}"
                        else:
                            label = f"{indent}{str(t.get('track', '0')).zfill(2)} — {t.get('title', 'Unknown')}"

                        # Structured cells: [title, featured artist, duration].
                        # The full artist is shown (only when it differs from the
                        # album artist); no string parsing, so any characters are safe.
                        _aa = (t.get('album_artist') or '').strip()
                        _ar = (t.get('artist') or '').strip()
                        _artist = _ar if (_ar and _ar.lower() != _aa.lower()) else ""
                        _dur = t.get('duration') or 0
                        _dur_str = ui_utils.format_time(int(_dur)) if _dur else ""

                        track_choices.append(prompt.Choice(
                            title=label, value=t['path'],
                            cells=[label, format_tag_values(_artist), _dur_str]))

                    _track_context = NAV_STACK[-1] if NAV_STACK else selection
                    _show_editor   = _cfg.get("show_metadata_editor", True)
                    # Hybrid header: Play all visible (also `p`); edit → `e` hint.
                    # With a single track, even Play all is noise — the lone track
                    # row already does both jobs.
                    _tsc: dict = {}
                    _teh: dict = {}
                    if len(final_tracks) <= 1:
                        _track_header_choices = []
                    else:
                        _track_header_choices = [
                            prompt.Choice(title="▸  Play all", value="__play_all__")
                        ]
                        _tsc["p"] = "__play_all__"; _teh["p"] = "play all"
                        if _show_editor:
                            _tsc["e"] = "__bulk_edit__"; _teh["e"] = "edit tags"

                    # Album artist shown in the header subtitle (#33).
                    _album_artist = format_tag_values(_album_artist_of(final_tracks))
                    _subtitle = _album_artist or (selection if _track_context != selection else cat_choice)

                    _all_track_choices = _track_header_choices + track_choices
                    if _track_cursor is None:        # start on the first real row
                        _track_cursor = len(_track_header_choices) if track_choices else 0

                    path_choice_obj = prompt.select(
                        "Tracks:",
                        choices=_all_track_choices,
                        header=_menu_header(_track_context, _subtitle),
                        columns=_TRACK_COLUMNS,
                        shortcuts=_tsc,
                        extra_hints=_teh,
                        index=_track_cursor,
                        **_queue_shortcut_kwargs(library,
                                                 disc_track_map=disc_track_map,
                                                 work_track_map=work_track_map),
                    )

                    if not path_choice_obj:
                        break

                    if path_choice_obj == "__play_all__":
                        res = play_queue([t['path'] for t in final_tracks], library=library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
                        continue

                    if path_choice_obj == "__bulk_edit__":
                        bulk_id3_manager(library, paths=[t['path'] for t in final_tracks])
                        continue

                    _track_cursor = _idx_of(_all_track_choices, path_choice_obj)

                    # Disc header selected — offer play or bulk edit for that disc
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__disc_"):
                        disc_val   = path_choice_obj[len("__disc_"):]
                        disc_paths = disc_track_map.get(disc_val, [])
                        subtitle   = next(
                            (t.get('disc_subtitle', '') for t in final_tracks if str(t.get('disc', '1')) == disc_val),
                            '',
                        )
                        disc_label   = subtitle if subtitle else f"Disc {disc_val}"
                        _show_editor = _cfg.get("show_metadata_editor", True)
                        _disc_choices = [prompt.Choice(title=f"▸  Play all — {disc_label}", value="__play_all__")]
                        if _show_editor:
                            _disc_choices.append(prompt.Choice(title=f"Edit tags — {disc_label}", value="__bulk_edit__"))

                        # With only "Play all" (editor hidden), skip the extra
                        # single-option screen and play the disc immediately.
                        if len(_disc_choices) == 1:
                            disc_action = "__play_all__"
                        else:
                            disc_action = prompt.select(
                                "",
                                choices=_disc_choices,
                                header=_menu_header(disc_label, _track_context),
                            )
                        if disc_action == "__play_all__":
                            res = play_queue(disc_paths, library=library)
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                        elif disc_action == "__bulk_edit__":
                            bulk_id3_manager(library, paths=disc_paths)
                        continue

                    # Work header selected — offer play or bulk edit for that work
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__work__"):
                        work_name    = path_choice_obj[len("__work__"):]
                        work_paths   = work_track_map.get(work_name, [])
                        _show_editor = _cfg.get("show_metadata_editor", True)
                        _work_choices = [prompt.Choice(title=f"▸  Play all — {work_name}", value="__play_all__")]
                        if _show_editor:
                            _work_choices.append(prompt.Choice(title=f"Edit tags — {work_name}", value="__bulk_edit__"))

                        # Only "Play all" (editor hidden) → skip the extra screen.
                        if len(_work_choices) == 1:
                            work_action = "__play_all__"
                        else:
                            work_action = prompt.select(
                                "",
                                choices=_work_choices,
                                header=_menu_header(work_name, _track_context),
                            )
                        if work_action == "__play_all__":
                            res = play_queue(work_paths, library=library)
                            if res == "QUIT_ALL":
                                return "QUIT_ALL"
                        elif work_action == "__bulk_edit__":
                            bulk_id3_manager(library, paths=work_paths)
                        continue

                    # LEVEL 5: Track action
                    selected_track = next((t for t in final_tracks if t['path'] == path_choice_obj), None)
                    track_title    = selected_track['title'] if selected_track else os.path.basename(path_choice_obj)
                    track_artist   = (format_tag_values(selected_track.get('artist'))
                                      if selected_track else '')
                    NAV_STACK.append(track_title)

                    _cfg_track = load_config()
                    while True:
                        _action_choices = ["Play"]
                        if _cfg_track.get("show_lyrics_editor", True) and find_lyrics(path_choice_obj):
                            _action_choices.append("Edit lyrics")
                        if _cfg_track.get("show_metadata_editor", True):
                            _action_choices.append("Edit metadata")

                        if len(_action_choices) == 1 or _autoplay():
                            action = "Play"
                        else:
                            action = prompt.select(
                                "Action:",
                                choices=_action_choices,
                                header=_menu_header(track_title, track_artist),
                            )

                        if not action:
                            break

                        if action == "Play":
                            ui_utils.clear_screen()
                            res = music_player(path_choice_obj)
                            ui_utils.clear_screen()
                            if res and res.get("status") == "QUIT_ALL":
                                return "QUIT_ALL"
                            if len(_action_choices) == 1 or _autoplay():
                                break

                        elif action == "Edit lyrics":
                            ui_utils.clear_screen()
                            lyrics_editor(path_choice_obj)
                            ui_utils.clear_screen()

                        elif action == "Edit metadata":
                            ui_utils.clear_screen()
                            inspect_tag_loop(path_choice_obj, library_metadata=selected_track, library=library)
                            ui_utils.clear_screen()
                            _cfg_track = load_config()

                    NAV_STACK.pop()

                if cat_choice in ("Artists", "Genres"):
                    NAV_STACK.pop()
                else:
                    break

            NAV_STACK.pop()

    finally:
        if NAV_STACK and NAV_STACK[-1] == cat_choice:
            NAV_STACK.pop()


def handle_browse(library_ref: list) -> str | None:
    """Show the "Browse by" category menu and route into browse_menu for the choice."""
    _cursor = 0
    _opts = ["Artists", "Albums", "Genres"]
    while True:
        choice = prompt.select(
            "",
            choices=_opts,
            header=_menu_header("Browse"),
            index=_cursor,
        )
        if not choice:
            break
        _cursor = _idx_of(_opts, choice)
        res = browse_menu(library_ref, choice)
        if res == "QUIT_ALL":
            return "QUIT_ALL"
    return None


def notification_centre() -> None:
    """A live panel of current background activities — opens from Settings or by
    clicking the status-bar ● beacon. Lists each running job with its live status
    and a pulsing dot, updating as they start/finish; closes on Esc / b / q, and
    shows a placeholder when nothing is running."""
    import sys
    import time
    from src.utils.terminal_input import raw_mode, get_key_non_blocking, clear_escape_buffer

    _hint_pairs = [("esc/b", "back")]
    hint_cells: dict = {}

    def _draw() -> None:
        tasks = list(ui_utils.BACKGROUND_TASKS.values())
        out = ["\033[H\033[3J\033[J" + C.HIDE,
               f"\n  {C.BOLD}Activity{C.RESET}"]
        out.append(f"   {C.DIM}{len(tasks)} running{C.RESET}\n\n" if tasks else "\n\n")
        if tasks:
            for msg in tasks:
                out.append(f"   {ui_utils.pulse_circle()}  {msg}\n")
        else:
            out.append(f"   {C.DIM}Nothing running right now.{C.RESET}\n")
        body = "".join(out) + "\n\n"
        # Pin the hints to the bottom, above the miniplayer and status bar, so
        # their keys hold a fixed position as the running-task list grows and
        # shrinks underneath them — and pick up the transport keys while audio
        # is playing, like every other screen.
        pairs = prompt.chrome_hint_pairs(_hint_pairs)
        hint = prompt._hint(*pairs)
        hint_lines = hint.split('\n')
        used = body.count('\n')
        pad = max(0, prompt._hint_pin_target() - used - len(hint_lines))
        sys.stdout.write(body + "\n" * pad + hint)
        sys.stdout.flush()
        hint_cells.clear()
        first_row = 1 + used + pad
        for k, line in enumerate(hint_lines):
            prompt.add_hint_click_cells(hint_cells, line, first_row + k, pairs)

    with raw_mode(sys.stdin):
        sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse
        sys.stdout.flush()
        try:
            last = None
            while True:
                # Re-key on the pulse frame only while active, so an idle panel is static.
                frame = int(time.time() * 6) if ui_utils.has_background_tasks() else 0
                sig = (tuple(sorted(ui_utils.BACKGROUND_TASKS.items())), frame)
                if sig != last:
                    _draw()
                    last = sig
                key = get_key_non_blocking()
                if key:
                    clear_escape_buffer()
                    if key.startswith('MOUSE_CLICK:'):
                        _p = key.split(':')
                        _r = int(_p[2]); _c = int(_p[3]) if len(_p) > 3 else 1
                        key = hint_cells.get((_r, _c)) or ''   # only the 'esc/b' glyphs act
                    if key in ('b', 'B', 'q', 'Q', '\x1b') or key == 'ESC':
                        break
                time.sleep(0.08)
        finally:
            sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse
            sys.stdout.flush()
    ui_utils.clear_screen()


def main_menu(library_ref: list) -> None:
    """Run the top-level main menu loop, dispatching to browse/search/history/settings."""
    _cursor = 0
    _opts = ["Browse", "Search", "Listening History", "Settings", "Exit"]
    while True:
        choice = prompt.select(
            "",
            choices=_opts,
            header=_menu_header("Music Player"),
            index=_cursor,
            allow_back=False,   # top level: no ←/b/Esc exit — only Enter or q/Exit
        )

        if not choice or choice == "Exit":
            break
        _cursor = _idx_of(_opts, choice)

        if choice == "Browse":
            res = handle_browse(library_ref)
            if res == "QUIT_ALL":
                break
        elif choice == "Search":
            res = handle_search(library_ref[0])
            if res == "QUIT_ALL":
                break
        elif choice == "Listening History":
            res = handle_history(library_ref[0])
            if res == "QUIT_ALL":
                break
        elif choice == "Settings":
            handle_settings(library_ref)
