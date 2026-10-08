"""Changing a file's chapters: rename, move, add and delete them in memory,
then write them back once, the way its kind of file keeps them (ID3 CHAP/CTOC,
Vorbis CHAPTERnnn comments, or an MP4 chapter track). Chapters are the
library's [[start seconds, title], ...] (see music_library.chapters_of)."""
from __future__ import annotations
import os
import re
import subprocess
import tempfile

import mutagen
from mutagen.id3 import CTOCFlags
from mutagen.mp4 import MP4

from backbone.log import log
from backtrack.id3.tag_formats import kind as tag_kind, open_tags

# How close two chapter starts may sit, in seconds, and how far a rewritten
# file's length may drift from the original's before it's rejected.
_MIN_GAP_S = 0.1
_LENGTH_TOLERANCE_S = 0.5


# --- the edits -----------------------------------------------------------------

def _check_start(chapters: list, i: int, start: float, duration: float) -> None:
    lo = chapters[i - 1][0] + _MIN_GAP_S if i > 0 else 0.0
    hi = chapters[i + 1][0] - _MIN_GAP_S if i + 1 < len(chapters) else duration - _MIN_GAP_S
    if not lo <= start <= hi:
        raise ValueError("A chapter has to start after the one before it and before the one after.")


def rename(chapters: list, i: int, title: str) -> list:
    return [c if j != i else [c[0], title.strip()] for j, c in enumerate(chapters)]


def move(chapters: list, i: int, start: float, duration: float) -> list:
    _check_start(chapters, i, start, duration)
    return [c if j != i else [round(start, 3), c[1]] for j, c in enumerate(chapters)]


def add(chapters: list, start: float, title: str, duration: float) -> tuple[list, int]:
    """The chapters with a new one starting at `start`, and its index."""
    if not 0 <= start < duration:
        raise ValueError("That time is outside the file.")
    if any(abs(c[0] - start) < _MIN_GAP_S for c in chapters):
        raise ValueError("A chapter already starts there.")
    out = sorted([*chapters, [round(start, 3), title.strip()]], key=lambda c: c[0])
    return out, next(j for j, c in enumerate(out) if c[0] == round(start, 3))


def delete(chapters: list, i: int) -> list:
    """Without chapter `i`: its time becomes part of the one before."""
    return [c for j, c in enumerate(chapters) if j != i]


_TIME = re.compile(r'^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$')


def parse_time(text: str, current: float = 0.0) -> float:
    """A time typed as h:mm:ss(.ms), m:ss or seconds; '+1.5' / '-0.2' move
    from `current`. Raises ValueError for anything else."""
    text = (text or '').strip()
    if text[:1] in '+-' and text[1:]:
        return current + (1 if text[0] == '+' else -1) * parse_time(text[1:])
    m = _TIME.match(text)
    if not m:
        raise ValueError(f"Not a time: {text!r} (try 1:53:38, 2:05.5 or +1.5)")
    parts = [p for p in m.groups() if p is not None]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def format_time(seconds: float) -> str:
    """h:mm:ss.mmm (or m:ss.mmm), the precision a chapter start needs."""
    ms = int(round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}" if h else f"{m}:{s:02d}.{ms:03d}"


# --- writing them back -----------------------------------------------------------

def can_write(path: str) -> bool:
    """Whether this file's chapters can be saved (an MP4 needs ffmpeg to rewrite it)."""
    k = tag_kind(path)
    if k == 'mp4':
        from backtrack.trim import engine
        return engine.HAS_FFMPEG
    return k in ('id3', 'vorbis')


def write(path: str, chapters: list, duration: float) -> None:
    """Replace the file's chapters with `chapters`. An MP4 is rewritten (the
    audio copied, not re-encoded) and its previous version kept in the trim
    backup store; other kinds change in their tags. Raises ValueError/OSError."""
    k = tag_kind(path)
    ends = [c[0] for c in chapters[1:]] + [duration]
    if k == 'id3':
        from backtrack.trim import engine
        ids = [f"ch{i}" for i in range(len(chapters))]
        engine.write_chapters(path, [(eid, int(c[0] * 1000), int(end * 1000), c[1])
                                     for eid, c, end in zip(ids, chapters, ends)],
                              ids, int(CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED))
    elif k == 'vorbis':
        tags, save = open_tags(path)
        for key in [k for k in tags.keys() if k.upper().startswith('CHAPTER')]:
            del tags[key]
        for n, (start, title) in enumerate(chapters, 1):
            ms = int(round(start * 1000))
            tags[f"CHAPTER{n:03d}"] = [f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:"
                                       f"{ms // 1000 % 60:02d}.{ms % 1000:03d}"]
            if title:
                tags[f"CHAPTER{n:03d}NAME"] = [title]
        save()
    elif k == 'mp4':
        _rewrite_mp4(path, chapters, ends)
    else:
        raise ValueError("This file type can't keep chapters.")


def _ffmetadata(chapters: list, ends: list) -> str:
    def esc(text: str) -> str:
        return re.sub(r'([=;#\\\n])', r'\\\1', text)
    out = [";FFMETADATA1"]
    for (start, title), end in zip(chapters, ends):
        out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                f"title={esc(title)}"]
    return "\n".join(out) + "\n"


def _rewrite_mp4(path: str, chapters: list, ends: list) -> None:
    """Rewrite an MP4 with new chapters: ffmpeg copies the audio and writes the
    chapter track; mutagen then puts every tag and the cover back. The result
    is checked before the original is backed up and replaced. (The new
    file's global metadata comes from the chapter file, which has none: that
    keeps the chapters' own titles, which '-map_metadata -1' would also drop.)"""
    from backtrack.trim import engine
    if not engine.HAS_FFMPEG:
        raise ValueError("Saving an m4b's chapters needs ffmpeg.")
    old = MP4(path)
    tags = dict(old.tags or {})
    d, ext = os.path.dirname(path) or ".", os.path.splitext(path)[1]
    fd, tmp = tempfile.mkstemp(prefix=".chapters_", suffix=ext, dir=d)
    os.close(fd)
    mfd, meta = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(mfd, "w", encoding="utf-8") as f:
        f.write(_ffmetadata(chapters, ends))
    try:
        run = subprocess.run([engine.FFMPEG_PATH, "-v", "error", "-y", "-i", path, "-f", "ffmetadata", "-i", meta,
                              "-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-c", "copy",
                              "-movflags", "+faststart", "-f", "mp4", tmp], capture_output=True, text=True)
        if run.returncode != 0:
            raise ValueError(f"ffmpeg couldn't rewrite the file: {run.stderr.strip()[-300:]}")
        new = MP4(tmp)
        if new.tags is None:
            new.add_tags()
        new.tags.clear()
        new.tags.update(tags)
        new.save()
        new = MP4(tmp)
        from backtrack.music_library import chapters_of
        got = [(round(start, 1), title) for start, title in chapters_of(new, tmp)]
        if (abs(new.info.length - old.info.length) > _LENGTH_TOLERANCE_S
                or got != [(round(c[0], 1), c[1]) for c in chapters]):
            raise ValueError("The rewritten file didn't check out; the original is untouched.")
        engine.backup_original(path, reason="chapters")
        os.replace(tmp, path)
        log.info("rewrote %s with %d chapters", path, len(chapters))
    finally:
        for p in (tmp, meta):
            if os.path.exists(p):
                os.remove(p)
