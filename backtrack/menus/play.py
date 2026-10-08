"""Playing from the menus: the play/queue actions a list offers, and handing
a list of tracks to the player."""
from __future__ import annotations
import os
import random
from backbone import keys, prompt, ui
from backtrack import books
from backtrack.config import load_config, setting
from backtrack.music_library import album_tracks, drop_moved, is_audiobook, random_album, sort_tracks, track_title
from backtrack.playback.player import music_player
from backtrack.playback.session import AFTER_PICK, REPEAT_OFF, active_session, is_client, saved_queue
from backtrack.id3.bulk_menu import bulk_id3_manager
from backtrack.menus.common import _menu_header


def play_queue(paths: list, mode: str = "linear", library: list | None = None) -> str | None:
    """Play a queue of file paths, in order ("linear") or shuffled ("shuffle")."""
    playlist = list(paths)
    if not playlist:                       # e.g. a letter filter that leaves nothing
        ui.show_status("Nothing to play.")
        return None
    if mode == "shuffle":
        random.shuffle(playlist)

    titles = _queue_titles_for_paths(playlist, library or [])

    # The shared session owns the queue and auto-advances in the background,
    # so this just starts it and opens the player. Minimising the
    # player ('b'/Esc) returns here with audio still playing; Stop ('s') ends it.
    session_mode = REPEAT_OFF
    music_player(playlist[0], queue_titles=titles, queue_index=0,
                 queue_paths=playlist, mode=session_mode)
    return None


def resume_queue(view: bool = True) -> None:
    """Play the queue a previous run left, from the track and moment it
    stopped; `view`: and open the player on it."""
    saved = saved_queue()
    if not saved:
        ui.show_status("Nothing to resume.")
        return
    ui.clear_screen()
    music_player(saved['queue'][saved['index']], queue_titles=saved['titles'],
                 queue_index=saved['index'], queue_paths=saved['queue'], mode=saved['mode'],
                 start_at=saved['elapsed'], view=view)
    ui.clear_screen()


def resume_title(saved: dict) -> str:
    """What Resume picks up: the track, where in it, and which chapter of a
    book or which item of the queue."""
    from backtrack.music_library import chapter_at, chapter_label, chapter_number, library_entry
    i, n = saved['index'], len(saved['queue'])
    at = f" at {ui.format_time(int(saved['elapsed']))}" if saved['elapsed'] >= 1 else ""
    where = f"{i + 1} of {n}"
    chapters = library_entry(saved['queue'][i]).get('chapters') or []
    if chapters:                             # a book: which chapter, not which queue item
        ci = chapter_at(chapters, saved['elapsed'])
        num, total = chapter_number(chapters, ci)
        where = f"Chapter {num} of {total}" if num else chapter_label(chapters, ci)[0]
    return f"{saved['titles'][i]}{at} · {where}"


def offer_resume() -> None:
    """If the last run left a queue, offer to pick it up where it stopped or
    start fresh. Esc decides later: the queue is kept and offered again (next
    run, or Now playing with nothing playing)."""
    from backtrack.playback.session import forget_saved_queue
    saved = saved_queue()
    if not saved:
        return
    pick = prompt.select("You were listening to:", choices=[
        prompt.Choice(title=f"Resume  {resume_title(saved)}", value="resume"),
        prompt.Choice(title="Start fresh", value="fresh"),
    ])
    if pick == "resume":
        resume_queue()
    elif pick == "fresh":
        forget_saved_queue()


def play_picked(paths: list, index: int, library: list | None = None,
                then: str | None = None) -> None:
    """Play the track at `index` of a list it was just picked from, then what
    Settings → After a picked track says (or `then`): nothing more, the rest of
    the list, or the queue that was playing."""
    then = then or setting(load_config(), 'after_pick')
    if then not in AFTER_PICK:
        then = 'list'
    paths = list(paths)
    ui.clear_screen()
    music_player(paths[index], queue_titles=_queue_titles_for_paths(paths, library or []),
                 queue_index=index, queue_paths=paths, then=then)
    ui.clear_screen()


_PLAY_ACTIONS = ("__play_all__", "__shuffle__", "__album_shuffle__", "__random_album__")


def _list_actions(show_editor: bool, albums: bool = True) -> list:
    """Play all / shuffle / album shuffle / edit all for a browse list (select's
    actions=). `albums` is whether the list holds more than one album; album
    shuffle means nothing within one. `E` edits everything listed; `e` is each
    list's edit-the-highlighted-row."""
    acts = [("library.play_all", "play all", "__play_all__"), ("library.shuffle", "shuffle", "__shuffle__")]
    if albums:
        acts.append(("library.album_shuffle", "album shuffle", "__album_shuffle__"))
        acts.append(("library.random_album", "random album", "__random_album__"))
    if show_editor:
        acts.append(("library.edit_all", "edit all", "__bulk_edit__"))
    return acts


