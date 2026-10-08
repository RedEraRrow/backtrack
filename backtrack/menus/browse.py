"""Browse: the category menu, each category's list, and the libraries view."""
from __future__ import annotations
import string
from backbone.numbering import roman
from backbone import prompt
from backbone import ui
from backbone.ui import Colors as C
from backtrack.music_library import (
    get_grouped_data, get_group_sort_key, format_tag_values, to_num, sort_tracks, sort_albums,
    resolve_levels, library_of, album_credit, chapter_label, is_audiobook,
)
from backtrack import books
from backtrack.config import load_config, music_dirs, library_name
from backbone import nav
from backbone.nav import NAV_STACK
from backtrack.id3.browser import inspect_tag_loop
from backtrack.id3.bulk_menu import bulk_id3_manager
from backtrack.menus.chapters import chapter_editor
from backtrack.menus import browse_columns as cols
from backtrack.menus.common import BROWSE_CATEGORIES, _ALBUM_COLUMNS, _TRACK_COLUMNS, _commit, _idx_of, _menu_header, browse_menu_keys
from backtrack.menus.play import (
    CHAPTER_SEP, _edit_paths, _list_actions, _list_result, _queue_shortcut_kwargs, _sorted_paths,
    play_chapter, play_picked, play_queue,
)
from backtrack.menus.sorting import _GROUP_SORTS, _pick_chain, _pick_sort, _sort_groups
from backtrack.config import setting


