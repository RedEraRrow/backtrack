"""The Directory tab: the music directories as folders on disk, down to their
audio files, and nothing else: no pictures or details, just the filesystem. A
folder plays and queues like an album (everything under it, in path order); a
file like a track."""
from __future__ import annotations
import os

from backbone import prompt, ui
from backbone.nav import NAV_STACK
from backtrack.config import library_name, load_config, music_dirs, setting
from backtrack.menus.common import _menu_header
from backtrack.menus.play import (
    _edit_paths, _list_actions, _list_result, _queue_shortcut_kwargs, play_picked,
)
from backtrack.music_library import VALID_AUDIO_EXTENSIONS


def handle_directory(library_ref: list) -> None:
    """The music directories; each opens as its folder."""
    place = prompt.ListPlace()
    while True:
        cfg = load_config()
        roots = [d for d in music_dirs(cfg) if os.path.isdir(d)]
        if not roots:
            ui.show_status("No music directories: add one in Settings → Music directories.")
            return
        names = {d: library_name(cfg, d) for d in roots}
        if len(roots) == 1:
            _folder(library_ref, roots[0], names[roots[0]], [], 0)
            return
        pick = prompt.select("", choices=[prompt.Choice(title=names[d], value=d) for d in roots],
                             header=_menu_header("Directory"), place=place, choose_label="Open")
        if not pick:
            return
        jump = _folder(library_ref, pick, names[pick], [prompt.Trail(list(names.values()), roots, pick, 0)], 1)
        if jump is not None:
            place.value = jump.value             # back to the directories, at the one clicked


def _listing(path: str, cfg: dict) -> tuple[list, list]:
    """A folder's subfolders that hold audio (in them or anywhere beneath)
    and its audio files, each in name order."""
    hide = setting(cfg, "ignore_hidden_files")
    try:
        entries = [e for e in os.scandir(path) if not (hide and e.name.startswith('.'))]
    except OSError:
        return [], []
    key = lambda e: e.name.casefold()
    return (sorted((e.path for e in entries if e.is_dir() and _holds_audio(e.path, hide)),
                   key=lambda p: os.path.basename(p).casefold()),
            [e.path for e in sorted(entries, key=key)
             if e.is_file() and e.name.lower().endswith(VALID_AUDIO_EXTENSIONS)])


def _holds_audio(path: str, hide: bool) -> bool:
    """Whether there's an audio file in `path` or any folder beneath it
    (hidden ones skipped with `hide`): the walk stops at the first."""
    for root, dirs, files in os.walk(path):
        if hide:
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            files = [f for f in files if not f.startswith('.')]
        if any(f.lower().endswith(VALID_AUDIO_EXTENSIONS) for f in files):
            return True
    return False


def _under(library: list, parent: str, subs: list) -> dict:
    """Each of `parent`'s subfolders' library tracks, anywhere beneath it, in
    path order."""
    out: dict = {s: [] for s in subs}
    pre = parent.rstrip(os.sep) + os.sep
    for t in library:
        if t['path'].startswith(pre):
            head, sep, _rest = t['path'][len(pre):].partition(os.sep)
            if sep and pre + head in out:
                out[pre + head].append(t)
    for tracks in out.values():
        tracks.sort(key=lambda t: t['path'])
    return out


def _folder(library_ref: list, path: str, name: str, trail: list, depth: int) -> prompt.JumpTo | None:
    """One folder, called `name`, at level `depth`: its subfolders, then its
    audio files. As a column browser, `trail` is the folders above it; a click
    on one of their rows comes back as the JumpTo returned, for the level it
    names to take."""
    NAV_STACK.append(name)
    place = prompt.ListPlace()
    try:
        while True:
            cfg = load_config()
            library = library_ref[0]
            subs, files = _listing(path, cfg)
            under = _under(library, path, subs)
            rows = ([prompt.Choice(title=f"{os.path.basename(s)}/", value=s) for s in subs]
                    + [prompt.Choice(title=os.path.basename(f), value=f) for f in files])
            if not rows:
                ui.show_status(f"No folders or audio files in {name}.")
                return None
            everything = [t['path'] for s in subs for t in under[s]] + files
            show_editor = setting(cfg, "show_metadata_editor")
            folder_paths = {s: [t['path'] for t in under[s]] for s in subs}

            def edit(value):
                return _edit_paths(library, folder_paths.get(value) or [value], value)

            pick = prompt.select(
                "", choices=rows, header=_menu_header(name), place=place, trail=trail,
                actions=_list_actions(show_editor), choose_label="Open",
                on_inspect=edit if show_editor else None, inspect_key='library.edit',
                extra_hints={"library.edit": "edit"} if show_editor else None,
                **_queue_shortcut_kwargs(library, group_paths=folder_paths, list_paths=everything),
            )
            if isinstance(pick, prompt.JumpTo):
                return pick                      # a folder above: theirs
            if not pick:
                return None
            if _list_result(pick, library, lambda: everything, lambda: None):
                continue
            if pick in under:
                here = prompt.Trail([os.path.basename(v) for v in subs + files], subs + files, pick, depth)
                jump = _folder(library_ref, pick, os.path.basename(pick), trail + [here], depth + 1)
                if jump is not None and jump.depth != depth:
                    return jump
                if jump is not None:
                    place.value = jump.value     # back here, at the one clicked
            elif pick in files:
                play_picked(files, files.index(pick), library)
    finally:
        if NAV_STACK and NAV_STACK[-1] == name:
            NAV_STACK.pop()
