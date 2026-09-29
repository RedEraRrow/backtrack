"""Search from the menus: the query screen, the grouped results, and playing an entity from them."""
from __future__ import annotations
import os
from src.utils import prompt
from src.utils import ui_utils
from src.music_library import format_tag_values
from src.history import get_recent_paths
from src import search as _search
from src.playback.playback import music_player
from src.config import load_config
from src.id3.id3_browser import inspect_tag_loop
from src.id3.bulk_id3_manager import bulk_id3_manager
from src.menus.common import _autoplay, _disc_track_cell, _menu_header
from src.menus.play import _PLAY_ACTIONS, _handle_queue_action, _list_actions, _play_list, _queue_action_choices, _sorted_paths


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