def browse_menu(library_ref: list, cat: str, scope: str | None = None,
                trail: list | None = None, open_group: str | None = None) -> prompt.JumpTo | None:
    """Drive the multi-level browse UI (groups → albums → tracks → actions) for
    one category (a BROWSE_CATEGORIES key), of the whole library, or of one
    music directory (`scope`). As a column browser, `trail` is the levels
    above (the categories); a click on one of their rows comes back as the
    JumpTo returned, for the caller to go back to. `open_group`: start inside
    that group (an album, an artist), back from it to the whole list."""
    library = library_ref[0]
    cat_choice, _field, _albums_level, _letters = BROWSE_CATEGORIES[cat]

    NAV_STACK.append(cat_choice)
    _group_place = prompt.ListPlace()
    _letter_mode   = None            # None → decide dynamically on first render
    _letter_filter = None            # active letter, or None = show everything
    _jump = None                     # a click on a column of a level above, on its way back up
    trail = list(trail or [])

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
                ui.show_status(f"Nothing here is tagged for {cat_choice.lower()}.")
                break
            _cat_key = _field
            _show_editor = setting(_cfg, "show_metadata_editor")
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
            _vis = max(4, ui.get_terminal_height() - 9)
            if open_group is not None and open_group not in grouped:
                open_group = None
            if _letter_mode is None:
                _letter_mode = (open_group is None and _can_letter and len(group_names) > _vis
                                and bool(setting(_cfg, "browse_by_letter")))
            if not _can_letter:
                _letter_mode = False

            # Play / shuffle / edit act on everything listed (scope is in the
            # header above); sort and the `/` view toggle sit beside them.
            _sc: dict = {}
            _eh: dict = {"library.edit": "edit"} if _show_editor else {}
            if _can_letter:
                _sc["library.letters"] = "__toggle__"
                _eh["library.letters"] = "full list" if _letter_mode else "by letter"

            group_paths = None
            if _letter_mode:
                # LETTER VIEW: compact A-Z index. Play all / Edit act on everything.
                names = _group_order(grouped, cat, _cfg)
                _choices = list(letters_found)
                _group_cols = None
                _header = _menu_header(cat_choice, _sub("by letter"))
            else:
                # FULL LIST VIEW: every item, optionally filtered to one letter.
                names = [n for n in _group_order(grouped, cat, _cfg)
                         if not _letter_filter or _letter(n) == _letter_filter]
                _sc["library.sort"] = "__sort__"
                _eh["library.sort"] = "sort"
                _choices = []
                group_paths = {name: [t['path'] for t in sort_tracks(grouped[name], _cfg)]
                               for name in names}
                # In album browse, show the album artist beside each album (dimmed),
                # matching the artist/genre → album sublist.
                if _field == 'album':
                    for _a in names:
                        _choices.append(prompt.Choice(
                            title=_a, value=_a,
                            cells=[_a, format_tag_values(album_credit(grouped[_a]))]))
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

            def _group_preview(value):
                """The highlighted group's picture and details over what it
                holds (a letter: its groups)."""
                if _letter_mode:
                    under = [n for n in group_names if _letter(n) == value]
                    return cols.preview(None, ui.plural(len(under), "item"), under)
                if value not in grouped:
                    return None
                art, lines = cols.tracks_details(value, grouped[value], person=_field in _PEOPLE,
                                                 credit=_field == 'album')
                return cols.preview((None if _field in _NO_PICTURE else art, lines),
                                    *_preview_of(grouped[value], _albums_level, _cfg))

            if open_group is not None:
                selection, open_group = open_group, None
                _group_place.value = selection
            else:
                selection = prompt.select(
                    "",
                    choices=_choices,
                    header=_header,
                    trail=trail,
                    preview=_group_preview,
                    columns=_group_cols,
                    shortcuts=_sc,
                    extra_hints=_eh,
                    actions=_list_actions(_show_editor),
                    on_inspect=_edit_group if _show_editor else None,
                    inspect_key='library.edit',
                    choose_label="Open",
                    place=_group_place,
                    **_queue_shortcut_kwargs(library, group_paths=group_paths),
                )

            if isinstance(selection, prompt.JumpTo):
                return selection             # a category: the caller's level
            if not selection:
                # Back from a letter's filtered list → return to the A-Z index,
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

            if selection == "__toggle__":
                _letter_mode   = not _letter_mode
                _letter_filter = None
                _group_place.reset()
                continue

            def _sort_this() -> None:
                if _field == 'album':
                    _tracks = [t for n in names for t in grouped[n]]
                    _pick_chain(_cfg, resolve_levels(_tracks, _cfg)[1], _header)
                else:
                    _new = _pick_sort(_group_sort, _GROUP_SORTS, _header)
                    _cfg["group_sorts"] = {**(_cfg.get("group_sorts") or {}), cat: _new}
                    _commit(_cfg, "group_sorts")

            if _list_result(selection, library,
                            lambda: _sorted_paths([grouped[n] for n in names], _cfg), _sort_this):
                continue

            if _letter_mode and selection in letters_found:
                _letter_filter = selection    # drill into that letter's full list
                _letter_mode   = False
                _group_place.reset()
                continue

            NAV_STACK.append(selection)
            selected_songs = grouped[selection]
            _album_place = prompt.ListPlace()
            # This level, as a column for the levels under it.
            trail_groups = trail + [prompt.Trail(names, names, selection, 1)]

            while True:  # LEVEL 3: Album Selection
                if _albums_level:
                    albums, album_list = _albums_of(selected_songs, _cfg)

                    # Always present the album list: even a single album is worth
                    # showing as its own entry (albums are distinct from the artist).
                    # Play / shuffle / edit hints act on every album; one album's
                    # row already does all of that one level down.
                    _show_editor = setting(_cfg, "show_metadata_editor")
                    _single_album = len(album_list) <= 1
                    _alb_choices: list = []
                    _asc: dict = {}
                    _aeh: dict = {"library.edit": "edit"} if _show_editor else {}
                    if not _single_album:
                        _asc["library.sort"] = "__sort__"; _aeh["library.sort"] = "sort"
                    # Show the album artist (dimmed) when it differs from the
                    # artist/genre we're browsing under.
                    for _a in album_list:
                        # Compare the *displayed* credit with the group we're under:
                        # an artist group is now named after the whole billing, so
                        # "Ada Lark, Bo Vale" matches and the column stays empty.
                        _aa = format_tag_values(album_credit(albums[_a]))
                        _aa = _aa if (_aa and _aa.lower() != selection.lower()) else ""
                        _alb_choices.append(prompt.Choice(
                            title=_a, value=_a, cells=[_a, _aa]))

                    album_paths = {name: [t['path'] for t in sort_tracks(albums[name], _cfg)]
                                   for name in album_list}
                    def _album_preview(value, _albums=albums):
                        """The highlighted album's cover and details over its tracks."""
                        if value not in _albums:
                            return None
                        return cols.preview(cols.tracks_details(value, _albums[value], credit=True),
                                            *_preview_of(_albums[value], False, _cfg))

                    alb = prompt.select(
                        "Albums:",
                        choices=_alb_choices,
                        header=_menu_header(selection, cat_choice),
                        trail=trail_groups,
                        preview=_album_preview,
                        columns=_ALBUM_COLUMNS,
                        shortcuts=_asc,
                        extra_hints=_aeh,
                        actions=None if _single_album else _list_actions(_show_editor),
                        on_inspect=((lambda a: _edit_paths(library, album_paths[a], a))
                                    if _show_editor else None),
                        inspect_key='library.edit',
                        choose_label="Open",
                        place=_album_place,
                        **_queue_shortcut_kwargs(library, group_paths=album_paths),
                    )

                    if isinstance(alb, prompt.JumpTo):
                        _jump = alb
                        break
                    if not alb:
                        break

                    if _list_result(alb, library, lambda: _sorted_paths([selected_songs], _cfg),
                                    lambda: _pick_chain(_cfg, resolve_levels(selected_songs, _cfg)[1],
                                                        _menu_header(selection, cat_choice))):
                        continue

                    NAV_STACK.append(alb)
                    track_paths = [s['path'] for s in albums[alb]]
                else:
                    track_paths = [s['path'] for s in selected_songs]

                _track_place = prompt.ListPlace()
                trail_tracks = trail_groups + ([prompt.Trail(album_list, album_list, alb, 2)]
                                               if _albums_level else [])
                while True:  # LEVEL 4: Track Selection
                    # Re-derive from library each iteration so tag edits show immediately
                    path_set     = set(track_paths)
                    final_tracks = sort_tracks([t for t in library if t['path'] in path_set], _cfg)

                    track_choices, disc_track_map, work_track_map = _track_rows(final_tracks, _cfg)

                    _track_context = NAV_STACK[-1] if NAV_STACK else selection
                    _show_editor   = setting(_cfg, "show_metadata_editor")
                    # Play / shuffle / edit-all hints; `e` edits the highlighted
                    # row. With a single track they're noise: the lone
                    # track row already does both jobs.
                    _teh: dict = {"library.edit": "edit"} if _show_editor else {}
                    _tsc: dict = {}
                    _track_actions = None
                    _is_book = bool(final_tracks) and all(is_audiobook(t['path'], _cfg) for t in final_tracks)
                    if _is_book:
                        # A book plays in order: p carries on where it was left
                        # (or starts it); r on any row starts it over.
                        _started = books.resume_point(final_tracks[0]['path']) is not None
                        _track_actions = [("library.play_all", "continue" if _started else "play", "__play_all__")]
                        if _show_editor:
                            _track_actions.append(("library.edit_all", "edit all", "__bulk_edit__"))
                    elif len(final_tracks) > 1:
                        _track_actions = _list_actions(
                            _show_editor, albums=len({(t.get('album'), t.get('album_artist'))
                                                      for t in final_tracks}) > 1)
                        _tsc["library.sort"] = "__sort__"; _teh["library.sort"] = "sort"

                    # Album artist shown in the header subtitle.
                    _album_artist = format_tag_values(album_credit(final_tracks))
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
                        ui.clear_screen()
                        if CHAPTER_SEP in path:     # a chapter row edits the chapters; the file is the album's e
                            file_path, ci = path.split(CHAPTER_SEP)
                            chapter_editor(file_path, int(ci), library)
                        else:
                            song = next((t for t in final_tracks if t['path'] == path), None)
                            inspect_tag_loop(path, library_metadata=song, library=library)
                        ui.clear_screen()
                        return ("__edited__", path)

                    def _track_preview(value):
                        """The highlighted track's (or chapter's) cover and
                        details; a disc or work heading's, over its tracks."""
                        by_path = {t['path']: t for t in final_tracks}
                        if not isinstance(value, str):
                            return None
                        heading = (disc_track_map.get(value[len("__disc_"):]) if value.startswith("__disc_")
                                   else work_track_map.get(value[len("__work__"):]) if value.startswith("__work__")
                                   else None)
                        if heading is not None:
                            tracks = [by_path[p] for p in heading if p in by_path]
                            name = heading_name(value, tracks)
                            return cols.preview(cols.tracks_details(name, tracks, credit=True),
                                                *_preview_of(tracks, False, _cfg))
                        if CHAPTER_SEP in value:
                            fp, ci = value.split(CHAPTER_SEP)
                            return cols.preview(cols.chapter_details(by_path[fp], int(ci))) if fp in by_path else None
                        return cols.preview(cols.track_details(by_path[value])) if value in by_path else None

                    path_choice_obj = prompt.select(
                        "Chapters:" if _is_book else "Tracks:",
                        choices=_all_track_choices,
                        header=_menu_header(_track_context, _subtitle),
                        trail=trail_tracks,
                        preview=_track_preview,
                        columns=_TRACK_COLUMNS,
                        shortcuts=_tsc,
                        extra_hints=_teh,
                        actions=_track_actions,
                        place=_track_place,
                        on_inspect=_inspect_track if _show_editor else None,
                        inspect_key='library.edit',
                        choose_label="Play",
                        **_queue_shortcut_kwargs(library,
                                                 disc_track_map=disc_track_map,
                                                 work_track_map=work_track_map,
                                                 list_paths=[t['path'] for t in final_tracks]),
                    )

                    if isinstance(path_choice_obj, prompt.JumpTo):
                        _jump = path_choice_obj
                        break
                    if not path_choice_obj:
                        break

                    if _list_result(path_choice_obj, library, lambda: [t['path'] for t in final_tracks],
                                    lambda: _pick_chain(_cfg, resolve_levels(final_tracks, _cfg)[1],
                                                        _menu_header(_track_context, _subtitle))):
                        continue

                    # Disc header selected: play that disc directly.
                    # Bulk-editing the disc's tags is `e` on this row, same as
                    # an ordinary track, so there's no separate disc page.
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__disc_"):
                        disc_val   = path_choice_obj[len("__disc_"):]
                        disc_paths = disc_track_map.get(disc_val, [])
                        play_queue(disc_paths, library=library)
                        continue

                    # Work header selected: offer play or bulk edit for that work
                    if isinstance(path_choice_obj, str) and path_choice_obj.startswith("__work__"):
                        work_name    = path_choice_obj[len("__work__"):]
                        work_paths   = work_track_map.get(work_name, [])
                        _show_editor = setting(_cfg, "show_metadata_editor")
                        _work_choices = [prompt.Choice(title=f"▸  Play all: {work_name}", value="__play_all__")]
                        if _show_editor:
                            _work_choices.append(prompt.Choice(title=f"Edit tags: {work_name}", value="__bulk_edit__"))

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
                            play_queue(work_paths, library=library)
                        elif work_action == "__bulk_edit__":
                            bulk_id3_manager(library, paths=work_paths)
                        continue

                    # A track plays directly, with the list after it as the
                    # after-a-picked-track setting says; metadata editing is
                    # `e`/`E` on the "Tracks:" list above.
                    _list_paths = [t['path'] for t in final_tracks]
                    if CHAPTER_SEP in path_choice_obj:
                        _path, _ci = path_choice_obj.split(CHAPTER_SEP)
                        _song = next(t for t in final_tracks if t['path'] == _path)
                        play_chapter(_list_paths, _list_paths.index(_path), _song['chapters'][int(_ci)][0], library)
                        continue
                    play_picked(_list_paths, _list_paths.index(path_choice_obj), library)

                if _albums_level:
                    NAV_STACK.pop()
                if _jump is not None and _jump.depth == 2 and _albums_level:
                    _album_place.value, _jump = _jump.value, None    # back to the albums, at the one clicked
                    continue
                if _jump is not None or not _albums_level:
                    break

            NAV_STACK.pop()
            if _jump is not None:
                if _jump.depth != 1:
                    return _jump                 # further up: the caller's
                _group_place.value, _jump = _jump.value, None       # back to the groups, at the one clicked

    finally:
        if NAV_STACK and NAV_STACK[-1] == cat_choice:
            NAV_STACK.pop()


