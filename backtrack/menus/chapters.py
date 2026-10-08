"""The chapter editor: one file's chapters, changed in memory and saved once
(see chapter_edit), opened with e on a chapter row in browse."""
from __future__ import annotations
import os

from backbone import keys, prompt, ui
from backtrack import chapter_edit as ce
from backtrack.menus.common import _menu_header, _TRACK_COLUMNS
from backtrack.music_library import chapter_label, library_entry, refresh_library_entry, track_title

keys.define("chapter_edit", "Chapter editor", [
    ("add", ("n",), "new chapter"),
    ("save", ("s",), "save the chapters"),
], within=("list", "global"))


def _playing_at(path: str) -> float | None:
    """Where playback is in `path`, when it's the file playing."""
    from backtrack.playback.session import current_now_playing
    np = current_now_playing()
    return float(np.get('elapsed') or 0) if np and np.get('file_path') == path else None


def chapter_editor(path: str, focus: int, library: list | None) -> None:
    """Edit `path`'s chapters, starting on chapter `focus`."""
    entry = library_entry(path)
    original = [list(c) for c in entry.get('chapters') or []]
    chapters = [list(c) for c in original]
    duration = float(entry.get('duration') or 0.0)
    name = entry.get('album') or track_title(path, entry)
    cursor = focus

    def _save() -> bool:
        if not ce.can_write(path):
            ui.show_error("saving an m4b's chapters needs ffmpeg, see README.md")
            return False
        try:
            ui.show_status("Saving chapters…")
            ce.write(path, chapters, duration)
        except (ValueError, OSError) as e:
            ui.show_error(f"chapters not saved: {e}")
            return False
        if library is not None:
            refresh_library_entry(library, path)
        original[:] = [list(c) for c in chapters]
        ui.show_status("Chapters saved." + (" The previous file is in the trim backups."
                                            if path.lower().endswith(('.m4a', '.m4b', '.mp4', '.m4p')) else ""))
        return True

    while True:
        dirty = chapters != original
        times = [ce.format_time(c[0]) for c in chapters]
        w = max(map(len, times), default=0)
        ends = [c[0] for c in chapters[1:]] + [duration]
        rows = [prompt.Choice(title=chapter_label(chapters, i)[0], value=f"ch{i}",
                              cells=[t.rjust(w), chapter_label(chapters, i)[0], "",
                                     ui.format_time(int(end - c[0]))])
                for i, (c, t, end) in enumerate(zip(chapters, times, ends))]
        sub = ui.plural(len(chapters), "chapter") + (" · unsaved changes" if dirty else "")
        hints = {'chapter_edit.add': 'new chapter'}
        if dirty:
            hints['chapter_edit.save'] = 'save'
        choice = prompt.select("Chapters:", choices=rows or [prompt.Choice(title="No chapters", value="__none__")],
                               header=_menu_header(name, sub), columns=_TRACK_COLUMNS,
                               index=min(cursor, max(0, len(rows) - 1)),
                               shortcuts={'chapter_edit.add': '__add__', 'chapter_edit.save': '__save__'},
                               extra_hints=hints, choose_label="Edit")
        if not choice:
            if not dirty:
                return
            leave = prompt.select("Unsaved chapter changes:", choices=["Save", "Discard", "Keep editing"])
            if leave == "Discard" or (leave == "Save" and _save()):
                return
            continue
        if choice == "__save__":
            if dirty:
                _save()
            continue
        if choice == "__add__":
            here = _playing_at(path)
            start_s = ce.format_time(here if here is not None else chapters[cursor][0] if chapters else 0.0)
            when = prompt.text("New chapter starts at, as h:mm:ss or ± seconds:", default=start_s)
            if not when:
                continue
            title = prompt.text("Title:") or ""
            try:
                chapters, cursor = ce.add(chapters, ce.parse_time(when, ce.parse_time(start_s)), title, duration)
            except ValueError as e:
                ui.show_status(str(e))
            continue
        if choice == "__none__":
            continue

        cursor = int(choice[2:])
        start, title = chapters[cursor]
        act = prompt.select("Action:", choices=["Rename", "Move start", "Play from here", "Delete"],
                            header=_menu_header(chapter_label(chapters, cursor)[0],
                                                f"starts {ce.format_time(start)}"))
        try:
            if act == "Rename":
                new = prompt.text("Title:", default=title)
                if new is not None:
                    chapters = ce.rename(chapters, cursor, new)
            elif act == "Move start":
                when = prompt.text("Starts at, as h:mm:ss or ± seconds:", default=ce.format_time(start))
                if when:
                    chapters = ce.move(chapters, cursor, ce.parse_time(when, start), duration)
            elif act == "Play from here":
                from backtrack.menus.play import play_chapter
                play_chapter([path], 0, start, library)
            elif act == "Delete" and prompt.confirm(f"Delete {chapter_label(chapters, cursor)[0]}? "
                                                    "Its time joins the chapter before."):
                chapters = ce.delete(chapters, cursor)
                cursor = max(0, cursor - 1)
        except ValueError as e:
            ui.show_status(str(e))
