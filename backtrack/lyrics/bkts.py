"""Backtrack script files (.bkts): one file per track holding its script and
its timed transcript, the pair a dialogue track is shown from.

A .bkts is a zip beside the track (`<name>.bkts`) with up to three members:
the script (`script.md`), the transcript (`transcript.json`) and, while the
lyric editor has unwritten edits, its working copy (`transcript.sync.json`).

Everything that reads a script or a transcript goes through
`formats._find_markdown_for_audio` and `_find_timing_files_for_audio`, and
both take a .bkts first. It is unpacked into backtrack's cache, once per
change to the archive, so those readers keep working on ordinary paths. A
write to one of those paths goes back into the archive through `save`.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import zipfile

from backbone.files import write_text_atomic
from backbone.log import log

EXT = '.bkts'
SCRIPT = 'script.md'
TRANSCRIPT = 'transcript.json'
WORKING = 'transcript.sync.json'
MEMBERS = (SCRIPT, TRANSCRIPT, WORKING)
_STAMP = '.source'


def archive_for(audio_path: str) -> str | None:
    """The .bkts beside a track, if there is one."""
    path = os.path.splitext(audio_path)[0] + EXT
    return path if os.path.isfile(path) else None


def _cache_root():
    from backtrack.music_library import CACHE_DIR
    return CACHE_DIR / 'bkts'


def _stat(archive: str) -> str:
    st = os.stat(archive)
    return f"{st.st_mtime_ns}:{st.st_size}"


def _unpacked(archive: str) -> str:
    """The cache folder holding the archive's members, refreshed when the
    archive has changed since it was last unpacked."""
    archive = os.path.abspath(archive)
    folder = _cache_root() / hashlib.sha1(archive.encode()).hexdigest()[:16]
    stamp = folder / _STAMP
    try:
        if stamp.read_text().split('\n', 1) == [_stat(archive), archive]:
            return str(folder)
    except OSError:
        pass
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if name in MEMBERS:
                (folder / name).write_bytes(zf.read(name))
    stamp.write_text(f"{_stat(archive)}\n{archive}")
    return str(folder)


def member(audio_path: str, name: str) -> str | None:
    """A path to one member of the track's .bkts, or None (no .bkts, or it
    lacks that member, or it can't be read, which is logged)."""
    archive = archive_for(audio_path)
    if not archive:
        return None
    try:
        path = os.path.join(_unpacked(archive), name)
    except (OSError, zipfile.BadZipFile) as exc:
        log.warning("couldn't read %s: %s", archive, exc)
        return None
    return path if os.path.isfile(path) else None


def source_of(path: str) -> str | None:
    """The .bkts an unpacked member came from, None for any other file."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        if os.path.commonpath([folder, str(_cache_root())]) != str(_cache_root()):
            return None
        return (open(os.path.join(folder, _STAMP), encoding='utf-8').read().split('\n', 1) + [''])[1] or None
    except (OSError, ValueError):
        return None


def label(path: str) -> str:
    """How to name a script or transcript to the user: its .bkts, if it's in one."""
    return os.path.basename(source_of(path) or path)


def _write(archive: str, changes: dict) -> None:
    """Rewrite the archive with `changes` (member → text, or None to drop it),
    keeping its other members, whole or not at all."""
    keep = {}
    if os.path.isfile(archive):
        with zipfile.ZipFile(archive) as zf:
            keep = {n: zf.read(n) for n in zf.namelist() if n in MEMBERS}
    for name, text in changes.items():
        if text is None:
            keep.pop(name, None)
        else:
            keep[name] = text.encode('utf-8')
    tmp = archive + '.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in MEMBERS:
            if name in keep:
                zf.writestr(name, keep[name])
    os.replace(tmp, archive)


def save(path: str, text: str) -> None:
    """Write a script or transcript file; if it's a member of a .bkts, the
    archive gets it too (and the unpacked copy stays current)."""
    write_text_atomic(path, text)
    archive = source_of(path)
    if archive:
        _write(archive, {os.path.basename(path): text})
        folder = os.path.dirname(path)
        with open(os.path.join(folder, _STAMP), 'w', encoding='utf-8') as fh:
            fh.write(f"{_stat(archive)}\n{archive}")


def pack(audio_path: str, script: str, transcript: str, working: str | None = None) -> str:
    """Make the track's .bkts from a script and a transcript file (and the
    editor's working copy, if there is one). The files themselves are left
    where they are. Returns the archive's path."""
    archive = os.path.splitext(audio_path)[0] + EXT
    read = lambda p: open(p, encoding='utf-8').read()      # noqa: E731
    _write(archive, {SCRIPT: read(script), TRANSCRIPT: read(transcript),
                     WORKING: read(working) if working else None})
    return archive