def _sorted_paths(groups: list, cfg: dict) -> list:
    """Every track of `groups` (lists of tracks, in the order shown), each
    group in the sort chain's order (what Play all plays). dict.fromkeys: a
    track under two groups (multi-value genre/artist) plays once."""
    return list(dict.fromkeys(t['path'] for g in groups for t in sort_tracks(g, cfg)))


# A chapter row's value in a track list: the file's path, this, the chapter's index.
CHAPTER_SEP = "\x1f"


def play_chapter(paths: list, index: int, start: float, library: list | None = None) -> None:
    """Play file `index` of a track list from a chapter picked in it; the rest
    of the list follows."""
    ui.clear_screen()
    music_player(paths[index], queue_titles=_queue_titles_for_paths(paths, library or []),
                 queue_index=index, queue_paths=paths, start_at=start)
    ui.clear_screen()


def _edit_paths(library: list, paths: list, value) -> tuple:
    """`e` on a row that stands for several tracks: bulk-edit them, then hand
    select() a result so the caller rebuilds the list (names may have changed)
    and puts the cursor back on `value`. A row that is one file (a book in a
    single m4b, a one-track album) opens that file's own editor instead."""
    paths = list(dict.fromkeys(paths))
    if len(paths) == 1:
        from backtrack.id3.browser import inspect_tag_loop
        ui.clear_screen()
        inspect_tag_loop(paths[0], library_metadata=next((t for t in library if t['path'] == paths[0]), None),
                         library=library)
        ui.clear_screen()
    else:
        bulk_id3_manager(library, paths=paths)
    return ("__edited__", value)


def _list_result(res, library: list, paths, sort) -> bool:
    """What every browse list does alike with select()'s result: rebuild after
    an edit, re-sort (`sort()`), play or shuffle, or bulk-edit everything listed
    (`paths()`, in the order shown). True when handled: go round again."""
    if isinstance(res, tuple) and res[0] == "__edited__":
        return True
    if res == "__sort__":
        sort()
    elif res in _PLAY_ACTIONS:
        _play_list(res, paths(), library)
    elif res == "__bulk_edit__":
        bulk_id3_manager(library, paths=paths())
    else:
        return False
    return True


def _book_safe(paths: list, shuffle: bool) -> tuple[list, bool]:
    """Audiobooks always play in order: shuffling a list leaves its books out,
    and a list of nothing but books plays in order instead."""
    if not shuffle:
        return paths, False
    music = [p for p in paths if not is_audiobook(p)]
    return (music, True) if music else (paths, False)


def _play_list(action: str, paths: list, library: list) -> str | None:
    """Play a list in order (Play all), shuffled (Shuffle), or with its albums
    in random order but each album's tracks kept in order (Album shuffle), or
    one album from it picked at random (Random album)."""
    if action == "__random_album__":
        album = random_album(paths, library)
        if not album:
            ui.show_status("No albums to pick from.")
            return None
        return play_queue(album, library=library)
    paths, shuffling = _book_safe(paths, action != "__play_all__")
    if not shuffling:
        action = "__play_all__"
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


def _queue_titles_for_paths(paths: list[str], library: list) -> list[str]:
    """The in-player queue's titles for `paths`, from their library entries."""
    by_path = {s['path']: s for s in library}
    return [track_title(p, by_path.get(p)) for p in paths]


# What can be done to the queue from a list: for the highlighted row (a track,
# or a group of tracks: an album, artist, disc, work or letter) and for the whole
# list. All are in the row's (o) and the list's (O) options menus; play next and
# add to queue also have keys, and Key bindings can give the others one.
# Shuffling, clearing and undoing the queue itself are the player's (its queue
# panel), not a list's.
keys.define("queue_actions", "Queue actions", [
    ("play_from_here", (), "play from here"),
    ("play_next", ("n",), "play next"),
    ("after_album", (), "play after the album that's playing"),
    ("add", ("a",), "add to the queue"),
    ("add_shuffled", (), "add to the queue, shuffled"),
    ("add_album", (), "add the track's album to the queue"),
    ("all_play_next", (), "play the whole list next"),
    ("all_after_album", (), "play the whole list after the album that's playing"),
    ("all_add", (), "add the whole list to the queue"),
    ("all_add_shuffled", (), "add the whole list to the queue, shuffled"),
    ("start_over", ("r",), "start an audiobook over"),
], within=("list", "global"))

_QUEUE_HINTS = {"play_next": "play next", "add": "queue", "after_album": "after album",
                "add_shuffled": "queue shuffled", "add_album": "queue album",
                "play_from_here": "from here", "start_over": "start over"}
# The row actions, each: (where it adds, shuffled), or None for the special ones.
_ROW_QUEUE = {"play_from_here": None, "play_next": ('next', False), "after_album": ('after_album', False),
              "add": ('end', False), "add_shuffled": ('end', True), "add_album": None,
              "start_over": None}