# Categories whose groups are people: their details show the person's picture.
_PEOPLE = ('artist', 'composer', 'lyricist', 'people')
# Categories whose groups have no picture of their own (one album's cover would mislead).
_NO_PICTURE = ('genre',)


def _group_order(grouped: dict, cat: str, cfg: dict) -> list:
    """A category's groups in the order its list shows them (and so does the
    preview of the category): by their sort keys, then the albums' sort chain
    or the category's chosen sort."""
    field = BROWSE_CATEGORIES[cat][1]
    names = sorted(grouped, key=lambda n: get_group_sort_key(n, grouped[n], field))
    if field == 'album':
        return sort_albums(names, grouped, cfg)
    return _sort_groups(names, grouped, field, (cfg.get("group_sorts") or {}).get(cat, "name"))


def _albums_of(songs: list, cfg: dict) -> tuple:
    """A group's albums: ({name: its tracks}, the names in the sort chain's order)."""
    albums = get_grouped_data(songs, 'album')
    return albums, sort_albums(list(albums.keys()), albums, cfg)


def _preview_of(songs: list, by_album: bool, cfg: dict) -> tuple:
    """What opening a group or album lists, for the preview column: (its
    title, its rows): the albums, or the tracks."""
    if by_album:
        names = _albums_of(songs, cfg)[1]
        return ui.plural(len(names), "album"), names
    rows = [r for r in _track_rows(sort_tracks(songs, cfg), cfg)[0] if not r.disabled]
    return ui.plural(sum(not r.value.startswith(("__disc_", "__work__")) for r in rows), "track"), \
        [ui.strip_ansi(str(r.title)) if r.value.startswith(("__disc_", "__work__")) else str(r.title) for r in rows]


