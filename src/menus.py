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
    get_grouped_data, get_group_sort_key, derive_album_credit, format_tag_values, album_year,
    to_num, sort_tracks, sort_albums, resolve_levels, sort_options, valid_levels, library_of,
    SORT_FIELDS, DEFAULT_SORT_LEVELS,
)
from src.history import get_history, clear_history, get_recent_paths
from src import search as _search
from src.playback.playback import music_player
from src.playback.session import REPEAT_OFF, REPEAT_ONE, REPEAT_ALL, active_session, is_client
from src.config import load_config, music_dirs, set_music_dirs, library_name, update_config, changed_keys
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
# Artist and genre lists order their names; everything that holds albums and
# tracks follows the shared sort chain (music_library.sort_tracks), or a
# library's own. Both are chosen with `s` and saved.
_GROUP_SORTS = [("name", "Name (A–Z)"), ("name_desc", "Name (Z–A)"), ("tracks", "Most tracks")]

_BY_TRACK = [['disc', 'asc'], ['track', 'asc'], ['title', 'asc'], ['date', 'asc']]
_CHAIN_PRESETS = [
    ("Album A–Z, then oldest", DEFAULT_SORT_LEVELS),
    ("Oldest first", [['album_year', 'asc'], ['album', 'asc']] + _BY_TRACK),
    ("Newest first", [['album_year', 'desc'], ['album', 'asc']] + _BY_TRACK),
    ("Album artist, then oldest", [['album_artist', 'asc'], ['album_year', 'asc'], ['album', 'asc']] + _BY_TRACK),
    ("Broadcast order", [['date', 'asc'], ['album', 'asc'], ['disc', 'asc'], ['track', 'asc'], ['title', 'asc']]),
]


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
    """Order artist/genre names by the chosen mode; ties fall back to name order."""
    def nk(n: str) -> str:
        """Sort key for group n (also used as the tiebreaker for other modes)."""
        return get_group_sort_key(n, grouped[n], cat_key)
    if mode == "name_desc":
        return sorted(names, key=nk, reverse=True)
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


def _direction_label(field: str, direction: str) -> str:
    """How a level's direction reads for its kind of field."""
    kind = SORT_FIELDS[field][2]
    asc = direction == 'asc'
    if kind == 'text':
        return "A–Z" if asc else "Z–A"
    if field in ('album_year', 'date'):
        return "oldest first" if asc else "newest first"
    return "lowest first" if asc else "highest first"


def _chain_summary(levels: list) -> str:
    """A chain in a few words, for a settings row: its preset's name, or
    "Custom" and how many levels."""
    for label, preset in _CHAIN_PRESETS:
        if valid_levels(preset) == levels:
            return label
    return f"Custom, {ui_utils.plural(len(levels), 'level')}"


def _commit(cfg: dict, *keys: str) -> None:
    """Save these keys of a screen's working config (update_config: into the
    config as it is on disk now), and bring the working copy up to date with
    anything saved meanwhile."""
    fresh = update_config({k: cfg[k] for k in keys})
    cfg.clear()
    cfg.update(fresh)


def _save_chain(cfg: dict, owner: str | None, levels: list | None) -> None:
    """Store a chain as the shared order (owner None) or a library's; None
    clears a library's so it follows the shared order again."""
    if owner is None:
        cfg["sort_levels"] = [list(lv) for lv in (levels or [])]
    else:
        libs = dict(cfg.get("library_sort_levels") or {})
        if levels:
            libs[owner] = [list(lv) for lv in levels]
        else:
            libs.pop(owner, None)
        cfg["library_sort_levels"] = libs
    _commit(cfg, "sort_levels" if owner is None else "library_sort_levels")


def _pick_chain(cfg: dict, owner: str | None, header, *, allow_shared: bool = False) -> bool:
    """The `s` / Settings sort picker for one chain: the presets, plus Custom…
    for the level editor. `allow_shared` adds "Use the shared order" (for a
    library). Returns True when something changed."""
    opts = sort_options(cfg)
    has_own = owner is not None and owner in opts['libraries']
    current = opts['libraries'][owner] if has_own else opts['levels']
    shared = owner is not None and not has_own
    whose = "shared order" if owner is None else f"{library_name(cfg, owner)}'s order"
    choices = []
    if allow_shared:
        choices.append(prompt.Choice(title=f"{'✔ ' if shared else '  '}Use the shared order", value="__shared__"))
    matched = False
    for label, levels in _CHAIN_PRESETS:
        on = not shared and valid_levels(levels) == current
        matched = matched or on
        choices.append(prompt.Choice(title=f"{'✔ ' if on else '  '}{label}", value=label))
    choices.append(prompt.Choice(title=f"{'✔ ' if not (matched or shared) else '  '}Custom…", value="__custom__"))
    start = next((i for i, c in enumerate(choices) if c.title.startswith('✔')), 0)
    sel = prompt.select(f"Sort by ({whose}):", choices=choices, header=header, index=start)
    if not sel:
        return False
    if sel == "__shared__":
        _save_chain(cfg, owner, None)
        return True
    if sel == "__custom__":
        edited = _edit_chain(current, header)
        if edited is None:
            return False
        _save_chain(cfg, owner, edited)
        return True
    _save_chain(cfg, owner, dict(_CHAIN_PRESETS)[sel])
    return True


