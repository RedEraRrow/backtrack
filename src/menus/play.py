"""Playing from the menus: the play/queue actions a list offers, and handing
a list of tracks to the player."""
from __future__ import annotations
import os
import random
from src.utils import ui_utils
from src.music_library import drop_moved, sort_tracks, track_title
from src.playback.playback import music_player
from src.playback.session import REPEAT_OFF, active_session, is_client
from src.id3.bulk_id3_manager import bulk_id3_manager


def play_queue(paths: list, mode: str = "linear", library: list | None = None) -> str | None:
    """Play a queue of file paths, in order ("linear") or shuffled ("shuffle")."""
    playlist = list(paths)
    if not playlist:                       # e.g. a letter filter that leaves nothing
        ui_utils.show_status("Nothing to play.")
        return None
    if mode == "shuffle":
        random.shuffle(playlist)

    titles = _queue_titles_for_paths(playlist, library or [])

    # The shared session owns the queue and auto-advances in the background
    # (feature #14), so this just starts it and opens the player. Minimising the
    # player ('b'/Esc) returns here with audio still playing; Stop ('s') ends it.
    session_mode = REPEAT_OFF
    music_player(playlist[0], queue_titles=titles, queue_index=0,
                 queue_paths=playlist, mode=session_mode)
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


def _list_result(res, library: list, paths, sort) -> bool:
    """What every browse list does alike with select()'s result: rebuild after
    an edit, re-sort (`sort()`), play or shuffle, or bulk-edit everything listed
    (`paths()`, in the order shown). True when handled — go round again."""
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
    """The in-player queue's titles for `paths`, from their library entries."""
    by_path = {s['path']: s for s in library}
    return [track_title(p, by_path.get(p)) for p in paths]


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
    paths = drop_moved(paths)
    if not paths:
        return True                        # nothing left to queue; drop_moved said why

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