def _category_keys(cfg: dict, scope: str | None) -> list:
    """The Browse menu's categories here. Libraries only means something with
    more than one directory, and not inside a library already."""
    return [k for k in browse_menu_keys(cfg)
            if k != 'libraries' or (scope is None and len(music_dirs(cfg)) > 1)]


def _heading(name: str, tracks: list) -> str:
    """A disc's or work's row: its name, then how many tracks and how long."""
    secs = sum(t.get('duration') or 0 for t in tracks)
    said = ui.plural(len(tracks), "track") + (f" · {ui.format_time(int(secs))}" if secs else "")
    return f"{C.BOLD}{name}{C.RESET}  {C.DIM}{said}{C.RESET}"


def _track_rows(final_tracks: list, cfg: dict) -> tuple:
    """A track list's rows (disc and work headings, tracks, a book's chapters in
    its file's place), with each disc's and work's tracks: (rows, by disc, by work).
    A heading is a row of its own, its name and what it holds, a blank row
    before each disc after the first; a track is its number, title, any
    featured artist and its length."""
    discs = {str(t.get('disc', '1')) for t in final_tracks}
    has_multiple_discs = len(discs) > 1
    disc_track_map: dict = {}
    work_track_map: dict = {}
    by_disc: dict = {}
    by_work: dict = {}
    for t in final_tracks:
        disc_val, work = str(t.get('disc', '1')), t.get('work', '').strip()
        disc_track_map.setdefault(disc_val, []).append(t['path'])
        by_disc.setdefault(disc_val, []).append(t)
        if work:
            work_track_map.setdefault(work, []).append(t['path'])
            by_work.setdefault(work, []).append(t)
    num_w = max((len(str(int(to_num(t.get('track'))))) for t in final_tracks), default=1)

    track_choices: list = []
    current_disc = current_work = None
    for t in final_tracks:
        disc_val, work = str(t.get('disc', '1')), t.get('work', '').strip()
        if has_multiple_discs and disc_val != current_disc:
            if track_choices:
                track_choices.append(prompt.separator(""))
            name = heading_name(f"__disc_{disc_val}", by_disc[disc_val])
            track_choices.append(prompt.Choice(title=_heading(name, by_disc[disc_val]), value=f"__disc_{disc_val}"))
            current_disc, current_work = disc_val, None
        if work and work != current_work:
            track_choices.append(prompt.Choice(title=_heading(work, by_work[work]), value=f"__work__{work}"))
            current_work = work

        # An audiobook file with chapters lists them in its place.
        chapters = t.get('chapters') or []
        if chapters and is_audiobook(t['path'], cfg):
            for i, (start, _title) in enumerate(chapters):
                end = chapters[i + 1][0] if i + 1 < len(chapters) else (t.get('duration') or start)
                label = chapter_label(chapters, i)[0]
                track_choices.append(prompt.Choice(
                    title=label, value=f"{t['path']}{CHAPTER_SEP}{i}",
                    cells=["", label, "", ui.format_time(int(end - start))]))
            continue

        # to_num: a hand-typed movement like "II" or "2a" can't crash the list.
        _mv = int(to_num(t.get('movement_number')))
        mv_name = t.get('movement_name', '').strip()
        name = (f"{roman(_mv)}. {mv_name}" if _mv > 0 else mv_name) if mv_name else t.get('title', 'Unknown')
        num = f"{int(to_num(t.get('track'))):>{num_w}}"
        label = f"{num}  {name}"

        # The full artist only when it differs from the album artist; no
        # string parsing, so any characters are safe.
        _aa = (t.get('album_artist') or '').strip()
        _ar = (t.get('artist') or '').strip()
        _artist = _ar if (_ar and _ar.lower() != _aa.lower()) else ""
        _dur = t.get('duration') or 0
        track_choices.append(prompt.Choice(
            title=label, value=t['path'],
            cells=[num, name, format_tag_values(_artist), ui.format_time(int(_dur)) if _dur else ""]))

    return track_choices, disc_track_map, work_track_map


