"""Format-agnostic tag writer for the bulk "Derive from filename" operation.

Writes a small, fixed set of fields — title, track(+total), disc(+total), album,
artist — to either MP3 (ID3v2) or the MP4 family (m4a/mp4/m4p/aac), so bulk
derivation works across formats. Two robustness guarantees the rest of the app
did not previously have:

  * **Blank MP3s** (no ID3 header) get a fresh ID3 created rather than raising.
  * **Non-MP3s** are written via their native MP4 atoms; anything genuinely
    unsupported returns ``unsupported=True`` instead of throwing.

Field semantics: ``track`` and ``disc`` are treated as single units that carry
their totals. "Fill blanks only" means a field is written only when its tag is
currently absent/empty, unless ``overwrite`` is set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TRCK, TPOS  # type: ignore[reportPrivateImportUsage]
from mutagen.mp4 import MP4  # type: ignore[reportPrivateImportUsage]

# The fields this writer understands (track/disc carry their totals).
FIELDS = ('title', 'track', 'disc', 'album', 'artist')

_MP3_EXTS = ('.mp3',)
_MP4_EXTS = ('.m4a', '.mp4', '.m4p', '.aac')


@dataclass
class WriteResult:
    written: list[str] = field(default_factory=list)          # fields actually written
    skipped_existing: list[str] = field(default_factory=list)  # chosen but already had a value
    error: str | None = None
    unsupported: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.written)


def format_kind(path: str) -> str:
    """Return 'mp3', 'mp4', or 'unsupported' for a path."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _MP3_EXTS:
        return 'mp3'
    if ext in _MP4_EXTS:
        return 'mp4'
    return 'unsupported'


def is_writable(path: str) -> bool:
    return format_kind(path) != 'unsupported'


def _fmt_pair(num, total) -> str:
    """ID3 numeric-pair text: 'n/total' when a total is present, else 'n'."""
    return f"{int(num)}/{int(total)}" if total else f"{int(num)}"


# ---------------------------------------------------------------------------
# Reading current field presence (to honour fill-blanks-only)
# ---------------------------------------------------------------------------

def _id3_present(audio: ID3) -> dict[str, bool]:
    def _txt(fid: str) -> bool:
        fr = audio.get(fid)
        return bool(fr and fr.text and str(fr.text[0]).strip())

    def _num(fid: str) -> bool:
        fr = audio.get(fid)
        if not (fr and fr.text):
            return False
        head = str(fr.text[0]).split('/')[0].strip()
        return bool(head) and head != '0'

    return {
        'title': _txt('TIT2'), 'artist': _txt('TPE1'), 'album': _txt('TALB'),
        'track': _num('TRCK'), 'disc': _num('TPOS'),
    }


def _mp4_present(audio: MP4) -> dict[str, bool]:
    tags = audio.tags or {}

    def _txt(atom: str) -> bool:
        v = tags.get(atom)
        return bool(v and str(v[0]).strip())

    def _pair(atom: str) -> bool:
        v = tags.get(atom)
        try:
            return bool(v and v[0][0])
        except (IndexError, TypeError):
            return False

    return {
        'title': _txt('\xa9nam'), 'artist': _txt('\xa9ART'), 'album': _txt('\xa9alb'),
        'track': _pair('trkn'), 'disc': _pair('disk'),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def present_fields(path: str) -> dict[str, bool]:
    """Which of :data:`FIELDS` already have a non-empty value on disk.

    Used to build the preview (write vs skip) without modifying the file.
    Returns all-False if the file can't be read or isn't writable.
    """
    kind = format_kind(path)
    try:
        if kind == 'mp3':
            try:
                return _id3_present(ID3(path))
            except ID3NoHeaderError:
                return {f: False for f in FIELDS}
        if kind == 'mp4':
            return _mp4_present(MP4(path))
    except Exception:
        pass
    return {f: False for f in FIELDS}


def write_fields(path: str, values: dict, apply_fields, overwrite: bool = False) -> WriteResult:
    """Write the chosen fields to ``path``.

    ``values`` is a parser dict (title/track/total_tracks/disc/total_discs/album/
    artist). ``apply_fields`` is the subset of :data:`FIELDS` the user opted into.
    Only fields with a derived value are written; existing values are preserved
    unless ``overwrite`` is True.
    """
    kind = format_kind(path)
    if kind == 'unsupported':
        return WriteResult(unsupported=True)

    apply_fields = set(apply_fields)
    res = WriteResult()

    try:
        if kind == 'mp3':
            try:
                audio = ID3(path)
            except ID3NoHeaderError:
                audio = ID3()               # fresh header for a blank MP3
            present = _id3_present(audio)
        else:
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            present = _mp4_present(audio)

        for f in FIELDS:
            if f not in apply_fields:
                continue
            val = values.get(f)
            if val is None or (isinstance(val, str) and not val.strip()):
                continue                    # nothing derived for this field
            if present.get(f) and not overwrite:
                res.skipped_existing.append(f)
                continue
            _set_field(audio, kind, f, values)
            res.written.append(f)

        if res.written:
            if kind == 'mp3':
                audio.save(path, v2_version=3)
            else:
                audio.save()
    except Exception as e:                  # never let one bad file abort a bulk run
        return WriteResult(error=str(e))

    return res


def _set_field(audio, kind: str, f: str, values: dict) -> None:
    if kind == 'mp3':
        if f == 'title':
            audio.setall('TIT2', [TIT2(encoding=3, text=[str(values['title'])])])
        elif f == 'artist':
            audio.setall('TPE1', [TPE1(encoding=3, text=[str(values['artist'])])])
        elif f == 'album':
            audio.setall('TALB', [TALB(encoding=3, text=[str(values['album'])])])
        elif f == 'track':
            audio.setall('TRCK', [TRCK(encoding=3, text=[_fmt_pair(values['track'], values.get('total_tracks'))])])
        elif f == 'disc':
            audio.setall('TPOS', [TPOS(encoding=3, text=[_fmt_pair(values['disc'], values.get('total_discs'))])])
    else:  # mp4
        tags = audio.tags
        if f == 'title':
            tags['\xa9nam'] = [str(values['title'])]
        elif f == 'artist':
            tags['\xa9ART'] = [str(values['artist'])]
        elif f == 'album':
            tags['\xa9alb'] = [str(values['album'])]
        elif f == 'track':
            tags['trkn'] = [(int(values['track']), int(values.get('total_tracks') or 0))]
        elif f == 'disc':
            tags['disk'] = [(int(values['disc']), int(values.get('total_discs') or 0))]