def _edit_chain(levels: list, header) -> list | None:
    """Edit a sort chain in place: ↵ changes a level's field, space flips its
    direction, d removes it, J/K move it. Returns the new chain on Save
    changes, or None when backed out of."""
    chain = [list(lv) for lv in levels]
    cursor = 0

    def _move(field: str, delta: int) -> bool:
        i = next(k for k, lv in enumerate(chain) if lv[0] == field)
        j = i + delta
        if not 0 <= j < len(chain):
            return False
        chain[i], chain[j] = chain[j], chain[i]
        return True

    def _row(act: str):
        """A row key's callback: (act, level) on a level row, nothing elsewhere."""
        return lambda f: (act, f) if f in SORT_FIELDS else None

    while True:
        choices: list = [prompt.Choice(title=f"{SORT_FIELDS[f][0]}, {_direction_label(f, d)}", value=f)
                         for f, d in chain]
        choices.append(prompt.separator())
        if len(chain) < len(SORT_FIELDS):
            choices.append(prompt.Choice(title="Add a level…", value="__add__"))
        choices.append(prompt.Choice(title="Reset to default", value="__reset__"))
        choices.append(prompt.Choice(title="Save changes", value="__save__"))
        sel = prompt.select("Sort levels (first wins; ties go to the next):",
                            choices=choices, header=header, index=cursor, on_move=_move,
                            row_actions={'SPACE': _row("flip"), 'd': _row("remove"),
                                         'DELETE': _row("remove"), 'BACKSPACE': _row("remove")},
                            row_action_hints={"space": "flip direction", "d": "remove"})
        if sel is None:
            return None
        if isinstance(sel, tuple):
            act, f = sel
            i = next(k for k, lv in enumerate(chain) if lv[0] == f)
            if act == "flip":
                chain[i][1] = 'desc' if chain[i][1] == 'asc' else 'asc'
                cursor = i
            else:
                del chain[i]
                cursor = max(0, min(i, len(chain) - 1))
            continue
        cursor = _idx_of(choices, sel, cursor)
        if sel == "__save__":
            return chain or [list(lv) for lv in DEFAULT_SORT_LEVELS]
        if sel == "__reset__":
            chain = [list(lv) for lv in DEFAULT_SORT_LEVELS]
            continue
        if sel == "__add__":
            f = _pick_field([x for x in SORT_FIELDS if x not in {c[0] for c in chain}], header)
            if f:
                chain.append([f, 'asc'])
                cursor = len(chain) - 1        # onto the new level, not whatever
                                               # row slid into "Add a level…"'s place
            continue
        i = next(k for k, lv in enumerate(chain) if lv[0] == sel)
        nf = _pick_field([x for x in SORT_FIELDS if x == sel or x not in {c[0] for c in chain}], header)
        if nf:
            chain[i][0] = nf


def _pick_field(fields: list, header) -> str | None:
    """Choose a field to sort a level by."""
    kinds = {'album': "per album", 'track': "per track"}
    choices = [prompt.Choice(title=SORT_FIELDS[f][0], value=f,
                             cells=[SORT_FIELDS[f][0], kinds[SORT_FIELDS[f][1]]]) for f in fields]
    return prompt.select("Sort by:", choices=choices, header=header, columns=_SETTINGS_COLUMNS)


# Search scope cycled with Tab in the live search screen (default: all fields).
# 'disc_label' is a computed field (search._disc_label) — a disc's subtitle
# when tagged, else "Disc N" for any multi-disc album — not a scope of its
# own (it rides along under "all fields", the same as "people" does).
_ALL_SEARCH_FIELDS = ['title', 'artist', 'album', 'composer', 'lyricist', 'genre', 'people', 'disc_label']
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