def heading_name(value: str, tracks: list) -> str:
    """What a disc or work heading row is called: the disc's subtitle, else
    "Disc n"; the work's name."""
    if value.startswith("__work__"):
        return value[len("__work__"):]
    return (tracks[0].get('disc_subtitle') or "").strip() if tracks and (tracks[0].get('disc_subtitle') or "").strip() \
        else f"Disc {value[len('__disc_'):]}"


_opening: list = []                     # (category, group) for Browse to open at, once


def open_in_browse(cat: str, path: str) -> bool:
    """Show the Browse tab, started afresh, inside the group of category `cat`
    that holds the track at `path` (its album, its artist). False, with a
    toast, when it's in none (not in the library) or outside tabs."""
    from backtrack.music_library import live_library
    field = BROWSE_CATEGORIES[cat][1]
    group = next((name for name, songs in get_grouped_data(live_library() or [], field).items()
                  if any(t['path'] == path for t in songs)), None)
    if group is None:
        ui.show_status(f"This track isn't in the library's {BROWSE_CATEGORIES[cat][0].lower()}.")
        return False
    _opening[:] = [(cat, group)]
    if nav.go_to_tab("Browse", fresh=True):
        return True
    _opening.clear()
    return False


def handle_browse(library_ref: list, scope: str | None = None) -> str | None:
    """Show the "Browse by" category menu and route into browse_menu for the
    choice. With more than one music directory, Libraries lists them (by their
    friendly names), each opening this same menu for just that directory."""
    _cursor = 0
    while True:
        cfg = load_config()
        keys = _category_keys(cfg, scope)
        _opts = [prompt.Choice(title=BROWSE_CATEGORIES[k][0], value=k) for k in keys]
        _source = ([t for t in library_ref[0] if library_of(t['path'], music_dirs(cfg)) == scope]
                   if scope else library_ref[0])
        _groups: dict = {}                  # each category's groups, worked out once per visit

        def _category_preview(key):
            """What the highlighted category lists."""
            if key == 'libraries':
                names = [library_name(cfg, d) for d in music_dirs(cfg)]
            else:
                if key not in _groups:              # in the order the category's own list has them
                    _groups[key] = _group_order(get_grouped_data(_source, BROWSE_CATEGORIES[key][1]), key, cfg)
                names = _groups[key]
            return cols.preview(None, ui.plural(len(names), "item"), names)
        if not _opts:
            ui.show_status("Nothing to browse: turn categories on in Settings → Browse menu.")
            return None
        if _opening:
            (cat, group), = _opening
            _opening.clear()
            if cat in keys:
                _cursor = keys.index(cat)
            browse_menu(library_ref, cat, scope=scope, open_group=group,
                        trail=[prompt.Trail([o.title for o in _opts], keys, cat, 0)] if cat in keys else None)
            continue
        choice = prompt.select(
            "",
            choices=_opts,
            header=_menu_header(library_name(cfg, scope) if scope else "Browse"),
            preview=_category_preview,
            index=_cursor,
            choose_label="Open",
        )
        if not choice:
            break
        _cursor = _idx_of(_opts, choice)
        if choice == "libraries":
            _browse_libraries(library_ref)
        else:
            jump = browse_menu(library_ref, choice, scope=scope,
                               trail=[prompt.Trail([o.title for o in _opts], keys, choice, 0)])
            if jump is not None:
                _cursor = _idx_of(_opts, jump.value)   # back to the categories, at the one clicked
    return None


def _browse_libraries(library_ref: list) -> str | None:
    """Browse → Libraries: pick a music directory, then browse just it."""
    _cursor = 0
    while True:
        cfg = load_config()
        dirs = music_dirs(cfg)
        choices = [prompt.Choice(title=library_name(cfg, d), value=d) for d in dirs]
        choice = prompt.select("", choices=choices, header=_menu_header("Libraries"), index=_cursor,
                               choose_label="Open")
        if not choice:
            return None
        _cursor = _idx_of(choices, choice)
        _name = library_name(cfg, choice)
        NAV_STACK.append(_name)
        try:
            handle_browse(library_ref, scope=choice)
        finally:
            if NAV_STACK and NAV_STACK[-1] == _name:
                NAV_STACK.pop()
