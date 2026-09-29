"""Browse: the category menu, each category's list, and the libraries view."""
from __future__ import annotations
import string
from src.utils.numbering import roman
from src.utils import prompt
from src.utils import ui_utils
from src.music_library import (
    get_grouped_data, get_group_sort_key, format_tag_values, to_num, sort_tracks, sort_albums,
    resolve_levels, library_of,
)
from src.playback.playback import music_player
from src.config import load_config, music_dirs, library_name
from src.state import NAV_STACK
from src.id3.id3_browser import inspect_tag_loop
from src.id3.bulk_id3_manager import bulk_id3_manager
from src.menus.common import BROWSE_CATEGORIES, _ALBUM_COLUMNS, _TRACK_COLUMNS, _album_artist_of, _commit, _idx_of, _menu_header, browse_menu_keys
from src.menus.play import _PLAY_ACTIONS, _edit_paths, _list_actions, _play_list, _queue_shortcut_kwargs, _sorted_paths, play_queue
from src.menus.sorting import _GROUP_SORTS, _pick_chain, _pick_sort, _sort_groups


def browse_menu(library_ref: list, cat: str, scope: str | None = None) -> str | None:
    """Drive the multi-level browse UI (groups → albums → tracks → actions) for
    one category (a BROWSE_CATEGORIES key) — of the whole library, or of one
    music directory (`scope`)."""
    library = library_ref[0]
    cat_choice, _field, _albums_level, _letters = BROWSE_CATEGORIES[cat]

    NAV_STACK.append(cat_choice)
    _group_place = prompt.ListPlace()
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
                place=_group_place,
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
                    _group_place.reset()
                    _group_place.value = _restore_letter
                    continue
                break

            if isinstance(selection, tuple) and selection[0] == "__edited__":
                continue

            if selection == "__toggle__":
                _letter_mode   = not _letter_mode
                _letter_filter = None
                _group_place.reset()
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
                _group_place.reset()
                continue

            NAV_STACK.append(selection)
            selected_songs = grouped[selection]
            _album_place = prompt.ListPlace()

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
                        place=_album_place,
                        **_queue_shortcut_kwargs(library, group_paths=album_paths),
                    )

                    if not alb:
                        break

                    if isinstance(alb, tuple) and alb[0] == "__edited__":
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

                    NAV_STACK.append(alb)
                    track_paths = [s['path'] for s in albums[alb]]
                else:
                    track_paths = [s['path'] for s in selected_songs]

                _track_place = prompt.ListPlace()
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
                        place=_track_place,
                        on_inspect=_inspect_track if _show_editor else None,
                        inspect_key='e',
                        **_queue_shortcut_kwargs(library,
                                                 disc_track_map=disc_track_map,
                                                 work_track_map=work_track_map),
                    )

                    if not path_choice_obj:
                        break

                    if isinstance(path_choice_obj, tuple) and path_choice_obj[0] == "__edited__":
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
