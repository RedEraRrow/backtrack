"""Where each audiobook got to: its file, position and speed, kept between runs
in books.json. A book is an album (album artist + album), or a single file
when it has no album tag; every track of it shares one entry."""
from __future__ import annotations
import json
import os
import threading
import time

from backbone.files import write_text_atomic
from backbone.log import log
from backtrack.music_library import album_tracks, library_entry, live_library

_lock = threading.Lock()
_UNTAGGED = ('', 'Unknown Album')


def _file():
    from backtrack.config import CONFIG_DIR
    return CONFIG_DIR / "books.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("books.json unreadable, starting empty: %s", exc)
        return {}


def _save(books: dict) -> None:
    try:
        write_text_atomic(_file(), json.dumps(books, indent=1))
    except OSError as exc:
        log.warning("books.json not saved, book positions lost: %s", exc)


def book_key(path: str) -> str:
    e = library_entry(path)
    album = (e.get('album') or '').strip()
    if album in _UNTAGGED:
        return path
    return f"{(e.get('album_artist') or '').strip()}\x1f{album}"


def book_tracks(path: str) -> list[str]:
    """The book's files in play order."""
    return [path] if book_key(path) == path else album_tracks(path, live_library() or [])


def remember(path: str, elapsed: float, rate: float, at_end: bool = False) -> None:
    """Save where the book playing `path` is. At the end of a file, the book
    moves on to its next file at 0, or is marked finished after its last."""
    finished = False
    if at_end:
        tracks = book_tracks(path)
        i = tracks.index(path) if path in tracks else len(tracks) - 1
        if i + 1 < len(tracks):
            path, elapsed = tracks[i + 1], 0.0
        else:
            finished = True
    with _lock:
        books = _load()
        books[book_key(path)] = {"path": path, "elapsed": round(elapsed, 1), "rate": rate,
                                 "finished": finished, "updated": int(time.time())}
        _save(books)


def resume_point(path: str) -> tuple[str, float] | None:
    """The file and position a started, unfinished book resumes at, or None."""
    b = _load().get(book_key(path))
    if not b or b.get("finished"):
        return None
    if not os.path.isfile(b.get("path", "")):
        log.info("book resume point gone, starting over: %s", b.get("path"))
        return None
    return b["path"], float(b.get("elapsed") or 0)


def rate_for(path: str) -> float:
    return float((_load().get(book_key(path)) or {}).get("rate") or 1.0)


def forget(path: str) -> None:
    """Start the book over: drop its position, keep its speed."""
    with _lock:
        books = _load()
        old = books.pop(book_key(path), None)
        if old is None:
            return
        if (old.get("rate") or 1.0) != 1.0:
            books[book_key(path)] = {"rate": old["rate"]}
        _save(books)