def _people_cell(song: dict, tokens: list):
    """People column: the matched person(s), highlighted — empty when no person
    in this track's cast matched (so the column reads as 'who matched')."""
    if not tokens:
        return ""
    entries = [f"{n} ({r})" if r else n for n, r in song.get('credits') or []]
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
    people = (_people_cell(s, tokens)
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

    _last: dict = {'results': [], 'entities': {}, 'counts': {}, 'query': ''}

    def _provider(query: str) -> list:
        """Search, group into sections, and build the rows for the whole screen."""
        _last['query'] = query      # kept for re-entry after "show all" or an entity
        results = _search.search(library, query, _fields(), recent=recent)
        tokens = _search.tokenize(query)
        ents = _search.collect_entities(results, tokens)
        disc_ents = _search.collect_disc_entities(results, tokens)
        _last['results'] = [r.song for r in results]
        _last['entities'] = ents
        _last['counts'] = {k: len(v) for k, v in ents.items()}
        _last['counts']['disc'] = len(disc_ents)
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
        best = max((e for v in list(ents.values()) + [disc_ents] for e in v),
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

        # Discs sit under albums: a subdivision of one, not a peer of artist/
        # genre/etc. — only surfaced for albums that actually have more than one.
        _section('disc', 'DISCS', 'disc', [e for e in disc_ents if e is not best],
                 lambda e: prompt.Choice(title=e.name, value=("__entity__", e),
                                         cells=_entity_cells(e, tokens)))

        # Tracks last: with the entities above them, the track list is for when
        # you want a specific recording rather than a body of work.
        tracks_shown = (len(query) >= _ENTITY_ONLY_UNTIL
                        or not (any(ents.values()) or disc_ents))
        if tracks_shown:
            _section('track', 'TRACKS', 'track', results,
                     lambda r: prompt.Choice(title=r.song.get('title', ''),
                                             value=r.song['path'],
                                             cells=_search_result_cells(r, tokens)))
        return choices

    def _hdr() -> list:
        mode = _SCOPE_CYCLE[scope['i']]
        label = "all fields" if mode == 'all' else mode
        singular = {kind: sing for kind, _lbl, sing in _SECTIONS}
        bits = [ui_utils.plural(v, singular.get(k, k))
                for k, v in _last['counts'].items() if v]
        sub = " · ".join(bits) if bits else f"{ui_utils.plural(len(library), 'track')} indexed"
        return _menu_header("Search", f"{sub}    scope: {label}")()

    def _edit_highlighted(value) -> None:
        """^E: edit the highlighted track directly, without leaving the
        search results — only a plain track row is editable, not an entity
        or a structural row like "show all"."""
        if not isinstance(value, str) or value.startswith("__"):
            return
        song = next((s for s in library if s['path'] == value), None)
        ui_utils.clear_screen()
        inspect_tag_loop(value, library_metadata=song, library=library)
        ui_utils.clear_screen()

    def _edit_all(_value) -> None:
        """^a: bulk-edit every track the search currently finds, without
        leaving the results (like ^e does for one)."""
        paths = [s['path'] for s in _last['results']]
        if paths:
            bulk_id3_manager(library, paths=paths)
            ui_utils.clear_screen()

    while True:
        selected = prompt.live_select(
            "", _provider, columns=_SEARCH_COLUMNS,
            header=_hdr, on_cycle=_cycle, cycle_key='\x06',   # ^F cycles scope
            section_nav=True,
            row_actions={'\x05': _edit_highlighted, '\x01': _edit_all} if _show_editor else None,
            extra_hints={'^f': 'scope', **({'^e': 'edit', '^a': 'edit all'} if _show_editor else {})},
            count_of=lambda: len(_last['results']),
            initial_query=_last['query'])
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

        # A track: play it (or queue it), then back to these results — the
        # query is kept (initial_query) — as entities and every browse level do.
        # Editing is ^e / ^a from the results: `live_select` types every other
        # key into the query.
        song_meta = next((s for s in library if s['path'] == selected), None)
        track_title = song_meta['title'] if song_meta else os.path.basename(selected)
        _action_choices = ["Play"] + _queue_action_choices()
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
        elif action:
            _handle_queue_action(action, selected, track_title, library)


def _play_entity(ent, library: list) -> str | None:
    """Open a chosen artist/album/disc/genre: list its tracks and act on one."""
    tracks = ent.tracks
    _show_editor = load_config().get("show_metadata_editor", True)
    choices = []
    for s in tracks:
        choices.append(prompt.Choice(
            title=s.get('title', ''), value=s['path'],
            cells=[s.get('title', ''), format_tag_values(s.get('artist', '')),
                   s.get('album', ''), "", _disc_track_cell(s),
                   ui_utils.format_time(int(s.get('duration') or 0)) if s.get('duration') else ""]))

    def _inspect_track(path: str) -> None:
        """`e`: edit the highlighted track only."""
        song = next((s for s in tracks if s['path'] == path), None)
        ui_utils.clear_screen()
        inspect_tag_loop(path, library_metadata=song, library=library)
        ui_utils.clear_screen()

    sub = ui_utils.plural(len(tracks), 'track')
    pick = prompt.select(f"{ent.kind.title()}:", choices=choices,
                         columns=_SEARCH_COLUMNS,
                         header=_menu_header(ent.name, sub),
                         on_inspect=_inspect_track if _show_editor else None, inspect_key='e',
                         extra_hints={'e': 'edit'} if _show_editor else None,
                         actions=(_list_actions(_show_editor, albums=len({(s.get('album'), s.get('album_artist'))
                                                                          for s in tracks}) > 1)
                                  if len(tracks) > 1 else None))
    if not pick:
        return None
    if pick in _PLAY_ACTIONS:
        return _play_list(pick, _sorted_paths([tracks], load_config()), library)
    if pick == "__bulk_edit__":
        bulk_id3_manager(library, paths=[s['path'] for s in tracks])
        return None
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
    cursor = 0
    while True:
        # Rebuilt each time round: playing a track adds to the history.
        res = _history_screen(library, cursor)
        if not isinstance(res, tuple):
            return res                      # backed out, or QUIT_ALL
        cursor = res[1]


def _history_screen(library: list, cursor: int):
    """One pass of the history list: None (back), "QUIT_ALL", or
    ("again", cursor) to show it again after playing or editing."""
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

    _show_editor = load_config().get("show_metadata_editor", True)

    def _inspect_history(path: str) -> None:
        """`e`: open the metadata editor (lyrics sync and trim live inside it
        too) for the highlighted entry without leaving this list."""
        song = next((s for s in library if s['path'] == path), None)
        ui_utils.clear_screen()
        inspect_tag_loop(path, library_metadata=song, library=library)
        ui_utils.clear_screen()

    selected = prompt.select(
        "",
        choices=choices,
        columns=_HISTORY_COLUMNS,
        header=_menu_header("Listening History", f"{len(history_entries)} recent"),
        on_inspect=_inspect_history if _show_editor else None,
        inspect_key='e',
        index=min(cursor, len(choices) - 1),
        shortcuts={'E': '__bulk_edit__'} if _show_editor else None,
        extra_hints={'e': 'edit', 'E': 'edit all'} if _show_editor else None,
        **_queue_shortcut_kwargs(library),
    )
    if not selected:
        return None

    if selected == "__bulk_edit__":
        # Once each: a track played several times is in the history several times.
        bulk_id3_manager(library, paths=list(dict.fromkeys(p for _, _, p in history_entries)))
        return ("again", cursor)

    picked = _idx_of(choices, selected, cursor)
    ui_utils.clear_screen()
    res = music_player(selected)
    ui_utils.clear_screen()
    if res and res.get("status") == "QUIT_ALL":
        return "QUIT_ALL"
    return ("again", picked)


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
    _commit(config, 'tag_name_preferences')
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
            _choices.append(prompt.Choice(title=f"{library_name(config, d)}  {d}{missing}", value=d,
                                          cells=[library_name(config, d), f"{d}{missing}"]))
        if dirs:
            _choices.append(prompt.separator())
            _choices.append(prompt.Choice(title="Re-scan now", value="__rescan__"))

        _sub = f"{len(dirs)} director{'y' if len(dirs) == 1 else 'ies'}"
        choice = prompt.select("", choices=_choices,
                               header=_menu_header("Music Directories", _sub),
                               columns=_SETTINGS_COLUMNS, index=_cursor)
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
            _commit(config, "music_directories", "music_directory")
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Added — {n} tracks.")
            continue

        if choice == "__rescan__":
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Done — {n} tracks.")
            continue

        # An existing directory row: rename it, give it its own sort order, or
        # remove it (its tracks leave the library).
        if choice in dirs:
            name = library_name(config, choice)
            own = choice in sort_options(config)['libraries']
            act = prompt.select("", choices=[
                prompt.Choice(title="Name…", value="name", cells=["Name…", name]),
                prompt.Choice(title="Sort order…", value="sort",
                              cells=["Sort order…", _chain_summary(sort_options(config)['libraries'][choice])
                                     if own else "shared"]),
                prompt.Choice(title="Remove…", value="remove", cells=["Remove…", ""]),
            ], columns=_SETTINGS_COLUMNS, header=_menu_header(name, choice))
            if act == "name":
                new = prompt.text("Name shown in Browse:", default=name)
                if new is not None:
                    names = dict(config.get("library_names") or {})
                    if new.strip() and new.strip() != os.path.basename(choice.rstrip(os.sep)):
                        names[choice] = new.strip()
                    else:
                        names.pop(choice, None)          # blank → back to the folder name
                    config["library_names"] = names
                    _commit(config, "library_names")
                continue
            if act == "sort":
                _pick_chain(config, choice, _menu_header(name, choice), allow_shared=True)
                continue
            if act != "remove":
                continue
            if len(dirs) == 1 and not prompt.confirm(
                    "That's the only directory — remove it and empty the library?"):
                continue
            if len(dirs) > 1 and not prompt.confirm(f"Remove {os.path.basename(choice) or choice}?"):
                continue
            set_music_dirs(config, [d for d in dirs if d != choice])
            # Its name and own sort order go with it, not left behind in config.
            config["library_names"] = {k: v for k, v in (config.get("library_names") or {}).items()
                                       if k != choice}
            config["library_sort_levels"] = {k: v for k, v in
                                             (config.get("library_sort_levels") or {}).items()
                                             if k != choice}
            _commit(config, "music_directories", "music_directory",
                    "library_names", "library_sort_levels")
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


# Settings rows that are on/off switches: space flips them, like ↵ does.
_SETTINGS_TOGGLES = {"history", "autoplay", "meta_editor", "lyrics_editor",
                     "plain_text", "sort_tags", "hidden", "key_hints", "inline_art", "debug"}


def _space_toggles(values) -> dict:
    """select() kwargs making space flip the rows in `values` (the on/off
    ones): select() returns ("__space__", row), and space does nothing on any
    other row."""
    return {"on_inspect": lambda v: ("__space__", v) if v in values else None,
            "inspect_key": "SPACE"}


def handle_settings(library_ref: list) -> None:
    """Run the interactive settings menu loop, applying and persisting each toggled option."""
    import copy
    _cursor = 0

    while True:
        # A fresh copy each time round, and only what this action changed is
        # saved (update_config) — never the whole dict back over newer changes.
        config = load_config()
        _before = copy.deepcopy(config)

        def _bool(key: str, default: bool) -> str:
            """Right-column glyph for an on/off setting."""
            return _state_glyph(config.get(key, default))

        _tasks = len(ui_utils.BACKGROUND_TASKS)
        _prefs = len(config.get("tag_name_preferences") or {})
        _hist = len(get_history(limit=10 ** 9))

        # (value, label, current state) — separators are plain strings.
        _rows: list = [
            prompt.separator("Playback"),
            ("lead_in",      "Lyric lead-in…",       f"{float(config['lyric_lead_in']):g}s"),
            ("autoplay",     "Auto-play on select",  _bool("autoplay_on_select", False)),
            ("key_hints",    "Key hints",            _state_glyph(prompt.hints_visible())),
            ("inline_art",   "Image album art (iTerm2)", _bool("art_inline_images", False)),
            prompt.separator("Library"),
            ("music_dirs",   "Music directories…",   ui_utils.plural(len(music_dirs(config)), "folder")),
            ("activity",     "Activity centre…",     f"{_tasks} running" if _tasks else "idle"),
            ("hidden",       "Hidden file filter",   _bool("ignore_hidden_files", False)),
            ("browse_menu",  "Browse menu…",         f"{len(browse_menu_keys(config))} of {len(BROWSE_CATEGORIES)} shown"),
            prompt.separator("Sorting"),
            ("sort_order",   "Sort order…",          _chain_summary(sort_options(config)['levels'])),
            ("sort_tags",    "Use sort-order tags",  _bool("sort_use_tags", True)),
            ("sort_words",   "Ignored leading words…",
             ", ".join(config.get("sort_ignore_words") or []) or "none"),
            prompt.separator("Editors"),
            ("meta_editor",  "Metadata editor",      _bool("show_metadata_editor", True)),
            ("lyrics_editor", "Lyrics editor",       _bool("show_lyrics_editor", True)),
            ("plain_text",   "Plain-text editing",   _bool("plain_text_editing", False)),
            ("tag_names",    "Tag name preferences…", ui_utils.plural(_prefs, "override") if _prefs else "none"),
            ("delimiter",    "Sort list delimiter…", config.get("sort_list_delimiter", "/")),
            prompt.separator("Diagnostics"),
            ("debug",        "Diagnostics log",      _bool("debug", False)),
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
            extra_hints={"space": "toggle"},
            **_space_toggles(_SETTINGS_TOGGLES),
        )

        if not choice:
            break
        if isinstance(choice, tuple):              # space on an on/off row
            choice = choice[1]

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

        elif choice == "debug":
            from src.utils.log import configure, log_path
            _toggled("debug", False, f" — writing to {log_path()}" if not config.get("debug") else "")
            configure(bool(config.get("debug")))

        elif choice == "inline_art":
            _toggled("art_inline_images", False)
            if (config.get("art_inline_images") and os.environ.get("TERM_PROGRAM") != "iTerm.app"
                    and os.environ.get("LC_TERMINAL") != "iTerm2"):
                ui_utils.show_status("On, but this isn't iTerm2, so the player keeps the text art.")

        elif choice == "key_hints":
            prompt.toggle_hints()          # the same switch as `i` / the corner

        elif choice == "autoplay":
            _toggled("autoplay_on_select", False)

        elif choice == "lead_in":
            val = prompt.text("Lead-in seconds:", default=str(config["lyric_lead_in"]))
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

        elif choice == "browse_menu":
            _edit_browse_menu(config)

        elif choice == "sort_order":
            _pick_chain(config, None, _menu_header("Sort order"))

        elif choice == "sort_tags":
            _toggled("sort_use_tags", True)

        elif choice == "sort_words":
            cur = ", ".join(config.get("sort_ignore_words") or [])
            new = prompt.text("Words ignored at the start of names (comma-separated):", default=cur)
            if new is not None:
                config["sort_ignore_words"] = [w.strip() for w in new.split(",") if w.strip()]

        elif choice == "music_dirs":
            _music_dirs_menu(config, library_ref)

        elif choice == "hidden":
            # Both states need a re-scan before the library reflects the change.
            _toggled("ignore_hidden_files", False, " — re-scan to apply.")

        if changed_keys(_before, config):
            update_config(changed_keys(_before, config))


def play_queue(paths: list, mode: str = "linear", library: list | None = None) -> str | None:
    """Play a queue of file paths in the given mode (linear, shuffle, repeat_one, repeat_all)."""
    playlist = list(paths)
    if not playlist:                       # e.g. a letter filter that leaves nothing
        ui_utils.show_status("Nothing to play.")
        return None
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


_PLAY_ACTIONS = ("__play_all__", "__shuffle__", "__album_shuffle__")


def _list_actions(show_editor: bool, albums: bool = True) -> list:
    """Play all / shuffle / album shuffle / edit all for a browse list (select's
    actions=). `albums` is whether the list holds more than one album — album
    shuffle means nothing within one. `E` edits everything listed; `e` is each
    list's edit-the-highlighted-row."""
    acts = [("p", "play all", "__play_all__"), ("x", "shuffle", "__shuffle__")]
    if albums:
        acts.append(("X", "album shuffle", "__album_shuffle__"))
    if show_editor:
        acts.append(("E", "edit all", "__bulk_edit__"))
    return acts


def _sorted_paths(groups: list, cfg: dict) -> list:
    """Every track of `groups` (lists of tracks, in the order shown), each
    group in the sort chain's order — what Play all plays. dict.fromkeys: a
    track under two groups (multi-value genre/artist) plays once."""
    return list(dict.fromkeys(t['path'] for g in groups for t in sort_tracks(g, cfg)))


def _edit_paths(library: list, paths: list, value) -> tuple:
    """`e` on a row that stands for several tracks: bulk-edit them, then hand
    select() a result so the caller rebuilds the list (names may have changed)
    and puts the cursor back on `value`."""
    bulk_id3_manager(library, paths=list(dict.fromkeys(paths)))
    return ("__edited__", value)


def _play_list(action: str, paths: list, library: list) -> str | None:
    """Play a list in order (Play all), shuffled (Shuffle), or with its albums
    in random order but each album's tracks kept in order (Album shuffle)."""
    if action == "__album_shuffle__":
        by_path = {t['path']: t for t in library}
        runs: list = []
        for p in paths:
            t = by_path.get(p, {})
            k = ((t.get('album') or '').strip(), (t.get('album_artist') or '').strip())
            if not runs or runs[-1][0] != k:
                runs.append((k, []))
            runs[-1][1].append(p)
        random.shuffle(runs)
        paths = [p for _k, ps in runs for p in ps]
    return play_queue(paths, mode="shuffle" if action == "__shuffle__" else "linear",
                      library=library)


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
        if not isinstance(value, str):
            return None, None
        # Disc/work header rows first: they're "__"-prefixed too, and the guard
        # below used to turn them away before these checks ever ran.
        if disc_track_map and value.startswith("__disc_"):
            disc_val = value[len("__disc_"):]
            return disc_track_map.get(disc_val), f"Disc {disc_val}"

        if work_track_map and value.startswith("__work__"):
            work_name = value[len("__work__"):]
            return work_track_map.get(work_name), work_name

        if value.startswith("__"):
            return None, None

        if group_paths and value in group_paths:
            return group_paths[value], value

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


# Browse categories: key → (label, field grouped by, drills into an album list
# first, offers the A–Z letter index). Which appear, and in what order, is the
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


def browse_menu(library_ref: list, cat: str, scope: str | None = None) -> str | None:
    """Drive the multi-level browse UI (groups → albums → tracks → actions) for
    one category (a BROWSE_CATEGORIES key) — of the whole library, or of one
    music directory (`scope`)."""
    library = library_ref[0]
    cat_choice, _field, _albums_level, _letters = BROWSE_CATEGORIES[cat]

    NAV_STACK.append(cat_choice)
    _group_cursor  = None            # None → land on the first real row
    _letter_mode   = None            # None → decide dynamically on first render
    _letter_filter = None            # active letter, or None = show everything

    try:
        while True:  # LEVEL 2: Group Selection
            _cfg = load_config()
            _dirs = music_dirs(_cfg)
            # Edits still go to `library` (the whole thing, saved as the cache);
            # only what is listed is narrowed to the scope.
            _source = ([t for t in library if library_of(t['path'], _dirs) == scope]
                       if scope else library)
            grouped  = get_grouped_data(_source, _field)
            if not grouped:
                ui_utils.show_status(f"Nothing here is tagged for {cat_choice.lower()}.")
                break
            _cat_key = _field
            _show_editor = _cfg.get("show_metadata_editor", True)
            _scope_name = library_name(_cfg, scope) if scope else None
            _group_sort = (_cfg.get("group_sorts") or {}).get(cat, "name")

            def _sub(extra: str | None = None) -> str | None:
                """Header subtitle: the library being browsed, then any detail."""
                return ", ".join(x for x in (_scope_name, extra) if x) or None

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
            _can_letter = _letters and len(letters_found) > 1
            _vis = max(4, ui_utils.get_terminal_height() - 9)
            if _letter_mode is None:
                _letter_mode = _can_letter and len(group_names) > _vis
            if not _can_letter:
                _letter_mode = False

            # Play / shuffle / edit act on everything listed (scope is in the
            # header above); sort and the `/` view toggle sit beside them.
            _sc: dict = {}
            _eh: dict = {"e": "edit"} if _show_editor else {}
            if _can_letter:
                _sc["/"] = "__toggle__"
                _eh["/"] = "full list" if _letter_mode else "by letter"

            group_paths = None
            if _letter_mode:
                # LETTER VIEW: compact A–Z index. Play all / Edit act on everything.
                names = (sort_albums(group_names, grouped, _cfg) if _field == 'album'
                         else _sort_groups(group_names, grouped, _cat_key, _group_sort))
                _choices = list(letters_found)
                _group_cols = None
                _header = _menu_header(cat_choice, _sub("by letter"))
            else:
                # FULL LIST VIEW: every item, optionally filtered to one letter.
                names = ([n for n in group_names if _letter(n) == _letter_filter]
                         if _letter_filter else list(group_names))
                if _field == 'album':
                    names = sort_albums(names, grouped, _cfg)
                else:
                    names = _sort_groups(names, grouped, _cat_key, _group_sort)
                _sc["s"] = "__sort__"
                _eh["s"] = "sort"
                _choices = []
                group_paths = {name: [t['path'] for t in sort_tracks(grouped[name], _cfg)]
                               for name in names}
                # In album browse, show the album artist beside each album (dimmed),
                # matching the artist/genre → album sublist.
                if _field == 'album':
                    for _a in names:
                        _choices.append(prompt.Choice(
                            title=_a, value=_a,
                            cells=[_a, format_tag_values(_album_artist_of(grouped[_a]))]))
                    _group_cols = _ALBUM_COLUMNS
                else:
                    _choices += names
                    _group_cols = None
                _header = _menu_header(cat_choice,
                                       _sub(f"letter: {_letter_filter}" if _letter_filter else None))

            if _group_cursor is None:
                _group_cursor = 0

            def _edit_group(value) -> tuple:
                """`e`: bulk-edit the highlighted artist/album/genre, or every
                group under the highlighted letter."""
                groups = ([n for n in group_names if _letter(n) == value]
                          if _letter_mode else [value])
                return _edit_paths(library, [s['path'] for n in groups for s in grouped[n]], value)

            selection = prompt.select(
                "",
                choices=_choices,
                header=_header,
                columns=_group_cols,
                shortcuts=_sc,
                extra_hints=_eh,
                actions=_list_actions(_show_editor),
                on_inspect=_edit_group if _show_editor else None,
                inspect_key='e',
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
                    _group_cursor   = (letters_found.index(_restore_letter)
                                       if _restore_letter in letters_found else None)
                    continue
                break

            if isinstance(selection, tuple) and selection[0] == "__edited__":
                _group_cursor = _idx_of(_choices, selection[1])
                continue

            if selection == "__toggle__":
                _letter_mode   = not _letter_mode
                _letter_filter = None
                _group_cursor  = None
                continue

            if selection == "__sort__":
                if _field == 'album':
                    _tracks = [t for n in names for t in grouped[n]]
                    _pick_chain(_cfg, resolve_levels(_tracks, _cfg)[1], _header)
                else:
                    _new = _pick_sort(_group_sort, _GROUP_SORTS, _header)
                    _cfg["group_sorts"] = {**(_cfg.get("group_sorts") or {}), cat: _new}
                    _commit(_cfg, "group_sorts")
                continue

            if selection in _PLAY_ACTIONS:
                res = _play_list(selection, _sorted_paths([grouped[n] for n in names], _cfg), library)
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

            while True:  # LEVEL 3: Album Selection
                if _albums_level:
                    albums = get_grouped_data(selected_songs, 'album')

                    album_list = sort_albums(list(albums.keys()), albums, _cfg)

                    # Always present the album list — even a single album is worth
                    # showing as its own entry (albums are distinct from the artist).
                    # Play / shuffle / edit hints act on every album; one album's
                    # row already does all of that one level down.
                    _show_editor = _cfg.get("show_metadata_editor", True)
                    _single_album = len(album_list) <= 1
                    _alb_choices: list = []
                    _asc: dict = {}
                    _aeh: dict = {"e": "edit"} if _show_editor else {}
                    if not _single_album:
                        _asc["s"] = "__sort__"; _aeh["s"] = "sort"
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

                    if _album_cursor is None:
                        _album_cursor = 0

                    album_paths = {name: [t['path'] for t in sort_tracks(albums[name], _cfg)]
                                   for name in album_list}
                    alb = prompt.select(
                        "Albums:",
                        choices=_alb_choices,
                        header=_menu_header(selection, cat_choice),
                        columns=_ALBUM_COLUMNS,
                        shortcuts=_asc,
                        extra_hints=_aeh,
                        actions=None if _single_album else _list_actions(_show_editor),
                        on_inspect=((lambda a: _edit_paths(library, album_paths[a], a))
                                    if _show_editor else None),
                        inspect_key='e',
                        index=_album_cursor,
                        **_queue_shortcut_kwargs(library, group_paths=album_paths),
                    )

                    if not alb:
                        break

                    if isinstance(alb, tuple) and alb[0] == "__edited__":
                        _album_cursor = _idx_of(_alb_choices, alb[1])
                        continue

                    if alb == "__sort__":
                        _pick_chain(_cfg, resolve_levels(selected_songs, _cfg)[1],
                                    _menu_header(selection, cat_choice))
                        continue

                    if alb in _PLAY_ACTIONS:
                        res = _play_list(alb, _sorted_paths([selected_songs], _cfg), library)
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
                    final_tracks = sort_tracks([t for t in library if t['path'] in path_set], _cfg)

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

                        # to_num: a hand-typed movement like "II" or "2a" can't crash the list.
                        _mv = int(to_num(t.get('movement_number')))
                        mv_num  = roman(_mv) if _mv > 0 else ""
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
                    # Play / shuffle / edit-all hints; `e` edits the highlighted
                    # row. With a single track they're noise — the lone
                    # track row already does both jobs.
                    _teh: dict = {"e": "edit"} if _show_editor else {}
                    _tsc: dict = {}
                    _track_actions = None
                    if len(final_tracks) > 1:
                        _track_actions = _list_actions(
                            _show_editor, albums=len({(t.get('album'), t.get('album_artist'))
                                                      for t in final_tracks}) > 1)
                        _tsc["s"] = "__sort__"; _teh["s"] = "sort"

                    # Album artist shown in the header subtitle (#33).
                    _album_artist = format_tag_values(_album_artist_of(final_tracks))
                    _subtitle = _album_artist or (selection if _track_context != selection else cat_choice)

                    _all_track_choices = track_choices
                    if _track_cursor is None:
                        _track_cursor = 0

                    def _inspect_track(path: str) -> tuple:
                        """`e`: open the metadata editor (lyrics sync and trim
                        live inside it too) for the highlighted track, or
                        bulk-edit the whole disc / work on its header row."""
                        if path.startswith("__disc_"):
                            return _edit_paths(library, disc_track_map.get(path[len("__disc_"):], []), path)
                        if path.startswith("__work__"):
                            return _edit_paths(library, work_track_map.get(path[len("__work__"):], []), path)
                        song = next((t for t in final_tracks if t['path'] == path), None)
                        ui_utils.clear_screen()
                        inspect_tag_loop(path, library_metadata=song, library=library)
                        ui_utils.clear_screen()
                        return ("__edited__", path)

                    path_choice_obj = prompt.select(
                        "Tracks:",
                        choices=_all_track_choices,
                        header=_menu_header(_track_context, _subtitle),
                        columns=_TRACK_COLUMNS,
                        shortcuts=_tsc,
                        extra_hints=_teh,
                        actions=_track_actions,
                        index=_track_cursor,
                        on_inspect=_inspect_track if _show_editor else None,
                        inspect_key='e',
                        **_queue_shortcut_kwargs(library,
                                                 disc_track_map=disc_track_map,
                                                 work_track_map=work_track_map),
                    )

                    if not path_choice_obj:
                        break

                    if isinstance(path_choice_obj, tuple) and path_choice_obj[0] == "__edited__":
                        _track_cursor = _idx_of(_all_track_choices, path_choice_obj[1])
                        continue

                    if path_choice_obj == "__sort__":
                        _pick_chain(_cfg, resolve_levels(final_tracks, _cfg)[1],
                                    _menu_header(_track_context, _subtitle))
                        continue

                    if path_choice_obj in _PLAY_ACTIONS:
                        res = _play_list(path_choice_obj, [t['path'] for t in final_tracks], library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
                        continue

                    if path_choice_obj == "__bulk_edit__":
                        bulk_id3_manager(library, paths=[t['path'] for t in final_tracks])
                        continue

                    _track_cursor = _idx_of(_all_track_choices, path_choice_obj)

                    # Disc header selected — play that disc directly.
                    # Bulk-editing the disc's tags is `e` on this row, same as
                    # an ordinary track, so there's no separate disc page.
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__disc_"):
                        disc_val   = path_choice_obj[len("__disc_"):]
                        disc_paths = disc_track_map.get(disc_val, [])
                        res = play_queue(disc_paths, library=library)
                        if res == "QUIT_ALL":
                            return "QUIT_ALL"
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

                    # A track plays directly — metadata editing is `e`/`E` on
                    # the "Tracks:" list above, not a separate action page.
                    ui_utils.clear_screen()
                    res = music_player(path_choice_obj)
                    ui_utils.clear_screen()
                    if res and res.get("status") == "QUIT_ALL":
                        return "QUIT_ALL"

                if _albums_level:
                    NAV_STACK.pop()
                else:
                    break

            NAV_STACK.pop()

    finally:
        if NAV_STACK and NAV_STACK[-1] == cat_choice:
            NAV_STACK.pop()


def handle_browse(library_ref: list, scope: str | None = None) -> str | None:
    """Show the "Browse by" category menu and route into browse_menu for the
    choice. With more than one music directory, Libraries lists them (by their
    friendly names), each opening this same menu for just that directory."""
    _cursor = 0
    while True:
        cfg = load_config()
        # Libraries only means something with more than one directory, and
        # not inside a library already.
        keys = [k for k in browse_menu_keys(cfg)
                if k != 'libraries' or (scope is None and len(music_dirs(cfg)) > 1)]
        _opts = [prompt.Choice(title=BROWSE_CATEGORIES[k][0], value=k) for k in keys]
        if not _opts:
            ui_utils.show_status("Nothing to browse: turn categories on in Settings → Browse menu.")
            return None
        choice = prompt.select(
            "",
            choices=_opts,
            header=_menu_header(library_name(cfg, scope) if scope else "Browse"),
            index=_cursor,
        )
        if not choice:
            break
        _cursor = _idx_of(_opts, choice)
        if choice == "libraries":
            res = _browse_libraries(library_ref)
        else:
            res = browse_menu(library_ref, choice, scope=scope)
        if res == "QUIT_ALL":
            return "QUIT_ALL"
    return None


def _edit_browse_menu(config: dict) -> None:
    """Settings → Browse menu: ↵ or space shows or hides a category, J/K move a
    shown one. Shown ones are listed first, in menu order; changes save as
    they're made."""
    cursor = 0
    follow = None          # the row to keep the cursor on once the list reorders

    def _move(key: str, delta: int) -> bool:
        shown = browse_menu_keys(config)
        if key not in shown:
            return False
        i = shown.index(key)
        j = i + delta
        if not 0 <= j < len(shown):
            return False
        shown[i], shown[j] = shown[j], shown[i]
        config["browse_menu"] = shown
        _commit(config, "browse_menu")
        return True

    while True:
        shown = browse_menu_keys(config)
        hidden = [k for k in BROWSE_CATEGORIES if k not in shown]
        choices: list = [prompt.Choice(title=BROWSE_CATEGORIES[k][0], value=k,
                                       cells=[BROWSE_CATEGORIES[k][0], ON_GLYPH]) for k in shown]
        if hidden:
            choices.append(prompt.separator("Hidden"))
            choices += [prompt.Choice(title=BROWSE_CATEGORIES[k][0], value=k,
                                      cells=[BROWSE_CATEGORIES[k][0], OFF_GLYPH]) for k in hidden]
        choices += [prompt.separator(), prompt.Choice(title="Reset to default", value="__reset__")]
        if follow:
            cursor, follow = _idx_of(choices, follow, cursor), None
        sel = prompt.select("", choices=choices, columns=_SETTINGS_COLUMNS,
                            header=_menu_header("Browse menu", f"{len(shown)} of {len(BROWSE_CATEGORIES)} shown"),
                            index=cursor, extra_hints={"space": "show/hide"}, on_move=_move,
                            **_space_toggles(set(BROWSE_CATEGORIES)))
        if not sel:
            return
        if sel == "__reset__":
            config["browse_menu"] = list(DEFAULT_BROWSE_MENU)
            _commit(config, "browse_menu")
            continue
        key = sel[1] if isinstance(sel, tuple) else sel
        shown = browse_menu_keys(config)             # J/K may have reordered it
        if key not in shown:
            shown.append(key)                        # shown again, at the end
        elif len(shown) > 1:
            shown.remove(key)
        else:
            ui_utils.show_status("Browse needs at least one category.")
        config["browse_menu"] = shown
        _commit(config, "browse_menu")
        follow = key


def _browse_libraries(library_ref: list) -> str | None:
    """Browse → Libraries: pick a music directory, then browse just it."""
    _cursor = 0
    while True:
        cfg = load_config()
        dirs = music_dirs(cfg)
        choices = [prompt.Choice(title=library_name(cfg, d), value=d) for d in dirs]
        choice = prompt.select("", choices=choices, header=_menu_header("Libraries"), index=_cursor)
        if not choice:
            return None
        _cursor = _idx_of(choices, choice)
        _name = library_name(cfg, choice)
        NAV_STACK.append(_name)
        try:
            res = handle_browse(library_ref, scope=choice)
        finally:
            if NAV_STACK and NAV_STACK[-1] == _name:
                NAV_STACK.pop()
        if res == "QUIT_ALL":
            return "QUIT_ALL"


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
        hint_cells.clear()
        out = ["\033[H\033[3J\033[J" + C.HIDE,
               "\n" + prompt.add_help_corner(f"  {C.BOLD}Activity{C.RESET}", 2, hint_cells, True)]
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
                    # Transport keys and clicks (on the hints or the miniplayer)
                    # act here, like every other screen; a clicked `esc/b`
                    # comes back as its key.
                    _ch = prompt.consume_chrome(key, hint_cells)
                    if _ch in (prompt.CHROME_HANDLED, prompt.CHROME_REDRAW):
                        last = None                     # repaint
                        continue
                    if _ch is not None:
                        key = _ch
                    elif key.startswith('MOUSE_CLICK:'):
                        key = ''
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