_LIST_QUEUE = {"all_play_next": ('next', False), "all_after_album": ('after_album', False),
               "all_add": ('end', False), "all_add_shuffled": ('end', True)}


def _queue(paths: list, library: list, where: str = 'end', shuffled: bool = False,
           what: str = '') -> None:
    """Add tracks to the queue (this window's session or the one joined), or,
    with nothing playing, start them playing."""
    paths = drop_moved(list(paths))
    if not paths:
        return                            # drop_moved said why
    a = active_session()
    paths, shuffled = _book_safe(paths, shuffled)
    if shuffled:
        random.shuffle(paths)
    titles = _queue_titles_for_paths(paths, library or [])
    if not a.is_active():
        a.start(paths[0], queue=paths, titles=titles)
        ui.show_status(f"Playing {titles[0]}")
        return
    a.edit('add', paths=paths, titles=titles, where=where)
    name = ui.plural(len(paths), 'track') if len(paths) > 1 else (what or titles[0])
    ui.show_status({'next': f"Playing next: {name}",
                    'after_album': f"Playing after this album: {name}",
                    'end': f"Added to the queue{', shuffled' if shuffled else ''}: {name}"}[where])


def _playing() -> bool:
    return active_session().is_active() or is_client()


def _queue_shortcut_kwargs(library: list,
                           group_paths: dict[str, list[str]] | None = None,
                           disc_track_map: dict[str, list[str]] | None = None,
                           work_track_map: dict[str, list[str]] | None = None,
                           list_paths: list | None = None) -> dict:
    """`select()` kwargs for the queue in a library list: the row actions
    (track or group, each where it applies) and the whole-list actions, all in
    the options menus (o, O); play next and add to queue also on their keys.
    Rows are tracks, or groups given a group-to-paths map. `list_paths`: the
    list's tracks in order (play from here; the whole list for a track list,
    else every group's tracks)."""
    whole = list(list_paths) if list_paths else [p for ps in (group_paths or {}).values() for p in ps]

    def _resolve(value) -> tuple[list[str] | None, str | None, str | None]:
        """(paths, title, track) for a row; track is set on a single-track row."""
        if not isinstance(value, str):
            return None, None, None
        # Disc/work header rows first: they're "__"-prefixed too, and the guard
        # below used to turn them away before these checks ever ran.
        if disc_track_map and value.startswith("__disc_"):
            disc_val = value[len("__disc_"):]
            return disc_track_map.get(disc_val), f"Disc {disc_val}", None
        if work_track_map and value.startswith("__work__"):
            work_name = value[len("__work__"):]
            return work_track_map.get(work_name), work_name, None
        if value.startswith("__"):
            return None, None, None
        if CHAPTER_SEP in value:                # a chapter row stands for its file, not a track
            paths, title, _track = _resolve(value.split(CHAPTER_SEP)[0])
            return paths, title, None
        if group_paths and value in group_paths:
            return group_paths[value], value, None
        song = next((s for s in library if s.get('path') == value), None)
        if song:
            return [value], song.get('title') or os.path.basename(value), value
        return None, None, None

    def _applies(spec: str, value) -> bool:
        name = spec.split('.', 1)[-1]
        paths, _title, track = _resolve(value)
        if not paths:
            return False
        if name in ("play_from_here", "add_album"):        # track options only
            return bool(track) and (name == "add_album" or track in whole)
        if name == "start_over":                           # a started audiobook's row
            return (is_audiobook(paths[0]) and len({books.book_key(p) for p in paths}) == 1
                    and books.resume_point(paths[0]) is not None)
        return name != "after_album" or _playing()

    def _row(name: str):
        def run(value) -> None:
            if not _applies(f"queue_actions.{name}", value):
                return
            paths, title, track = _resolve(value)
            if name == "play_from_here":
                play_picked(whole, whole.index(track), library, then='list')
            elif name == "add_album":
                _queue(album_tracks(track, library), library, what="its album")
            elif name == "start_over":
                books.forget(paths[0])
                play_queue(books.book_tracks(paths[0]), library=library)
            else:
                where, shuffled = _ROW_QUEUE[name]
                _queue(paths, library, where=where, shuffled=shuffled, what=title or '')
        return run

    def _all(name: str):
        def run() -> None:
            where, shuffled = _LIST_QUEUE[name]
            if where == 'after_album' and not _playing():
                ui.show_status("Nothing is playing.")
            elif whole:
                _queue(whole, library, where=where, shuffled=shuffled, what="the list")
        return run

    return {
        'row_actions': {f"queue_actions.{n}": _row(n) for n in _ROW_QUEUE},
        'row_action_hints': {f"queue_actions.{n}": _QUEUE_HINTS[n] for n in _ROW_QUEUE},
        'row_action_applies': _applies,
        'list_actions': {f"queue_actions.{n}": _all(n) for n in _LIST_QUEUE
                         if whole and (n != "all_after_album" or _playing())},
    }
